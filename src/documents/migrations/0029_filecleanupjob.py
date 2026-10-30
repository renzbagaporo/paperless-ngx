import uuid

import django.utils.timezone
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("documents", "0028_document_page_data")]

    operations = [
        migrations.CreateModel(
            name="FileCleanupJob",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("root_document_id", models.PositiveIntegerField(null=True)),
                ("paths", models.JSONField(default=list)),
                ("version_ids", models.JSONField(default=list)),
                ("version_identities", models.JSONField(default=dict)),
                ("created", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "available_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
            ],
            options={"ordering": ("created", "id"), "default_permissions": ()},
        ),
    ]
