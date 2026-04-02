from django.contrib import admin, messages

from . import docker_ops
from .models import InstanceStatus, ManagedDatabase


def _docker_field_names() -> list[str]:
    return [
        "container_id",
        "container_name",
        "status",
        "last_error",
        "updated_at",
    ]


@admin.register(ManagedDatabase)
class ManagedDatabaseAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "engine",
        "image_tag",
        "host_port",
        "status",
        "container_name",
        "updated_at",
    )
    list_filter = ("engine", "status")
    search_fields = ("name", "container_name", "container_id")
    readonly_fields = (
        "container_id",
        "container_name",
        "status",
        "last_error",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (None, {"fields": ("name", "engine", "image_tag")}),
        ("Network", {"fields": ("host_port",)}),
        ("MySQL", {"fields": ("mysql_root_password", "mysql_database")}),
        (
            "Docker",
            {
                "fields": (
                    "container_id",
                    "container_name",
                    "status",
                    "last_error",
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    actions = [
        "action_create_and_start",
        "action_start",
        "action_stop",
        "action_sync_status",
        "action_recreate_container",
        "action_remove_container",
        "action_remove_container_and_volume",
    ]

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.pk and obj.container_id:
            ro.extend(["engine", "image_tag"])
        return ro

    @admin.action(description="Create container and start (pull image if needed)")
    def action_create_and_start(self, request, queryset):
        for obj in queryset:
            try:
                docker_ops.create_and_start(obj)
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: {exc}",
                    level=messages.ERROR,
                )
            finally:
                obj.save(update_fields=_docker_field_names())

    @admin.action(description="Start existing container")
    def action_start(self, request, queryset):
        for obj in queryset:
            try:
                docker_ops.start_container(obj)
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: {exc}",
                    level=messages.ERROR,
                )
            finally:
                obj.save(update_fields=_docker_field_names())

    @admin.action(description="Stop container")
    def action_stop(self, request, queryset):
        for obj in queryset:
            try:
                docker_ops.stop_container(obj)
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: {exc}",
                    level=messages.ERROR,
                )
            finally:
                obj.save(update_fields=_docker_field_names())

    @admin.action(description="Sync status from Docker")
    def action_sync_status(self, request, queryset):
        for obj in queryset:
            docker_ops.sync_status(obj)
            obj.save(update_fields=_docker_field_names())
        self.message_user(request, f"Synced {queryset.count()} instance(s).")

    @admin.action(
        description="Recreate container (keep data volume; use after port or image change)"
    )
    def action_recreate_container(self, request, queryset):
        for obj in queryset:
            docker_ops.recreate_container(obj)
            if obj.status == InstanceStatus.RUNNING:
                self.message_user(request, f"Recreated and started: {obj}.")
            else:
                self.message_user(
                    request,
                    f"{obj}: {obj.last_error or obj.get_status_display()}",
                    level=messages.ERROR,
                )

    @admin.action(description="Remove container only (keeps named volume and data)")
    def action_remove_container(self, request, queryset):
        for obj in queryset:
            try:
                docker_ops.remove_container(obj, remove_volume=False)
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: {exc}",
                    level=messages.ERROR,
                )
            finally:
                obj.save(update_fields=_docker_field_names())
        self.message_user(request, "Remove finished (volumes preserved).")

    @admin.action(description="Remove container and delete its Docker volume (destructive)")
    def action_remove_container_and_volume(self, request, queryset):
        for obj in queryset:
            try:
                docker_ops.remove_container(obj, remove_volume=True)
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: {exc}",
                    level=messages.ERROR,
                )
            finally:
                obj.save(update_fields=_docker_field_names())
        self.message_user(
            request,
            "Destructive remove completed where possible; check errors above.",
            level=messages.WARNING,
        )
