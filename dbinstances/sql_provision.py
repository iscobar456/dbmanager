from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import pymysql
from pymysql import err as pymysql_err

from .models import DatabaseEngine, InstanceStatus, ManagedDatabaseUser, UserKind

logger = logging.getLogger(__name__)

_CONNECT_HOST = "127.0.0.1"
_POLL_INTERVAL_SEC = 1.0
ProgressFn = Callable[[str, str], None]


def _truncate(msg: str, limit: int = 2000) -> str:
    msg = msg.strip()
    if len(msg) <= limit:
        return msg
    return msg[: limit - 3] + "..."


def _sql_quote_user_host(username: str, host: str) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "''")

    return f"'{esc(username)}'@'{esc(host)}'"


def _pct_for_mogrify(fragment: str) -> str:
    """Escape % so PyMySQL can merge this fragment with %s via query % args."""
    return fragment.replace("%", "%%")


def wait_for_mysql(
    port: int,
    *,
    password: str,
    timeout_sec: float = 90.0,
    progress: ProgressFn | None = None,
) -> None:
    deadline = time.monotonic() + timeout_sec
    last_exc: Exception | None = None
    last_prog = 0.0
    first = True
    while time.monotonic() < deadline:
        if progress:
            now = time.monotonic()
            if first:
                progress(
                    "wait_mysql",
                    "Waiting for MySQL to accept connections…",
                )
                first = False
                last_prog = now
            elif now - last_prog >= 12.0:
                progress(
                    "wait_mysql",
                    "Still waiting for MySQL to accept connections…",
                )
                last_prog = now
        try:
            conn = pymysql.connect(
                host=_CONNECT_HOST,
                port=port,
                user="root",
                password=password,
                connect_timeout=5,
                read_timeout=30,
                write_timeout=30,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                return
            finally:
                conn.close()
        except pymysql_err.Error as e:
            last_exc = e
            logger.debug("wait_for_mysql retry: %s", e)
            time.sleep(_POLL_INTERVAL_SEC)
    raise TimeoutError(
        f"MySQL not reachable on {_CONNECT_HOST}:{port} within {timeout_sec}s: {last_exc}"
    )


def _create_or_alter_user(cur: pymysql.cursors.Cursor, user: ManagedDatabaseUser) -> None:
    qh = _pct_for_mogrify(_sql_quote_user_host(user.username, user.host))
    try:
        cur.execute(
            f"CREATE USER {qh} IDENTIFIED BY %s",
            (user.password,),
        )
    except pymysql_err.Error:
        cur.execute(
            f"ALTER USER {qh} IDENTIFIED BY %s",
            (user.password,),
        )


def _grant_for_user(
    cur: pymysql.cursors.Cursor,
    user: ManagedDatabaseUser,
) -> None:
    qh = _sql_quote_user_host(user.username, user.host)
    gds = list(user.granted_databases.all())
    if gds:
        for ld in gds:
            db = ld.schema_name
            if not re.match(r"^[a-zA-Z0-9_]+$", db):
                raise ValueError(f"Invalid grant database name: {db!r}")
            cur.execute(f"GRANT ALL PRIVILEGES ON `{db}`.* TO {qh}")
    else:
        cur.execute(f"GRANT ALL PRIVILEGES ON *.* TO {qh}")


def ensure_root_wildcard_account(
    cur: pymysql.cursors.Cursor,
    root: ManagedDatabaseUser,
) -> None:
    """Create or update the managed root row in MySQL (typically `root`@`%`) and grant globally."""
    if root.kind != UserKind.ROOT:
        raise ValueError("ensure_root_wildcard_account expects kind=root")
    _create_or_alter_user(cur, root)
    qh = _sql_quote_user_host(root.username, root.host)
    cur.execute(f"GRANT ALL PRIVILEGES ON *.* TO {qh} WITH GRANT OPTION")


def provision_databases_and_users(
    instance: DatabaseEngine,
    *,
    progress: ProgressFn | None = None,
) -> None:
    root = instance.db_users.filter(kind=UserKind.ROOT).first()
    if root is None:
        root = instance.ensure_root_db_user()
    pwd = root.password

    app_users = list(
        instance.db_users.filter(kind=UserKind.APPLICATION).order_by("id")
    )

    if progress:
        progress(
            "provision_root_wildcard",
            "Ensuring root@'%' remote access…",
        )

    conn = pymysql.connect(
        host=_CONNECT_HOST,
        port=instance.host_port,
        user="root",
        password=pwd,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
    )
    try:
        with conn.cursor() as cur:
            ensure_root_wildcard_account(cur, root)
            if not app_users:
                cur.execute("FLUSH PRIVILEGES")
                conn.commit()
                return
            if progress:
                progress(
                    "provision_sql",
                    "Creating logical databases and syncing application users…",
                )
            for ld in instance.logical_databases.all():
                name = ld.schema_name
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}`")
            for u in app_users:
                _create_or_alter_user(cur, u)
                _grant_for_user(cur, u)
            cur.execute("FLUSH PRIVILEGES")
        conn.commit()
    finally:
        conn.close()


def try_provision_after_start(
    instance: DatabaseEngine,
    *,
    progress: ProgressFn | None = None,
) -> None:
    """Set or clear user_provision_error. Caller saves instance."""
    root = instance.ensure_root_db_user()
    try:
        wait_for_mysql(
            instance.host_port,
            password=root.password,
            progress=progress,
        )
    except TimeoutError as e:
        instance.user_provision_error = _truncate(str(e))
        logger.exception("wait_for_mysql failed")
        return
    try:
        provision_databases_and_users(instance, progress=progress)
    except pymysql_err.Error as e:
        instance.user_provision_error = _truncate(str(e))
        logger.exception("provision_databases_and_users failed")
    except ValueError as e:
        instance.user_provision_error = _truncate(str(e))
        logger.exception("grant validation failed")
    else:
        instance.user_provision_error = ""


def sync_engine_databases_and_users(
    instance: DatabaseEngine,
    *,
    progress: ProgressFn | None = None,
    wait_timeout: float = 45.0,
) -> None:
    """Wait for MySQL and run provision. Raises if engine is not RUNNING or on SQL errors."""
    if instance.status != InstanceStatus.RUNNING:
        raise ValueError(
            "Engine is not running; start the container before syncing SQL.",
        )
    root = instance.db_users.filter(kind=UserKind.ROOT).first()
    if root is None:
        root = instance.ensure_root_db_user()
    wait_for_mysql(
        instance.host_port,
        password=root.password,
        timeout_sec=wait_timeout,
        progress=progress,
    )
    provision_databases_and_users(instance, progress=progress)
