"""Lock ordering and rollback regressions found by independent storage review."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from threading import current_thread
from unittest import mock

import pytest
from django.conf import settings
from django.db import DatabaseError
from django.db import OperationalError
from django.db import connection
from django.db import connections
from django.db import transaction
from django.test import override_settings
from filelock import FileLock as RealFileLock
from filelock import Timeout

from documents.models import Document
from documents.ocr_queue import _install_page
from documents.signals.handlers import update_filename_and_move_files
from documents.tests.test_file_cleanup_recovery import evidence
from documents.tests.test_file_cleanup_recovery import existing_document
from documents.tests.test_file_updates import write_pdf
from documents.utils import compute_checksum


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("action", ["install", "backfill"])
def test_metadata_rename_and_install_cannot_deadlock(paperless_dirs, action):
    if connection.vendor != "postgresql":
        pytest.skip("Requires independent PostgreSQL row locks")
    from documents.file_handling import generate_filename

    document = existing_document(paperless_dirs, 10)
    output = write_pdf(paperless_dirs.scratch_dir / "result.pdf", [700])
    thumbnail = paperless_dirs.scratch_dir / "thumbnail.webp"
    thumbnail.write_bytes(b"new thumbnail")
    if action == "backfill":
        from documents.models import DocumentPage

        DocumentPage.global_objects.filter(document=document).hard_delete()
    rename_has_media = Event()
    install_waits_media = Event()

    def row_is_already_locked(doc_id):
        try:
            with transaction.atomic():
                Document.objects.select_for_update(nowait=True).get(pk=doc_id)
            return False
        except DatabaseError:
            return True
        finally:
            connections.close_all()

    def generation(doc, *args, **kwargs):
        if current_thread().name.startswith("metadata"):
            rename_has_media.set()
            with ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="lock_probe",
            ) as probe:
                protected = probe.submit(row_is_already_locked, doc.pk).result(
                    timeout=5,
                )
            if not protected:
                assert install_waits_media.wait(10)
        return generate_filename(doc, *args, **kwargs)

    def installer_lock(path, *args, **kwargs):
        if path == settings.MEDIA_LOCK:
            install_waits_media.set()
            return RealFileLock(path, timeout=2)
        return RealFileLock(path, *args, **kwargs)

    def rename():
        try:
            doc = Document.objects.get(pk=document.pk)
            doc.title = "Renamed during OCR"
            doc.save()
        finally:
            connections.close_all()

    def install():
        try:
            if action == "backfill":
                from documents.page_data import backfill_document

                backfill_document(document.pk)
            else:
                _install_page(
                    Document.objects.get(pk=document.pk),
                    output,
                    [evidence("Completed OCR")],
                    thumbnail,
                )
        finally:
            connections.close_all()

    with (
        override_settings(FILENAME_FORMAT="{title}"),
        mock.patch(
            "documents.signals.handlers.generate_filename",
            side_effect=generation,
        ),
        mock.patch("documents.ocr_queue.FileLock", side_effect=installer_lock),
        mock.patch("documents.page_data.FileLock", side_effect=installer_lock),
        mock.patch("documents.file_updates._publish"),
        mock.patch("documents.ocr_queue.after_page_commit"),
    ):
        with (
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="metadata",
            ) as metadata,
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="installer",
            ) as installation,
        ):
            rename_future = metadata.submit(rename)
            assert rename_has_media.wait(10)
            install_future = installation.submit(install)
            errors = []
            for future in (install_future, rename_future):
                try:
                    future.result(timeout=15)
                except Exception as error:
                    errors.append(type(error).__name__)
    document.refresh_from_db()
    assert not errors, (
        "Row lock / MEDIA_LOCK inversion; unbounded production locks cannot progress"
    )
    stored_file = document.archive_path or document.source_path
    assert stored_file.is_file()
    assert compute_checksum(stored_file) == (
        document.archive_checksum or document.checksum
    )


@pytest.mark.django_db(transaction=True)
def test_same_checksum_rollback_restores_thumbnail(paperless_dirs):
    doc = existing_document(paperless_dirs, 11)
    original = doc.source_path.read_bytes()
    output = paperless_dirs.scratch_dir / "same_bytes.pdf"
    output.write_bytes(original)
    thumbnail = paperless_dirs.scratch_dir / "thumbnail.webp"
    thumbnail.write_bytes(b"new thumbnail")
    with mock.patch(
        "documents.ocr_queue.save_pages",
        side_effect=RuntimeError("rollback"),
    ):
        with pytest.raises(RuntimeError):
            _install_page(doc, output, [evidence("Changed OCR")], thumbnail)
    doc.refresh_from_db()
    assert doc.content == "Old 11"
    assert doc.thumbnail_path.read_bytes() == b"old thumb", (
        "Checksum equality cannot distinguish commit from rollback"
    )


@pytest.mark.django_db(transaction=True)
@override_settings(FILENAME_FORMAT="{title}")
def test_rename_commit_failure_restores_original_and_archive(paperless_dirs):
    document = existing_document(paperless_dirs, 12)
    original = document.source_path
    original_bytes = original.read_bytes()
    archive = write_pdf(paperless_dirs.archive_dir / "previous.pdf", [500])
    archive_bytes = archive.read_bytes()
    Document.objects.filter(pk=document.pk).update(
        title="New metadata title",
        archive_filename=archive.name,
        archive_checksum=compute_checksum(archive),
    )
    document.refresh_from_db()
    with (
        mock.patch.object(
            connection,
            "commit",
            side_effect=OperationalError("synthetic commit failure"),
        ),
        pytest.raises(OperationalError, match="synthetic commit failure"),
    ):
        update_filename_and_move_files(Document, document)
    document.refresh_from_db()
    assert document.source_path == original
    assert document.archive_path == archive
    assert original.read_bytes() == original_bytes
    assert archive.read_bytes() == archive_bytes
    assert list(paperless_dirs.originals_dir.rglob("*.pdf")) == [original]
    assert list(paperless_dirs.archive_dir.rglob("*.pdf")) == [archive]


@pytest.mark.django_db(transaction=True)
def test_failed_rename_commit_and_competing_install_cannot_swap_document_bytes(
    paperless_dirs,
):
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("Requires independent PostgreSQL row locks")
    import shutil

    a = existing_document(paperless_dirs, 1)
    b = existing_document(paperless_dirs, 2)
    old = a.source_path
    original = old.read_bytes()
    Document.objects.filter(pk=a.pk).update(title="Renamed root")
    Document.objects.filter(pk=b.pk).update(title=old.stem)
    output = write_pdf(paperless_dirs.scratch_dir / "other_document.pdf", [900])
    thumb = paperless_dirs.scratch_dir / "other_thumb.webp"
    thumb.write_bytes(b"other thumbnail")
    rename_ready_to_commit = Event()
    other_copied_old_path = Event()
    compensation_finished = Event()
    native_copy = shutil.copyfile

    def copy(source, destination, *args, **kwargs):
        result = native_copy(source, destination, *args, **kwargs)
        if current_thread().name.startswith("install") and destination == old:
            other_copied_old_path.set()
        return result

    def rollback_lock(path, *args, **kwargs):
        if current_thread().name.startswith("install") and path == settings.MEDIA_LOCK:
            assert compensation_finished.wait(10)
        return RealFileLock(path, *args, **kwargs)

    def rename():
        try:
            conn = connections["default"]

            def fail_commit():
                rename_ready_to_commit.set()
                probe = RealFileLock(settings.MEDIA_LOCK, timeout=0)
                try:
                    probe.acquire()
                except Timeout:
                    pass
                else:
                    probe.release()
                    assert other_copied_old_path.wait(10)
                raise OperationalError("synthetic rename commit failure")

            with mock.patch.object(conn, "commit", side_effect=fail_commit):
                update_filename_and_move_files(Document, Document.objects.get(pk=a.pk))
        except OperationalError:
            pass
        finally:
            compensation_finished.set()
            connections.close_all()

    def install():
        try:
            assert rename_ready_to_commit.wait(10)
            _install_page(
                Document.objects.get(pk=b.pk),
                output,
                [evidence("Other document OCR")],
                thumb,
            )
        except Exception as error:
            return type(error).__name__
        finally:
            connections.close_all()

    with (
        override_settings(FILENAME_FORMAT="{title}", STORE_ONLY_CURRENT_FILE=True),
        mock.patch("documents.ocr_queue.shutil.copyfile", side_effect=copy),
        mock.patch("documents.file_updates.FileLock", side_effect=rollback_lock),
        mock.patch("documents.file_updates._publish"),
        mock.patch("documents.ocr_queue.after_page_commit"),
    ):
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="rename") as rp,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="install") as ip,
        ):
            rf = rp.submit(rename)
            it = ip.submit(install)
            rf.result(timeout=20)
            error = it.result(timeout=20)
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.source_path.read_bytes() == original, (
        "Failed rename compensation retained another document bytes at original DB path"
    )
    assert error is None
    assert compute_checksum(b.source_path) == b.checksum
    assert a.source_path != b.source_path
