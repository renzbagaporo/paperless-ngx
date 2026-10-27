from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("paperless", "0018_applicationconfiguration_barcode_store_values"),
    ]

    operations = [
        migrations.AlterField(
            model_name="applicationconfiguration",
            name="remote_ocr_engine",
            field=models.CharField(
                blank=True,
                choices=[
                    ("azureai", "Azure AI Document Intelligence"),
                    ("llamaparse", "LlamaParse"),
                ],
                max_length=32,
                null=True,
                verbose_name="Sets the remote OCR engine",
            ),
        ),
    ]
