import tempfile
import unittest
from pathlib import Path

import student_management_mcp_sse as service
from demo_source_pipeline import import_source_documents
from persistent_store import PersistentStore


class TrainingRoomAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = service.STORE
        service.STORE = PersistentStore(
            sqlite_path=Path(self.temp_dir.name) / "student_management.db"
        )
        import_source_documents(
            service.STORE, Path(__file__).resolve().parent / "演示业务源文件"
        )

    def tearDown(self) -> None:
        service.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_lists_available_rooms_for_requested_period(self) -> None:
        result = service.list_available_training_rooms(
            date="2026-08-17",
            start_time="14:00",
            end_time="16:00",
            min_capacity=40,
        )

        available_ids = {item["room_id"] for item in result["available_rooms"]}
        self.assertEqual(result["available_room_count"], 3)
        self.assertSetEqual(available_ids, {"TR-302", "TR-205", "TR-306"})
        self.assertNotIn("TR-118", available_ids)

        all_capacities = service.list_available_training_rooms(
            date="2026-08-17",
            start_time="14:00",
            end_time="16:00",
        )
        occupied_ids = {item["room_id"] for item in all_capacities["occupied_rooms"]}
        self.assertIn("TR-118", occupied_ids)

    def test_specific_room_reports_booking_conflict(self) -> None:
        result = service.get_training_room_availability(
            room_query="118",
            date="2026-08-17",
            start_time="14:00",
            end_time="16:00",
        )

        self.assertEqual(result["availability"], "已占用")
        self.assertEqual(result["confirmed_conflicts"][0]["booking_id"], "BK-20260817-02")


if __name__ == "__main__":
    unittest.main()
