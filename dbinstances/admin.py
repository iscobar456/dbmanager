from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet

from . import docker_ops
from .models import (
    DatabaseEngine,
    LogicalDatabase,
    ManagedDatabaseUser,
    InstanceStatus,
    UserKind,
)
from .sql_provision import provision_application_users, wait_for_mysql


def _docker_field_names() -> list[str]:
    return [
        "container_id",
        "container_name",
        "status",
        "last_error",
        "user_provision_error",
        "updated_at",
    ]


class LogicalDatabaseInline(admin.TabularInline):
    model = LogicalDatabase
    extra = 0
    fields = ("schema_name", "label")


class ManagedDatabaseUserInlineFormSet(BaseInlineFormSet):
    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        root_count = 0
        engine = self.instance
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            if form.cleaned_data.get("kind") == UserKind.ROOT:
                root_count += 1
                gds = form.cleaned_data.get("granted_databases")
                if gds and engine.pk:
                    raise ValidationError(
                        "Root users cannot have granted databases."
                    )
            if form.cleaned_data.get("kind") == UserKind.APPLICATION and engine.pk:
                gds = form.cleaned_data.get("granted_databases")
                if gds:
                    for ld in gds:
                        if ld.engine_id != engine.pk:
                            raise ValidationError(
                                "Granted databases must belong to this engine."
                            )
        if root_count > 1:
            raise ValidationError(
                "Only one Root user is allowed per database engine."
            )


class ManagedDatabaseUserInline(admin.StackedInline):
    model = ManagedDatabaseUser
    extra = 0
    formset = ManagedDatabaseUserInlineFormSet
    filter_horizontal = ("granted_databases",)
    fields = ("kind", "username", "password", "host", "granted_databases")

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "granted_databases":
            obj = kwargs.pop("obj", None)
            if obj is not None:
                kwargs["queryset"] = LogicalDatabase.objects.filter(engine=obj)
            else:
                kwargs["queryset"] = LogicalDatabase.objects.none()
        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(DatabaseEngine)
class DatabaseEngineAdmin(admin.ModelAdmin):
    inlines = (LogicalDatabaseInline, ManagedDatabaseUserInline)
    list_display = (
        "name",
        "vendor",
        "image_tag",
        "host_port",
        "status",
        "container_name",
        "updated_at",
    )
    list_filter = ("vendor", "status")
    search_fields = ("name", "container_name", "container_id")
    readonly_fields = (
        "container_id",
        "container_name",
        "status",
        "last_error",
        "user_provision_error",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (None, {"fields": ("name", "vendor", "image_tag")}),
        ("Network", {"fields": ("host_port",)}),
        (
            "Docker",
            {
                "fields": (
                    "container_id",
                    "container_name",
                    "status",
                    "last_error",
                    "user_provision_error",
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
        "action_sync_users_to_database",
        "action_recreate_container",
        "action_remove_container",
        "action_remove_container_and_volume",
    ]

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.pk and obj.container_id:
            ro.extend(["vendor", "image_tag"])
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
        self.message_user(request, f"Synced {queryset.count()} engine(s).")

    @admin.action(description="Sync application users into the database (SQL)")
    def action_sync_users_to_database(self, request, queryset):
        for obj in queryset:
            if obj.status != InstanceStatus.RUNNING:
                self.message_user(
                    request,
                    f"{obj}: engine is not running; skipped.",
                    level=messages.WARNING,
                )
                continue
            root = obj.db_users.filter(kind=UserKind.ROOT).first()
            if root is None:
                root = obj.ensure_root_db_user()
            try:
                wait_for_mysql(obj.host_port, password=root.password, timeout_sec=45.0)
                provision_application_users(obj)
            except Exception as exc:
                obj.user_provision_error = str(exc)[:2000]
                self.message_user(
                    request,
                    f"{obj}: user sync failed: {exc}",
                    level=messages.ERROR,
                )
            else:
                obj.user_provision_error = ""
                self.message_user(request, f"User sync OK: {obj}.")
            obj.save(
                update_fields=["user_provision_error", "updated_at"],
            )

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


@admin.register(LogicalDatabase)
class LogicalDatabaseAdmin(admin.ModelAdmin):
    list_display = ("schema_name", "label", "engine")
    list_filter = ("engine",)
    search_fields = ("schema_name", "label", "engine__name")
    raw_id_fields = ("engine",)


@admin.register(ManagedDatabaseUser)
class ManagedDatabaseUserAdmin(admin.ModelAdmin):
    list_display = ("username", "host", "kind", "engine")
    list_filter = ("kind",)
    filter_horizontal = ("granted_databases",)
    raw_id_fields = ("engine",)
    search_fields = ("username", "engine__name")

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "granted_databases":
            obj_id = request.resolver_match.kwargs.get("object_id")
            if obj_id:
                try:
                    u = ManagedDatabaseUser.objects.get(pk=obj_id)
                    kwargs["queryset"] = LogicalDatabase.objects.filter(
                        engine=u.engine_id
                    )
                except ManagedDatabaseUser.DoesNotExist:
                    kwargs["queryset"] = LogicalDatabase.objects.none()
        return super().formfield_for_manytomany(db_field, request, **kwargs)
