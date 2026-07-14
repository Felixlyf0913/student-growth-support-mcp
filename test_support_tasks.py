import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import student_management_mcp_sse as service


class SupportTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_task_file = service.SUPPORT_TASK_FILE
        self.original_followup_file = service.FOLLOWUP_FILE
        service.SUPPORT_TASK_FILE = root / "support_tasks.json"
        service.FOLLOWUP_FILE = root / "followup_records.json"
        service.save_json(service.SUPPORT_TASK_FILE, [])
        service.save_json(service.FOLLOWUP_FILE, [])

    def tearDown(self) -> None:
        service.SUPPORT_TASK_FILE = self.original_task_file
        service.FOLLOWUP_FILE = self.original_followup_file
        self.temp_dir.cleanup()

    def test_create_task_uses_profile_and_does_not_push_by_default(self) -> None:
        with patch.object(service, "send_dingtalk_markdown") as sender:
            result = service.create_student_support_task(
                student_query="S004",
                owner="辅导员",
                due_date="2026-07-18",
            )

        self.assertFalse(result["notification"]["requested"])
        sender.assert_not_called()
        task = result["task"]
        self.assertEqual(task["student_name"], "陈雨欣")
        self.assertEqual(task["attention_level"], "高关注")
        self.assertEqual(task["status"], "待处理")
        self.assertEqual(task["priority"], "紧急")
        self.assertTrue(task["measures"])
        saved = json.loads(service.SUPPORT_TASK_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["task_id"], task["task_id"])

    def test_create_task_pushes_only_when_explicitly_requested(self) -> None:
        with patch.object(
            service,
            "send_dingtalk_markdown",
            return_value=("教师工作群", {"errcode": 0, "errmsg": "ok"}),
        ) as sender:
            result = service.create_student_support_task(
                student_query="S004",
                owner="辅导员",
                due_date="2026-07-18",
                push_to_dingtalk=True,
            )

        self.assertTrue(result["notification"]["sent"])
        self.assertEqual(result["notification"]["channel"], "教师工作群")
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[3], "teacher")

    def test_update_completed_task_can_sync_followup_record(self) -> None:
        created = service.create_student_support_task(
            student_query="S004",
            owner="辅导员",
            due_date="2026-07-18",
        )["task"]

        result = service.update_student_support_task(
            task_id=created["task_id"],
            status="已完成",
            progress_note="已完成线下谈心，学生反馈课程压力较大。",
            next_action="联系任课教师并于一周后复访。",
            sync_followup=True,
        )

        task = result["task"]
        self.assertEqual(task["status"], "已完成")
        self.assertTrue(task["completed_at"])
        self.assertEqual(len(task["progress_history"]), 1)
        records = service.followups()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["student_id"], "S004")
        self.assertEqual(records[0]["status"], "已完成")

    def test_list_tasks_supports_overdue_and_status_summary(self) -> None:
        service.create_student_support_task(
            student_query="S002",
            owner="王老师",
            due_date="2000-01-01",
        )
        completed = service.create_student_support_task(
            student_query="S004",
            owner="王老师",
            due_date="2000-01-01",
        )["task"]
        service.update_student_support_task(
            task_id=completed["task_id"],
            status="已完成",
            progress_note="已完成帮扶。",
            next_action="保持常规关注。",
        )

        result = service.list_student_support_tasks(
            owner="王老师",
            overdue_only=True,
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["student_id"], "S002")
        self.assertEqual(result["status_summary"]["待处理"], 1)
        self.assertEqual(result["status_summary"]["已完成"], 1)

    def test_class_dashboard_and_weekly_report_include_task_summary(self) -> None:
        service.create_student_support_task(
            student_query="S004",
            owner="辅导员",
            due_date="2026-07-18",
        )

        dashboard = service.get_class_dashboard("电商2403")
        self.assertEqual(dashboard["support_task_summary"]["待处理"], 1)

        with patch.object(
            service,
            "send_dingtalk_markdown",
            return_value=("教师工作群", {"errcode": 0, "errmsg": "ok"}),
        ):
            report = service.generate_and_send_class_weekly_report("电商2403")

        self.assertEqual(report["summary"]["support_task_status"]["待处理"], 1)


if __name__ == "__main__":
    unittest.main()
