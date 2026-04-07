from __future__ import annotations

import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from django.conf import settings

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)


def sql_import_staging_root() -> Path:
    root = Path(settings.MEDIA_ROOT) / "sql_import_staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def partial_paths(upload_id: str) -> tuple[Path, Path, Path]:
    base = sql_import_staging_root()
    return (
        base / f"partial_{upload_id}.part",
        base / f"partial_{upload_id}.meta.json",
        base / f"partial_{upload_id}.lock",
    )


def _sweep_stale_partial_uploads(ttl_sec: int) -> None:
    if ttl_sec <= 0:
        return
    base = sql_import_staging_root()
    now = time.time()
    try:
        for meta_path in base.glob("partial_*.meta.json"):
            try:
                if now - meta_path.stat().st_mtime <= ttl_sec:
                    continue
            except OSError:
                continue
            stem = meta_path.name.removeprefix("partial_").removesuffix(".meta.json")
            part, _m, lock = partial_paths(stem)
            for p in (meta_path, part, lock):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove stale chunk upload file %s", p)
    except OSError as e:
        logger.debug("Chunk upload sweep skipped: %s", e)


def _with_upload_lock(upload_id: str, fn: Callable[[], Any]) -> Any:
    _part, _meta, lock_path = partial_paths(upload_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lockf:
        if fcntl is not None:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def read_meta(upload_id: str) -> dict[str, Any] | None:
    _part, meta_path, _lock = partial_paths(upload_id)
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_meta(upload_id: str, meta: dict[str, Any]) -> None:
    _part, meta_path, _lock = partial_paths(upload_id)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def init_upload(
    *,
    logical_db_id: int,
    user_id: int,
    filename: str,
    total_size: int,
    extension: str,
) -> str:
    ttl = int(
        getattr(
            settings,
            "SQL_IMPORT_CHUNK_UPLOAD_TTL_SEC",
            86400,
        )
    )
    _sweep_stale_partial_uploads(ttl)
    upload_id = secrets.token_urlsafe(32)
    part_path, meta_path, _lock = partial_paths(upload_id)
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.touch()
    meta = {
        "logical_db_id": logical_db_id,
        "user_id": user_id,
        "filename": filename[:512],
        "total_size": total_size,
        "next_chunk_index": 0,
        "received_bytes": 0,
        "extension": extension,
        "created": time.time(),
    }
    write_meta(upload_id, meta)
    return upload_id


def append_chunk(
    upload_id: str,
    *,
    chunk_index: int,
    data: bytes,
    expect_user_id: int,
    expect_logical_db_id: int,
) -> dict[str, int]:
    def work() -> dict[str, int]:
        meta = read_meta(upload_id)
        if not meta:
            raise ValueError("Unknown or expired upload_id")
        if int(meta.get("user_id", -1)) != expect_user_id:
            raise PermissionError("Upload session user mismatch")
        if int(meta.get("logical_db_id", -1)) != expect_logical_db_id:
            raise PermissionError("Upload session database mismatch")
        if chunk_index != int(meta["next_chunk_index"]):
            raise ValueError(
                f"Expected chunk index {meta['next_chunk_index']}, got {chunk_index}",
            )
        total = int(meta["total_size"])
        received = int(meta["received_bytes"])
        if received >= total:
            raise ValueError("Upload already complete")
        if received + len(data) > total:
            raise ValueError("Chunk would exceed declared total_size")
        part_path, _meta, _lock = partial_paths(upload_id)
        with open(part_path, "ab") as out:
            out.write(data)
        meta["received_bytes"] = received + len(data)
        meta["next_chunk_index"] = int(meta["next_chunk_index"]) + 1
        write_meta(upload_id, meta)
        return {"received_bytes": meta["received_bytes"], "total_size": total}

    return _with_upload_lock(upload_id, work)


def finalize_upload(
    upload_id: str,
    *,
    expect_user_id: int,
    expect_logical_db_id: int,
) -> Path:
    def work() -> Path:
        meta = read_meta(upload_id)
        if not meta:
            raise ValueError("Unknown or expired upload_id")
        if int(meta.get("user_id", -1)) != expect_user_id:
            raise PermissionError("Upload session user mismatch")
        if int(meta.get("logical_db_id", -1)) != expect_logical_db_id:
            raise PermissionError("Upload session database mismatch")
        total = int(meta["total_size"])
        received = int(meta["received_bytes"])
        if received != total:
            raise ValueError(
                f"Incomplete upload: received {received}, expected {total}",
            )
        part_path, meta_path, lock_path = partial_paths(upload_id)
        ext = str(meta.get("extension", ".sql"))
        dest = part_path.parent / f"{uuid.uuid4()}{ext}"
        if dest.exists():
            raise OSError("Destination collision")
        os.replace(part_path, dest)
        meta_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)
        return dest

    return _with_upload_lock(upload_id, work)
