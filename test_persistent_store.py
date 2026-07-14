import tempfile
import unittest
from pathlib import Path

from persistent_store import PersistentStore


class PersistentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "student_management.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sqlite_records_survive_new_store_instance(self) -> None:
        first = PersistentStore(sqlite_path=self.db_path)
        first.upsert_record(
            "support_tasks",
            "ST001",
            {"task_id": "ST001", "status": "待处理"},
        )

        second = PersistentStore(sqlite_path=self.db_path)

        self.assertEqual(
            second.list_records("support_tasks"),
            [{"task_id": "ST001", "status": "待处理"}],
        )
        self.assertEqual(second.backend, "sqlite")

    def test_audit_entries_can_be_filtered_by_task(self) -> None:
        store = PersistentStore(sqlite_path=self.db_path)
        store.append_audit(
            task_id="ST001",
            action="created",
            actor="辅导员",
            before_status="",
            after_status="待处理",
            details={"due_date": "2026-07-18"},
        )
        store.append_audit(
            task_id="ST002",
            action="created",
            actor="王老师",
            before_status="",
            after_status="待处理",
            details={},
        )

        entries = store.list_audit(task_id="ST001")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["task_id"], "ST001")
        self.assertEqual(entries[0]["details"]["due_date"], "2026-07-18")

    def test_replace_records_and_metadata_support_demo_reset(self) -> None:
        store = PersistentStore(sqlite_path=self.db_path)
        store.upsert_record(
            "support_tasks",
            "ST001",
            {"task_id": "ST001", "status": "待处理"},
        )
        store.set_metadata("legacy_seed_version", "1")

        store.replace_records("support_tasks", [], "task_id")

        self.assertEqual(store.list_records("support_tasks"), [])
        self.assertEqual(store.get_metadata("legacy_seed_version"), "1")


if __name__ == "__main__":
    unittest.main()
