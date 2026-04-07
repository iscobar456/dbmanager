from __future__ import annotations

import logging
import shutil
from pathlib import Path

from celery import shared_task
from django.utils import timezone

from . import docker_ops
from .models import (
    DatabaseEngine,
    DockerAdminJob,
    DockerJobKind,
    DockerJobStatus,
    InstanceStatus,
)
from .sql_import import apply_sql_dump
from .sql_provision import sync_engine_databases_and_users

logger = logging.getLogger(__name__)

_ENGINE_UPDATE_FIELDS = [
    "container_id",
    "container_name",
    "status",
    "last_error",
    "user_provision_error",
    "updated_at",
]


def _save_engine_state(instance: DatabaseEngine) -> None:
    instance.save(update_fields=_ENGINE_UPDATE_FIELDS)


def _job_report(job_pk: str, step: str, message: str) -> None:
    DockerAdminJob.objects.filter(pk=job_pk).update(
        step=step[:64],
        message=message[:2000],
        updated_at=timezone.now(),
    )


@shared_task(bind=True, ignore_result=True)
def run_docker_admin_job(self, job_id: str) -> None:
    job = DockerAdminJob.objects.select_related("engine").get(pk=job_id)
    engine = job.engine

    def report(step: str, msg: str) -> None:
        _job_report(str(job.pk), step, msg)

    job.status = DockerJobStatus.RUNNING
    job.message = "Starting…"
    job.save(update_fields=["status", "message", "updated_at"])

    try:
        if job.kind == DockerJobKind.CREATE_AND_START:
            docker_ops.create_and_start(engine, progress=report)
        elif job.kind == DockerJobKind.RECREATE_CONTAINER:
            docker_ops.recreate_container(engine, progress=report)
            if engine.status == InstanceStatus.ERROR:
                raise RuntimeError(engine.last_error or "Recreate container failed")
        elif job.kind == DockerJobKind.SYNC_DATABASES_AND_USERS:
            report("sync", "Syncing databases and application users…")
            sync_engine_databases_and_users(engine, progress=report)
            engine.user_provision_error = ""
        elif job.kind == DockerJobKind.IMPORT_SQL_DUMP:
            ld = job.logical_database
            if ld is None:
                raise ValueError("Import job is missing logical_database.")
            path = (job.sql_import_path or "").strip()
            if not path:
                raise ValueError("Import job is missing sql_import_path.")
            staging = Path(path)
            extract_dir = staging.with_name(staging.stem + "_extract")
            try:
                apply_sql_dump(ld, path, progress=report)
            finally:
                try:
                    if extract_dir.is_dir():
                        shutil.rmtree(extract_dir, ignore_errors=True)
                except OSError:
                    logger.warning(
                        "Could not remove SQL zip extract dir %s", extract_dir
                    )
                try:
                    if staging.is_file():
                        staging.unlink()
                except OSError:
                    logger.warning("Could not remove SQL staging file %s", path)
        else:
            raise ValueError(f"Unknown job kind: {job.kind!r}")

        _save_engine_state(engine)
    except Exception as exc:
        logger.exception("Docker admin job %s failed", job_id)
        if job.kind == DockerJobKind.SYNC_DATABASES_AND_USERS:
            engine.user_provision_error = str(exc)[:2000]
        try:
            _save_engine_state(engine)
        except Exception:
            logger.exception("Could not save engine after failed job")
        job.refresh_from_db()
        job.status = DockerJobStatus.FAILURE
        job.error = str(exc)[:2000]
        job.finished_at = timezone.now()
        job.save(
            update_fields=["status", "error", "finished_at", "updated_at"],
        )
        return

    job.refresh_from_db()
    job.status = DockerJobStatus.SUCCESS
    job.finished_at = timezone.now()
    job.save(
        update_fields=["status", "finished_at", "updated_at"],
    )
