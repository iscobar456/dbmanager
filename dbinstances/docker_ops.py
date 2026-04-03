from __future__ import annotations

import logging
import re

import docker
from docker.errors import DockerException, NotFound
from docker.types import Mount

from .models import DatabaseEngine, InstanceStatus
from .sql_provision import try_provision_after_start

logger = logging.getLogger(__name__)


def _container_ids_match(daemon_id: str, stored: str) -> bool:
    if not daemon_id or not stored:
        return False
    if daemon_id == stored:
        return True
    return daemon_id.startswith(stored) or stored.startswith(daemon_id)


def get_client() -> docker.DockerClient:
    return docker.from_env()


def _truncate_error(msg: str, limit: int = 2000) -> str:
    msg = msg.strip()
    if len(msg) <= limit:
        return msg
    return msg[: limit - 3] + "..."


def sync_status(instance: DatabaseEngine, client: docker.DockerClient | None = None) -> None:
    """Update status fields from Docker. Does not call save()."""
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        if not instance.container_id:
            instance.status = InstanceStatus.STOPPED
            instance.last_error = ""
            return

        try:
            c = client.containers.get(instance.container_id)
            instance.container_id = c.id
            instance.container_name = c.name.lstrip("/") if c.name else instance.container_name
            if c.status == "running":
                instance.status = InstanceStatus.RUNNING
            else:
                instance.status = InstanceStatus.STOPPED
            instance.last_error = ""
        except NotFound:
            instance.status = InstanceStatus.MISSING
            instance.last_error = "Container id not found in Docker."
    except DockerException as e:
        instance.status = InstanceStatus.ERROR
        instance.last_error = _truncate_error(str(e))
        logger.exception("Docker error during sync_status")
    finally:
        if own_client:
            client.close()


def ensure_volume(name: str, client: docker.DockerClient) -> None:
    try:
        client.volumes.get(name)
    except NotFound:
        client.volumes.create(name=name)


def _sanitize_repo_tag(tag: str) -> str:
    tag = tag.strip()
    if not tag or len(tag) > 128:
        raise ValueError("Invalid image tag")
    if re.search(r"[\s\n\r]", tag):
        raise ValueError("Image tag must not contain whitespace")
    return tag


def pull_image(instance: DatabaseEngine, client: docker.DockerClient) -> None:
    repo = instance.vendor
    tag = _sanitize_repo_tag(instance.image_tag)
    image_ref = f"{repo}:{tag}"
    client.images.pull(repo, tag=tag)
    logger.info("Pulled image %s", image_ref)


def create_and_start(instance: DatabaseEngine, client: docker.DockerClient | None = None) -> None:
    """
    Create Docker volume (if needed), pull image, create container, start.
    Updates instance container_id, container_name, status, last_error.
    Caller must save() the instance.
    """
    if instance.pk is None:
        raise ValueError("Instance must be saved before create_and_start")

    own_client = client is None
    if own_client:
        client = get_client()

    try:
        if instance.container_id:
            try:
                existing = client.containers.get(instance.container_id)
                if existing.status == "running":
                    instance.status = InstanceStatus.RUNNING
                    instance.container_name = existing.name.lstrip("/")
                    instance.last_error = ""
                    return
                existing.start()
                instance.status = InstanceStatus.RUNNING
                instance.last_error = ""
                return
            except NotFound:
                instance.container_id = ""
                instance.container_name = ""

        pull_image(instance, client)

        vol = instance.volume_name
        ensure_volume(vol, client)

        root_pw = instance.root_password_for_docker()
        env = {"MYSQL_ROOT_PASSWORD": root_pw}
        logical = list(instance.logical_databases.all())
        if len(logical) == 1:
            env["MYSQL_DATABASE"] = logical[0].schema_name

        name = instance.suggested_container_name()
        mounts = [
            Mount(target="/var/lib/mysql", source=vol, type="volume"),
        ]

        try:
            old = client.containers.get(name)
            if instance.container_id and _container_ids_match(old.id, instance.container_id):
                pass
            else:
                raise ValueError(
                    f"Container name {name!r} is already in use by another container."
                )
        except NotFound:
            pass

        container = client.containers.create(
            image=instance.docker_image,
            name=name,
            environment=env,
            mounts=mounts,
            ports={"3306/tcp": ("0.0.0.0", instance.host_port)},
            restart_policy={"Name": "unless-stopped"},
            detach=True,
        )
        container.start()
        instance.container_id = container.id
        instance.container_name = name
        instance.status = InstanceStatus.RUNNING
        instance.last_error = ""
        try_provision_after_start(instance)
    except (DockerException, ValueError) as e:
        instance.status = InstanceStatus.ERROR
        instance.last_error = _truncate_error(str(e))
        logger.exception("create_and_start failed for instance pk=%s", instance.pk)
        raise
    finally:
        if own_client:
            client.close()


def start_container(instance: DatabaseEngine, client: docker.DockerClient | None = None) -> None:
    own_client = client is None
    if own_client:
        client = get_client()
    try:
        if not instance.container_id:
            raise ValueError("No container id; use Create and start first.")
        c = client.containers.get(instance.container_id)
        c.start()
        instance.status = InstanceStatus.RUNNING
        instance.last_error = ""
    except NotFound:
        instance.status = InstanceStatus.MISSING
        instance.last_error = "Container id not found in Docker."
        raise
    except DockerException as e:
        instance.status = InstanceStatus.ERROR
        instance.last_error = _truncate_error(str(e))
        raise
    finally:
        if own_client:
            client.close()


def stop_container(instance: DatabaseEngine, client: docker.DockerClient | None = None) -> None:
    own_client = client is None
    if own_client:
        client = get_client()
    try:
        if not instance.container_id:
            instance.status = InstanceStatus.STOPPED
            return
        c = client.containers.get(instance.container_id)
        c.stop(timeout=10)
        instance.status = InstanceStatus.STOPPED
        instance.last_error = ""
    except NotFound:
        instance.status = InstanceStatus.MISSING
        instance.last_error = "Container id not found in Docker."
    except DockerException as e:
        instance.status = InstanceStatus.ERROR
        instance.last_error = _truncate_error(str(e))
        raise
    finally:
        if own_client:
            client.close()


def remove_container(
    instance: DatabaseEngine,
    *,
    remove_volume: bool = False,
    client: docker.DockerClient | None = None,
) -> None:
    own_client = client is None
    if own_client:
        client = get_client()
    try:
        vol_name = instance.volume_name if instance.pk else ""
        cid = instance.container_id
        if cid:
            try:
                c = client.containers.get(cid)
                c.remove(force=True)
            except NotFound:
                pass
        instance.container_id = ""
        instance.container_name = ""
        instance.status = InstanceStatus.STOPPED
        instance.last_error = ""

        if remove_volume and vol_name:
            try:
                v = client.volumes.get(vol_name)
                v.remove(force=True)
            except NotFound:
                pass
    except DockerException as e:
        instance.status = InstanceStatus.ERROR
        instance.last_error = _truncate_error(str(e))
        raise
    finally:
        if own_client:
            client.close()


def recreate_container(instance: DatabaseEngine, client: docker.DockerClient | None = None) -> None:
    """Remove existing container (keep volume) and create_and_start with current fields."""
    own_client = client is None
    if own_client:
        client = get_client()
    try:
        remove_container(instance, remove_volume=False, client=client)
        instance.save(
            update_fields=[
                "container_id",
                "container_name",
                "status",
                "last_error",
                "user_provision_error",
                "updated_at",
            ]
        )
        try:
            create_and_start(instance, client=client)
        except (DockerException, ValueError):
            pass
        instance.save(
            update_fields=[
                "container_id",
                "container_name",
                "status",
                "last_error",
                "user_provision_error",
                "updated_at",
            ]
        )
    finally:
        if own_client:
            client.close()
