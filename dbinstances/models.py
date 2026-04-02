import re

from django.core.exceptions import ValidationError
from django.db import models


class Engine(models.TextChoices):
    MYSQL = "mysql", "MySQL"
    MARIADB = "mariadb", "MariaDB"


class InstanceStatus(models.TextChoices):
    STOPPED = "stopped", "Stopped"
    RUNNING = "running", "Running"
    MISSING = "missing", "Missing"
    ERROR = "error", "Error"


def _slugify_container_label(name: str, pk: int | None) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-").lower()
    base = base[:40] or "db"
    suffix = f"-{pk}" if pk else "-new"
    full = f"dbmgr-{base}{suffix}"
    return full[:240]


class ManagedDatabase(models.Model):
    name = models.CharField(max_length=200)
    engine = models.CharField(
        max_length=16,
        choices=Engine.choices,
        default=Engine.MYSQL,
    )
    image_tag = models.CharField(
        max_length=64,
        help_text="Docker image tag, e.g. 8.0, 8.4, 11, lts",
    )
    host_port = models.PositiveIntegerField(
        unique=True,
        help_text="Host TCP port published on 0.0.0.0 (LAN-accessible).",
    )
    mysql_root_password = models.CharField(max_length=256)
    mysql_database = models.CharField(max_length=64, blank=True)

    container_id = models.CharField(max_length=128, blank=True)
    container_name = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=16,
        choices=InstanceStatus.choices,
        default=InstanceStatus.STOPPED,
    )
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.engine}:{self.image_tag})"

    def clean(self) -> None:
        super().clean()
        if not (1 <= self.host_port <= 65535):
            raise ValidationError({"host_port": "Port must be between 1 and 65535."})

    @property
    def docker_image(self) -> str:
        return f"{self.engine}:{self.image_tag}"

    @property
    def volume_name(self) -> str:
        if self.pk is None:
            raise ValueError("Instance must be saved before volume_name is defined")
        return f"dbmanager_data_{self.pk}"

    def suggested_container_name(self) -> str:
        return _slugify_container_label(self.name, self.pk)
