import json
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from docker.errors import DockerException

from . import docker_ops, sql_chunk_upload
from .job_queue import DockerJobConflict, enqueue_docker_admin_job
from .models import (
    DatabaseEngine,
    DockerAdminJob,
    DockerJobKind,
    DockerJobStatus,
    InstanceStatus,
    LogicalDatabase,
    ManagedDatabaseUser,
    UserKind,
)


def _sql_import_staging_suffix(filename: str) -> str | None:
    n = (filename or "").strip().lower()
    if n.endswith(".sql.gz"):
        return ".sql.gz"
    if n.endswith(".zip"):
        return ".zip"
    if n.endswith(".sql"):
        return ".sql"
    return None


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

    def get_formset(self, request, obj=None, **kwargs):
        """Inline formfield callbacks never receive parent ``obj``; scope M2M here."""
        parent_engine = obj

        def formfield_callback(db_field, **cb_kwargs):
            if db_field.name == "granted_databases":
                cb_kwargs = dict(cb_kwargs)
                if parent_engine is not None:
                    cb_kwargs["queryset"] = LogicalDatabase.objects.filter(
                        engine=parent_engine
                    )
                else:
                    cb_kwargs["queryset"] = LogicalDatabase.objects.none()
            return self.formfield_for_dbfield(db_field, request, **cb_kwargs)

        return super().get_formset(
            request,
            obj,
            formfield_callback=formfield_callback,
            **kwargs,
        )


@admin.register(DatabaseEngine)
class DatabaseEngineAdmin(admin.ModelAdmin):
    change_form_template = "admin/dbinstances/databaseengine/change_form.html"
    change_list_template = "admin/dbinstances/databaseengine/change_list.html"
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
        "action_sync_databases_and_users",
        "action_recreate_container",
        "action_remove_container",
        "action_remove_container_and_volume",
    ]

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.pk and obj.container_id:
            ro.extend(["vendor", "image_tag"])
        return ro

    def save_model(self, request, obj, form, change):
        prev_port = None
        if change and obj.pk:
            prev_port = (
                DatabaseEngine.objects.filter(pk=obj.pk)
                .values_list("host_port", flat=True)
                .first()
            )
        super().save_model(request, obj, form, change)
        if (
            change
            and obj.container_id
            and prev_port is not None
            and prev_port != obj.host_port
        ):
            self.message_user(
                request,
                "Host port was updated in Django, but the running container still "
                "uses the old published port until you recreate it. Use the changelist "
                'action “Recreate container (keep data volume)”, then sync databases '
                "and users if needed.",
                level=messages.WARNING,
            )

    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.model_name
        return [
            path(
                "<int:object_id>/docker/start/",
                self.admin_site.admin_view(self.docker_start_view),
                name="%s_%s_docker_start" % info,
            ),
            path(
                "<int:object_id>/docker/stop/",
                self.admin_site.admin_view(self.docker_stop_view),
                name="%s_%s_docker_stop" % info,
            ),
            path(
                "<int:object_id>/docker/create-and-start/",
                self.admin_site.admin_view(self.docker_create_and_start_view),
                name="%s_%s_docker_create_and_start" % info,
            ),
            path(
                "<int:object_id>/docker/logs/",
                self.admin_site.admin_view(self.docker_logs_view),
                name="%s_%s_docker_logs" % info,
            ),
            path(
                "<int:object_id>/user-provision-error/",
                self.admin_site.admin_view(self.user_provision_error_view),
                name="%s_%s_user_provision_error" % info,
            ),
            path(
                "<int:object_id>/docker/sync-status/",
                self.admin_site.admin_view(self.docker_sync_status_view),
                name="%s_%s_docker_sync_status" % info,
            ),
            path(
                "<int:object_id>/docker/sync-databases-and-users/",
                self.admin_site.admin_view(
                    self.docker_sync_databases_and_users_view
                ),
                name="%s_%s_docker_sync_databases_and_users" % info,
            ),
            path(
                "<int:object_id>/docker/job/<uuid:job_id>/",
                self.admin_site.admin_view(self.docker_job_progress_view),
                name="%s_%s_docker_job_progress" % info,
            ),
            path(
                "<int:object_id>/docker/job/<uuid:job_id>/status/",
                self.admin_site.admin_view(self.docker_job_status_view),
                name="%s_%s_docker_job_status" % info,
            ),
        ] + super().get_urls()

    def _change_view_url(self, obj):
        return reverse(
            "admin:%s_%s_change"
            % (self.model._meta.app_label, self.model._meta.model_name),
            args=[obj.pk],
        )

    def _docker_job_progress_url(self, obj, job: DockerAdminJob) -> str:
        return reverse(
            "admin:dbinstances_databaseengine_docker_job_progress",
            args=[obj.pk, job.pk],
        )

    def _docker_job_status_url(self, obj, job: DockerAdminJob) -> str:
        return reverse(
            "admin:dbinstances_databaseengine_docker_job_status",
            args=[obj.pk, job.pk],
        )

    def _save_engine_docker_fields(self, obj):
        obj.save(update_fields=_docker_field_names())

    def _docker_start_one(self, request, obj):
        try:
            docker_ops.start_container(obj)
        except Exception as exc:
            self.message_user(
                request,
                f"{obj}: {exc}",
                level=messages.ERROR,
            )
        finally:
            self._save_engine_docker_fields(obj)

    def _docker_stop_one(self, request, obj):
        try:
            docker_ops.stop_container(obj)
        except Exception as exc:
            self.message_user(
                request,
                f"{obj}: {exc}",
                level=messages.ERROR,
            )
        finally:
            self._save_engine_docker_fields(obj)

    def docker_start_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        self._docker_start_one(request, obj)
        return redirect(self._change_view_url(obj))

    def docker_stop_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        self._docker_stop_one(request, obj)
        return redirect(self._change_view_url(obj))

    def docker_create_and_start_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        if obj.container_id:
            self.message_user(
                request,
                f"{obj}: A container is already recorded. Use Start, or clear the "
                "container from Docker first.",
                level=messages.WARNING,
            )
            return redirect(self._change_view_url(obj))
        try:
            job = enqueue_docker_admin_job(obj.pk, DockerJobKind.CREATE_AND_START)
        except DockerJobConflict:
            self.message_user(
                request,
                f"{obj}: Another job is already queued or running for this engine.",
                level=messages.WARNING,
            )
            return redirect(self._change_view_url(obj))
        except Exception as exc:
            self.message_user(
                request,
                f"{obj}: Could not queue create/start job (Redis/Celery?): {exc}",
                level=messages.ERROR,
            )
            return redirect(self._change_view_url(obj))
        self.message_user(
            request,
            "Create container and start job queued (pull, volume, container, provision).",
            level=messages.INFO,
        )
        return redirect(self._docker_job_progress_url(obj, job))

    def _sync_docker_status_fields(self, obj):
        docker_ops.sync_status(obj)
        self._save_engine_docker_fields(obj)

    def docker_sync_status_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        self._sync_docker_status_fields(obj)
        self.message_user(
            request,
            f"Synced Docker status for {obj.name!r}.",
        )
        return redirect(self._change_view_url(obj))

    def docker_sync_databases_and_users_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        try:
            job = enqueue_docker_admin_job(
                obj.pk, DockerJobKind.SYNC_DATABASES_AND_USERS
            )
        except DockerJobConflict:
            self.message_user(
                request,
                f"{obj}: Another Docker or SQL job is already queued or running "
                "for this engine. Wait for it to finish, then try again.",
                level=messages.WARNING,
            )
            return redirect(self._change_view_url(obj))
        except Exception as exc:
            self.message_user(
                request,
                f"{obj}: Could not queue sync job (is Redis running and Celery "
                f"worker started?): {exc}",
                level=messages.ERROR,
            )
            return redirect(self._change_view_url(obj))
        self.message_user(
            request,
            "Sync job queued. Progress updates below.",
            level=messages.INFO,
        )
        return redirect(self._docker_job_progress_url(obj, job))

    def docker_job_progress_view(self, request, object_id, job_id):
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        job = get_object_or_404(
            DockerAdminJob.objects.select_related("logical_database"),
            pk=job_id,
        )
        if job.engine_id != obj.pk:
            raise PermissionDenied

        context = {
            **self.admin_site.each_context(request),
            "title": f"Docker job — {obj.name}",
            "opts": self.model._meta,
            "engine": obj,
            "job": job,
            "status_poll_url": self._docker_job_status_url(obj, job),
            "change_url": self._change_view_url(obj),
        }
        return TemplateResponse(
            request,
            "admin/dbinstances/databaseengine/docker_job_progress.html",
            context,
        )

    def docker_job_status_view(self, request, object_id, job_id):
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        job = get_object_or_404(
            DockerAdminJob.objects.select_related("logical_database"),
            pk=job_id,
        )
        if job.engine_id != obj.pk:
            raise PermissionDenied

        job.refresh_from_db()
        done = job.status in (
            DockerJobStatus.SUCCESS,
            DockerJobStatus.FAILURE,
        )
        return JsonResponse(
            {
                "status": job.status,
                "step": job.step,
                "message": job.message,
                "error": job.error,
                "finished": done,
                "success": job.status == DockerJobStatus.SUCCESS,
            }
        )

    def docker_logs_view(self, request, object_id):
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied

        raw_tail = request.GET.get("tail", str(docker_ops.DOCKER_LOGS_TAIL_DEFAULT))
        try:
            tail_n = int(raw_tail)
        except ValueError:
            tail_n = docker_ops.DOCKER_LOGS_TAIL_DEFAULT
        tail_n = max(
            docker_ops.DOCKER_LOGS_TAIL_MIN,
            min(tail_n, docker_ops.DOCKER_LOGS_TAIL_MAX),
        )

        log_text = ""
        error = None
        if not obj.container_id:
            error = "No container id yet. Create and start a container first."
        else:
            try:
                log_text = docker_ops.fetch_container_logs(obj, tail=tail_n)
            except ValueError as exc:
                error = str(exc)
            except DockerException as exc:
                error = str(exc)

        context = {
            **self.admin_site.each_context(request),
            "title": f"Container logs — {obj.name}",
            "opts": self.model._meta,
            "engine": obj,
            "log_text": log_text,
            "error": error,
            "tail": tail_n,
            "tail_presets": (500, 2000, 10000, 50000),
            "tail_default": docker_ops.DOCKER_LOGS_TAIL_DEFAULT,
        }
        return TemplateResponse(
            request,
            "admin/dbinstances/databaseengine/logs.html",
            context,
        )

    def user_provision_error_view(self, request, object_id):
        if request.method not in ("GET", "HEAD"):
            return HttpResponseNotAllowed(["GET", "HEAD"])
        obj = get_object_or_404(DatabaseEngine, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied

        context = {
            **self.admin_site.each_context(request),
            "title": f"User provision error — {obj.name}",
            "opts": self.model._meta,
            "engine": obj,
            "provision_error_text": (obj.user_provision_error or "").strip(),
        }
        return TemplateResponse(
            request,
            "admin/dbinstances/databaseengine/user_provision_error.html",
            context,
        )

    @admin.action(description="Create container and start (pull image if needed)")
    def action_create_and_start(self, request, queryset):
        queued = []
        for obj in queryset:
            try:
                job = enqueue_docker_admin_job(
                    obj.pk, DockerJobKind.CREATE_AND_START
                )
                queued.append((obj, job))
            except DockerJobConflict:
                self.message_user(
                    request,
                    f"{obj}: Another job is already queued or running for this engine.",
                    level=messages.WARNING,
                )
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: Could not queue job (Redis/Celery?): {exc}",
                    level=messages.ERROR,
                )
        if len(queued) == 1:
            obj, job = queued[0]
            self.message_user(request, "Create/start job queued.", level=messages.INFO)
            return redirect(self._docker_job_progress_url(obj, job))
        for obj, job in queued:
            self.message_user(
                request,
                f"{obj}: Queued create/start (job {job.pk}).",
                level=messages.SUCCESS,
            )

    @admin.action(description="Start existing container")
    def action_start(self, request, queryset):
        for obj in queryset:
            self._docker_start_one(request, obj)

    @admin.action(description="Stop container")
    def action_stop(self, request, queryset):
        for obj in queryset:
            self._docker_stop_one(request, obj)

    @admin.action(description="Sync status from Docker")
    def action_sync_status(self, request, queryset):
        for obj in queryset:
            self._sync_docker_status_fields(obj)
        self.message_user(request, f"Synced {queryset.count()} engine(s).")

    @admin.action(
        description="Sync logical databases and application users to the server (SQL)"
    )
    def action_sync_databases_and_users(self, request, queryset):
        queued = []
        for obj in queryset:
            try:
                job = enqueue_docker_admin_job(
                    obj.pk, DockerJobKind.SYNC_DATABASES_AND_USERS
                )
                queued.append((obj, job))
            except DockerJobConflict:
                self.message_user(
                    request,
                    f"{obj}: Another job is already queued or running for this engine.",
                    level=messages.WARNING,
                )
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: Could not queue job (Redis/Celery?): {exc}",
                    level=messages.ERROR,
                )
        if len(queued) == 1:
            obj, job = queued[0]
            self.message_user(request, "Sync job queued.", level=messages.INFO)
            return redirect(self._docker_job_progress_url(obj, job))
        for obj, job in queued:
            self.message_user(
                request,
                f"{obj}: Queued SQL sync (job {job.pk}).",
                level=messages.SUCCESS,
            )

    @admin.action(
        description="Recreate container (keep data volume; use after port or image change)"
    )
    def action_recreate_container(self, request, queryset):
        queued = []
        for obj in queryset:
            try:
                job = enqueue_docker_admin_job(
                    obj.pk, DockerJobKind.RECREATE_CONTAINER
                )
                queued.append((obj, job))
            except DockerJobConflict:
                self.message_user(
                    request,
                    f"{obj}: Another job is already queued or running for this engine.",
                    level=messages.WARNING,
                )
            except Exception as exc:
                self.message_user(
                    request,
                    f"{obj}: Could not queue job (Redis/Celery?): {exc}",
                    level=messages.ERROR,
                )
        if len(queued) == 1:
            obj, job = queued[0]
            self.message_user(request, "Recreate job queued.", level=messages.INFO)
            return redirect(self._docker_job_progress_url(obj, job))
        for obj, job in queued:
            self.message_user(
                request,
                f"{obj}: Queued recreate (job {job.pk}).",
                level=messages.SUCCESS,
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
                self._save_engine_docker_fields(obj)
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
                self._save_engine_docker_fields(obj)
        self.message_user(
            request,
            "Destructive remove completed where possible; check errors above.",
            level=messages.WARNING,
        )


@admin.register(LogicalDatabase)
class LogicalDatabaseAdmin(admin.ModelAdmin):
    change_form_template = "admin/dbinstances/logicaldatabase/change_form.html"
    list_display = ("schema_name", "label", "engine")
    list_filter = ("engine",)
    search_fields = ("schema_name", "label", "engine__name")
    raw_id_fields = ("engine",)

    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.model_name
        return [
            path(
                "<path:object_id>/import-sql/",
                self.admin_site.admin_view(self.import_sql_view),
                name="%s_%s_import_sql" % info,
            ),
            path(
                "<path:object_id>/import-sql/chunk/init/",
                self.admin_site.admin_view(self.import_sql_chunk_init),
                name="%s_%s_import_sql_chunk_init" % info,
            ),
            path(
                "<path:object_id>/import-sql/chunk/upload/",
                self.admin_site.admin_view(self.import_sql_chunk_upload),
                name="%s_%s_import_sql_chunk_upload" % info,
            ),
            path(
                "<path:object_id>/import-sql/chunk/complete/",
                self.admin_site.admin_view(self.import_sql_chunk_complete),
                name="%s_%s_import_sql_chunk_complete" % info,
            ),
        ] + super().get_urls()

    def import_sql_view(self, request, object_id):
        obj = get_object_or_404(LogicalDatabase, pk=object_id)
        if not self.has_change_permission(request, obj):
            raise PermissionDenied

        engine = obj.engine
        changelist_url = reverse("admin:dbinstances_logicaldatabase_changelist")
        change_url = reverse("admin:dbinstances_logicaldatabase_change", args=[obj.pk])

        if request.method == "GET":
            context = {
                **self.admin_site.each_context(request),
                "title": f"Import SQL — {obj.schema_name}",
                "opts": self.model._meta,
                "logical_db": obj,
                "engine": engine,
                "max_bytes": settings.SQL_IMPORT_MAX_UPLOAD_BYTES,
                "max_mb": settings.SQL_IMPORT_MAX_UPLOAD_BYTES // (1024 * 1024),
                "zip_max_uncompressed": getattr(
                    settings,
                    "SQL_IMPORT_ZIP_MAX_UNCOMPRESSED_BYTES",
                    2 * settings.SQL_IMPORT_MAX_UPLOAD_BYTES,
                ),
                "change_url": change_url,
                "changelist_url": changelist_url,
                "chunk_init_url": reverse(
                    "admin:dbinstances_logicaldatabase_import_sql_chunk_init",
                    args=[obj.pk],
                ),
                "chunk_upload_url": reverse(
                    "admin:dbinstances_logicaldatabase_import_sql_chunk_upload",
                    args=[obj.pk],
                ),
                "chunk_complete_url": reverse(
                    "admin:dbinstances_logicaldatabase_import_sql_chunk_complete",
                    args=[obj.pk],
                ),
                "chunk_size_bytes": settings.SQL_IMPORT_CHUNK_SIZE_BYTES,
                "chunk_threshold_bytes": settings.SQL_IMPORT_CHUNK_THRESHOLD_BYTES,
                "chunk_threshold_mb": settings.SQL_IMPORT_CHUNK_THRESHOLD_BYTES
                // (1024 * 1024),
            }
            return TemplateResponse(
                request,
                "admin/dbinstances/logicaldatabase/import_sql.html",
                context,
            )

        if request.method != "POST":
            return HttpResponseNotAllowed(["GET", "POST", "HEAD"])

        upl = request.FILES.get("sql_file")
        if not upl:
            self.message_user(request, "No file was uploaded.", level=messages.ERROR)
            return redirect(request.path)
        ext = _sql_import_staging_suffix(upl.name)
        if ext is None:
            self.message_user(
                request,
                "Only .sql, .sql.gz, or .zip files are accepted.",
                level=messages.ERROR,
            )
            return redirect(request.path)
        max_b = settings.SQL_IMPORT_MAX_UPLOAD_BYTES
        if upl.size > max_b:
            self.message_user(
                request,
                f"File exceeds maximum size ({max_b} bytes).",
                level=messages.ERROR,
            )
            return redirect(request.path)

        if engine.status != InstanceStatus.RUNNING:
            self.message_user(
                request,
                "The database engine is not running; start the container first.",
                level=messages.ERROR,
            )
            return redirect(request.path)

        staging_root = Path(settings.MEDIA_ROOT) / "sql_import_staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        dest = staging_root / f"{uuid.uuid4()}{ext}"
        try:
            with dest.open("wb") as out:
                for chunk in upl.chunks():
                    out.write(chunk)
        except OSError as exc:
            self.message_user(
                request,
                f"Could not save upload: {exc}",
                level=messages.ERROR,
            )
            return redirect(change_url)

        try:
            job = enqueue_docker_admin_job(
                engine.pk,
                DockerJobKind.IMPORT_SQL_DUMP,
                logical_database=obj,
                sql_import_path=str(dest.resolve()),
            )
        except DockerJobConflict:
            dest.unlink(missing_ok=True)
            self.message_user(
                request,
                "Another job is already queued or running for this engine. "
                "Wait for it to finish, then try again.",
                level=messages.WARNING,
            )
            return redirect(change_url)
        except Exception as exc:
            dest.unlink(missing_ok=True)
            self.message_user(
                request,
                f"Could not queue import (Redis/Celery?): {exc}",
                level=messages.ERROR,
            )
            return redirect(change_url)

        self.message_user(
            request,
            "SQL import job queued. Progress opens next.",
            level=messages.INFO,
        )
        return redirect(
            reverse(
                "admin:dbinstances_databaseengine_docker_job_progress",
                args=[engine.pk, job.pk],
            )
        )

    def import_sql_chunk_init(self, request, object_id):
        if request.method != "POST":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        obj = get_object_or_404(LogicalDatabase, pk=object_id)
        if not self.has_change_permission(request, obj):
            return JsonResponse({"error": "Forbidden"}, status=403)
        engine = obj.engine
        if engine.status != InstanceStatus.RUNNING:
            return JsonResponse(
                {"error": "Database engine is not running."},
                status=400,
            )
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
        filename = (payload.get("filename") or "").strip()
        try:
            total_size = int(payload.get("total_size"))
        except (TypeError, ValueError):
            return JsonResponse({"error": "total_size required (integer)"}, status=400)
        if total_size < 1:
            return JsonResponse({"error": "total_size must be positive"}, status=400)
        max_b = settings.SQL_IMPORT_MAX_UPLOAD_BYTES
        if total_size > max_b:
            return JsonResponse(
                {"error": f"File exceeds maximum size ({max_b} bytes)."},
                status=400,
            )
        ext = _sql_import_staging_suffix(filename)
        if ext is None:
            return JsonResponse(
                {"error": "Only .sql, .sql.gz, or .zip files are accepted."},
                status=400,
            )
        try:
            upload_id = sql_chunk_upload.init_upload(
                logical_db_id=obj.pk,
                user_id=request.user.pk,
                filename=filename,
                total_size=total_size,
                extension=ext,
            )
        except OSError as exc:
            return JsonResponse({"error": f"Could not start upload: {exc}"}, status=500)
        return JsonResponse(
            {
                "upload_id": upload_id,
                "chunk_size": settings.SQL_IMPORT_CHUNK_SIZE_BYTES,
            }
        )

    def import_sql_chunk_upload(self, request, object_id):
        if request.method != "POST":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        obj = get_object_or_404(LogicalDatabase, pk=object_id)
        if not self.has_change_permission(request, obj):
            return JsonResponse({"error": "Forbidden"}, status=403)
        upload_id = (request.headers.get("X-Upload-Id") or "").strip()
        if not upload_id:
            return JsonResponse({"error": "Missing X-Upload-Id header"}, status=400)
        try:
            chunk_index = int(request.headers.get("X-Chunk-Index", ""))
        except ValueError:
            return JsonResponse({"error": "Invalid X-Chunk-Index"}, status=400)
        max_chunk = int(settings.SQL_IMPORT_CHUNK_SIZE_BYTES)
        data = request.body
        if len(data) > max_chunk:
            return JsonResponse(
                {"error": f"Chunk larger than server limit ({max_chunk} bytes)"},
                status=400,
            )
        try:
            out = sql_chunk_upload.append_chunk(
                upload_id,
                chunk_index=chunk_index,
                data=data,
                expect_user_id=request.user.pk,
                expect_logical_db_id=obj.pk,
            )
        except PermissionError:
            return JsonResponse({"error": "Forbidden"}, status=403)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse(out)

    def import_sql_chunk_complete(self, request, object_id):
        if request.method != "POST":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        obj = get_object_or_404(LogicalDatabase, pk=object_id)
        if not self.has_change_permission(request, obj):
            return JsonResponse({"error": "Forbidden"}, status=403)
        engine = obj.engine
        if engine.status != InstanceStatus.RUNNING:
            return JsonResponse(
                {"error": "Database engine is not running."},
                status=400,
            )
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
        upload_id = (payload.get("upload_id") or "").strip()
        if not upload_id:
            return JsonResponse({"error": "upload_id required"}, status=400)
        try:
            dest = sql_chunk_upload.finalize_upload(
                upload_id,
                expect_user_id=request.user.pk,
                expect_logical_db_id=obj.pk,
            )
        except PermissionError:
            return JsonResponse({"error": "Forbidden"}, status=403)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except OSError as exc:
            return JsonResponse({"error": str(exc)}, status=500)
        try:
            job = enqueue_docker_admin_job(
                engine.pk,
                DockerJobKind.IMPORT_SQL_DUMP,
                logical_database=obj,
                sql_import_path=str(dest.resolve()),
            )
        except DockerJobConflict:
            dest.unlink(missing_ok=True)
            return JsonResponse(
                {
                    "error": "Another job is already queued or running for this engine.",
                },
                status=409,
            )
        except Exception as exc:
            dest.unlink(missing_ok=True)
            return JsonResponse(
                {"error": f"Could not queue import: {exc}"},
                status=500,
            )
        progress_url = reverse(
            "admin:dbinstances_databaseengine_docker_job_progress",
            args=[engine.pk, job.pk],
        )
        return JsonResponse({"redirect": progress_url})


# @admin.register(ManagedDatabaseUser)
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
