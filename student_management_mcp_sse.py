from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from calendar import monthrange
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from demo_source_pipeline import (
    SOURCE_LABEL,
    SOURCE_VERSION,
    import_source_documents,
    next_followup_reminders,
)
from persistent_store import PersistentStore
from role_access import (
    require_role_session,
    start_demo_role_session as start_role_demo_session,
    verify_demo_identity,
)


BASE_DIR = Path(__file__).resolve().parent
STUDENT_FILE = BASE_DIR / "student_records.json"
FOLLOWUP_FILE = BASE_DIR / "followup_records.json"
ROSTER_FILE = BASE_DIR / "roster_students.json"
SUPPORT_TASK_FILE = BASE_DIR / "support_tasks.json"
TRAINING_ROOM_FILE = BASE_DIR / "training_room_records.json"
SOURCE_DOCUMENT_DIR = BASE_DIR / "演示业务源文件"
SQLITE_FILE = BASE_DIR / ".data" / "student_management.db"

STORE = PersistentStore(sqlite_path=SQLITE_FILE)

SUPPORT_TASK_STATUSES = ("待处理", "跟进中", "已完成", "已关闭")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def students() -> list[dict[str, Any]]:
    imported = STORE.list_records("student_profiles")
    if imported:
        # Dynamic profiles take precedence, while legacy synthetic cases remain
        # available for the explicit crisis-routing demonstration.
        imported_ids = {item["student_id"] for item in imported}
        return imported + [
            item for item in load_json(STUDENT_FILE)
            if item["student_id"] not in imported_ids
        ]
    return load_json(STUDENT_FILE)


def followups() -> list[dict[str, Any]]:
    return STORE.list_records("followups")


def roster_students() -> list[dict[str, Any]]:
    imported = STORE.list_records("roster_students")
    if imported:
        return imported
    if not ROSTER_FILE.exists():
        return []
    return load_json(ROSTER_FILE)


def support_tasks() -> list[dict[str, Any]]:
    return STORE.list_records("support_tasks")


def training_room_records() -> dict[str, list[dict[str, Any]]]:
    imported_rooms = STORE.list_records("training_rooms")
    if imported_rooms:
        return {
            "rooms": imported_rooms,
            "schedules": STORE.list_records("training_room_schedules"),
            "equipment": STORE.list_records("training_room_equipment"),
            "safety_and_loans": STORE.list_records("training_room_safety_and_loans"),
        }
    if not TRAINING_ROOM_FILE.exists():
        return {"rooms": [], "schedules": [], "equipment": [], "safety_and_loans": []}
    return load_json(TRAINING_ROOM_FILE)


def training_room_data_note() -> str:
    return (
        "班级和学生基础信息来自授权演示名册；实训室课表、设备、报修、巡检和借用记录为比赛演示台账，"
        "仅用于功能展示与辅助研判，需由授权人员结合正式业务系统复核。"
    )


def find_training_room(query: str) -> dict[str, Any]:
    normalized = re.sub(r"\s+", "", query.strip().lower())
    for room in training_room_records()["rooms"]:
        candidates = (
            room["room_id"],
            room["name"],
            room["room_number"],
            f'{room["building"]}{room["room_number"]}',
        )
        if any(normalized == re.sub(r"\s+", "", value.lower()) for value in candidates):
            return room
    raise ValueError(f"未找到实训室：{query}")


def time_to_minutes(value: str) -> int:
    normalized = value.strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized):
        raise ValueError("时间必须使用 HH:MM 格式，例如 14:00。")
    hour, minute = normalized.split(":")
    return int(hour) * 60 + int(minute)


def room_availability_details(
    room: dict[str, Any],
    date: str,
    requested_start: int,
    requested_end: int,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str]:
    """Return the booking state for one room without exposing a write operation."""
    bookings = [
        item
        for item in training_room_records()["schedules"]
        if item["room_id"] == room["room_id"] and item["date"] == date
    ]
    conflicts: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for booking in bookings:
        overlap = (
            requested_start < time_to_minutes(booking["end_time"])
            and requested_end > time_to_minutes(booking["start_time"])
        )
        if not overlap:
            continue
        if booking["approval"] == "已通过":
            conflicts.append(booking)
        else:
            pending.append(booking)
    if conflicts:
        return (
            "已占用",
            conflicts,
            pending,
            "该时段已有已通过排课或预约，建议更换时段或联系实训中心管理员协调。",
        )
    if pending:
        return (
            "待确认",
            conflicts,
            pending,
            "该时段存在待审核预约，建议先核实审批结果后再安排使用。",
        )
    return (
        "可预约",
        conflicts,
        pending,
        "当前演示台账未发现冲突记录，正式预约仍需按学校场地审批流程办理。",
    )


def initialize_storage_from_legacy_json() -> None:
    if STORE.get_metadata("legacy_json_seed_version"):
        return
    if not followups() and FOLLOWUP_FILE.exists():
        STORE.replace_records("followups", load_json(FOLLOWUP_FILE), "record_id")
    if not support_tasks() and SUPPORT_TASK_FILE.exists():
        STORE.replace_records("support_tasks", load_json(SUPPORT_TASK_FILE), "task_id")
    STORE.set_metadata("legacy_json_seed_version", "1")


initialize_storage_from_legacy_json()


def initialize_source_document_import() -> None:
    """Seed the durable store from packaged office ledgers on first deployment."""
    if STORE.get_metadata("source_document_seed_version") == SOURCE_VERSION:
        return
    import_source_documents(STORE, SOURCE_DOCUMENT_DIR)


initialize_source_document_import()


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
    raise ValueError(f"未在比赛演示名册中找到学生：{query}")


def resolve_class_name(class_name: str) -> str:
    """兼容自然语言中的班级后缀，如“S604124移动班”。"""
    normalized = re.sub(r"\s+", "", class_name.strip())
    class_names = {
        item["class_name"] for item in students()
    } | {
        item["class_name"] for item in roster_students()
    }
    if normalized in class_names:
        return normalized
    if normalized.endswith("班") and normalized[:-1] in class_names:
        return normalized[:-1]
    return normalized


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


def normalize_optional_date(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field_name}必须使用 YYYY-MM-DD 格式。") from exc
    return normalized


def add_months(base: datetime, months: int) -> str:
    target_month = base.month - 1 + months
    year = base.year + target_month // 12
    month = target_month % 12 + 1
    day = min(base.day, monthrange(year, month)[1])
    return f"{year:04d}-{month:02d}-{day:02d}"


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


CRISIS_SIGNAL_KEYWORDS = (
    "自伤",
    "伤害自己",
    "不想活",
    "轻生",
    "伤人",
    "暴力威胁",
    "连续失联",
)


def has_crisis_signal(item: dict[str, Any]) -> bool:
    """Only explicit safety signals enter the urgent offline-verification lane."""
    text = " ".join(
        str(item.get(field, ""))
        for field in ("dorm_feedback", "notes")
    )
    return any(keyword in text for keyword in CRISIS_SIGNAL_KEYWORDS)


def attention_level_for(item: dict[str, Any]) -> str:
    if has_crisis_signal(item):
        return "需立即线下核实"
    return level_from_score(score_attention(item))


def build_reasons(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if has_crisis_signal(item):
        reasons.append("出现需立即线下核实的安全信号（仅作分流提醒，非心理诊断）")
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
    level = attention_level_for(item)
    if level == "需立即线下核实":
        actions.append(
            "建议立即线下核实学生安全与实际情况，按学校应急流程联系学院学生工作负责人、心理健康教育中心或保卫部门；必要时由学校按制度联络监护人或属地应急资源。"
        )
        actions.append("系统仅作安全分流提醒，不作心理诊断，不替代专业评估和线下处置。")
    elif level == "高关注":
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
def verify_role_access(account_id: str, verification_code: str) -> dict[str, Any]:
    """核验比赛演示身份并签发30分钟角色令牌。账号示例：S004-DEMO、HT-S604124、CO-DIGITAL、AD-DIGITAL、OP-TRAINING。正式环境应对接学校统一身份认证。"""
    return verify_demo_identity(account_id, verification_code)


@mcp.tool()
def start_demo_role_session(role_name: str) -> dict[str, Any]:
    """录屏门户已选择角色时，自动完成学生、班主任、辅导员或行政人员的演示核验，并返回短时会话令牌。正式上线应替换为学校统一身份认证。"""
    return start_role_demo_session(role_name)


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
    """向钉钉班级群或教师工作群发送已确认的通知；可明确@全体或按完整手机号定向@。"""
    return _send_dingtalk_notice_implementation(
        title,
        content,
        target,
        notice_type,
        recipient_role,
        at_mobiles,
        mention_all,
    )


@mcp.tool()
def get_role_access_status(session_token: str) -> dict[str, Any]:
    """查询当前已核验角色、数据范围和可用能力；不要向最终用户展示完整令牌。"""
    session = require_role_session(
        session_token,
        ("student", "head_teacher", "counselor", "administrator", "service_staff"),
    )
    return {
        "verified": True,
        "display_role": session["display_role"],
        "display_name": session["display_name"],
        "allowed_classes": session["allowed_classes"],
        "capabilities": session["capabilities"],
        "session_valid": True,
        "note": "敏感治理数据仅在已核验角色和授权范围内展示；正式上线应使用学校统一身份认证。",
    }


@mcp.tool()
def get_authorized_student_profile(session_token: str, query: str) -> dict[str, Any]:
    """按已核验角色查询学生画像。学生仅可查本人；班主任限本班；辅导员限授权班级；行政人员不返回个人风险明细。"""
    student = find_student(query)
    session = require_role_session(
        session_token,
        ("student", "head_teacher", "counselor", "administrator"),
        target_student_id=student["student_id"],
        target_class_name=student["class_name"],
    )
    if session["role"] == "administrator":
        return {
            "access": "aggregate_only",
            "message": "行政管理人员默认仅可查看匿名班级聚合态势，不展示学生个人风险画像。请改为查询班级匿名看板。",
        }
    result = get_student_profile(query)
    result["access"] = {"role": session["display_role"], "scope": "本人" if session["role"] == "student" else "授权班级"}
    return result


@mcp.tool()
def get_my_learning_and_training_status(session_token: str) -> dict[str, Any]:
    """学生查询本人近30天考勤、实训日志、个人待办和可执行建议；不会展示管理侧风险等级、筛查或其他学生信息。"""
    session = require_role_session(session_token, ("student",))
    student_id = session.get("student_id", "")
    if not student_id:
        raise ValueError("当前学生演示账号未绑定学生信息，请重新完成身份核验。")

    profile = get_student_profile(student_id)
    student = profile["student"]
    training_records = [
        item for item in STORE.list_records("training_operation_records")
        if item.get("学号") == student_id
    ]
    related_tasks = [
        item for item in support_tasks()
        if item.get("student_id") == student_id and item.get("status") != "已关闭"
    ]

    to_do: list[str] = []
    if int(student.get("training_log_missing", 0) or 0) > 0:
        to_do.append(f"补齐 {student['training_log_missing']} 份实训日志，并按实训指导教师要求完成规范提交。")
    if int(student.get("attendance_absences_30d", 0) or 0) > 0:
        to_do.append("核对近期考勤记录；如存在误记，请按学校流程及时提交说明。")
    if int(student.get("failed_courses", 0) or 0) > 0:
        to_do.append("联系任课教师或学业导师，确认补考、重修或学习辅导安排。")
    if not to_do:
        to_do.append("当前没有待处理的考勤或实训事项，请继续保持学习和实训记录完整。")

    return {
        "student": {
            "student_id": student["student_id"],
            "name": student["name"],
            "college": student["college"],
            "major": student["major"],
            "class_name": student["class_name"],
        },
        "attendance_30d": {
            "absence_count": student.get("attendance_absences_30d", 0),
            "late_count": student.get("late_count_30d", 0),
            "academic_trend": student.get("gpa_trend", "未同步"),
            "failed_course_count": student.get("failed_courses", 0),
        },
        "training": {
            "missing_log_count": student.get("training_log_missing", 0),
            "operation_issue_count": student.get("training_issue_count", 0),
            "latest_records": training_records[:3],
        },
        "personal_to_do": to_do,
        "support_progress": [
            {
                "task_id": item.get("task_id", ""),
                "status": item.get("status", ""),
                "next_action": item.get("next_action", ""),
                "due_date": item.get("due_date", ""),
            }
            for item in related_tasks
        ],
        "privacy": "当前仅返回已核验学生本人的学习、实训和个人待办，不展示管理侧风险标签、筛查详情或其他学生信息。",
        "data_note": "考勤和实训数据由班主任、辅导员或实训指导教师维护的比赛演示业务台账导入 PostgreSQL；正式环境应对接教务、考勤和实训系统。",
    }


@mcp.tool()
def get_authorized_class_dashboard(session_token: str, class_name: str) -> dict[str, Any]:
    """按已核验角色查看班级态势。班主任和辅导员限授权班级；行政人员默认返回匿名聚合数据。"""
    normalized_class = resolve_class_name(class_name)
    session = require_role_session(
        session_token,
        ("head_teacher", "counselor", "administrator"),
        target_class_name=normalized_class,
    )
    result = get_class_dashboard(normalized_class)
    if session["role"] == "administrator":
        result["top_attention_students"] = [
            {"level": item["level"], "reasons": item["reasons"]}
            for item in result["top_attention_students"]
        ]
        result["privacy"] = "行政视图已做匿名化处理，不展示学生姓名和学号。"
    result["access"] = {"role": session["display_role"], "scope": "授权班级"}
    return result


@mcp.tool()
def get_authorized_student_growth_timeline(session_token: str, query: str) -> dict[str, Any]:
    """按已核验角色查询学生业务时间线。学生仅本人；班主任和辅导员限授权班级；行政人员不返回个人明细。"""
    student = find_student(query)
    session = require_role_session(
        session_token,
        ("student", "head_teacher", "counselor", "administrator"),
        target_student_id=student["student_id"],
        target_class_name=student["class_name"],
    )
    if session["role"] == "administrator":
        return {
            "access": "aggregate_only",
            "message": "行政管理人员默认仅查看匿名班级聚合态势，不展示个人考勤、筛查、谈话或帮扶时间线。",
        }
    result = get_student_growth_timeline(query)
    result["access"] = {"role": session["display_role"], "scope": "本人" if session["role"] == "student" else "授权班级"}
    return result


@mcp.tool()
def get_authorized_class_operational_records(session_token: str, class_name: str) -> dict[str, Any]:
    """按已核验角色查询班级动态台账。班主任和辅导员限授权班级；行政人员仅返回匿名聚合态势。"""
    normalized_class = resolve_class_name(class_name)
    session = require_role_session(
        session_token,
        ("head_teacher", "counselor", "administrator"),
        target_class_name=normalized_class,
    )
    if session["role"] == "administrator":
        dashboard = get_authorized_class_dashboard(session_token, normalized_class)
        return {
            "access": "aggregate_only",
            "dashboard": dashboard,
            "message": "行政视图已做匿名化处理，不展示学生个人考勤、筛查、谈话和任务明细。",
        }
    result = get_class_operational_records(normalized_class)
    result["access"] = {"role": session["display_role"], "scope": "授权班级"}
    return result


@mcp.tool()
def list_authorized_followup_reminders(session_token: str, days: int = 14) -> dict[str, Any]:
    """按已核验角色查看复访提醒。班主任仅本班，辅导员仅授权班级。"""
    session = require_role_session(session_token, ("head_teacher", "counselor"))
    result = list_followup_reminders(days)
    allowed = set(session["allowed_classes"])
    result["items"] = [
        item for item in result["items"]
        if "*" in allowed or item["class_name"] in allowed
    ]
    result["total"] = len(result["items"])
    result["overdue_count"] = sum(1 for item in result["items"] if item["status"] == "已逾期")
    result["access"] = {"role": session["display_role"], "scope": "授权班级"}
    return result


@mcp.tool()
def get_student_profile(query: str) -> dict[str, Any]:
    """按学号或姓名查询学生成长画像；优先使用已从演示业务台账导入 PostgreSQL 的动态数据。"""
    try:
        item = find_student(query)
    except ValueError:
        roster_item = find_roster_student(query)
        return {
            "student": {
                "student_id": str(roster_item["student_id"]),
                "name": roster_item["name"],
                "grade": roster_item["grade"],
                "major": roster_item["major"],
                "class_name": roster_item["class_name"],
                "college": roster_item["college"],
            },
            "computed_attention_score": None,
            "computed_attention_level": "未评估",
            "followup_records": [],
            "suggested_actions": [
                "当前仅接入该生的比赛演示名册基础信息，尚未接入考勤、成绩、实训和帮扶指标。",
                "如需开展治理研判，请由授权人员补充或同步对应业务台账后再查询。",
            ],
            "data_note": "该结果来自比赛演示名册，不对真实个人作风险判断或帮扶任务建议。",
        }
    related = [record for record in followups() if record["student_id"] == item["student_id"]]
    score = score_attention(item)
    return {
        "student": item,
        "computed_attention_score": score,
        "computed_attention_level": attention_level_for(item),
        "followup_records": related,
        "suggested_actions": suggest_actions(item, score),
        "data_note": (
            "基础名册、考勤与学业、实训、筛查和谈话记录均来自项目文件夹的比赛演示模拟业务台账，"
            "已导入 PostgreSQL；预警只用于提示核实与帮扶分流，不构成诊断或最终认定。"
        ),
    }


@mcp.tool()
def get_data_ingestion_status() -> dict[str, Any]:
    """查询项目文件夹业务台账的入库状态、文件清单和 PostgreSQL 记录数量，不返回连接信息。"""
    manifests = STORE.list_records("source_documents")
    return {
        "source_label": SOURCE_LABEL,
        "source_version": STORE.get_metadata("source_document_seed_version"),
        "storage_backend": STORE.label,
        "documents": manifests,
        "record_counts": {
            "roster_students": STORE.count_records("roster_students"),
            "attendance_records": STORE.count_records("attendance_records"),
            "training_operation_records": STORE.count_records("training_operation_records"),
            "psychological_screenings": STORE.count_records("psychological_screenings"),
            "followups": STORE.count_records("followups"),
            "support_tasks": STORE.count_records("support_tasks"),
            "risk_alerts": STORE.count_records("risk_alerts"),
            "training_rooms": STORE.count_records("training_rooms"),
            "training_room_schedules": STORE.count_records("training_room_schedules"),
            "training_room_equipment": STORE.count_records("training_room_equipment"),
            "training_room_safety_and_loans": STORE.count_records("training_room_safety_and_loans"),
        },
        "workflow": [
            "工作人员维护 Excel、Word、PDF 业务台账",
            "服务自动解析并规范化入库 PostgreSQL",
            "智能体按角色范围检索班级、学生、任务和提醒",
            "谈心与任务更新通过 MCP 回写形成闭环",
        ],
        "note": "全部为比赛演示模拟数据；正式上线应通过学校 OA、教务、实训和统一身份认证接口同步。",
    }


@mcp.tool()
def get_student_growth_timeline(query: str) -> dict[str, Any]:
    """查询某学生的考勤、实训、心理筛查建议、谈话、任务和预警时间线；适用于辅导员或班主任的个案研判。"""
    profile = get_student_profile(query)
    student = profile["student"]
    student_id = student["student_id"]
    attendance = [item for item in STORE.list_records("attendance_records") if item.get("学号") == student_id]
    training = [item for item in STORE.list_records("training_operation_records") if item.get("学号") == student_id]
    screenings = [item for item in STORE.list_records("psychological_screenings") if item.get("学号") == student_id]
    tasks = [item for item in support_tasks() if item.get("student_id") == student_id]
    alerts = [item for item in STORE.list_records("risk_alerts") if item.get("student_id") == student_id]
    timeline: list[dict[str, Any]] = []
    for item in attendance:
        timeline.append({"date": item.get("统计周期", ""), "type": "考勤与学业", "detail": item})
    for item in training:
        timeline.append({"date": item.get("记录日期", ""), "type": "实训记录", "detail": item})
    for item in screenings:
        timeline.append({"date": item.get("筛查日期", ""), "type": "心理筛查建议", "detail": item})
    for item in profile["followup_records"]:
        timeline.append({"date": item.get("created_at", ""), "type": "谈心/跟进", "detail": item})
    for item in tasks:
        timeline.append({"date": item.get("updated_at", item.get("created_at", "")), "type": "帮扶任务", "detail": item})
    return {
        "student": student,
        "attention": {
            "score": profile["computed_attention_score"],
            "level": profile["computed_attention_level"],
            "reasons": build_reasons(student),
        },
        "alerts": alerts,
        "timeline": sorted(timeline, key=lambda item: item["date"], reverse=True),
        "suggested_actions": profile["suggested_actions"],
        "data_note": profile.get("data_note", "比赛演示模拟数据。"),
    }


@mcp.tool()
def get_class_operational_records(class_name: str) -> dict[str, Any]:
    """汇总某班的考勤、实训、筛查建议、谈心和帮扶任务动态；适用于班主任和辅导员查看班级治理台账。"""
    normalized_class = resolve_class_name(class_name)
    dashboard = get_class_dashboard(normalized_class)
    student_ids = {item["student_id"] for item in students() if item["class_name"] == normalized_class}
    if not student_ids:
        student_ids = {item["student_id"] for item in roster_students() if item["class_name"] == normalized_class}
    return {
        "dashboard": dashboard,
        "attendance_records": [item for item in STORE.list_records("attendance_records") if item.get("学号") in student_ids],
        "training_records": [item for item in STORE.list_records("training_operation_records") if item.get("学号") in student_ids],
        "screening_suggestions": [item for item in STORE.list_records("psychological_screenings") if item.get("学号") in student_ids],
        "followup_records": [item for item in followups() if item.get("student_id") in student_ids],
        "support_tasks": [item for item in support_tasks() if item.get("student_id") in student_ids],
        "active_alerts": [item for item in STORE.list_records("risk_alerts") if item.get("student_id") in student_ids],
        "data_note": "班级动态由项目文件夹中的比赛演示台账入库生成；个人信息应仅在已核验、授权范围内使用。",
    }


@mcp.tool()
def list_followup_reminders(days: int = 14) -> dict[str, Any]:
    """查询未来指定天数内即将到期或已逾期的谈心复访提醒，默认14天；适用于辅导员日常待办。"""
    reminders = next_followup_reminders(STORE, days)
    return {
        "days": days,
        "total": len(reminders),
        "overdue_count": sum(1 for item in reminders if item["status"] == "已逾期"),
        "items": reminders,
        "note": "提醒基于比赛演示模拟谈话台账生成，正式环境应按学校工作制度和授权范围执行。",
    }


@mcp.tool()
def list_attention_students(level: str = "") -> dict[str, Any]:
    """筛选当前需要重点关注的学生名单，可传入低关注、中关注或高关注。"""
    rows: list[dict[str, Any]] = []
    for item in students():
        score = score_attention(item)
        computed_level = attention_level_for(item)
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
    next_followup_date: str = "",
    followup_cycle_months: int = 0,
) -> dict[str, Any]:
    """写入谈心谈话或帮扶跟进记录；可指定下次复访日期或按月设置复访周期，形成提醒闭环。"""
    find_student(student_id)
    if followup_cycle_months < 0 or followup_cycle_months > 24:
        raise ValueError("复访周期月数必须在0到24之间。")
    safe_next_date = normalize_optional_date(next_followup_date, "下次复访日期")
    if safe_next_date and followup_cycle_months:
        raise ValueError("请二选一：指定下次复访日期，或填写复访周期月数。")
    if followup_cycle_months:
        safe_next_date = add_months(datetime.now(), followup_cycle_months)
    record = {
        "record_id": f"F{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "student_id": student_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "owner": owner,
        "summary": summary,
        "next_action": next_action,
        "status": status,
        "next_followup_date": safe_next_date,
        "followup_cycle_months": followup_cycle_months,
        "data_label": "比赛演示模拟数据" if student_id.isdigit() else "演示治理样本",
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
    level = attention_level_for(student)
    priority_map = {"需立即线下核实": "紧急", "高关注": "紧急", "中关注": "重点", "低关注": "常规"}
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
            "source_document_count": STORE.count_records("source_documents"),
            "imported_roster_count": STORE.count_records("roster_students"),
            "attendance_record_count": STORE.count_records("attendance_records"),
            "training_record_count": STORE.count_records("training_operation_records"),
            "screening_record_count": STORE.count_records("psychological_screenings"),
            "risk_alert_count": STORE.count_records("risk_alerts"),
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
            "当前使用 PostgreSQL，源台账入库、任务、谈话和审计数据可跨部署保留。"
            if STORE.durable_across_deploys
            else "当前使用本地 SQLite；在 Render 上应配置 DATABASE_URL 以实现跨部署持久化。"
        ),
    }


@mcp.tool()
def get_class_dashboard(class_name: str) -> dict[str, Any]:
    """按班级汇总关注等级、缺勤、实训异常和帮扶跟进情况。"""
    class_name = resolve_class_name(class_name)
    rows = [student for student in students() if student["class_name"] == class_name]
    roster_rows = [student for student in roster_students() if student["class_name"] == class_name]
    if not rows and not roster_rows:
        raise ValueError(f"未找到班级：{class_name}")

    levels = {"需立即线下核实": 0, "高关注": 0, "中关注": 0, "低关注": 0}
    for item in rows:
        levels[attention_level_for(item)] += 1

    # 班级规模优先采用授权演示名册；未纳入治理样本的学生按常规关注统计。
    roster_count = len(roster_rows)
    student_count = roster_count or len(rows)
    if roster_count > len(rows):
        levels["低关注"] += roster_count - len(rows)

    student_ids = {item["student_id"] for item in rows}
    class_tasks = [
        task for task in support_tasks() if task["student_id"] in student_ids
    ]
    return {
        "class_name": class_name,
        "student_count": student_count,
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
                    "level": attention_level_for(item),
                    "reasons": build_reasons(item),
                }
                for item in rows
            ],
            key=lambda row: {"需立即线下核实": 4, "高关注": 3, "中关注": 2, "低关注": 1}[row["level"]],
            reverse=True,
        ),
        "data_note": (
            "班级人数、专业与姓名信息来自授权演示名册；关注、考勤和实训指标来自比赛录屏用的学生治理演示台账，"
            "仅用于辅助研判和功能展示，需由授权人员结合业务记录复核。"
            if roster_rows
            else "该班级数据来自比赛演示治理样本，仅用于辅助研判和功能展示。"
        ),
    }


@mcp.tool()
def get_roster_student(query: str) -> dict[str, Any]:
    """按学号或姓名查询比赛演示名册中的基础画像。"""
    item = find_roster_student(query)
    classmates = [
        row for row in roster_students()
        if row["class_name"] == item["class_name"]
    ]
    return {
        "student": item,
        "class_size": len(classmates),
        "class_gender_distribution": count_by(classmates, "gender"),
        "note": "该结果来自比赛演示虚拟名册，手机号字段为已脱敏号码。",
    }


@mcp.tool()
def list_class_roster(class_name: str) -> dict[str, Any]:
    """按班级查询比赛演示名册，返回学生列表和班级基础统计。"""
    class_name = resolve_class_name(class_name)
    rows = [
        item for item in roster_students()
        if item["class_name"] == class_name
    ]
    if not rows:
        raise ValueError(f"未在比赛演示名册中找到班级：{class_name}")
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
        "note": "该结果来自比赛演示虚拟名册，手机号字段为已脱敏号码。",
    }


@mcp.tool()
def get_roster_dashboard(scope: str = "数字技术学院") -> dict[str, Any]:
    """生成比赛演示名册的学院、年级、专业、班级基础分布看板。"""
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


def _send_dingtalk_notice_implementation(
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
    push_to_dingtalk: bool = False,
    mention_all: bool = False,
) -> dict[str, Any]:
    """生成匿名班级工作周报；仅在明确要求时推送钉钉，适合辅导员或班委工作群。"""
    class_name = resolve_class_name(class_name)
    roster = [item for item in roster_students() if item["class_name"] == class_name]
    risk_rows = [item for item in students() if item["class_name"] == class_name]
    if not roster and not risk_rows:
        raise ValueError(f"未找到班级：{class_name}")

    roster_count = len(roster) if roster else len(risk_rows)
    gender_distribution = count_by(roster, "gender") if roster else {}
    major_distribution = count_by(roster, "major") if roster else count_by(risk_rows, "major")
    grade_distribution = count_by(roster, "grade") if roster else count_by(risk_rows, "grade")
    attention_levels = {"需立即线下核实": 0, "高关注": 0, "中关注": 0, "低关注": 0}
    for item in risk_rows:
        attention_levels[attention_level_for(item)] += 1
    if roster_count > len(risk_rows):
        attention_levels["低关注"] += roster_count - len(risk_rows)

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
        f"- 关注情况：需立即线下核实{attention_levels['需立即线下核实']}人、高关注{attention_levels['高关注']}人、中关注{attention_levels['中关注']}人、"
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
    notification: dict[str, Any] = {
        "requested": push_to_dingtalk,
        "sent": False,
        "channel": "",
        "dingtalk_result": {},
    }
    if push_to_dingtalk:
        channel, result = send_dingtalk_markdown(
            title,
            report,
            target,
            "teacher",
            None,
            mention_all,
        )
        notification.update(
            {
                "sent": result.get("errcode") == 0,
                "channel": channel,
                "dingtalk_result": result,
            }
        )
    return {
        "sent": notification["sent"],
        "push_requested": push_to_dingtalk,
        "class_name": class_name,
        "target": target,
        "channel": notification["channel"],
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
        "report": report,
        "notification": notification,
    }


@mcp.tool()
def get_training_room_availability(
    room_query: str,
    date: str,
    start_time: str,
    end_time: str,
) -> dict[str, Any]:
    """查询指定实训室在某日某时段是否可预约。room_query 可填 302、TR-302 或实训室名称；时间使用 HH:MM。"""
    try:
        datetime.strptime(date.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD 格式，例如 2026-08-19。") from exc
    requested_start = time_to_minutes(start_time)
    requested_end = time_to_minutes(end_time)
    if requested_start >= requested_end:
        raise ValueError("结束时间必须晚于开始时间。")

    room = find_training_room(room_query)
    availability, conflicts, pending, recommendation = room_availability_details(
        room, date.strip(), requested_start, requested_end
    )
    bookings = [
        item
        for item in training_room_records()["schedules"]
        if item["room_id"] == room["room_id"] and item["date"] == date.strip()
    ]

    return {
        "room": room,
        "query_date": date.strip(),
        "requested_time": f"{start_time.strip()}-{end_time.strip()}",
        "availability": availability,
        "confirmed_conflicts": conflicts,
        "pending_bookings": pending,
        "day_bookings": bookings,
        "recommendation": recommendation,
        "data_note": training_room_data_note(),
    }


@mcp.tool()
def list_available_training_rooms(
    date: str,
    start_time: str,
    end_time: str,
    min_capacity: int = 0,
    equipment_keyword: str = "",
) -> dict[str, Any]:
    """查询某日某时段可用实训室。学生或教师问“哪个实训室有空”“有没有能容纳40人的实训室”“某时段哪里可预约”时调用。可按最小容量和设备关键词筛选。"""
    try:
        datetime.strptime(date.strip(), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD 格式，例如 2026-08-19。") from exc
    requested_start = time_to_minutes(start_time)
    requested_end = time_to_minutes(end_time)
    if requested_start >= requested_end:
        raise ValueError("结束时间必须晚于开始时间。")
    if min_capacity < 0:
        raise ValueError("最小容量不能小于 0。")

    keyword = re.sub(r"\s+", "", equipment_keyword.lower())
    available_rooms: list[dict[str, Any]] = []
    pending_rooms: list[dict[str, Any]] = []
    occupied_rooms: list[dict[str, Any]] = []
    for room in training_room_records()["rooms"]:
        if room.get("status") not in ("正常开放", "部分开放"):
            continue
        if int(room.get("capacity", 0)) < min_capacity:
            continue
        searchable = re.sub(
            r"\s+", "", f"{room['name']} {room['equipment_summary']}".lower()
        )
        if keyword and keyword not in searchable:
            continue
        availability, conflicts, pending, recommendation = room_availability_details(
            room, date.strip(), requested_start, requested_end
        )
        item = {
            "room_id": room["room_id"],
            "room_name": room["name"],
            "location": f"{room['building']}{room['room_number']}",
            "capacity": room["capacity"],
            "equipment_summary": room["equipment_summary"],
            "open_hours": room["open_hours"],
            "availability": availability,
            "conflicting_bookings": conflicts,
            "pending_bookings": pending,
            "recommendation": recommendation,
        }
        if availability == "可预约":
            available_rooms.append(item)
        elif availability == "待确认":
            pending_rooms.append(item)
        else:
            occupied_rooms.append(item)
    if not available_rooms and not pending_rooms and not occupied_rooms:
        raise ValueError("未找到符合容量、设备或开放状态条件的实训室。")
    return {
        "query_date": date.strip(),
        "requested_time": f"{start_time.strip()}-{end_time.strip()}",
        "filters": {
            "min_capacity": min_capacity or "不限",
            "equipment_keyword": equipment_keyword or "不限",
        },
        "available_room_count": len(available_rooms),
        "available_rooms": available_rooms,
        "pending_room_count": len(pending_rooms),
        "pending_rooms": pending_rooms,
        "occupied_room_count": len(occupied_rooms),
        "occupied_rooms": occupied_rooms,
        "recommendation": "结果基于比赛演示台账；选择场地后，仍需按学校正式预约与审批流程办理。",
        "data_note": training_room_data_note(),
    }


@mcp.tool()
def get_class_training_schedule(
    class_name: str,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """查询班级实训安排。班级名称可填写 S604124移动班；日期可选，使用 YYYY-MM-DD。"""
    class_name = resolve_class_name(class_name)
    records = training_room_records()
    rooms = {item["room_id"]: item for item in records["rooms"]}
    schedules = [
        item for item in records["schedules"] if item["class_name"] == class_name
    ]
    if start_date:
        schedules = [item for item in schedules if item["date"] >= start_date.strip()]
    if end_date:
        schedules = [item for item in schedules if item["date"] <= end_date.strip()]
    if not schedules:
        raise ValueError(f"未找到班级“{class_name}”在指定范围内的实训安排。")
    enriched = [
        {
            **item,
            "room_name": rooms[item["room_id"]]["name"],
            "room_location": f'{rooms[item["room_id"]]["building"]}{rooms[item["room_id"]]["room_number"]}',
        }
        for item in sorted(schedules, key=lambda row: (row["date"], row["start_time"]))
    ]
    return {
        "class_name": class_name,
        "schedule_count": len(enriched),
        "schedules": enriched,
        "recommendation": "建议实训指导教师在每次实训前核验设备状态，并在结束后确认日志提交与设备归还情况。",
        "data_note": training_room_data_note(),
    }


@mcp.tool()
def get_equipment_repair_status(query: str = "") -> dict[str, Any]:
    """查询实训设备状态与报修工单。可按设备名称、设备编号、报修工单号或实训室编号查询；留空返回全部未正常设备。"""
    records = training_room_records()
    rooms = {item["room_id"]: item for item in records["rooms"]}
    normalized = re.sub(r"\s+", "", query.strip().lower())
    rows: list[dict[str, Any]] = []
    for item in records["equipment"]:
        searchable = " ".join(
            str(item.get(field, ""))
            for field in ("equipment_id", "name", "ticket_id", "room_id", "asset_status")
        ).lower()
        if normalized and normalized not in re.sub(r"\s+", "", searchable):
            continue
        if not normalized and item["asset_status"] == "在用":
            continue
        rows.append(
            {
                **item,
                "room_name": rooms[item["room_id"]]["name"],
                "room_location": f'{rooms[item["room_id"]]["building"]}{rooms[item["room_id"]]["room_number"]}',
            }
        )
    if not rows:
        raise ValueError(f"未找到与“{query}”匹配的设备或报修工单。")
    return {
        "query": query or "全部异常设备",
        "equipment_count": len(rows),
        "equipment": rows,
        "recommendation": "设备维修中、待检修或停用时，应在正式场地排课前由实训中心确认替代设备或调整安排。",
        "data_note": training_room_data_note(),
    }


@mcp.tool()
def generate_training_room_safety_todos(room_query: str = "") -> dict[str, Any]:
    """生成实训室安全巡检、整改与设备归还待办。可选指定 302、TR-302 或实训室名称。"""
    records = training_room_records()
    rooms = {item["room_id"]: item for item in records["rooms"]}
    room_id = find_training_room(room_query)["room_id"] if room_query.strip() else ""
    active_records = [
        item
        for item in records["safety_and_loans"]
        if (not room_id or item["room_id"] == room_id)
        and item["status"] not in ("已整改", "已归还")
    ]
    todos = [
        {
            "todo_id": item["record_id"],
            "type": item["record_type"],
            "room_name": rooms[item["room_id"]]["name"],
            "subject": item["subject"],
            "detail": item["detail"],
            "status": item["status"],
            "due_date": item["due_date"],
            "responsible_role": item["responsible_role"],
            "suggested_action": item["action"],
        }
        for item in sorted(active_records, key=lambda row: (row["due_date"], row["record_type"]))
    ]
    return {
        "scope": rooms[room_id]["name"] if room_id else "全部实训室",
        "todo_count": len(todos),
        "todos": todos,
        "recommendation": "请按截止时间核验整改、归还或设备检查结果；需要推送提醒时，应由用户明确指定接收群或接收对象。",
        "data_note": training_room_data_note(),
    }


@mcp.tool()
def get_class_training_operations_overview(
    class_name: str,
    start_date: str = "",
    end_date: str = "",
) -> dict[str, Any]:
    """班级实训运行概览。用户问某班实训安排、实训室安全待办、设备归还或异常设备时优先调用；可选日期范围。"""
    schedule_result = get_class_training_schedule(class_name, start_date, end_date)
    todo_result = generate_training_room_safety_todos()
    equipment_result = get_equipment_repair_status()
    return {
        "class_name": schedule_result["class_name"],
        "training_schedule": schedule_result["schedules"],
        "active_safety_and_return_todos": todo_result["todos"],
        "abnormal_equipment": equipment_result["equipment"],
        "recommendation": (
            "建议按实训日期先核验场地和异常设备，再完成安全整改与借用设备归还；"
            "涉及排课调整或设备停用时，应由实训中心管理员复核。"
        ),
        "data_note": training_room_data_note(),
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
                "source_documents": STORE.count_records("source_documents"),
                "student_profiles": STORE.count_records("student_profiles"),
                "risk_alerts": STORE.count_records("risk_alerts"),
                "training_rooms": STORE.count_records("training_rooms"),
                "training_room_schedules": STORE.count_records("training_room_schedules"),
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
