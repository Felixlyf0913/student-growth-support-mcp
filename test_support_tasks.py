import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import student_management_mcp_sse as service
from persistent_store import PersistentStore


class SupportTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_store = service.STORE
        service.STORE = PersistentStore(sqlite_path=root / "student_management.db")
        service.STORE.replace_records("support_tasks", [], "task_id")
        service.STORE.replace_records("followups", [], "record_id")

    def tearDown(self) -> None:
        service.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_create_task_uses_profile_and_does_not_push_by_default(self) -> None:
        with patch.object(service, "send_dingtalk_markdown") as sender:
            result = service.create_student_support_task(
                student_query="S004",
                owner="辅导员",
                due_date="2026-07-18",
                created_by="张老师",
            )

        self.assertFalse(result["notification"]["requested"])
        sender.assert_not_called()
        task = result["task"]
        self.assertEqual(task["student_name"], "陈雨欣")
        self.assertEqual(task["attention_level"], "高关注")
        self.assertEqual(task["status"], "待处理")
        self.assertEqual(task["priority"], "紧急")
        self.assertEqual(task["created_by"], "张老师")
        self.assertTrue(task["measures"])
        saved = service.support_tasks()
        self.assertEqual(saved[0]["task_id"], task["task_id"])
        audit = service.STORE.list_audit(task_id=task["task_id"])
        self.assertEqual(audit[0]["action"], "created")
        self.assertEqual(audit[0]["actor"], "张老师")

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
            updated_by="李老师",
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
        audit = service.get_support_task_audit(created["task_id"])
        self.assertEqual(audit["items"][0]["after_status"], "已完成")
        self.assertEqual(audit["items"][0]["actor"], "李老师")

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

    def test_readiness_does_not_expose_secrets(self) -> None:
        with patch.dict(
            service.os.environ,
            {
                "DINGTALK_TEACHER_WEBHOOK": "https://example.test/secret",
                "DINGTALK_S604124_WEBHOOK": "https://example.test/class-secret",
            },
            clear=False,
        ):
            result = service.get_system_readiness()

        self.assertEqual(result["storage"]["backend"], "sqlite")
        self.assertTrue(result["dingtalk"]["teacher_group_configured"])
        self.assertNotIn("example.test", str(result))


if __name__ == "__main__":
    unittest.main()
