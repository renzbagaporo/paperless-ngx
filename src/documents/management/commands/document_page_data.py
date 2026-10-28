"""Preview/backfill page data, or explicitly enrich a bounded number of pages."""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db.models import Q

from documents.models import Document
from documents.page_data import backfill_document
from documents.page_enrichment import enrich_document
from documents.page_enrichment import missing_rich_pages
from documents.versioning import get_latest_version_for_root


class Command(BaseCommand):
    help = "Recover page text locally or enrich missing rich parsing with LlamaParse. Defaults to preview."

    def add_arguments(self, parser):
        parser.add_argument("operation", choices=["backfill", "enrich"])
        parser.add_argument("--document", type=int, action="append")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--max-pages", type=int)
        parser.add_argument("--include-history", action="store_true")

    def handle(self, *args, **options):
        operation = options["operation"]
        limit = options["max_pages"]
        if operation == "enrich" and options["apply"] and (limit is None or limit < 1):
            raise CommandError(
                "Paid enrichment requires --max-pages with a positive limit.",
            )
        documents = (
            Document.global_objects if operation == "backfill" else Document.objects
        ).order_by("pk")
        if options["document"]:
            documents = documents.filter(pk__in=options["document"])
        if (
            operation == "enrich"
            and not options["include_history"]
            and not options["document"]
        ):
            documents = documents.filter(root_document__isnull=True)
        documents = documents.filter(
            Q(mime_type="application/pdf") | Q(archive_filename__isnull=False),
        )
        identifiers = [
            (
                get_latest_version_for_root(document).pk
                if operation == "enrich"
                and not options["include_history"]
                and document.root_document_id is None
                else document.pk
            )
            for document in documents
        ]
        failures = 0
        completed = 0
        for document_id in identifiers:
            document = Document.objects.get(pk=document_id)
            if operation == "enrich":
                count = missing_rich_pages(document)
                self.stdout.write(
                    f"Document {document_id}: {count} pages missing rich parsing",
                )
            else:
                self.stdout.write(f"Document {document_id}: local page-text backfill")
            if not options["apply"]:
                continue
            try:
                if operation == "backfill":
                    completed += int(backfill_document(document_id))
                elif completed < limit:
                    completed += enrich_document(
                        document_id,
                        max_pages=limit - completed,
                    )
                else:
                    break
            except Exception as error:
                # Backfill skips damaged files; paid work stops on every error,
                # including provider quota/auth errors. No automatic paid retry.
                if operation == "enrich":
                    raise CommandError(
                        f"Enrichment stopped at document {document_id}: {error}",
                    ) from error
                self.stderr.write(f"Document {document_id}: {error}")
                failures += 1
        self.stdout.write(
            f"Completed: {completed}; failed: {failures}; apply: {options['apply']}",
        )
        if failures:
            raise CommandError(
                "Some documents could not be backfilled; successful documents are retained.",
            )
