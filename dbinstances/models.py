import re
import secrets

from django.core.exceptions import ValidationError
from django.db import models


class DatabaseVendor(models.TextChoices):
    MYSQL = "mysql", "MySQL"
    MARIADB = "mariadb", "MariaDB"


class InstanceStatus(models.TextChoices):
    STOPPED = "stopped", "Stopped"
    RUNNING = "running", "Running"
    MISSING = "missing", "Missing"
    ERROR = "error", "Error"


class UserKind(models.TextChoices):
    ROOT = "root", "Root"
    APPLICATION = "application", "Application"


def _slugify_container_label(name: str, pk: int | None) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip("-").lower()
    base = base[:40] or "db"
    suffix = f"-{pk}" if pk else "-new"
    full = f"dbmgr-{base}{suffix}"
    return full[:240]


class DatabaseEngine(models.Model):
    """Docker-backed MySQL/MariaDB server (published port, one data volume)."""

    name = models.CharField(max_length=200)
    vendor = models.CharField(
        max_length=16,
        choices=DatabaseVendor.choices,
        default=DatabaseVendor.MYSQL,
    )
    image_tag = models.CharField(
        max_length=64,
        help_text="Docker image tag, e.g. 8.0, 8.4, 11, lts",
    )
    host_port = models.PositiveIntegerField(
        unique=True,
        help_text=(
            "Host TCP port published on 0.0.0.0 (LAN-accessible). "
            "If a container already exists, change this only together with the "
            "changelist action “Recreate container (keep data volume)” so Docker "
            "republishes MySQL on the new port."
        ),
    )

    container_id = models.CharField(max_length=128, blank=True)
    container_name = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=16,
        choices=InstanceStatus.choices,
        default=InstanceStatus.STOPPED,
    )
    last_error = models.TextField(blank=True)
    user_provision_error = models.TextField(
        blank=True,
        help_text="Last error from SQL sync (logical databases, application users, grants).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.vendor}:{self.image_tag})"

    def clean(self) -> None:
        super().clean()
        if not (1 <= self.host_port <= 65535):
            raise ValidationError({"host_port": "Port must be between 1 and 65535."})

    @property
    def docker_image(self) -> str:
        return f"{self.vendor}:{self.image_tag}"

    @property
    def volume_name(self) -> str:
        if self.pk is None:
            raise ValueError("Instance must be saved before volume_name is defined")
        return f"dbmanager_data_{self.pk}"

    def suggested_container_name(self) -> str:
        return _slugify_container_label(self.name, self.pk)

    def ensure_root_db_user(self) -> "ManagedDatabaseUser":
        if self.pk is None:
            raise ValueError("Instance must be saved before ensure_root_db_user")
        user, _created_q = ManagedDatabaseUser.objects.get_or_create(
            engine=self,
            kind=UserKind.ROOT,
            defaults={
                "username": "root",
                "password": secrets.token_urlsafe(32),
                "host": "%",
            },
        )
        return user

    def root_password_for_docker(self) -> str:
        return self.ensure_root_db_user().password


class LogicalDatabase(models.Model):
    """A schema within one engine (real MySQL database name)."""

    engine = models.ForeignKey(
        DatabaseEngine,
        on_delete=models.CASCADE,
        related_name="logical_databases",
    )
    schema_name = models.CharField(
        max_length=64,
        help_text="MySQL database name (CREATE DATABASE).",
    )
    label = models.CharField(
        max_length=200,
        blank=True,
        help_text="Optional display label in admin.",
    )

    class Meta:
        ordering = ["schema_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["engine", "schema_name"],
                name="dbinstances_logicaldb_unique_per_engine",
            ),
        ]

    def __str__(self) -> str:
        if self.label:
            return f"{self.label} ({self.schema_name})"
        return self.schema_name

    def clean(self) -> None:
        super().clean()
        if not re.match(r"^[a-zA-Z0-9_]+$", self.schema_name):
            raise ValidationError(
                {"schema_name": "Only letters, digits, and underscore allowed."}
            )


class ManagedDatabaseUser(models.Model):
    engine = models.ForeignKey(
        DatabaseEngine,
        on_delete=models.CASCADE,
        related_name="db_users",
    )
    kind = models.CharField(max_length=32, choices=UserKind.choices)
    username = models.CharField(max_length=64)
    password = models.CharField(max_length=256)
    host = models.CharField(
        max_length=255,
        default="%",
        help_text="MySQL account host, e.g. % for any remote host.",
    )
    granted_databases = models.ManyToManyField(
        LogicalDatabase,
        blank=True,
        related_name="users_granted",
        help_text="For application users: schemas this account may access "
        "(ALL PRIVILEGES on each schema.*). Empty means *.* (dev only).",
    )

    class Meta:
        ordering = ["kind", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["engine"],
                condition=models.Q(kind=UserKind.ROOT),
                name="dbinstances_engine_single_root",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.username}@{self.host} ({self.get_kind_display()})"

    def clean(self) -> None:
        super().clean()
        if self.kind == UserKind.ROOT:
            if self.username != "root":
                raise ValidationError(
                    {"username": 'Root kind must use username "root".'}
                )
            if self.pk and self.granted_databases.exists():
                raise ValidationError(
                    {"granted_databases": "Root user does not use granted databases."}
                )
        if self.kind == UserKind.APPLICATION:
            if not re.match(r"^[a-zA-Z0-9_]+$", self.username):
                raise ValidationError(
                    {
                        "username": "Use only letters, digits, and underscore for "
                        "application usernames."
                    }
                )
        eid = self.engine_id
        if eid and self.pk:
            for ld in self.granted_databases.all():
                if ld.engine_id != eid:
                    raise ValidationError(
                        {
                            "granted_databases": "Each granted database must belong to "
                            "the same engine as this user."
                        }
                    )
