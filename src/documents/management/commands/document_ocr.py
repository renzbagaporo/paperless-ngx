"""Inspect pending OCR or explicitly retry a bounded amount of saved work."""

import json

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from documents.models import Document
from documents.models import Tag
from documents.ocr_queue import pending_pages
from documents.ocr_queue import retry_pending_ocr
from documents.ocr_status import STATUS_NAMES
from documents.versioning import get_latest_version_for_root


class Command(BaseCommand):
    help = "List pending OCR; --apply --max-pages explicitly retries saved pages."

    def add_arguments(self, parser):
        parser.add_argument("--document", type=int, action="append")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--max-pages", type=int)
        parser.add_argument("--legacy-helper-owner", type=int)

    def handle(self, *args, **options):
        if options["legacy_helper_owner"] is not None:
            owner_id = options["legacy_helper_owner"]
            if owner_id < 1 or options["apply"]:
                raise CommandError(
                    "The legacy helper tag report is read-only and requires a positive owner ID.",
                )
            candidates = {}
            for tag in Tag.objects.filter(owner_id=owner_id).order_by("pk"):
                name = tag.name.casefold()
                if name in STATUS_NAMES:
                    if name in candidates:
                        raise CommandError(
                            f"Multiple legacy candidates for {name}; select the intended ID explicitly.",
                        )
                    candidates[name] = tag.pk
            self.stdout.write(json.dumps(candidates, sort_keys=True))
            return
        limit = options["max_pages"]
        if options["apply"] and (limit is None or limit < 1):
            raise CommandError("Paid OCR retry requires a positive --max-pages.")
        roots = Document.objects.filter(root_document__isnull=True).order_by("pk")
        if options["document"]:
            roots = roots.filter(pk__in=options["document"])
        completed = 0
        for root in roots:
            current = get_latest_version_for_root(root)
            count = len(pending_pages(current))
            if not count:
                continue
            self.stdout.write(f"Document {root.pk}: {count} pending OCR pages")
            if options["apply"]:
                count = retry_pending_ocr(root.pk, max_pages=limit - completed)
                completed += count
                if not count or completed >= limit:
                    break
        self.stdout.write(f"Completed: {completed}; apply: {options['apply']}")
