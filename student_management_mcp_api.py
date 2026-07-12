from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
STUDENT_FILE = BASE_DIR / "student_records.json"
FOLLOWUP_FILE = BASE_DIR / "followup_records.json"

app = FastAPI(title="学生管理 MCP 原型服务", version="0.1.0")


class ToolCall(BaseModel):
    tool: str
    arguments: Dict[str, Any] = {}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def students() -> List[Dict[str, Any]]:
    return load_json(STUDENT_FILE)


def followups() -> List[Dict[str, Any]]:
    return load_json(FOLLOWUP_FILE)


def find_student(query: str) -> Dict[str, Any]:
    query = query.strip()
    for item in students():
        if item["student_id"] == query or item["name"] == query:
            return item
    raise HTTPException(status_code=404, detail=f"未找到学生：{query}")


def score_attention(item: Dict[str, Any]) -> int:
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


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/tools/list")
def list_tools() -> Dict[str, Any]:
    return {
        "tools": [
            {
                "name": "get_student_profile",
                "description": "按学号或姓名查询学生成长画像，汇总学业、考勤、宿舍、实训、资助和跟进情况。",
                "input_schema": {"query": "学生学号或姓名"}
            },
            {
                "name": "list_attention_students",
                "description": "筛选当前需要重点关注的学生名单，并给出关注原因。",
                "input_schema": {"level": "可选：低关注/中关注/高关注，默认返回中关注和高关注"}
            },
            {
                "name": "create_followup_record",
                "description": "写入一条谈心谈话或帮扶跟进记录，形成学生管理闭环。",
                "input_schema": {
                    "student_id": "学生学号",
                    "owner": "记录人或角色",
                    "summary": "跟进摘要",
                    "next_action": "下一步动作",
                    "status": "状态，如待跟进/跟进中/已完成"
                }
            },
            {
                "name": "get_class_dashboard",
                "description": "按班级汇总关注等级、缺勤、日志缺项、实训异常和帮扶跟进情况。",
                "input_schema": {"class_name": "班级名称"}
            }
        ]
    }


@app.post("/tools/call")
def call_tool(call: ToolCall) -> Dict[str, Any]:
    if call.tool == "get_student_profile":
        query = str(call.arguments.get("query", ""))
        item = find_student(query)
        related = [f for f in followups() if f["student_id"] == item["student_id"]]
        score = score_attention(item)
        return {
            "student": item,
            "computed_attention_score": score,
            "computed_attention_level": level_from_score(score),
            "followup_records": related,
            "suggested_actions": suggest_actions(item, score)
        }

    if call.tool == "list_attention_students":
        target = call.arguments.get("level")
        rows = []
        for item in students():
            score = score_attention(item)
            level = level_from_score(score)
            if target and level != target:
                continue
            if not target and level == "低关注":
                continue
            rows.append({
                "student_id": item["student_id"],
                "name": item["name"],
                "class_name": item["class_name"],
                "attention_level": level,
                "score": score,
                "reasons": build_reasons(item)
            })
        return {"items": sorted(rows, key=lambda x: x["score"], reverse=True)}

    if call.tool == "create_followup_record":
        args = call.arguments
        student_id = str(args.get("student_id", ""))
        find_student(student_id)
        data = followups()
        record = {
            "record_id": f"F{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "student_id": student_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "owner": str(args.get("owner", "学生工作负责人")),
            "summary": str(args.get("summary", "")),
            "next_action": str(args.get("next_action", "")),
            "status": str(args.get("status", "跟进中"))
        }
        data.append(record)
        save_json(FOLLOWUP_FILE, data)
        return {"created": record}

    if call.tool == "get_class_dashboard":
        class_name = str(call.arguments.get("class_name", ""))
        rows = [s for s in students() if s["class_name"] == class_name]
        if not rows:
            raise HTTPException(status_code=404, detail=f"未找到班级：{class_name}")
        levels = {"低关注": 0, "中关注": 0, "高关注": 0}
        for item in rows:
            levels[level_from_score(score_attention(item))] += 1
        return {
            "class_name": class_name,
            "student_count": len(rows),
            "attention_levels": levels,
            "absence_total_30d": sum(int(s["attendance_absences_30d"]) for s in rows),
            "late_total_30d": sum(int(s["late_count_30d"]) for s in rows),
            "training_log_missing_total": sum(int(s["training_log_missing"]) for s in rows),
            "training_issue_total": sum(int(s["training_issue_count"]) for s in rows),
            "followup_count": len([f for f in followups() if any(s["student_id"] == f["student_id"] for s in rows)]),
            "top_attention_students": sorted(
                [
                    {
                        "student_id": s["student_id"],
                        "name": s["name"],
                        "level": level_from_score(score_attention(s)),
                        "reasons": build_reasons(s)
                    }
                    for s in rows
                ],
                key=lambda x: {"高关注": 3, "中关注": 2, "低关注": 1}[x["level"]],
                reverse=True
            )
        }

    raise HTTPException(status_code=400, detail=f"未知工具：{call.tool}")


def build_reasons(item: Dict[str, Any]) -> List[str]:
    reasons = []
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


def suggest_actions(item: Dict[str, Any], score: int) -> List[str]:
    actions = []
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
