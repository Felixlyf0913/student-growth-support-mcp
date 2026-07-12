from __future__ import annotations

import json
import os
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


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def students() -> list[dict[str, Any]]:
    return load_json(STUDENT_FILE)


def followups() -> list[dict[str, Any]]:
    return load_json(FOLLOWUP_FILE)


def find_student(query: str) -> dict[str, Any]:
    query = query.strip()
    for item in students():
        if item["student_id"] == query or item["name"] == query:
            return item
    raise ValueError(f"未找到学生：{query}")


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
