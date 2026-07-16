from __future__ import annotations

import argparse
from datetime import date, timedelta

import student_management_mcp_sse as service


CONFIRMATION = "RESET-DEMO-DATA"


def reset_demo_data(mode: str) -> dict[str, object]:
    baseline_followups = (
        service.load_json(service.FOLLOWUP_FILE)
        if service.FOLLOWUP_FILE.exists()
        else []
    )
    service.STORE.replace_records("support_tasks", [], "task_id")
    service.STORE.replace_records("followups", baseline_followups, "record_id")
    service.STORE.clear_audit()

    created_tasks: list[str] = []
    if mode == "sample":
        first = service.create_student_support_task(
            student_query="S004",
            owner="辅导员",
            due_date=(date.today() + timedelta(days=3)).isoformat(),
            requirements_confirmed=True,
            push_to_dingtalk=False,
        )["task"]
        second = service.create_student_support_task(
            student_query="S002",
            owner="王老师",
            due_date=(date.today() + timedelta(days=5)).isoformat(),
            requirements_confirmed=True,
            push_to_dingtalk=False,
        )["task"]
        service.update_student_support_task(
            task_id=second["task_id"],
            status="跟进中",
            progress_note="已完成首次联系，待进一步核实课程与实训情况。",
            next_action="两日内完成线下谈心并补充记录。",
            sync_followup=False,
            push_update=False,
        )
        created_tasks = [first["task_id"], second["task_id"]]

    readiness = service.get_system_readiness()
    return {
        "mode": mode,
        "storage": readiness["storage"],
        "data": readiness["data"],
        "created_task_ids": created_tasks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="恢复校务智枢比赛演示数据。该脚本不会发送钉钉消息。"
    )
    parser.add_argument(
        "--mode",
        choices=("baseline", "sample"),
        default="baseline",
        help="baseline 清空任务并恢复基础跟进记录；sample 额外生成两条演示任务。",
    )
    parser.add_argument(
        "--confirm",
        required=True,
        help=f"必须输入 {CONFIRMATION} 才会执行。",
    )
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        raise SystemExit("确认文本不正确，未修改任何数据。")
    print(reset_demo_data(args.mode))


if __name__ == "__main__":
    main()
