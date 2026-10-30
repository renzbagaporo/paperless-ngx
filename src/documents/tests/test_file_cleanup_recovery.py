"""Real commit, rollback, concurrency and schema checks for file installation."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock

import pytest
from django.conf import settings
from django.db import DatabaseError
from django.db import connection
from django.db import connections
from django.db import transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents.file_cleanup import complete_cleanup
from documents.file_cleanup import reconcile_cleanup
from documents.file_cleanup import record_cleanup
from documents.models import Document
from documents.models import FileCleanupJob
from documents.ocr_queue import _install_page
from documents.ocr_recovery import reconcile
from documents.page_data import save_pages
from documents.tests.test_file_updates import write_pdf
from documents.utils import compute_checksum
from paperless_testing.factories import DocumentFactory


def evidence(text):
    return dict(
        origin=uuid.uuid4(),
        source_page=1,
        rotation=0,
        text=text,
        markdown=text,
        layout={},
        provenance={"engine": "llamaparse", "store_only_current_file": True},
    )


def existing_document(dirs, number):
    doc = DocumentFactory(
        title="Shared title",
        mime_type="application/pdf",
        content=f"Old {number}",
        page_count=1,
    )
    write_pdf(doc.source_path, [100 + number])
    doc.thumbnail_path.write_bytes(b"old thumb")
    Document.objects.filter(pk=doc.pk).update(
        checksum=compute_checksum(doc.source_path),
        filename=str(doc.source_path.relative_to(dirs.originals_dir)),
    )
    doc.refresh_from_db()
    with mock.patch("documents.ocr_queue.after_page_commit"):
        save_pages(doc, [evidence(f"Old {number}")], automatic_content=f"Old {number}")
    return doc


@pytest.mark.django_db(transaction=True)
def test_concurrent_installs_cannot_overwrite_or_delete_another_root(paperless_dirs):
    if connection.vendor != "postgresql":
        pytest.skip("Requires independent row-locking transactions")
    from documents.file_handling import generate_unique_filename

    docs = [existing_document(paperless_dirs, n) for n in (1, 2)]
    outputs = [
        write_pdf(paperless_dirs.scratch_dir / f"result_{n}.pdf", [500 + n])
        for n in (1, 2)
    ]
    thumbnail = paperless_dirs.scratch_dir / "thumbnail.webp"
    thumbnail.write_bytes(b"new thumbnail")
    barrier = Barrier(2)

    def interleaving_allocator(*args, **kwargs):
        candidate = generate_unique_filename(*args, **kwargs)
        probe = FileLock(settings.MEDIA_LOCK, timeout=0)
        try:
            probe.acquire()
        except Timeout:
            pass  # Allocation is protected; another worker cannot choose it.
        else:
            probe.release()
            barrier.wait(timeout=10)  # Reproduce the unprotected allocation race.
        return candidate

    def install(doc_id, output, number):
        try:
            _install_page(
                Document.objects.get(pk=doc_id),
                output,
                [evidence(f"New {number}")],
                thumbnail,
            )
        finally:
            connections.close_all()

    with (
        override_settings(FILENAME_FORMAT="{title}", STORE_ONLY_CURRENT_FILE=True),
        mock.patch(
            "documents.file_handling.generate_unique_filename",
            side_effect=interleaving_allocator,
        ),
        mock.patch("documents.file_updates._publish"),
        mock.patch("documents.ocr_queue.after_page_commit"),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(install, doc.pk, output, n)
                for doc, output, n in zip(docs, outputs, (1, 2), strict=True)
            ]
            for future in futures:
                future.result(timeout=30)
    for doc, output in zip(docs, outputs, strict=True):
        doc.refresh_from_db()
        assert doc.source_path.read_bytes() == output.read_bytes()
        assert compute_checksum(doc.source_path) == doc.checksum
    assert docs[0].filename != docs[1].filename


@pytest.mark.django_db(transaction=True)
def test_cleanup_survives_a_crash_after_commit_and_document_deletion(paperless_dirs):
    doc = existing_document(paperless_dirs, 3)
    original_hash = doc.checksum
    result = write_pdf(paperless_dirs.scratch_dir / "result.pdf", [700])
    thumbnail = paperless_dirs.scratch_dir / "thumbnail.webp"
    thumbnail.write_bytes(b"new thumb")
    with (
        override_settings(STORE_ONLY_CURRENT_FILE=True),
        mock.patch(
            "documents.file_updates._cleanup_after_commit",
            side_effect=SystemExit("post-commit crash"),
        ),
        mock.patch("documents.file_updates._publish"),
        pytest.raises(SystemExit),
    ):
        _install_page(doc, result, [evidence("Recovered OCR")], thumbnail)
    doc.refresh_from_db()
    assert FileCleanupJob.objects.count() == 1
    with mock.patch("documents.file_updates._publish"):
        reconcile()
    doc.refresh_from_db()
    assert doc.source_path.is_file()
    assert compute_checksum(doc.source_path) == doc.checksum
    assert not any(
        compute_checksum(path) == original_hash
        for path in paperless_dirs.originals_dir.rglob("*.pdf")
    )
    assert not FileCleanupJob.objects.exists()

    old_path = paperless_dirs.originals_dir / "retired_without_root.pdf"
    old_path.write_bytes(b"retired")
    job = record_cleanup(doc.pk, [old_path], [])
    doc.delete()
    Document.deleted_objects.filter(pk=doc.pk).delete()
    assert FileCleanupJob.objects.filter(pk=job.pk).exists()
    reconcile_cleanup()
    assert not old_path.exists()
    assert not FileCleanupJob.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_rollback_keeps_original_bytes_and_discards_cleanup_intent(paperless_dirs):
    doc = existing_document(paperless_dirs, 4)
    original_path = doc.source_path
    original = original_path.read_bytes()
    result = write_pdf(paperless_dirs.scratch_dir / "result.pdf", [800])
    thumbnail = paperless_dirs.scratch_dir / "thumbnail.webp"
    thumbnail.write_bytes(b"new thumb")
    with (
        mock.patch(
            "documents.ocr_queue.save_pages",
            side_effect=RuntimeError("rollback"),
        ),
        pytest.raises(RuntimeError, match="rollback"),
    ):
        _install_page(doc, result, [evidence("OCR")], thumbnail)
    doc.refresh_from_db()
    assert doc.source_path == original_path
    assert original_path.read_bytes() == original
    assert not FileCleanupJob.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_cleanup_never_deletes_referenced_files_or_paths_outside_storage(
    paperless_dirs,
):
    doc = existing_document(paperless_dirs, 5)
    referenced = FileCleanupJob.objects.create(
        root_document_id=doc.pk,
        paths=[{"area": "originals", "path": doc.filename}],
    )
    assert complete_cleanup(referenced.pk)
    assert doc.source_path.exists()
    sentinel = paperless_dirs.scratch_dir / "sentinel"
    sentinel.write_bytes(b"keep")
    invalid = FileCleanupJob.objects.create(
        paths=[{"area": "originals", "path": str(sentinel)}],
    )
    assert not complete_cleanup(invalid.pk)
    assert sentinel.read_bytes() == b"keep"
    assert FileCleanupJob.objects.filter(pk=invalid.pk, attempts=1).exists()


@pytest.mark.django_db(transaction=True)
def test_cleanup_does_not_delete_a_replacement_at_an_old_path(paperless_dirs):
    path = paperless_dirs.originals_dir / "reused.pdf"
    path.write_bytes(b"retired original")
    job = record_cleanup(1, [path], [])
    path.unlink()
    path.write_bytes(b"a new owner's uncommitted replacement")
    assert not complete_cleanup(job.pk)
    assert path.read_bytes() == b"a new owner's uncommitted replacement"
    assert FileCleanupJob.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_postcommit_callback_error_cannot_unlink_the_committed_file(paperless_dirs):
    from documents.page_data import save_pages as actual_save

    doc = existing_document(paperless_dirs, 6)
    result = write_pdf(paperless_dirs.scratch_dir / "postcommit.pdf", [900])
    thumbnail = paperless_dirs.scratch_dir / "postcommit.webp"
    thumbnail.write_bytes(b"new thumb")

    def save_and_register_failure(*args, **kwargs):
        actual_save(*args, **kwargs)

        def fail():
            raise RuntimeError("nonrobust postcommit callback")

        transaction.on_commit(fail)

    with (
        mock.patch(
            "documents.ocr_queue.save_pages",
            side_effect=save_and_register_failure,
        ),
        pytest.raises(RuntimeError, match="nonrobust postcommit"),
    ):
        _install_page(doc, result, [evidence("Committed OCR")], thumbnail)
    doc.refresh_from_db()
    assert doc.source_path.read_bytes() == result.read_bytes()
    assert doc.thumbnail_path.read_bytes() == b"new thumb"


@pytest.mark.django_db(transaction=True)
def test_cleanup_migration_preserves_existing_document_identity_and_bytes(
    paperless_dirs,
):
    executor = MigrationExecutor(connection)
    previous = [("documents", "0028_document_page_data")]
    latest = [("documents", "0029_filecleanupjob")]
    executor.migrate(previous)
    try:
        apps = executor.loader.project_state(previous).apps
        old_document = apps.get_model("documents", "Document")
        source = write_pdf(paperless_dirs.originals_dir / "migration.pdf", [101])
        original = source.read_bytes()
        digest = compute_checksum(source)
        row = old_document.objects.create(
            title="Migration sentinel",
            content="Saved content",
            mime_type="application/pdf",
            checksum=digest,
            filename=source.name,
            archive_serial_number=9876,
        )
        old_page = apps.get_model("documents", "DocumentPage")
        old_page.objects.create(
            document=row,
            page=1,
            source_page=1,
            file_checksum=digest,
            text="Page text",
            markdown="# Page text",
            layout={"table": [["Saved", "10"]]},
            provenance={"engine": "llamaparse"},
        )
        identifier = row.pk
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        preserved = Document.objects.get(pk=identifier)
        assert preserved.title == "Migration sentinel"
        assert preserved.content == "Saved content"
        assert preserved.archive_serial_number == 9876
        assert preserved.source_path.read_bytes() == original
        assert preserved.parsed_pages.get().layout == {"table": [["Saved", "10"]]}
        assert not FileCleanupJob.objects.exists()
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_obsolete_cleanup_cannot_delete_unrelated_imported_revision_with_reused_ids(
    paperless_dirs,
):
    root = existing_document(paperless_dirs, 1)
    version = existing_document(paperless_dirs, 2)
    Document.objects.filter(pk=version.pk).update(
        root_document_id=root.pk,
        version_index=1,
        deleted_at=timezone.now(),
        transaction_id=uuid.uuid4(),
    )
    job = record_cleanup(
        root.pk,
        [root.source_path, version.source_path, version.thumbnail_path],
        [version.pk],
    )
    root_id, version_id = (root.pk, version.pk)
    Document.global_objects.filter(pk__in=[root_id, version_id]).hard_delete()
    imported_root_path = write_pdf(
        paperless_dirs.originals_dir / "new_imported_root.pdf",
        [600],
    )
    imported_version_path = write_pdf(
        paperless_dirs.originals_dir / "new_imported_version.pdf",
        [800],
    )
    imported_root = Document.objects.create(
        pk=root_id,
        title="Unrelated imported collection",
        mime_type="application/pdf",
        checksum=compute_checksum(imported_root_path),
        content="New imported root",
        filename=None,
    )
    Document.objects.filter(pk=root_id).update(filename=imported_root_path.name)
    imported_version = Document.objects.create(
        pk=version_id,
        root_document=imported_root,
        version_index=1,
        title="Unrelated imported trashed revision",
        mime_type="application/pdf",
        checksum=compute_checksum(imported_version_path),
        content="New imported revision",
        filename=None,
    )
    Document.objects.filter(pk=version_id).update(
        filename=imported_version_path.name,
        deleted_at=timezone.now(),
        transaction_id=uuid.uuid4(),
    )
    assert Document.deleted_objects.filter(pk=version_id).exists()
    assert complete_cleanup(job.pk)
    assert Document.global_objects.filter(pk=imported_version.pk).exists(), (
        "Stale outbox intent hard-deleted unrelated imported trashed revision"
    )


@pytest.mark.django_db(transaction=True)
def test_cleanup_rechecks_deletion_identity_at_actual_hard_delete(paperless_dirs):
    if connection.vendor != "postgresql":
        pytest.skip("Real row-lock protection requires PostgreSQL")
    from documents.file_updates import _remove_obsolete

    root = existing_document(paperless_dirs, 1)
    version = existing_document(paperless_dirs, 2)
    original_deletion = uuid.uuid4()
    Document.objects.filter(pk=version.pk).update(
        root_document_id=root.pk,
        version_index=1,
        deleted_at=timezone.now(),
        transaction_id=original_deletion,
    )
    job = record_cleanup(
        root.pk,
        [version.source_path, version.thumbnail_path],
        [version.pk],
    )
    restore_won = []

    def restore_and_retrash_in_other_connection():
        try:
            with transaction.atomic():
                current = Document.deleted_objects.select_for_update(nowait=True).get(
                    pk=version.pk,
                )
                current.restore(strict=False)
                Document.objects.get(pk=version.pk).delete()
                newer = Document.deleted_objects.get(pk=version.pk)
                assert newer.transaction_id != original_deletion
            return True
        except DatabaseError:
            return False
        finally:
            connections.close_all()

    def restore_and_retrash(paths, version_ids, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as worker:
            restore_won.append(
                worker.submit(restore_and_retrash_in_other_connection).result(
                    timeout=15,
                ),
            )
        return _remove_obsolete(paths, version_ids, **kwargs)

    with mock.patch(
        "documents.file_updates._remove_obsolete",
        side_effect=restore_and_retrash,
    ):
        complete_cleanup(job.pk)
    assert (
        not restore_won[0] or Document.deleted_objects.filter(pk=version.pk).exists()
    ), "Cleanup validated an old deletion UUID then hard-deleted a new trash lifetime"
