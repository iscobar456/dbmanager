from __future__ import annotations

import logging

from django.db import transaction

from .models import DatabaseEngine, DockerAdminJob, DockerJobStatus

logger = logging.getLogger(__name__)


class DockerJobConflict(Exception):
    """Another job is already queued or running for this engine."""


def enqueue_docker_admin_job(
    engine_pk: int,
    kind: str,
    *,
    logical_database: LogicalDatabase | None = None,
    sql_import_path: str = "",
) -> DockerAdminJob:
    """
    Create a job row (with row lock on the engine) and dispatch the Celery task.
    Rolls back the job row if the broker cannot accept the task.
    """
    with transaction.atomic():
        engine = DatabaseEngine.objects.select_for_update().get(pk=engine_pk)
        busy = DockerAdminJob.objects.filter(
            engine=engine,
            status__in=(DockerJobStatus.PENDING, DockerJobStatus.RUNNING),
        ).exists()
        if busy:
            raise DockerJobConflict(
                "Another Docker or SQL job is already queued or running for this engine.",
            )
        job = DockerAdminJob.objects.create(
            engine=engine,
            kind=kind,
            status=DockerJobStatus.PENDING,
            logical_database=logical_database,
            sql_import_path=sql_import_path,
        )

    try:
        from .tasks import run_docker_admin_job

        async_result = run_docker_admin_job.delay(str(job.pk))
    except Exception:
        logger.exception("Failed to dispatch Celery task for job %s", job.pk)
        job.delete()
        raise

    job.celery_task_id = async_result.id
    job.save(update_fields=["celery_task_id"])
    return job
