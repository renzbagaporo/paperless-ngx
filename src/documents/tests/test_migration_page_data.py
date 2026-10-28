from paperless_testing.migrations import TestMigrations


class PageDataMigrationTests(TestMigrations):
    migrate_from = "0027_documentbarcode"
    migrate_to = "0028_document_page_data"

    def setUpBeforeMigration(self, apps):
        Document = apps.get_model("documents", "Document")
        root = Document.objects.create(
            title="Existing notice",
            content="Existing OCR",
            checksum="file-hash",
            mime_type="application/pdf",
            archive_serial_number=1000001,
            filename="existing.pdf",
            page_count=3,
        )
        Document.objects.create(
            root_document=root,
            version_index=1,
            title=root.title,
            content="Version OCR",
            checksum="version-hash",
            mime_type="application/pdf",
            filename="version.pdf",
            page_count=4,
        )
        self.expected = list(Document.objects.order_by("pk").values())

    def test_schema_upgrade_preserves_existing_rows_and_requires_no_ocr(self):
        Document = self.apps.get_model("documents", "Document")
        Page = self.apps.get_model("documents", "DocumentPage")
        self.assertEqual(list(Document.objects.order_by("pk").values()), self.expected)
        self.assertEqual(Page.objects.count(), 0)


class ReversePageDataMigrationTests(TestMigrations):
    migrate_from = "0028_document_page_data"
    migrate_to = "0027_documentbarcode"

    def setUpBeforeMigration(self, apps):
        Document = apps.get_model("documents", "Document")
        Page = apps.get_model("documents", "DocumentPage")
        document = Document.objects.create(
            title="Existing notice",
            content="Existing OCR",
            checksum="file-hash",
            mime_type="application/pdf",
            archive_serial_number=1000001,
        )
        Page.objects.create(
            document=document,
            page=1,
            source_page=1,
            file_checksum="file-hash",
            text="Existing OCR",
        )
        self.expected = list(Document.objects.values())

    def test_schema_rollback_preserves_documents(self):
        Document = self.apps.get_model("documents", "Document")
        self.assertEqual(list(Document.objects.values()), self.expected)
