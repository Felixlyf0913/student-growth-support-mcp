from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


BASE_DIR = Path(__file__).resolve().parent
STUDENT_FILE = BASE_DIR / "student_records.json"
FOLLOWUP_FILE = BASE_DIR / "followup_records.json"
ROSTER_FILE = BASE_DIR / "roster_students.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def students() -> list[dict[str, Any]]:
    return load_json(STUDENT_FILE)


def followups() -> list[dict[str, Any]]:
    return load_json(FOLLOWUP_FILE)


def roster_students() -> list[dict[str, Any]]:
    if not ROSTER_FILE.exists():
        return []
    return load_json(ROSTER_FILE)


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


def send_dingtalk_markdown(title: str, text: str, mention_all: bool = False) -> dict[str, Any]:
    webhook = os.environ.get("DINGTALK_ROBOT_WEBHOOK", "").strip()
    if not webhook:
        raise ValueError("未配置 DINGTALK_ROBOT_WEBHOOK 环境变量，暂不能推送钉钉消息。")
    keyword = os.environ.get("DINGTALK_KEYWORD", "MCP").strip() or "MCP"
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"{keyword}｜{title}",
            "text": text,
        },
        "at": {
            "isAtAll": mention_all,
        },
    }
    return post_json(webhook, payload)


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
        "面向校策通枢学生管理场景，提供学生画像查询、关注名单筛选、"
        "帮扶记录写入和班级态势看板能力。输出仅用于学生工作辅助研判。"
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
    data = followups()
    record = {
        "record_id": f"F{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "student_id": student_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "owner": owner,
        "summary": summary,
        "next_action": next_action,
        "status": status,
    }
    data.append(record)
    save_json(FOLLOWUP_FILE, data)
    return {"created": record}


@mcp.tool()
def get_class_dashboard(class_name: str) -> dict[str, Any]:
    """按班级汇总关注等级、缺勤、实训异常和帮扶跟进情况。"""
    rows = [student for student in students() if student["class_name"] == class_name]
    if not rows:
        raise ValueError(f"未找到班级：{class_name}")

    levels = {"低关注": 0, "中关注": 0, "高关注": 0}
    for item in rows:
        levels[level_from_score(score_attention(item))] += 1

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
    mention_all: bool = False,
) -> dict[str, Any]:
    """把通知推送到钉钉群；用户明确要求时可通过 mention_all 提醒全体成员。"""
    safe_title = title.strip() or "通知"
    safe_content = content.strip()
    if not safe_content:
        raise ValueError("推送内容不能为空。")

    keyword = os.environ.get("DINGTALK_KEYWORD", "MCP").strip() or "MCP"
    message = (
        f"## {keyword}｜{notice_type}：{safe_title}\n\n"
        f"**接收对象：** {target}\n\n"
        f"{safe_content}\n\n"
        f"> 本消息由校策通枢智能体生成，请相关老师结合实际情况核对后执行。"
    )
    result = send_dingtalk_markdown(safe_title, message, mention_all)
    ok = result.get("errcode") == 0
    return {
        "sent": ok,
        "target": target,
        "notice_type": notice_type,
        "title": safe_title,
        "mention_all": mention_all,
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
        f"- 帮扶跟进：累计{len(class_followups)}条记录"
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
        f"> 本周报由校策通枢根据当前已接入数据自动生成，未展示学生姓名等敏感明细，请辅导员结合实际情况复核。"
    )
    result = send_dingtalk_markdown(title, report, mention_all)
    return {
        "sent": result.get("errcode") == 0,
        "class_name": class_name,
        "target": target,
        "week_label": week_label,
        "mention_all": mention_all,
        "summary": {
            "student_count": roster_count,
            "gender_distribution": gender_distribution,
            "major_distribution": major_distribution,
            "grade_distribution": grade_distribution,
            "attention_levels": attention_levels if risk_rows else None,
            "followup_count": len(class_followups),
        },
        "privacy": "公开周报未展示学生姓名和个人风险明细。",
        "dingtalk_result": result,
    }


async def health(_request: Any) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "student-growth-support-mcp"})


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/", app=mcp.sse_app()),
    ]
)


if __name__ == "__main__":
    mcp.run(transport="sse")
