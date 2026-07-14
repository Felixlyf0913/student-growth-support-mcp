from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


class PersistentStore:
    """Small JSON-record store backed by PostgreSQL or local SQLite."""

    def __init__(
        self,
        database_url: str | None = None,
        sqlite_path: str | Path | None = None,
    ) -> None:
        configured_url = (database_url or os.environ.get("DATABASE_URL", "")).strip()
        self._lock = threading.RLock()
        if configured_url:
            self.backend = "postgresql"
            self.database_url = (
                "postgresql://" + configured_url[len("postgres://") :]
                if configured_url.startswith("postgres://")
                else configured_url
            )
            self.sqlite_path = None
        else:
            self.backend = "sqlite"
            self.database_url = ""
            self.sqlite_path = Path(
                sqlite_path
                or os.environ.get("SQLITE_PATH", "")
                or Path(__file__).resolve().parent / "student_management.db"
            ).resolve()
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @property
    def durable_across_deploys(self) -> bool:
        return self.backend == "postgresql"

    @property
    def label(self) -> str:
        if self.backend == "postgresql":
            return "PostgreSQL"
        return f"SQLite ({self.sqlite_path.name})"

    @property
    def placeholder(self) -> str:
        return "%s" if self.backend == "postgresql" else "?"

    def _connect(self) -> Any:
        if self.backend == "postgresql":
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "已配置 DATABASE_URL，但缺少 psycopg 数据库驱动。"
                ) from exc
            return psycopg.connect(self.database_url, connect_timeout=10)
        return sqlite3.connect(self.sqlite_path, timeout=10)

    @contextmanager
    def _connection(self) -> Any:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS app_records (
                bucket TEXT NOT NULL,
                record_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (bucket, record_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS support_task_audit (
                audit_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                before_status TEXT NOT NULL,
                after_status TEXT NOT NULL,
                details TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS app_metadata (
                meta_key TEXT PRIMARY KEY,
                meta_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )
        with self._lock, self._connection() as connection:
            cursor = connection.cursor()
            for statement in statements:
                cursor.execute(statement)

    def list_records(self, bucket: str) -> list[dict[str, Any]]:
        query = (
            "SELECT payload FROM app_records "
            f"WHERE bucket = {self.placeholder} ORDER BY created_at, record_id"
        )
        with self._lock, self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(query, (bucket,))
            rows = cursor.fetchall()
        return [json.loads(row[0]) for row in rows]

    def count_records(self, bucket: str) -> int:
        query = f"SELECT COUNT(*) FROM app_records WHERE bucket = {self.placeholder}"
        with self._lock, self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(query, (bucket,))
            row = cursor.fetchone()
        return int(row[0])

    def upsert_record(
        self,
        bucket: str,
        record_id: str,
        payload: dict[str, Any],
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        marker = self.placeholder
        query = f"""
            INSERT INTO app_records
                (bucket, record_id, payload, created_at, updated_at)
            VALUES ({marker}, {marker}, {marker}, {marker}, {marker})
            ON CONFLICT (bucket, record_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
        """
        values = (
            bucket,
            record_id,
            json.dumps(payload, ensure_ascii=False),
            now,
            now,
        )
        with self._lock, self._connection() as connection:
            connection.cursor().execute(query, values)

    def replace_records(
        self,
        bucket: str,
        records: list[dict[str, Any]],
        id_field: str,
    ) -> None:
        delete_query = f"DELETE FROM app_records WHERE bucket = {self.placeholder}"
        marker = self.placeholder
        insert_query = f"""
            INSERT INTO app_records
                (bucket, record_id, payload, created_at, updated_at)
            VALUES ({marker}, {marker}, {marker}, {marker}, {marker})
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(delete_query, (bucket,))
            for record in records:
                cursor.execute(
                    insert_query,
                    (
                        bucket,
                        str(record[id_field]),
                        json.dumps(record, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

    def append_audit(
        self,
        task_id: str,
        action: str,
        actor: str,
        before_status: str,
        after_status: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        entry = {
            "audit_id": f"AUD-{uuid.uuid4().hex}",
            "task_id": task_id,
            "action": action,
            "actor": actor,
            "before_status": before_status,
            "after_status": after_status,
            "details": details,
            "occurred_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
        }
        marker = self.placeholder
        query = f"""
            INSERT INTO support_task_audit
                (audit_id, task_id, action, actor, before_status,
                 after_status, details, occurred_at)
            VALUES ({marker}, {marker}, {marker}, {marker}, {marker},
                    {marker}, {marker}, {marker})
        """
        values = (
            entry["audit_id"],
            entry["task_id"],
            entry["action"],
            entry["actor"],
            entry["before_status"],
            entry["after_status"],
            json.dumps(entry["details"], ensure_ascii=False),
            entry["occurred_at"],
        )
        with self._lock, self._connection() as connection:
            connection.cursor().execute(query, values)
        return entry

    def list_audit(
        self,
        task_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        marker = self.placeholder
        if task_id:
            query = f"""
                SELECT audit_id, task_id, action, actor, before_status,
                       after_status, details, occurred_at
                FROM support_task_audit
                WHERE task_id = {marker}
                ORDER BY occurred_at DESC, audit_id DESC
                LIMIT {marker}
            """
            values: tuple[Any, ...] = (task_id, limit)
        else:
            query = f"""
                SELECT audit_id, task_id, action, actor, before_status,
                       after_status, details, occurred_at
                FROM support_task_audit
                ORDER BY occurred_at DESC, audit_id DESC
                LIMIT {marker}
            """
            values = (limit,)
        with self._lock, self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(query, values)
            rows = cursor.fetchall()
        return [
            {
                "audit_id": row[0],
                "task_id": row[1],
                "action": row[2],
                "actor": row[3],
                "before_status": row[4],
                "after_status": row[5],
                "details": json.loads(row[6]),
                "occurred_at": row[7],
            }
            for row in rows
        ]

    def get_metadata(self, key: str) -> str:
        query = f"SELECT meta_value FROM app_metadata WHERE meta_key = {self.placeholder}"
        with self._lock, self._connection() as connection:
            cursor = connection.cursor()
            cursor.execute(query, (key,))
            row = cursor.fetchone()
        return row[0] if row else ""

    def set_metadata(self, key: str, value: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        marker = self.placeholder
        query = f"""
            INSERT INTO app_metadata (meta_key, meta_value, updated_at)
            VALUES ({marker}, {marker}, {marker})
            ON CONFLICT (meta_key) DO UPDATE SET
                meta_value = excluded.meta_value,
                updated_at = excluded.updated_at
        """
        with self._lock, self._connection() as connection:
            connection.cursor().execute(query, (key, value, now))

    def clear_audit(self) -> None:
        with self._lock, self._connection() as connection:
            connection.cursor().execute("DELETE FROM support_task_audit")

    def count_audit(self) -> int:
        with self._lock, self._connection() as connection:
            row = connection.cursor().execute(
                "SELECT COUNT(*) FROM support_task_audit"
            ).fetchone()
        return int(row[0])
