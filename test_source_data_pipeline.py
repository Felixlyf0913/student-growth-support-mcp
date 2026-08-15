import tempfile
import unittest
from pathlib import Path

from demo_source_pipeline import SOURCE_VERSION, import_source_documents, next_followup_reminders
from persistent_store import PersistentStore


class SourceDataPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PersistentStore(
            sqlite_path=Path(self.temp_dir.name) / "student_management.db"
        )
        self.source_dir = Path(__file__).resolve().parent / "演示业务源文件"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_import_reads_office_ledgers_and_builds_profiles(self) -> None:
        result = import_source_documents(self.store, self.source_dir)

        self.assertEqual(result["source_version"], SOURCE_VERSION)
        self.assertEqual(result["documents"], 7)
        self.assertEqual(result["roster_students"], 199)
        self.assertEqual(self.store.count_records("student_profiles"), 199)
        self.assertEqual(self.store.count_records("risk_alerts"), 5)
        self.assertEqual(self.store.count_records("training_rooms"), 5)
        self.assertEqual(self.store.count_records("training_room_schedules"), 10)
        self.assertEqual(self.store.count_records("training_room_equipment"), 8)
        self.assertEqual(self.store.count_records("training_room_safety_and_loans"), 6)
        profile = next(
            item
            for item in self.store.list_records("student_profiles")
            if item["student_id"] == "60412403"
        )
        self.assertEqual(profile["attendance_absences_30d"], 5)
        self.assertEqual(profile["training_log_missing"], 3)
        self.assertIn("筛查建议", profile["dorm_feedback"])

    def test_imported_talk_ledger_generates_near_term_reminders(self) -> None:
        import_source_documents(self.store, self.source_dir)

        reminders = next_followup_reminders(self.store, days=14)

        self.assertTrue(any(item["student_id"] == "60412403" for item in reminders))
        self.assertTrue(any(item["next_followup_date"] == "2026-08-18" for item in reminders))


if __name__ == "__main__":
    unittest.main()
