import tempfile
import unittest
from pathlib import Path

import demo_data_maintenance as maintenance
import student_management_mcp_sse as service
from persistent_store import PersistentStore


class DemoDataMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = service.STORE
        service.STORE = PersistentStore(
            sqlite_path=Path(self.temp_dir.name) / "student_management.db"
        )

    def tearDown(self) -> None:
        service.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_baseline_clears_tasks_and_restores_followups(self) -> None:
        service.create_student_support_task(
            student_query="S004",
            owner="辅导员",
            due_date="2026-07-18",
        )

        result = maintenance.reset_demo_data("baseline")

        self.assertEqual(result["data"]["support_task_count"], 0)
        self.assertEqual(result["data"]["followup_record_count"], 1)
        self.assertEqual(result["data"]["audit_entry_count"], 0)

    def test_sample_creates_two_tasks_without_notifications(self) -> None:
        result = maintenance.reset_demo_data("sample")

        self.assertEqual(result["data"]["support_task_count"], 2)
        self.assertEqual(result["data"]["support_task_status"]["待处理"], 1)
        self.assertEqual(result["data"]["support_task_status"]["跟进中"], 1)
        self.assertEqual(len(result["created_task_ids"]), 2)
        self.assertTrue(
            all(
                not task["notification"]["requested"]
                for task in service.support_tasks()
            )
        )


if __name__ == "__main__":
    unittest.main()
