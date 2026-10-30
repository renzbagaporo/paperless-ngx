"""Transactional cleanup outbox; broker availability never owns deletion intent."""

import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents.file_handling import validate_path_in_root
from documents.models import Document
from documents.models import FileCleanupJob

logger = logging.getLogger("paperless.file_cleanup")


def storage_roots():
    return {
        "originals": settings.ORIGINALS_DIR,
        "archive": settings.ARCHIVE_DIR,
        "thumbnails": settings.THUMBNAIL_DIR,
    }


def file_identity(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return [stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_size]


def version_identity(document):
    return {
        "transaction": str(document.transaction_id)
        if document.transaction_id
        else None,
        "added": document.added.isoformat(),
        "checksum": document.checksum,
        "archive_checksum": document.archive_checksum,
        "filename": document.filename,
        "archive_filename": document.archive_filename,
    }


def record_cleanup(root_id: int, paths: list[Path], version_ids: list[int]):
    """Call inside the same transaction as the new file revision."""
    encoded = []
    for path in set(paths):
        for area, directory in storage_roots().items():
            if path.is_relative_to(directory):
                validate_path_in_root(path, directory)
                encoded.append(
                    {
                        "area": area,
                        "path": str(path.relative_to(directory)),
                        "identity": file_identity(path),
                    },
                )
                break
        else:
            logger.error("Refusing cleanup intent outside document storage")
    if not encoded and not version_ids:
        return None
    return FileCleanupJob.objects.create(
        root_document_id=root_id,
        paths=encoded,
        version_ids=version_ids,
        version_identities={
            str(version.pk): version_identity(version)
            for version in Document.deleted_objects.filter(
                pk__in=version_ids,
                root_document_id=root_id,
            )
        },
    )


def complete_cleanup(job_id) -> bool:
    from documents.file_updates import _remove_obsolete

    try:
        with FileLock(settings.DATA_DIR / "file-cleanup.lock", timeout=0):
            job = FileCleanupJob.objects.filter(pk=job_id).first()
            if job is None:
                return True
            paths = []
            identities = {}
            for item in job.paths:
                directory = storage_roots()[item["area"]]
                relative = Path(item["path"])
                if relative.is_absolute():
                    raise ValueError("Cleanup paths must be relative to storage")
                path = directory / relative
                validate_path_in_root(path, directory)
                paths.append(path)
                identities[path] = item.get("identity")
            # IDs may survive a delete/restore/import. Never delete a revision
            # belonging to a different collection, or a live revision.
            _remove_obsolete(
                paths,
                job.version_ids,
                identities=identities,
                version_identities=job.version_identities,
                root_id=job.root_document_id,
            )
            job.delete()
            return True
    except Timeout:
        return False
    except Exception:
        FileCleanupJob.objects.filter(pk=job_id).update(
            attempts=F("attempts") + 1,
            available_at=timezone.now() + timedelta(minutes=5),
        )
        logger.exception("Retired-file cleanup remains queued")
        return False


def reconcile_cleanup(limit: int = 100) -> int:
    jobs = list(
        FileCleanupJob.objects.filter(available_at__lte=timezone.now())
        .order_by("created", "id")
        .values_list("pk", flat=True)[:limit],
    )
    return sum(complete_cleanup(job_id) for job_id in jobs)
