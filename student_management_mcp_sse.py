from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from persistent_store import PersistentStore


BASE_DIR = Path(__file__).resolve().parent
STUDENT_FILE = BASE_DIR / "student_records.json"
FOLLOWUP_FILE = BASE_DIR / "followup_records.json"
ROSTER_FILE = BASE_DIR / "roster_students.json"
SUPPORT_TASK_FILE = BASE_DIR / "support_tasks.json"
SQLITE_FILE = BASE_DIR / ".data" / "student_management.db"

STORE = PersistentStore(sqlite_path=SQLITE_FILE)

SUPPORT_TASK_STATUSES = ("待处理", "跟进中", "已完成", "已关闭")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def students() -> list[dict[str, Any]]:
    return load_json(STUDENT_FILE)


def followups() -> list[dict[str, Any]]:
    return STORE.list_records("followups")


def roster_students() -> list[dict[str, Any]]:
    if not ROSTER_FILE.exists():
        return []
    return load_json(ROSTER_FILE)


def support_tasks() -> list[dict[str, Any]]:
    return STORE.list_records("support_tasks")


def initialize_storage_from_legacy_json() -> None:
    if STORE.get_metadata("legacy_json_seed_version"):
        return
    if not followups() and FOLLOWUP_FILE.exists():
        STORE.replace_records("followups", load_json(FOLLOWUP_FILE), "record_id")
    if not support_tasks() and SUPPORT_TASK_FILE.exists():
        STORE.replace_records("support_tasks", load_json(SUPPORT_TASK_FILE), "task_id")
    STORE.set_metadata("legacy_json_seed_version", "1")


initialize_storage_from_legacy_json()


def find_student(query: str) -> dict[str, Any]:
    query = query.strip()
    for item in students():
        if item["student_id"] == query or item["name"] == query:
            return item
    raise ValueError(f"未找到学生：{query}")


def find_roster_student(query: str) -> dict[str, Any]:
    query = query.strip()
    for item in roster_students():
        if item["student_id"] == query or item["name"] == query:
            return item
    raise ValueError(f"未在真实名册中找到学生：{query}")


def find_support_task(task_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = task_id.strip().upper()
    for item in tasks:
        if item["task_id"].upper() == normalized:
            return item
    raise ValueError(f"未找到帮扶任务：{task_id}")


def normalize_due_date(value: str) -> str:
    normalized = value.strip()
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("完成期限必须使用 YYYY-MM-DD 格式。") from exc
    return normalized


def summarize_task_status(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        status: sum(1 for item in items if item["status"] == status)
        for status in SUPPORT_TASK_STATUSES
    }


def is_task_overdue(item: dict[str, Any]) -> bool:
    return (
        item["status"] not in ("已完成", "已关闭")
        and item["due_date"] < datetime.now().strftime("%Y-%m-%d")
    )


def count_by(items: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get(field, "") or "未填写")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda row: (-row[1], row[0])))


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"钉钉机器人返回异常：HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"钉钉机器人连接失败：{exc.reason}") from exc


def resolve_dingtalk_webhook(target: str, recipient_role: str = "auto") -> tuple[str, str]:
    normalized = target.strip().lower()
    normalized_role = recipient_role.strip().lower()
    class_match = re.search(r"([a-z]\d{6})", normalized)
    class_code = class_match.group(1).upper() if class_match else ""
    class_candidates: list[tuple[str, str]] = []
    if class_code:
        class_candidates = [
            (f"{class_code}班级群", f"DINGTALK_CLASS_{class_code}_WEBHOOK"),
            (f"{class_code}班级群", f"DINGTALK_{class_code}_WEBHOOK"),
        ]

    if normalized_role in ("teacher", "counselor", "教师", "辅导员") or any(
        keyword in normalized for keyword in ("辅导员", "教师", "老师", "teacher")
    ):
        candidates = (
            ("教师工作群", "DINGTALK_TEACHER_WEBHOOK"),
            ("默认群", "DINGTALK_ROBOT_WEBHOOK"),
        )
    elif normalized_role in ("student", "学生") or class_code or any(
        keyword in normalized for keyword in ("班级群", "学生群")
    ):
        candidates = tuple(class_candidates) + (
            ("默认群", "DINGTALK_ROBOT_WEBHOOK"),
        )
    else:
        candidates = (
            ("默认群", "DINGTALK_ROBOT_WEBHOOK"),
            ("教师工作群", "DINGTALK_TEACHER_WEBHOOK"),
            ("S604124班级群", "DINGTALK_S604124_WEBHOOK"),
        )
    for channel, variable in candidates:
        webhook = os.environ.get(variable, "").strip()
        if webhook:
            return channel, webhook
    raise ValueError(f"未找到接收对象“{target}”对应的钉钉 Webhook 环境变量。")


def normalize_at_mobiles(at_mobiles: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for mobile in at_mobiles or []:
        digits = re.sub(r"\D", "", mobile)
        if "*" in mobile or len(digits) != 11:
            raise ValueError("定向提醒必须使用完整的11位手机号，脱敏号码不能用于钉钉@。")
        if digits not in normalized:
            normalized.append(digits)
    if len(normalized) > 20:
        raise ValueError("单次定向提醒最多支持20个手机号。")
    return normalized


def send_dingtalk_markdown(
    title: str,
    text: str,
    target: str,
    recipient_role: str = "auto",
    at_mobiles: list[str] | None = None,
    mention_all: bool = False,
) -> tuple[str, dict[str, Any]]:
    channel, webhook = resolve_dingtalk_webhook(target, recipient_role)
    if not webhook:
        raise ValueError("未配置钉钉机器人 Webhook 环境变量，暂不能推送钉钉消息。")
    keyword = os.environ.get("DINGTALK_KEYWORD", "MCP").strip() or "MCP"
    mobiles = normalize_at_mobiles(at_mobiles)
    mention_text = " ".join(f"@{mobile}" for mobile in mobiles)
    rendered_text = f"{text}\n\n{mention_text}" if mention_text else text
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"{keyword}｜{title}",
            "text": rendered_text,
        },
        "at": {
            "atMobiles": mobiles,
            "isAtAll": mention_all,
        },
    }
    return channel, post_json(webhook, payload)


def score_attention(item: dict[str, Any]) -> int:
    score = 0
    score += min(int(item["attendance_absences_30d"]), 5) * 2
    score += min(int(item["late_count_30d"]), 5)
    score += int(item["failed_courses"]) * 3
    score += int(item["training_log_missing"]) * 2
    score += int(item["training_issue_count"]) * 2
    if "下降" in item["gpa_trend"]:
        score += 4
    if "困难" in item["financial_status"] or "待认定" in item["financial_status"]:
        score += 2
    if "情绪" in item["dorm_feedback"] or "沟通减少" in item["dorm_feedback"]:
        score += 4
    if not item["last_followup"]:
        score += 1
    return score


def level_from_score(score: int) -> str:
    if score >= 18:
        return "高关注"
    if score >= 8:
        return "中关注"
    return "低关注"


def build_reasons(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if item["attendance_absences_30d"]:
        reasons.append(f"近30天缺勤{item['attendance_absences_30d']}次")
    if item["late_count_30d"]:
        reasons.append(f"近30天迟到{item['late_count_30d']}次")
    if item["failed_courses"]:
        reasons.append(f"不及格课程{item['failed_courses']}门")
    if item["training_log_missing"]:
        reasons.append(f"实训日志缺交{item['training_log_missing']}次")
    if item["training_issue_count"]:
        reasons.append(f"实训异常记录{item['training_issue_count']}次")
    if "下降" in item["gpa_trend"]:
        reasons.append(f"学业趋势：{item['gpa_trend']}")
    if "困难" in item["financial_status"] or "待认定" in item["financial_status"]:
        reasons.append(f"资助状态：{item['financial_status']}")
    if item["dorm_feedback"] != "正常":
        reasons.append(f"宿舍反馈：{item['dorm_feedback']}")
    return reasons or ["暂无明显异常，保持常规关注"]


def suggest_actions(item: dict[str, Any], score: int) -> list[str]:
    actions: list[str] = []
    level = level_from_score(score)
    if level == "高关注":
        actions.append("建议辅导员尽快线下谈心，核实学业、生活适应、经济支持和安全事项。")
    elif level == "中关注":
        actions.append("建议一周内完成一次关怀沟通，明确学习或实训改进任务。")
    else:
        actions.append("建议保持常规提醒，关注后续考勤和实训记录。")
    if item["failed_courses"] or "下降" in item["gpa_trend"]:
        actions.append("联系任课教师或学业导师，确认课程困难并安排学业帮扶。")
    if item["training_log_missing"] or item["training_issue_count"]:
        actions.append("联系实训指导教师，核实日志缺项和操作规范问题。")
    if "困难" in item["financial_status"] or "待认定" in item["financial_status"]:
        actions.append("核实资助状态，必要时引导学生补充资助认定或临时困难申请材料。")
    actions.append("所有结论仅作学生工作辅助，不替代处分、资助、成绩或安全责任的最终认定。")
    return actions


mcp = FastMCP(
    "学生成长画像与主动帮扶 MCP 服务",
    instructions=(
        "面向校务智枢学生管理场景，提供学生画像查询、关注名单筛选、"
        "帮扶任务闭环、记录写入、班级态势看板和钉钉通知能力。"
        "写操作和消息推送仅在用户明确要求时执行，输出仅用于学生工作辅助研判。"
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    mount_path="/",
    sse_path="/sse",
    message_path="/messages/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def get_student_profile(query: str) -> dict[str, Any]:
    """按学号或姓名查询学生成长画像，并给出关注等级和帮扶建议。"""
    item = find_student(query)
    related = [record for record in followups() if record["student_id"] == item["student_id"]]
    score = score_attention(item)
    return {
        "student": item,
        "computed_attention_score": score,
        "computed_attention_level": level_from_score(score),
        "followup_records": related,
        "suggested_actions": suggest_actions(item, score),
    }


@mcp.tool()
def list_attention_students(level: str = "") -> dict[str, Any]:
    """筛选当前需要重点关注的学生名单，可传入低关注、中关注或高关注。"""
    rows: list[dict[str, Any]] = []
    for item in students():
        score = score_attention(item)
        computed_level = level_from_score(score)
        if level and computed_level != level:
            continue
        if not level and computed_level == "低关注":
            continue
        rows.append(
            {
                "student_id": item["student_id"],
                "name": item["name"],
                "class_name": item["class_name"],
                "attention_level": computed_level,
                "score": score,
                "reasons": build_reasons(item),
            }
        )
    return {"items": sorted(rows, key=lambda row: row["score"], reverse=True)}


@mcp.tool()
def create_followup_record(
    student_id: str,
    owner: str,
    summary: str,
    next_action: str,
    status: str = "跟进中",
) -> dict[str, Any]:
    """写入一条谈心谈话或帮扶跟进记录，形成学生管理闭环。"""
    find_student(student_id)
    record = {
        "record_id": f"F{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "student_id": student_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "owner": owner,
        "summary": summary,
        "next_action": next_action,
        "status": status,
    }
    STORE.upsert_record("followups", record["record_id"], record)
    return {"created": record}


@mcp.tool()
def create_student_support_task(
    student_query: str,
    owner: str,
    due_date: str,
    requirements_confirmed: bool = False,
    created_by: str = "当前操作人",
    objective: str = "",
    measures: list[str] | None = None,
    priority: str = "自动",
    push_to_dingtalk: bool = False,
    target: str = "辅导员工作群",
    mention_all: bool = False,
) -> dict[str, Any]:
    """创建帮扶任务。仅当用户已明确提供负责人和截止日期时，requirements_confirmed 才能为 true。"""
    if not requirements_confirmed:
        return {
            "created": False,
            "blocked": True,
            "reason": "missing_user_confirmation",
            "required_user_input": ["负责人", "具体截止日期"],
            "message": (
                "尚未确认任务负责人和截止日期。请直接向用户追问这两项信息，"
                "不得根据画像、角色或当前日期自行补全，也不要重试创建。"
            ),
        }
    student = find_student(student_query)
    safe_owner = owner.strip()
    if not safe_owner:
        raise ValueError("帮扶任务必须指定负责人。")
    safe_created_by = created_by.strip() or "当前操作人"
    safe_due_date = normalize_due_date(due_date)
    score = score_attention(student)
    level = level_from_score(score)
    priority_map = {"高关注": "紧急", "中关注": "重点", "低关注": "常规"}
    safe_priority = priority.strip() or "自动"
    if safe_priority == "自动":
        safe_priority = priority_map[level]
    if safe_priority not in ("紧急", "重点", "常规"):
        raise ValueError("任务优先级仅支持：自动、紧急、重点、常规。")

    safe_objective = objective.strip() or (
        f"围绕{student['name']}当前{level}事项完成一次核实、帮扶和复访安排。"
    )
    safe_measures = [item.strip() for item in measures or [] if item.strip()]
    if not safe_measures:
        safe_measures = suggest_actions(student, score)[:3]

    now = datetime.now()
    task = {
        "task_id": f"ST{now.strftime('%Y%m%d%H%M%S%f')}",
        "student_id": student["student_id"],
        "student_name": student["name"],
        "class_name": student["class_name"],
        "attention_score": score,
        "attention_level": level,
        "risk_reasons": build_reasons(student),
        "priority": safe_priority,
        "objective": safe_objective,
        "measures": safe_measures,
        "owner": safe_owner,
        "created_by": safe_created_by,
        "due_date": safe_due_date,
        "status": "待处理",
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "completed_at": "",
        "progress_history": [],
    }
    notification: dict[str, Any] = {
        "requested": push_to_dingtalk,
        "sent": False,
        "channel": "",
    }
    if push_to_dingtalk:
        keyword = os.environ.get("DINGTALK_KEYWORD", "MCP").strip() or "MCP"
        title = f"{student['name']}{level}帮扶任务"
        reason_text = "；".join(task["risk_reasons"][:4])
        measure_text = "\n".join(
            f"{index}. {item}" for index, item in enumerate(safe_measures, start=1)
        )
        message = (
            f"## {keyword}｜帮扶任务：{title}\n\n"
            f"**任务编号：** {task['task_id']}\n\n"
            f"**学生：** {student['name']}（{student['student_id']}，{student['class_name']}）\n\n"
            f"**关注等级：** {level}（{score}分）\n\n"
            f"**主要依据：** {reason_text}\n\n"
            f"**负责人：** {safe_owner}\n\n"
            f"**完成期限：** {safe_due_date}\n\n"
            f"**帮扶措施：**\n{measure_text}\n\n"
            f"> 请在授权范围内核实处置，注意保护学生隐私，完成后更新任务进展。"
        )
        try:
            channel, result = send_dingtalk_markdown(
                title,
                message,
                target,
                "teacher",
                None,
                mention_all,
            )
            notification.update(
                {
                    "sent": result.get("errcode") == 0,
                    "channel": channel,
                    "result": result,
                }
            )
        except Exception as exc:
            notification["error"] = str(exc)

    task["notification"] = notification
    STORE.upsert_record("support_tasks", task["task_id"], task)
    STORE.append_audit(
        task_id=task["task_id"],
        action="created",
        actor=task["created_by"],
        before_status="",
        after_status=task["status"],
        details={
            "student_id": task["student_id"],
            "priority": task["priority"],
            "due_date": task["due_date"],
            "notification_requested": notification["requested"],
            "notification_sent": notification["sent"],
        },
    )
    return {"created": True, "task": task, "notification": notification}


@mcp.tool()
def update_student_support_task(
    task_id: str,
    status: str = "",
    progress_note: str = "",
    next_action: str = "",
    owner: str = "",
    due_date: str = "",
    updated_by: str = "当前操作人",
    sync_followup: bool = False,
    push_update: bool = False,
    target: str = "辅导员工作群",
    mention_all: bool = False,
) -> dict[str, Any]:
    """更新帮扶任务状态和进展；可同步写入帮扶记录并按明确要求推送进展。"""
    data = support_tasks()
    task = find_support_task(task_id, data)
    before_task = deepcopy(task)
    safe_status = status.strip()
    safe_progress = progress_note.strip()
    safe_next_action = next_action.strip()
    safe_owner = owner.strip()
    safe_due_date = due_date.strip()
    safe_updated_by = updated_by.strip() or "当前操作人"
    if not any((safe_status, safe_progress, safe_next_action, safe_owner, safe_due_date)):
        raise ValueError("请至少提供一项需要更新的任务信息。")
    if safe_status and safe_status not in SUPPORT_TASK_STATUSES:
        raise ValueError("任务状态仅支持：待处理、跟进中、已完成、已关闭。")
    if sync_followup and (not safe_progress or not safe_next_action):
        raise ValueError("同步帮扶记录时必须提供本次进展和下一步措施。")

    now = datetime.now()
    if safe_status:
        task["status"] = safe_status
        task["completed_at"] = (
            now.strftime("%Y-%m-%d %H:%M")
            if safe_status in ("已完成", "已关闭")
            else ""
        )
    if safe_owner:
        task["owner"] = safe_owner
    if safe_due_date:
        task["due_date"] = normalize_due_date(safe_due_date)
    if safe_progress or safe_next_action:
        task.setdefault("progress_history", []).append(
            {
                "recorded_at": now.strftime("%Y-%m-%d %H:%M"),
                "owner": task["owner"],
                "recorded_by": safe_updated_by,
                "progress_note": safe_progress,
                "next_action": safe_next_action,
            }
        )
    task["updated_at"] = now.strftime("%Y-%m-%d %H:%M")
    task["last_updated_by"] = safe_updated_by

    followup_record = None
    if sync_followup:
        followup_record = {
            "record_id": f"F{now.strftime('%Y%m%d%H%M%S%f')}",
            "student_id": task["student_id"],
            "created_at": now.strftime("%Y-%m-%d %H:%M"),
            "owner": task["owner"],
            "summary": safe_progress,
            "next_action": safe_next_action,
            "status": task["status"],
            "task_id": task["task_id"],
        }
        STORE.upsert_record("followups", followup_record["record_id"], followup_record)

    notification: dict[str, Any] = {
        "requested": push_update,
        "sent": False,
        "channel": "",
    }
    if push_update:
        keyword = os.environ.get("DINGTALK_KEYWORD", "MCP").strip() or "MCP"
        title = f"帮扶任务进展：{task['student_name']}"
        message = (
            f"## {keyword}｜帮扶任务进展\n\n"
            f"**任务编号：** {task['task_id']}\n\n"
            f"**学生：** {task['student_name']}（{task['student_id']}）\n\n"
            f"**当前状态：** {task['status']}\n\n"
            f"**负责人：** {task['owner']}\n\n"
            f"**本次进展：** {safe_progress or '本次仅更新任务基础信息'}\n\n"
            f"**下一步：** {safe_next_action or '按原任务计划继续推进'}\n\n"
            f"> 本消息由校务智枢记录，请在授权范围内核实使用。"
        )
        try:
            channel, result = send_dingtalk_markdown(
                title,
                message,
                target,
                "teacher",
                None,
                mention_all,
            )
            notification.update(
                {
                    "sent": result.get("errcode") == 0,
                    "channel": channel,
                    "result": result,
                }
            )
        except Exception as exc:
            notification["error"] = str(exc)

    task["last_notification"] = notification
    STORE.upsert_record("support_tasks", task["task_id"], task)
    STORE.append_audit(
        task_id=task["task_id"],
        action="updated",
        actor=safe_updated_by,
        before_status=before_task["status"],
        after_status=task["status"],
        details={
            "owner_before": before_task["owner"],
            "owner_after": task["owner"],
            "due_date_before": before_task["due_date"],
            "due_date_after": task["due_date"],
            "progress_note": safe_progress,
            "next_action": safe_next_action,
            "followup_synced": bool(followup_record),
            "notification_requested": notification["requested"],
            "notification_sent": notification["sent"],
        },
    )
    return {
        "updated": True,
        "task": task,
        "followup_record": followup_record,
        "notification": notification,
    }


@mcp.tool()
def list_student_support_tasks(
    student_query: str = "",
    status: str = "",
    owner: str = "",
    overdue_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """查询学生、负责人、状态或逾期帮扶任务，并返回任务状态统计。"""
    safe_status = status.strip()
    if safe_status and safe_status not in SUPPORT_TASK_STATUSES:
        raise ValueError("任务状态仅支持：待处理、跟进中、已完成、已关闭。")
    if limit < 1 or limit > 100:
        raise ValueError("单次查询数量必须在1到100之间。")

    rows = support_tasks()
    safe_student = student_query.strip()
    safe_owner = owner.strip()
    if safe_student:
        rows = [
            item
            for item in rows
            if safe_student in (item["student_id"], item["student_name"])
        ]
    if safe_owner:
        rows = [item for item in rows if safe_owner in item["owner"]]
    status_summary = summarize_task_status(rows)
    overdue_count = sum(1 for item in rows if is_task_overdue(item))
    if safe_status:
        rows = [item for item in rows if item["status"] == safe_status]
    if overdue_only:
        rows = [item for item in rows if is_task_overdue(item)]
    rows = sorted(
        rows,
        key=lambda item: (item["due_date"], item["created_at"]),
    )
    return {
        "total": len(rows),
        "status_summary": status_summary,
        "overdue_count": overdue_count,
        "items": rows[:limit],
        "filters": {
            "student_query": safe_student,
            "status": safe_status,
            "owner": safe_owner,
            "overdue_only": overdue_only,
        },
    }


@mcp.tool()
def get_support_task_audit(
    task_id: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """查询帮扶任务创建和状态变更审计台账，可按任务编号筛选。"""
    if limit < 1 or limit > 100:
        raise ValueError("单次审计查询数量必须在1到100之间。")
    safe_task_id = task_id.strip().upper()
    if safe_task_id:
        find_support_task(safe_task_id, support_tasks())
    items = STORE.list_audit(task_id=safe_task_id, limit=limit)
    return {
        "task_id": safe_task_id,
        "total": len(items),
        "items": items,
        "note": "审计台账只记录操作轨迹，不包含数据库连接信息或钉钉密钥。",
    }


@mcp.tool()
def get_system_readiness() -> dict[str, Any]:
    """只读检查数据存储、任务数据和钉钉渠道配置状态，不返回任何密钥。"""
    tasks = support_tasks()
    records = followups()
    class_webhook_count = sum(
        1
        for key, value in os.environ.items()
        if value.strip()
        and (
            re.fullmatch(r"DINGTALK_CLASS_[A-Z0-9]+_WEBHOOK", key)
            or re.fullmatch(r"DINGTALK_[A-Z]\d{6}_WEBHOOK", key)
        )
    )
    return {
        "ready": True,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "storage": {
            "backend": STORE.backend,
            "label": STORE.label,
            "durable_across_deploys": STORE.durable_across_deploys,
            "database_url_configured": bool(os.environ.get("DATABASE_URL", "").strip()),
        },
        "data": {
            "support_task_count": len(tasks),
            "followup_record_count": len(records),
            "audit_entry_count": STORE.count_audit(),
            "support_task_status": summarize_task_status(tasks),
            "overdue_task_count": sum(1 for task in tasks if is_task_overdue(task)),
        },
        "dingtalk": {
            "default_group_configured": bool(
                os.environ.get("DINGTALK_ROBOT_WEBHOOK", "").strip()
            ),
            "teacher_group_configured": bool(
                os.environ.get("DINGTALK_TEACHER_WEBHOOK", "").strip()
            ),
            "class_group_configured_count": class_webhook_count,
        },
        "recommendation": (
            "当前使用 PostgreSQL，任务数据可跨部署保留。"
            if STORE.durable_across_deploys
            else "当前使用本地 SQLite；在 Render 上应配置 DATABASE_URL 以实现跨部署持久化。"
        ),
    }


@mcp.tool()
def get_class_dashboard(class_name: str) -> dict[str, Any]:
    """按班级汇总关注等级、缺勤、实训异常和帮扶跟进情况。"""
    rows = [student for student in students() if student["class_name"] == class_name]
    if not rows:
        raise ValueError(f"未找到班级：{class_name}")

    levels = {"低关注": 0, "中关注": 0, "高关注": 0}
    for item in rows:
        levels[level_from_score(score_attention(item))] += 1

    student_ids = {item["student_id"] for item in rows}
    class_tasks = [
        task for task in support_tasks() if task["student_id"] in student_ids
    ]
    return {
        "class_name": class_name,
        "student_count": len(rows),
        "attention_levels": levels,
        "absence_total_30d": sum(int(item["attendance_absences_30d"]) for item in rows),
        "late_total_30d": sum(int(item["late_count_30d"]) for item in rows),
        "training_log_missing_total": sum(int(item["training_log_missing"]) for item in rows),
        "training_issue_total": sum(int(item["training_issue_count"]) for item in rows),
        "followup_count": len(
            [
                record
                for record in followups()
                if any(item["student_id"] == record["student_id"] for item in rows)
            ]
        ),
        "support_task_summary": summarize_task_status(class_tasks),
        "support_task_overdue_count": sum(
            1 for task in class_tasks if is_task_overdue(task)
        ),
        "top_attention_students": sorted(
            [
                {
                    "student_id": item["student_id"],
                    "name": item["name"],
                    "level": level_from_score(score_attention(item)),
                    "reasons": build_reasons(item),
                }
                for item in rows
            ],
            key=lambda row: {"高关注": 3, "中关注": 2, "低关注": 1}[row["level"]],
            reverse=True,
        ),
    }


@mcp.tool()
def get_roster_student(query: str) -> dict[str, Any]:
    """按学号或姓名查询真实学生名册中的基础画像。"""
    item = find_roster_student(query)
    classmates = [
        row for row in roster_students()
        if row["class_name"] == item["class_name"]
    ]
    return {
        "student": item,
        "class_size": len(classmates),
        "class_gender_distribution": count_by(classmates, "gender"),
        "note": "该结果来自演示版真实学生名册，手机号字段为已脱敏号码。",
    }


@mcp.tool()
def list_class_roster(class_name: str) -> dict[str, Any]:
    """按班级查询真实名册，返回学生列表和班级基础统计。"""
    rows = [
        item for item in roster_students()
        if item["class_name"] == class_name
    ]
    if not rows:
        raise ValueError(f"未在真实名册中找到班级：{class_name}")
    return {
        "class_name": class_name,
        "student_count": len(rows),
        "gender_distribution": count_by(rows, "gender"),
        "major_distribution": count_by(rows, "major"),
        "grade_distribution": count_by(rows, "grade"),
        "students": sorted(
            [
                {
                    "student_id": item["student_id"],
                    "name": item["name"],
                    "gender": item["gender"],
                    "major": item["major"],
                    "grade": item["grade"],
                    "phone_masked": item["phone_masked"],
                }
                for item in rows
            ],
            key=lambda row: row["student_id"],
        ),
        "note": "该结果来自演示版真实学生名册，手机号字段为已脱敏号码。",
    }


@mcp.tool()
def get_roster_dashboard(scope: str = "数字技术学院") -> dict[str, Any]:
    """生成真实名册的学院、年级、专业、班级基础分布看板。"""
    rows = roster_students()
    if scope:
        scoped = [
            item for item in rows
            if scope in item["college"]
            or scope in item["major"]
            or scope in item["grade"]
            or scope in item["class_name"]
        ]
    else:
        scoped = rows
    if not scoped:
        raise ValueError(f"未在真实名册中找到范围：{scope}")
    class_counts = count_by(scoped, "class_name")
    return {
        "scope": scope or "全部",
        "student_count": len(scoped),
        "college_distribution": count_by(scoped, "college"),
        "grade_distribution": count_by(scoped, "grade"),
        "major_distribution": count_by(scoped, "major"),
        "gender_distribution": count_by(scoped, "gender"),
        "class_distribution": class_counts,
        "top_classes": [
            {"class_name": name, "student_count": count}
            for name, count in list(class_counts.items())[:10]
        ],
        "note": "该看板基于演示版真实学生名册生成，用于院系学生基础数据核验和展示。",
    }


@mcp.tool()
def send_dingtalk_notice(
    title: str,
    content: str,
    target: str = "班级群",
    notice_type: str = "班级通知",
    recipient_role: str = "auto",
    at_mobiles: list[str] | None = None,
    mention_all: bool = False,
) -> dict[str, Any]:
    """按学生/教师/班级自动选择钉钉群；可按完整手机号定向@，或明确要求时@全体。"""
    safe_title = title.strip() or "通知"
    safe_content = content.strip()
    if not safe_content:
        raise ValueError("推送内容不能为空。")

    keyword = os.environ.get("DINGTALK_KEYWORD", "MCP").strip() or "MCP"
    message = (
        f"## {keyword}｜{notice_type}：{safe_title}\n\n"
        f"**接收对象：** {target}\n\n"
        f"{safe_content}\n\n"
        f"> 本消息由校务智枢智能体生成，请相关老师结合实际情况核对后执行。"
    )
    mobiles = normalize_at_mobiles(at_mobiles)
    channel, result = send_dingtalk_markdown(
        safe_title,
        message,
        target,
        recipient_role,
        mobiles,
        mention_all,
    )
    ok = result.get("errcode") == 0
    return {
        "sent": ok,
        "target": target,
        "channel": channel,
        "notice_type": notice_type,
        "recipient_role": recipient_role,
        "title": safe_title,
        "mention_all": mention_all,
        "mention_count": len(mobiles),
        "dingtalk_result": result,
        "note": "消息已自动包含钉钉安全关键词。" + ("已提醒全体成员。" if mention_all else ""),
    }


@mcp.tool()
def generate_and_send_class_weekly_report(
    class_name: str,
    target: str = "辅导员工作群",
    week_label: str = "本周",
    mention_all: bool = False,
) -> dict[str, Any]:
    """生成隐去学生姓名的班级工作周报并推送钉钉，适合辅导员或班委工作群。"""
    roster = [item for item in roster_students() if item["class_name"] == class_name]
    risk_rows = [item for item in students() if item["class_name"] == class_name]
    if not roster and not risk_rows:
        raise ValueError(f"未找到班级：{class_name}")

    roster_count = len(roster) if roster else len(risk_rows)
    gender_distribution = count_by(roster, "gender") if roster else {}
    major_distribution = count_by(roster, "major") if roster else count_by(risk_rows, "major")
    grade_distribution = count_by(roster, "grade") if roster else count_by(risk_rows, "grade")
    attention_levels = {"低关注": 0, "中关注": 0, "高关注": 0}
    for item in risk_rows:
        attention_levels[level_from_score(score_attention(item))] += 1

    student_ids = {item["student_id"] for item in risk_rows}
    class_followups = [record for record in followups() if record["student_id"] in student_ids]
    class_tasks = [task for task in support_tasks() if task["student_id"] in student_ids]
    task_status = summarize_task_status(class_tasks)
    overdue_tasks = sum(1 for task in class_tasks if is_task_overdue(task))
    absence_total = sum(int(item["attendance_absences_30d"]) for item in risk_rows)
    late_total = sum(int(item["late_count_30d"]) for item in risk_rows)
    training_missing = sum(int(item["training_log_missing"]) for item in risk_rows)
    training_issues = sum(int(item["training_issue_count"]) for item in risk_rows)
    keyword = os.environ.get("DINGTALK_KEYWORD", "MCP").strip() or "MCP"

    def format_distribution(data: dict[str, int]) -> str:
        return "、".join(f"{name}{count}人" for name, count in data.items()) or "暂无数据"

    dynamic_summary = (
        f"- 关注情况：高关注{attention_levels['高关注']}人、中关注{attention_levels['中关注']}人、"
        f"低关注{attention_levels['低关注']}人\n"
        f"- 近30天考勤：缺勤{absence_total}次、迟到{late_total}次\n"
        f"- 实训情况：日志缺项{training_missing}次、异常记录{training_issues}次\n"
        f"- 帮扶跟进：累计{len(class_followups)}条记录\n"
        f"- 帮扶任务：待处理{task_status['待处理']}项、跟进中{task_status['跟进中']}项、"
        f"已完成{task_status['已完成']}项、逾期{overdue_tasks}项"
        if risk_rows
        else "- 动态风险、考勤、实训及帮扶数据尚未接入该班级，本期仅展示名册基础态势"
    )
    title = f"{class_name}{week_label}学生工作周报"
    report = (
        f"## {keyword}｜班级周报：{title}\n\n"
        f"**接收对象：** {target}\n\n"
        f"### 一、班级基础态势\n"
        f"- 在册学生：{roster_count}人\n"
        f"- 性别分布：{format_distribution(gender_distribution)}\n"
        f"- 专业分布：{format_distribution(major_distribution)}\n"
        f"- 年级分布：{format_distribution(grade_distribution)}\n\n"
        f"### 二、学生工作动态\n{dynamic_summary}\n\n"
        f"### 三、下周工作建议\n"
        f"1. 核对考勤、实训日志和重点事项完成情况；\n"
        f"2. 对需关注学生开展分级沟通，敏感信息仅在授权范围内流转；\n"
        f"3. 更新帮扶记录和复访待办，形成闭环留痕。\n\n"
        f"> 本周报由校务智枢根据当前已接入数据自动生成，未展示学生姓名等敏感明细，请辅导员结合实际情况复核。"
    )
    channel, result = send_dingtalk_markdown(
        title,
        report,
        target,
        "teacher",
        None,
        mention_all,
    )
    return {
        "sent": result.get("errcode") == 0,
        "class_name": class_name,
        "target": target,
        "channel": channel,
        "week_label": week_label,
        "mention_all": mention_all,
        "summary": {
            "student_count": roster_count,
            "gender_distribution": gender_distribution,
            "major_distribution": major_distribution,
            "grade_distribution": grade_distribution,
            "attention_levels": attention_levels if risk_rows else None,
            "followup_count": len(class_followups),
            "support_task_status": task_status,
            "support_task_overdue_count": overdue_tasks,
        },
        "privacy": "公开周报未展示学生姓名和个人风险明细。",
        "dingtalk_result": result,
    }


async def health(_request: Any) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "student-growth-support-mcp",
            "storage": {
                "backend": STORE.backend,
                "durable_across_deploys": STORE.durable_across_deploys,
            },
            "counts": {
                "support_tasks": STORE.count_records("support_tasks"),
                "followups": STORE.count_records("followups"),
                "audit_entries": STORE.count_audit(),
            },
        }
    )


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/", app=mcp.sse_app()),
    ]
)


if __name__ == "__main__":
    mcp.run(transport="sse")
