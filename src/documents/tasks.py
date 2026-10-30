import datetime
import logging
import shutil
import uuid
import zipfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from tempfile import mkstemp

from celery import Task
from celery import shared_task
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError
from django.db import models
from django.db import transaction
from django.db.models.signals import post_save
from django.utils import timezone
from filelock import FileLock

from documents import sanity_checker
from documents.barcodes import BarcodePlugin
from documents.barcodes import read_barcode_values
from documents.bulk_download import ArchiveOnlyStrategy
from documents.bulk_download import OriginalsOnlyStrategy
from documents.caching import clear_document_caches
from documents.classifier import DocumentClassifier
from documents.classifier import load_classifier
from documents.consumer import AsnCheckPlugin
from documents.consumer import ConsumeFileDuplicateError
from documents.consumer import ConsumerPlugin
from documents.consumer import ConsumerPreflightPlugin
from documents.consumer import WorkflowTriggerPlugin
from documents.consumer import should_produce_archive
from documents.data_models import ConsumableDocument
from documents.data_models import ConsumeFileDuplicateResult
from documents.data_models import ConsumeFileStoppedResult
from documents.data_models import ConsumeFileSuccessResult
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import StoredBarcode
from documents.double_sided import CollatePlugin
from documents.matching import prefilter_documents_by_workflowtrigger
from documents.models import Correspondent
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import DocumentBarcode
from documents.models import DocumentType
from documents.models import PaperlessTask
from documents.models import ShareLink
from documents.models import ShareLinkBundle
from documents.models import StoragePath
from documents.models import Tag
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.plugins.base import ConsumeTaskPlugin
from documents.plugins.base import StopConsumeTaskError
from documents.plugins.helpers import ProgressManager
from documents.plugins.helpers import ProgressStatusOptions
from documents.sanity_checker import SanityCheckFailedException
from documents.search._backend import SearchIndexLockError
from documents.signals import document_updated
from documents.signals.handlers import cleanup_document_deletion
from documents.signals.handlers import run_workflows
from documents.signals.handlers import send_websocket_document_updated
from documents.utils import IterWrapper
from documents.utils import compute_checksum
from documents.utils import identity
from documents.versioning import annotate_effective_content
from documents.versioning import get_latest_version_for_root
from documents.workflows.utils import get_workflows_for_trigger
from paperless.config import AIConfig
from paperless.config import BarcodeConfig
from paperless.config import RemoteOCRConfig
from paperless.logging import consume_task_id
from paperless.parsers import ParserContext
from paperless.parsers.registry import get_parser_registry
from paperless_ai.exceptions import LLMTimeoutError
from paperless_ai.indexing import llm_index_add_or_update_document
from paperless_ai.indexing import llm_index_remove_document
from paperless_ai.indexing import update_llm_index

if settings.AUDIT_LOG_ENABLED:
    from auditlog.models import LogEntry
logger = logging.getLogger("paperless.tasks")


@shared_task
def index_optimize() -> None:
    logger.info(
        "index_optimize is a no-op — Tantivy manages segment merging automatically.",
    )


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(SearchIndexLockError,),
    max_retries=5,
    retry_backoff=60,
    retry_jitter=True,
)
def index_document(self, document_id: int) -> None:
    """
    Deferred single-document index write.

    Used as a self-healing fallback when add_or_update() exhausts its lock retry
    budget during high-concurrency consumption. Runs via batch_update() directly
    to avoid re-entering the deferred scheduling path in add_or_update().

    If the document was deleted before this task runs, it exits cleanly.
    """
    from documents.search import get_backend

    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        logger.info(
            "index_document: document %d no longer exists; skipping",
            document_id,
        )
        return
    with get_backend().batch_update() as batch:
        batch.add_or_update(document)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(SearchIndexLockError,),
    max_retries=5,
    retry_backoff=60,
    retry_jitter=True,
)
def remove_document_from_index(self, doc_id: int) -> None:
    """
    Deferred single-document index removal.

    Used as a self-healing fallback when remove() exhausts its lock retry budget.
    Operates only on the Tantivy index; no database lookup required.
    If the document has already been removed, the term-query delete is a no-op.
    """
    from documents.search import get_backend

    with get_backend().batch_update() as batch:
        batch.remove(doc_id)


@shared_task
def train_classifier(
    *,
    status_callback: Callable[[str], None] | None = None,
) -> str:
    if (
        not Tag.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
        and not DocumentType.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
        and not Correspondent.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
        and not StoragePath.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
    ):
        result = "No automatic matching items, not training"
        logger.info(result)
        # Special case, items were once auto and trained, so remove the model
        # and prevent its use again
        if settings.MODEL_FILE.exists():  # pragma: no cover
            logger.info(f"Removing {settings.MODEL_FILE} so it won't be used")
            settings.MODEL_FILE.unlink()
        return result

    classifier = load_classifier()

    if not classifier:
        classifier = DocumentClassifier()

    if classifier.train(status_callback=status_callback):
        logger.info(
            f"Saving updated classifier model to {settings.MODEL_FILE}...",
        )
        classifier.save()
        return "Training completed successfully"
    else:
        logger.debug("Training data unchanged.")
        return "Training data unchanged"


@shared_task(bind=True)
def consume_file(
    self: Task,
    input_doc: ConsumableDocument,
    overrides: DocumentMetadataOverrides | None = None,
) -> (
    ConsumeFileSuccessResult
    | ConsumeFileStoppedResult
    | ConsumeFileDuplicateResult
    | None
):
    token = consume_task_id.set((self.request.id or "")[:8])
    try:
        # Default no overrides
        if overrides is None:
            overrides = DocumentMetadataOverrides()

        plugins: list[type[ConsumeTaskPlugin]] = (
            [
                ConsumerPreflightPlugin,
                ConsumerPlugin,
            ]
            if input_doc.root_document_id is not None
            else [
                ConsumerPreflightPlugin,
                AsnCheckPlugin,
                CollatePlugin,
                BarcodePlugin,
                AsnCheckPlugin,  # Re-run ASN check after barcode reading
                WorkflowTriggerPlugin,
                ConsumerPlugin,
            ]
        )

        with (
            ProgressManager(
                overrides.filename or input_doc.original_file.name,
                self.request.id,
            ) as status_mgr,
            TemporaryDirectory(dir=settings.SCRATCH_DIR) as tmp_dir,
        ):
            tmp_dir = Path(tmp_dir)
            msg = None
            for plugin_class in plugins:
                plugin_name = plugin_class.NAME

                plugin = plugin_class(
                    input_doc,
                    overrides,
                    status_mgr,
                    tmp_dir,
                    self.request.id,
                )

                if not plugin.able_to_run:
                    logger.debug(f"Skipping plugin {plugin_name}")
                    continue

                try:
                    logger.debug(f"Executing plugin {plugin_name}")
                    plugin.setup()

                    msg = plugin.run()

                    if msg is not None:
                        logger.info(f"{plugin_name} completed with: {msg}")
                    else:
                        logger.info(f"{plugin_name} completed with no message")

                    overrides = plugin.metadata

                except StopConsumeTaskError as e:
                    logger.info(f"{plugin_name} requested task exit: {e.message}")
                    return ConsumeFileStoppedResult(reason=e.message)

                except ConsumeFileDuplicateError as e:
                    logger.info(f"{plugin_name} rejected duplicate: {e}")
                    return ConsumeFileDuplicateResult(
                        duplicate_of=e.duplicate_id,
                        duplicate_in_trash=e.in_trash,
                    )

                except Exception as e:
                    logger.exception(f"{plugin_name} failed: {e}")
                    status_mgr.send_progress(
                        ProgressStatusOptions.FAILED,
                        f"{e}",
                        100,
                        100,
                    )
                    raise

                finally:
                    plugin.cleanup()

        return msg
    finally:
        consume_task_id.reset(token)


@shared_task(bind=True)
def update_document_file(
    self: Task,
    input_doc: ConsumableDocument,
    *,
    append: bool,
    keep_only_current_file: bool,
    remote_ocr: bool | None = None,
    user_id: int | None = None,
    operations: list[dict] | None = None,
    source_document_id: int | None = None,
    source_mode: str = "latest_version",
    source_checksum: str | None = None,
    edit_only: bool = False,
    expected_current_document_id: int | None = None,
    expected_current_checksum: str | None = None,
) -> ConsumeFileSuccessResult:
    from documents.file_updates import update_file

    try:
        with ProgressManager(input_doc.original_file.name, self.request.id) as progress:
            progress.send_progress(
                ProgressStatusOptions.WORKING,
                "Processing document file",
                10,
                100,
                owner_id=user_id,
            )
            try:
                result = update_file(
                    input_doc.root_document_id,
                    None if edit_only else input_doc.original_file,
                    append=append,
                    keep_only_current_file=keep_only_current_file,
                    remote_ocr=remote_ocr,
                    user_id=user_id,
                    operations=operations,
                    source_document_id=source_document_id,
                    source_mode=source_mode,
                    source_checksum=source_checksum,
                    expected_current_document_id=expected_current_document_id,
                    expected_current_checksum=expected_current_checksum,
                )
            except Exception as error:
                progress.send_progress(
                    ProgressStatusOptions.FAILED,
                    str(error),
                    100,
                    100,
                    owner_id=user_id,
                )
                raise
            progress.send_progress(
                ProgressStatusOptions.SUCCESS,
                "Document file updated",
                100,
                100,
                document_id=result["document_id"],
                owner_id=user_id,
            )
            return result
    finally:
        if not edit_only:
            input_doc.original_file.unlink(missing_ok=True)
            # This directory is created for this API request alone.
            try:
                input_doc.original_file.parent.rmdir()
            except OSError:
                pass


@shared_task(autoretry_for=(OSError, DatabaseError), retry_backoff=30, max_retries=5)
def compact_document_files(
    document_id: int,
    file_checksum: str | None = None,
    archive_checksum: str | None = None,
) -> None:
    from documents.file_updates import compact_ingested_root

    if file_checksum is None:
        logger.info(
            "Skipping file compaction for document %s without a source revision identity",
            document_id,
        )
        return
    compact_ingested_root(document_id, file_checksum, archive_checksum)


@shared_task(autoretry_for=(OSError, DatabaseError), retry_backoff=30, max_retries=5)
def cleanup_obsolete_document_files(paths: list[str], version_ids: list[int]) -> None:
    from documents.file_updates import _remove_obsolete

    _remove_obsolete([Path(path) for path in paths], version_ids)


@shared_task
def sanity_check(*, raise_on_error: bool = True) -> str:
    messages = sanity_checker.check_sanity()
    messages.log_messages()

    if not messages.has_error and not messages.has_warning and not messages.has_info:
        return "No issues detected."

    parts: list[str] = []
    if messages.document_error_count:
        parts.append(f"{messages.document_error_count} document(s) with errors")
    if messages.document_warning_count:
        parts.append(f"{messages.document_warning_count} document(s) with warnings")
    if messages.document_info_count:
        parts.append(f"{messages.document_info_count} document(s) with infos")
    if messages.global_warning_count:
        parts.append(f"{messages.global_warning_count} global warning(s)")

    summary = ", ".join(parts) + " found."

    if messages.has_error:
        message = summary + " Check logs for details."
        if raise_on_error:
            raise SanityCheckFailedException(message)
        return message

    return summary


@shared_task
def bulk_update_documents(document_ids, *, ocr_retry_only_ids=None) -> None:
    from documents.search import get_backend

    document_ids = list(document_ids)
    # Annotated so the signal handlers below (e.g. matching) don't query the
    # versions of each document. Indexing re-queries and re-annotates its own
    # copy via add_or_update_ids() below, after these signals (and any
    # workflow they trigger) have had a chance to mutate the documents.
    documents = annotate_effective_content(
        Document.objects.filter(id__in=document_ids),
    )

    retry_only_ids = set(ocr_retry_only_ids or [])
    index_ids = []
    for doc in documents:
        clear_document_caches(doc.pk)
        from documents.ocr_queue import pending_pages

        current = get_latest_version_for_root(doc)
        pending = bool(pending_pages(current))
        if doc.pk in retry_only_ids:
            send_websocket_document_updated(sender=None, document=doc)
        else:
            document_updated.send(
                sender=None,
                document=doc,
                logging_group=uuid.uuid4(),
                skip_ai_index=True,
            )
            if not pending:
                index_ids.append(doc.pk)
        post_save.send(Document, instance=doc, created=False)

    with get_backend().batch_update() as batch:
        batch.add_or_update_ids(document_ids)

    ai_config = AIConfig()
    if ai_config.llm_index_enabled and index_ids:
        positions = {str(pk): index for index, pk in enumerate(document_ids)}
        index_ids.sort(key=lambda pk: positions.get(str(pk), len(positions)))
        from documents.ocr_index import acknowledge_manual_index
        from documents.ocr_index import mark_pending

        while True:
            root_ids = sorted(
                {
                    doc.root_document_id or doc.pk
                    for doc in Document.objects.filter(pk__in=index_ids).only(
                        "pk",
                        "root_document_id",
                    )
                },
            )
            with ExitStack() as locks:
                for root_id in root_ids:
                    locks.enter_context(
                        FileLock(
                            settings.DATA_DIR / f"document-index-work-{root_id}.lock",
                        ),
                    )
                documents = list(Document.objects.filter(pk__in=index_ids))
                if any(
                    (doc.root_document_id or doc.pk) not in root_ids
                    for doc in documents
                ):
                    continue
                snapshots = [(doc, mark_pending(doc)) for doc in documents]
                update_llm_index(
                    rebuild=False,
                    document_ids=index_ids,
                    require_completed=True,
                )
                for doc, expected in snapshots:
                    if expected is not None:
                        acknowledge_manual_index(doc, expected)
                break


def _read_barcodes_for_reprocess(document: Document) -> list[StoredBarcode] | None:
    """
    Reads the barcodes of the original again, e.g. for documents consumed
    before storing them was enabled. Returns None if they should be left as
    they are: storing is off, the file can't be scanned with the current
    settings, or the scan failed.
    """
    barcode_settings = BarcodeConfig()
    if not barcode_settings.barcode_store_values:
        return None
    try:
        with TemporaryDirectory(dir=settings.SCRATCH_DIR) as tmpdir:
            return read_barcode_values(
                document.source_path,
                document.mime_type,
                barcode_settings,
                Path(tmpdir),
            )
    except Exception as e:
        logger.warning(f"Could not read barcodes of document {document}: {e}")
        return None


@shared_task
def update_document_content_maybe_archive_file(
    document_id,
    *,
    remote_ocr: bool = False,
    pending_only: bool = False,
) -> None:
    if pending_only:
        document = Document.objects.filter(pk=document_id).first()
        if document is None:
            return
    else:
        document = Document.objects.get(pk=document_id)
    root_id = document.root_document_id or document.pk
    from documents.ocr_queue import pending_pages
    from documents.ocr_queue import retry_pending_ocr

    current = (
        get_latest_version_for_root(document)
        if document.root_document_id is None
        else document
    )
    config = RemoteOCRConfig()

    def use_saved_ocr(candidate):
        return bool(pending_pages(candidate)) and (
            pending_only
            or (
                config.remote_ocr_engine == "llamaparse"
                and (remote_ocr or config.remote_ocr_by_default)
            )
        )

    if use_saved_ocr(current):
        # Preserve a root request until retry owns the file lock and can
        # select the then-current revision. Explicit child requests stay scoped.
        retry_pending_ocr(document.pk, requested_only=pending_only)
        return
    if pending_only:
        return
    with FileLock(settings.DATA_DIR / f"document-file-{root_id}.lock"):
        document.refresh_from_db()
        current = (
            get_latest_version_for_root(document)
            if document.root_document_id is None
            else document
        )
        if not use_saved_ocr(current):
            _update_document_content_maybe_archive_file(
                current.pk,
                remote_ocr=remote_ocr,
            )
            return
    # A file update completed while this task waited. The retry function owns
    # its lock and performs its own final revision selection.
    retry_pending_ocr(document.pk, requested_only=pending_only)


def _update_document_content_maybe_archive_file(
    document_id,
    *,
    remote_ocr: bool = False,
) -> None:
    """
    Re-creates OCR content and thumbnail for a document, and archive file if
    it exists.

    Remote OCR is used only when the engine is configured to handle everything
    or if explicitly asked for via ``remote_ocr``.
    """
    document = Document.objects.get(id=document_id)

    mime_type = document.mime_type

    parser_class = get_parser_registry().get_parser_for_file(
        mime_type,
        document.original_filename or "",
        document.source_path,
        allow_remote=remote_ocr or RemoteOCRConfig().remote_ocr_by_default,
    )

    if not parser_class:
        logger.error(
            f"No parser found for mime type {mime_type}, cannot "
            f"archive document {document} (ID: {document_id})",
        )
        return

    with parser_class() as parser:
        parser.configure(ParserContext())
        from paperless.parsers.llamaparse import LlamaParseDocumentParser

        try:
            with FileLock(settings.MEDIA_LOCK):
                document.refresh_from_db()
                before_content = document.content
                expected_source_checksum = compute_checksum(
                    document.archive_path or document.source_path,
                )
                policy_source = document.source_path
                parse_source = document.source_path
                parse_mime_type = mime_type
                if isinstance(parser, LlamaParseDocumentParser):
                    original = (
                        parser._tempdir / f"original_input{document.source_path.suffix}"
                    )
                    shutil.copyfile(document.source_path, original)
                    policy_source = original
                    parse_source = original
                    if document.archive_path is not None:
                        parse_source = parser._tempdir / "stored_input.pdf"
                        shutil.copyfile(document.archive_path, parse_source)
                        parse_mime_type = "application/pdf"
            produce_archive = should_produce_archive(
                parser,
                mime_type,
                policy_source,
            )
            from documents.page_data import stored_pages

            previous_pages = stored_pages(document) or []

            def bind_previous_evidence(evidence, previous):
                from documents.page_data import preserve_page_state

                if len(evidence) != len(previous):
                    return
                for page, old in zip(evidence, previous, strict=True):
                    relative_basis = page["provenance"].get("layout_rotation", 0)
                    for field in ("origin", "source_page", "rotation"):
                        page[field] = old[field]
                    preserve_page_state(old["provenance"], page["provenance"])
                    page["provenance"]["layout_rotation"] = (
                        old["rotation"] + relative_basis
                    ) % 360
                    page["provenance"].pop("ocr_job", None)
                    retry = page["provenance"].get("ocr_retry")
                    if retry:
                        retry.update(requested=False, running=False)

            parser_options = {}
            if isinstance(parser, LlamaParseDocumentParser):
                # Reprocess the same physical rendition that pending retries,
                # page edits and splits retain. The remote parser removes its
                # old invisible OCR before submitting compressed page content.
                parser_options["auto_rotate"] = not any(
                    page["rotation"] or page["provenance"].get("manual_rotation")
                    for page in previous_pages
                )

                def remember_reprocess_job(job):
                    from documents.ocr_queue import defer_existing_ocr
                    from documents.ocr_recovery import RETRY
                    from documents.ocr_recovery import store_requests

                    status = {"status": "pending", "reason": "in_progress"}
                    if job is not None:
                        status["job"] = job
                    defer_existing_ocr(document, status, parser.get_source_rendition())
                    pages = stored_pages(document)
                    for page in pages:
                        page["provenance"][RETRY] = {
                            "requested": False,
                            "running": job is not None,
                            "attempts": 1,
                            "resume": True,
                            "sent_at": None,
                        }
                    store_requests(document, pages)

                parser._job_state_callback = remember_reprocess_job
            parser.parse(
                parse_source,
                parse_mime_type,
                produce_archive=produce_archive,
                **parser_options,
            )

            status = getattr(type(parser), "get_ocr_status", None)
            if status and status(parser).get("status") == "pending":
                from documents.ocr_queue import defer_existing_ocr

                pending_rendition = parser.get_archive_path()
                if isinstance(parser, LlamaParseDocumentParser):
                    pending_rendition = (
                        parser.get_source_rendition() or pending_rendition
                    )
                defer_existing_ocr(document, status(parser), pending_rendition)
                if isinstance(parser, LlamaParseDocumentParser):
                    from documents.ocr_recovery import finish

                    finish(document)
                return

            barcodes = _read_barcodes_for_reprocess(document)

            thumbnail = parser.get_thumbnail(
                (parser.get_source_rendition() or parse_source)
                if isinstance(parser, LlamaParseDocumentParser)
                else document.source_path,
                "application/pdf"
                if isinstance(parser, LlamaParseDocumentParser)
                else mime_type,
            )

            page_index_coordinated = False
            if parser.get_archive_path():
                from documents.ingestion_checks import collect_ingestion_checks
                from documents.ocr_queue import _install_page
                from documents.page_data import parser_pages

                evidence = parser_pages(parser, parser.get_archive_path())
                bind_previous_evidence(evidence, previous_pages)
                _install_page(
                    document,
                    parser.get_archive_path(),
                    evidence,
                    thumbnail,
                    collect_ingestion_checks(parser, parser.get_text()),
                    reprocess_content=parser.get_text(),
                    expected_content=before_content,
                    expected_source_checksum=expected_source_checksum,
                )
                page_index_coordinated = True
                with transaction.atomic():
                    if barcodes is not None:
                        document.barcodes.all().delete()
                        DocumentBarcode.objects.bulk_create(
                            DocumentBarcode(document=document, **barcode)
                            for barcode in barcodes
                        )
                    if settings.AUDIT_LOG_ENABLED:
                        LogEntry.objects.log_create(
                            instance=document,
                            changes={"content": [before_content, document.content]},
                            additional_data={"reason": "Update document content"},
                            action=LogEntry.Action.UPDATE,
                        )
            else:
                with transaction.atomic():
                    oldDocument = Document.objects.get(pk=document.pk)
                    content_update = Document.objects.filter(pk=document.pk)
                    if isinstance(parser, LlamaParseDocumentParser):
                        content_update = content_update.filter(content=before_content)
                    content_written = content_update.update(
                        content=parser.get_text(),
                    )

                    if settings.AUDIT_LOG_ENABLED and content_written:
                        LogEntry.objects.log_create(
                            instance=oldDocument,
                            changes={
                                "content": [oldDocument.content, parser.get_text()],
                            },
                            additional_data={
                                "reason": "Update document content",
                            },
                            action=LogEntry.Action.UPDATE,
                        )

                    if barcodes is not None:
                        document.barcodes.all().delete()
                        DocumentBarcode.objects.bulk_create(
                            DocumentBarcode(document=document, **barcode)
                            for barcode in barcodes
                        )
                        # metadata_etag includes modified
                        Document.objects.filter(pk=document.pk).update(
                            modified=timezone.now(),
                        )

                    with FileLock(settings.MEDIA_LOCK):
                        if isinstance(parser, LlamaParseDocumentParser):
                            document.refresh_from_db()
                            if (
                                compute_checksum(
                                    document.archive_path or document.source_path,
                                )
                                != expected_source_checksum
                            ):
                                raise ValueError(
                                    "Stored file changed during OCR; result not installed.",
                                )
                        shutil.move(thumbnail, document.thumbnail_path)

                        if (
                            document.mime_type == "application/pdf"
                            or document.has_archive_version
                            or isinstance(parser, LlamaParseDocumentParser)
                        ):
                            from documents.page_data import parser_pages
                            from documents.page_data import save_pages
                            from documents.page_data import stored_pages

                            document.refresh_from_db()
                            previous_pages = stored_pages(document)
                            reuse_existing = (
                                status(parser).get("status") == "skip"
                                if isinstance(parser, LlamaParseDocumentParser)
                                else parser.get_archive_path() is None
                            )
                            reuse_existing = bool(
                                reuse_existing
                                and previous_pages
                                and not any(
                                    page["provenance"].get("ocr_status") == "pending"
                                    for page in previous_pages
                                ),
                            )
                            evidence = (
                                previous_pages
                                if reuse_existing
                                else parser_pages(
                                    parser,
                                    parser.get_source_rendition() or parse_source
                                    if isinstance(parser, LlamaParseDocumentParser)
                                    else document.archive_path or document.source_path,
                                )
                            )
                            if not reuse_existing:
                                bind_previous_evidence(evidence, previous_pages or [])
                            save_pages(
                                document,
                                evidence,
                                automatic_content=parser.get_text() or "",
                                manual_content=not bool(content_written),
                                index_pending=True,
                            )
                            page_index_coordinated = True

            document.refresh_from_db()
            logger.info(
                f"Updating index for document {document_id} ({document.archive_checksum})",
            )
            from documents.search import get_backend

            get_backend().add_or_update(document)

            ai_config = AIConfig()
            if ai_config.llm_index_enabled and not page_index_coordinated:
                llm_index_add_or_update_document(document)

            clear_document_caches(document.pk)
            if document.root_document_id is not None:
                clear_document_caches(document.root_document_id)

        except Exception:
            logger.exception(
                f"Error while parsing document {document} (ID: {document_id})",
            )


@shared_task
def reconcile_queued_ocr() -> int:
    from documents.ocr_recovery import reconcile

    return reconcile()


@shared_task
def empty_trash(doc_ids=None) -> None:
    if doc_ids is None:
        logger.info("Emptying trash of all expired documents")
    documents = (
        Document.deleted_objects.filter(id__in=doc_ids)
        if doc_ids is not None
        else Document.deleted_objects.filter(
            deleted_at__lt=timezone.localtime(timezone.now())
            - datetime.timedelta(
                days=settings.EMPTY_TRASH_DELAY,
            ),
        )
    )

    try:
        deleted_document_ids = list(documents.values_list("id", flat=True))
        # Temporarily connect the cleanup handler
        models.signals.post_delete.connect(cleanup_document_deletion, sender=Document)
        documents.delete()  # this is effectively a hard delete
        logger.info(f"Deleted {len(deleted_document_ids)} documents from trash")

        if settings.AUDIT_LOG_ENABLED:
            # Delete the audit log entries for documents that dont exist anymore
            LogEntry.objects.filter(
                content_type=ContentType.objects.get_for_model(Document),
                object_id__in=deleted_document_ids,
            ).delete()
    except Exception as e:  # pragma: no cover
        logger.exception(f"Error while emptying trash: {e}")
    finally:
        models.signals.post_delete.disconnect(
            cleanup_document_deletion,
            sender=Document,
        )


@shared_task
def check_scheduled_workflows() -> None:
    """
    Check and run all enabled scheduled workflows.

    Scheduled triggers are evaluated based on a target date field (e.g. added, created, modified, or a custom date field),
    combined with a day offset:
        - Positive offsets mean the workflow should trigger AFTER the specified date (e.g., offset = +7 → trigger 7 days after)
        - Negative offsets mean the workflow should trigger BEFORE the specified date (e.g., offset = -7 → trigger 7 days before)

    Once a document satisfies this condition, and recurring/non-recurring constraints are met, the workflow is run.
    """
    scheduled_workflows = get_workflows_for_trigger(
        WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
    )
    if scheduled_workflows.count() > 0:
        logger.debug(f"Checking {len(scheduled_workflows)} scheduled workflows")
        now = timezone.now()
        for workflow in scheduled_workflows:
            schedule_triggers = workflow.triggers.filter(
                type=WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
            )
            trigger: WorkflowTrigger
            for trigger in schedule_triggers:
                documents = Document.objects.none()
                offset_td = datetime.timedelta(days=trigger.schedule_offset_days)
                threshold = now - offset_td
                logger.debug(
                    f"Trigger {trigger.id}: checking if (date + {offset_td}) <= now ({now})",
                )

                match trigger.schedule_date_field:
                    case WorkflowTrigger.ScheduleDateField.ADDED:
                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            added__lte=threshold,
                        )

                    case WorkflowTrigger.ScheduleDateField.CREATED:
                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            created__lte=threshold,
                        )

                    case WorkflowTrigger.ScheduleDateField.MODIFIED:
                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            modified__lte=threshold,
                        )

                    case WorkflowTrigger.ScheduleDateField.CUSTOM_FIELD:
                        # cap earliest date to avoid massive scans
                        earliest_date = now - datetime.timedelta(days=365)
                        if offset_td.days < -365:
                            logger.warning(
                                f"Trigger {trigger.id} has large negative offset ({offset_td.days}), "
                                f"limiting earliest scan date to {earliest_date}",
                            )

                        cf_filter_kwargs = {
                            "field": trigger.schedule_date_custom_field,
                            "value_date__isnull": False,
                            "value_date__lte": threshold,
                            "value_date__gte": earliest_date,
                        }

                        recent_cf_instances = CustomFieldInstance.objects.filter(
                            **cf_filter_kwargs,
                        )

                        matched_ids = [
                            cfi.document_id
                            for cfi in recent_cf_instances
                            if cfi.value_date
                            and (
                                timezone.make_aware(
                                    datetime.datetime.combine(
                                        cfi.value_date,
                                        datetime.time.min,
                                    ),
                                )
                                + offset_td
                                <= now
                            )
                        ]

                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            id__in=matched_ids,
                        )

                if documents.exists():
                    documents = prefilter_documents_by_workflowtrigger(
                        documents,
                        trigger,
                    )

                if documents.exists():
                    logger.debug(
                        f"Found {documents.count()} documents for trigger {trigger}",
                    )
                    for document in documents:
                        workflow_runs = WorkflowRun.objects.filter(
                            document=document,
                            type=WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
                            workflow=workflow,
                        ).order_by("-run_at")
                        if not trigger.schedule_is_recurring and workflow_runs.exists():
                            logger.debug(
                                f"Skipping document {document} for non-recurring workflow {workflow} as it has already been run",
                            )
                            continue

                        if (
                            trigger.schedule_is_recurring
                            and workflow_runs.exists()
                            and (
                                workflow_runs.first().run_at
                                > now
                                - datetime.timedelta(
                                    days=trigger.schedule_recurring_interval_days,
                                )
                            )
                        ):
                            # schedule is recurring but the last run was within the number of recurring interval days
                            logger.debug(
                                f"Skipping document {document} for recurring workflow {workflow} as the last run was within the recurring interval",
                            )
                            continue
                        run_workflows(
                            trigger_type=WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
                            workflow_to_run=workflow,
                            document=document,
                        )
                        # Scheduled workflows dont send document_updated signal, so send a websocket update here to ensure clients are updated
                        send_websocket_document_updated(
                            sender=None,
                            document=document,
                        )


def update_document_parent_tags(tag: Tag, new_parent: Tag) -> None:
    """
    When a tag's parent changes, ensure all documents containing the tag also have
    the parent tag (and its ancestors) applied.
    """
    doc_tag_relationship = Document.tags.through

    doc_ids: list[int] = list(
        Document.objects.filter(tags=tag).values_list("pk", flat=True),
    )

    if not doc_ids:
        return

    parent_ids = [new_parent.id, *new_parent.get_ancestors_pks()]

    parent_ids = list(dict.fromkeys(parent_ids))

    existing_pairs = set(
        doc_tag_relationship.objects.filter(
            document_id__in=doc_ids,
            tag_id__in=parent_ids,
        ).values_list("document_id", "tag_id"),
    )

    to_create: list = []
    affected: set[int] = set()

    for doc_id in doc_ids:
        for parent_id in parent_ids:
            if (doc_id, parent_id) in existing_pairs:
                continue

            to_create.append(
                doc_tag_relationship(document_id=doc_id, tag_id=parent_id),
            )
            affected.add(doc_id)

    if to_create:
        doc_tag_relationship.objects.bulk_create(
            to_create,
            ignore_conflicts=True,
        )

    if affected:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": list(affected)},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )


@shared_task
def llmindex_index(
    *,
    iter_wrapper: IterWrapper[Document] = identity,
    rebuild: bool = False,
) -> str | None:
    ai_config = AIConfig()
    if not ai_config.llm_index_enabled:  # pragma: no cover
        logger.info("LLM index is disabled, skipping update.")
        return None

    from paperless_ai.indexing import update_llm_index

    return update_llm_index(
        iter_wrapper=iter_wrapper,
        rebuild=rebuild,
    )


@shared_task(
    bind=True,
    autoretry_for=(LLMTimeoutError,),
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    retry_jitter=True,
)
def apply_ai_suggestions(
    self,
    action_id: int,
    document_id: int,
    *,
    expected_metadata: dict | None = None,
) -> None:
    """
    Deferred "apply AI suggestions" workflow action.
    """
    from documents.models import WorkflowAction
    from documents.workflows.ai import apply_ai_suggestions_to_document

    try:
        action = WorkflowAction.objects.get(pk=action_id)
        document = Document.objects.select_related("owner").get(pk=document_id)
    except (WorkflowAction.DoesNotExist, Document.DoesNotExist):
        logger.warning(
            "Workflow action %s or document %s no longer exists, "
            "not applying AI suggestions",
            action_id,
            document_id,
        )
        return

    options = (
        {"expected_metadata": expected_metadata}
        if expected_metadata is not None
        else {}
    )
    if not apply_ai_suggestions_to_document(action, document, **options):
        return

    # No document_updated signal to avoid loop
    clear_document_caches(document.pk)
    index_document.delay(document.pk)

    ai_config = AIConfig()
    if ai_config.llm_index_enabled:
        update_document_in_llm_index.apply_async(kwargs={"document": document})


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=5)
def update_document_in_llm_index(self, document, *, personal_index_token=None) -> None:
    from documents.ocr_index import acknowledge_manual_index
    from documents.ocr_index import mark_pending
    from documents.ocr_index import run_queued_index

    if personal_index_token is not None:
        run_queued_index(
            document,
            personal_index_token,
            llm_index_add_or_update_document,
        )
    else:
        while True:
            root_id = document.root_document_id or document.pk
            with FileLock(settings.DATA_DIR / f"document-index-work-{root_id}.lock"):
                document = Document.objects.filter(pk=document.pk).first()
                if document is None:
                    return
                if (document.root_document_id or document.pk) != root_id:
                    continue
                expected = mark_pending(document)
                try:
                    if llm_index_add_or_update_document(document) is False:
                        raise RuntimeError("LLM index write was deferred")
                except Exception as error:
                    if expected is None and self.request.retries < 5:
                        raise self.retry(
                            exc=error,
                            countdown=min(60 * 2**self.request.retries, 900),
                        )
                    raise
                if expected is not None:
                    acknowledge_manual_index(document, expected)
                return


@shared_task
def remove_document_from_llm_index(document) -> None:
    llm_index_remove_document(document)


@shared_task
def build_share_link_bundle(bundle_id: int) -> None:
    try:
        bundle = (
            ShareLinkBundle.objects.filter(pk=bundle_id)
            .prefetch_related("documents")
            .get()
        )
    except ShareLinkBundle.DoesNotExist:
        logger.warning("Share link bundle %s no longer exists.", bundle_id)
        return

    bundle.remove_file()
    bundle.status = ShareLinkBundle.Status.PROCESSING
    bundle.last_error = None
    bundle.size_bytes = None
    bundle.built_at = None
    bundle.file_path = ""
    bundle.save(
        update_fields=[
            "status",
            "last_error",
            "size_bytes",
            "built_at",
            "file_path",
        ],
    )

    documents = list(bundle.documents.all().order_by("pk"))

    _, temp_zip_path_str = mkstemp(suffix=".zip", dir=settings.SCRATCH_DIR)
    temp_zip_path = Path(temp_zip_path_str)

    try:
        strategy_class = (
            ArchiveOnlyStrategy
            if bundle.file_version == ShareLink.FileVersion.ARCHIVE
            else OriginalsOnlyStrategy
        )
        with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            strategy = strategy_class(zipf)
            for document in documents:
                strategy.add_document(document)

        output_dir = settings.SHARE_LINK_BUNDLE_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        final_path = (output_dir / f"{bundle.slug}.zip").resolve()
        if final_path.exists():
            final_path.unlink()
        shutil.move(temp_zip_path, final_path)

        bundle.file_path = f"{bundle.slug}.zip"
        bundle.size_bytes = final_path.stat().st_size
        bundle.status = ShareLinkBundle.Status.READY
        bundle.built_at = timezone.now()
        bundle.last_error = None
        bundle.save(
            update_fields=[
                "file_path",
                "size_bytes",
                "status",
                "built_at",
                "last_error",
            ],
        )
        logger.info("Built share link bundle %s", bundle.pk)
    except Exception as exc:
        logger.exception(
            "Failed to build share link bundle %s: %s",
            bundle_id,
            exc,
        )
        bundle.status = ShareLinkBundle.Status.FAILED
        bundle.last_error = {
            "bundle_id": bundle_id,
            "exception_type": exc.__class__.__name__,
            "message": str(exc),
            "timestamp": timezone.now().isoformat(),
        }
        bundle.save(update_fields=["status", "last_error"])
        try:
            temp_zip_path.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            temp_zip_path.unlink(missing_ok=True)
        except OSError:
            pass


@shared_task
def cleanup_expired_share_link_bundles() -> None:
    now = timezone.now()
    expired_qs = ShareLinkBundle.objects.filter(
        expiration__isnull=False,
        expiration__lt=now,
    )
    count = 0
    for bundle in expired_qs.iterator():
        count += 1
        try:
            bundle.delete()
        except Exception as exc:
            logger.warning(
                "Failed to delete expired share link bundle %s: %s",
                bundle.pk,
                exc,
            )
    if count:
        logger.info("Deleted %s expired share link bundle(s)", count)
