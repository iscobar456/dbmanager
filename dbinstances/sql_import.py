from __future__ import annotations

import gzip
import logging
import queue
import re
import shutil
import time
import threading
import subprocess
import zipfile
from pathlib import Path

import pymysql
from django.conf import settings

from .models import InstanceStatus, LogicalDatabase
from .sql_provision import ProgressFn

logger = logging.getLogger(__name__)

_CONNECT_HOST = "127.0.0.1"
_CHUNK_BYTES = 1024 * 1024


def _mysql_init_command_argv() -> list[str]:
    init = getattr(settings, "SQL_IMPORT_MYSQL_INIT_COMMAND", "") or ""
    init = init.strip()
    if not init:
        return []
    return ["--init-command", init]


def _fmt_bytes(num: int) -> str:
    n = float(num)
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    if num < 1024**3:
        return f"{n / (1024**2):.2f} MiB"
    return f"{n / (1024**3):.2f} GiB"


def _import_progress_message(sent: int, total: int | None) -> str:
    if total is not None and total > 0:
        pct = min(100.0, 100.0 * sent / total)
        return (
            f"Streaming SQL into mysql… {_fmt_bytes(sent)} / {_fmt_bytes(total)} "
            f"({pct:.1f}%)"
        )
    return f"Streaming SQL into mysql… {_fmt_bytes(sent)} sent"


def _flush_import_progress_queue(
    q: queue.SimpleQueue[int] | None,
    progress_cb: ProgressFn | None,
    *,
    total_bytes: int | None,
) -> None:
    """Apply latest queued byte offset via progress_cb (main thread only)."""
    if q is None or progress_cb is None:
        return
    latest: int | None = None
    while True:
        try:
            latest = q.get_nowait()
        except queue.Empty:
            break
    if latest is not None:
        progress_cb("import", _import_progress_message(latest, total_bytes))


def _validate_schema_name(schema: str) -> None:
    if not re.match(r"^[a-zA-Z0-9_]+$", schema):
        raise ValueError(f"Invalid schema name for import: {schema!r}")


def ensure_database_exists(engine, schema_name: str, *, root_password: str) -> None:
    _validate_schema_name(schema_name)
    conn = pymysql.connect(
        host=_CONNECT_HOST,
        port=engine.host_port,
        user="root",
        password=root_password,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{schema_name}`")
        conn.commit()
    finally:
        conn.close()


def _run_mysql_cmd(
    cmd: list[str],
    stdin_f,
    *,
    timeout_sec: int,
    progress: ProgressFn | None = None,
    total_bytes: int | None = None,
) -> None:
    """Run mysql with stdin; raise RuntimeError with client stderr on failure.

    ``stdin_f`` is copied in-process into the client stdin pipe so streams such
    as :class:`gzip.GzipFile` decompress correctly. Passing ``GzipFile`` directly
    to :func:`subprocess.run` would duplicate the underlying compressed fd and
    send gzip bytes to ``mysql``.

    Import byte counts are queued from the feeder thread and flushed here so
    ``progress`` runs only on the calling thread (safe with Django DB backends).
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        raise RuntimeError(f"failed to start mysql client: {e}") from e

    interval_bytes = max(
        256 * 1024,
        int(
            getattr(
                settings,
                "SQL_IMPORT_PROGRESS_INTERVAL_BYTES",
                5 * 1024 * 1024,
            ),
        ),
    )
    min_interval_sec = float(
        getattr(settings, "SQL_IMPORT_PROGRESS_MIN_INTERVAL_SEC", 2.0),
    )
    progress_queue: queue.SimpleQueue[int] | None = (
        queue.SimpleQueue() if progress else None
    )

    def _feed_stdin() -> None:
        sent = 0
        last_report_sent = 0
        last_report_time = time.monotonic()
        try:
            while True:
                chunk = stdin_f.read(_CHUNK_BYTES)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                sent += len(chunk)
                if progress_queue is not None:
                    now = time.monotonic()
                    due_bytes = sent - last_report_sent >= interval_bytes
                    due_time = now - last_report_time >= min_interval_sec
                    if due_bytes or due_time:
                        progress_queue.put(sent)
                        last_report_sent = sent
                        last_report_time = now
            if progress_queue is not None:
                progress_queue.put(sent)
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    feeder = threading.Thread(target=_feed_stdin, daemon=True)
    feeder.start()
    deadline = time.monotonic() + timeout_sec
    try:
        # communicate() must run only after stdin is fully written: with
        # stdin=PIPE and input=None, communicate() closes stdin immediately,
        # which races the feeder and cuts off the stream (broken .sql.gz imports).
        while feeder.is_alive():
            if time.monotonic() >= deadline:
                proc.kill()
                feeder.join(timeout=2.0)
                raise TimeoutError(
                    f"mysql command timed out after {timeout_sec} seconds",
                )
            _flush_import_progress_queue(
                progress_queue,
                progress,
                total_bytes=total_bytes,
            )
            remaining = deadline - time.monotonic()
            feeder.join(timeout=min(0.5, max(0.05, remaining)))
        _flush_import_progress_queue(
            progress_queue,
            progress,
            total_bytes=total_bytes,
        )
        out_budget = max(0.1, deadline - time.monotonic())
        stdout, stderr = proc.communicate(timeout=out_budget)
    except subprocess.TimeoutExpired:
        proc.kill()
        feeder.join(timeout=2.0)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except Exception:
            stdout, stderr = b"", b""
        raise TimeoutError(
            f"mysql command timed out after {timeout_sec} seconds",
        )

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        detail = err or out or "(no output from mysql client)"
        if len(detail) > 8000:
            detail = detail[:8000] + "\n… (truncated)"
        raise RuntimeError(
            f"mysql client exited with status {proc.returncode}: {detail}",
        )


def _run_mysql_stdin(
    engine,
    schema: str,
    pwd: str,
    stdin_f,
    *,
    timeout_sec: int,
    progress: ProgressFn | None = None,
    total_bytes: int | None = None,
) -> None:
    mysql_bin = shutil.which("mysql")
    init_args = _mysql_init_command_argv()
    if mysql_bin:
        cmd = [
            mysql_bin,
            f"-h{_CONNECT_HOST}",
            f"-P{engine.host_port}",
            "-uroot",
            f"-p{pwd}",
        ]
        cmd.extend(init_args)
        cmd.append(schema)
        logger.info("SQL import via host mysql into schema %s", schema)
        _run_mysql_cmd(
            cmd,
            stdin_f,
            timeout_sec=timeout_sec,
            progress=progress,
            total_bytes=total_bytes,
        )
    else:
        cmd = [
            "docker",
            "exec",
            "-i",
            engine.container_id,
            "mysql",
            "-uroot",
            f"-p{pwd}",
        ]
        cmd.extend(init_args)
        cmd.append(schema)
        logger.info(
            "SQL import via docker exec into schema %s container %s",
            schema,
            engine.container_id[:12],
        )
        _run_mysql_cmd(
            cmd,
            stdin_f,
            timeout_sec=timeout_sec,
            progress=progress,
            total_bytes=total_bytes,
        )


def _extract_single_sql_from_zip(zip_path: Path, extract_dir: Path) -> tuple[Path, int]:
    """
    Validate zip (paths, uncompressed size, exactly one top-level .sql), extract,
    and return ``(path_to_sql, uncompressed_sql_bytes)``.
    """
    cap = getattr(
        settings,
        "SQL_IMPORT_ZIP_MAX_UNCOMPRESSED_BYTES",
        512 * 1024 * 1024,
    )

    with zipfile.ZipFile(zip_path) as z:
        file_members: list[zipfile.ZipInfo] = []
        total = 0
        for zi in z.infolist():
            if zi.is_dir():
                continue
            fn = zi.filename
            p = Path(fn)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"Unsafe path in zip: {fn!r}")
            if len(p.parts) != 1:
                raise ValueError(
                    "Zip must contain only top-level files (no subfolders): "
                    f"{fn!r}"
                )
            total += zi.file_size
            file_members.append(zi)

        if total > cap:
            raise ValueError(
                f"Zip uncompressed size ({total} bytes) exceeds limit ({cap})."
            )

        sql_members = [
            zi for zi in file_members if zi.filename.lower().endswith(".sql")
        ]
        if not sql_members:
            raise ValueError("Zip contains no .sql file at the top level.")
        if len(sql_members) > 1:
            names = ", ".join(zi.filename for zi in sql_members)
            raise ValueError(
                f"Zip must contain exactly one .sql file at the top level; found: {names}"
            )

        one = sql_members[0]
        sql_size = int(one.file_size)
        extract_dir.mkdir(parents=True, exist_ok=True)
        z.extract(one, extract_dir)
        return extract_dir / one.filename, sql_size


def apply_sql_dump(
    logical: LogicalDatabase,
    sql_path: Path | str,
    *,
    progress: ProgressFn | None = None,
    timeout_sec: int | None = None,
) -> None:
    """
    Load SQL into ``logical.schema_name`` via ``mysql`` stdin or ``docker exec``.

    Accepts plain ``.sql``, gzip-compressed ``.sql.gz``, or ``.zip`` containing
    exactly one top-level ``.sql`` file.
    """
    sql_path = Path(sql_path)
    if not sql_path.is_file():
        raise FileNotFoundError(f"SQL staging file not found: {sql_path}")

    engine = logical.engine
    if engine.status != InstanceStatus.RUNNING:
        raise ValueError(
            "Database engine is not running; start the container before importing SQL.",
        )
    if not engine.container_id:
        raise ValueError("No container id for this engine.")

    schema = logical.schema_name
    _validate_schema_name(schema)

    root = engine.ensure_root_db_user()
    pwd = root.password
    timeout_sec = timeout_sec or getattr(
        settings,
        "SQL_IMPORT_MYSQL_TIMEOUT_SEC",
        3600,
    )

    if progress:
        progress("prepare", f"Ensuring database `{schema}` exists…")
    ensure_database_exists(engine, schema, root_password=pwd)

    name_lower = sql_path.name.lower()
    if name_lower.endswith(".sql.gz"):
        if progress:
            progress(
                "import",
                "Decompressing gzip and streaming into mysql "
                "(total size unknown until finished)…",
            )
        with gzip.open(sql_path, "rb") as stdin_f:
            _run_mysql_stdin(
                engine,
                schema,
                pwd,
                stdin_f,
                timeout_sec=timeout_sec,
                progress=progress,
                total_bytes=None,
            )
        return

    if sql_path.suffix.lower() == ".zip":
        extract_dir = sql_path.with_name(sql_path.stem + "_extract")
        if progress:
            progress("unzip", "Extracting zip and validating contents…")
        inner_sql, sql_total = _extract_single_sql_from_zip(sql_path, extract_dir)
        if progress:
            progress("import", "Streaming SQL into mysql…")
        with open(inner_sql, "rb") as stdin_f:
            _run_mysql_stdin(
                engine,
                schema,
                pwd,
                stdin_f,
                timeout_sec=timeout_sec,
                progress=progress,
                total_bytes=sql_total,
            )
        return

    plain_total = sql_path.stat().st_size
    if progress:
        progress("import", "Streaming SQL into mysql…")
    with open(sql_path, "rb") as stdin_f:
        _run_mysql_stdin(
            engine,
            schema,
            pwd,
            stdin_f,
            timeout_sec=timeout_sec,
            progress=progress,
            total_bytes=plain_total,
        )
