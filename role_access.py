"""比赛演示用的角色核验与最小权限控制。

正式部署时可将 verify_role_access 的账号核验替换为学校统一身份认证回调；
当前实现只使用不含真实账号密码的演示身份，便于录屏验证授权边界。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


DEMO_CODE = os.environ.get("ROLE_DEMO_VERIFICATION_CODE", "CAMPUS-DEMO-2026")
TOKEN_SECRET = os.environ.get("ROLE_ACCESS_SIGNING_SECRET", "demo-role-access-secret")
TOKEN_TTL_SECONDS = 30 * 60

DEMO_IDENTITIES: dict[str, dict[str, Any]] = {
    "S004-DEMO": {
        "role": "student",
        "display_role": "学生",
        "display_name": "学生演示账号",
        "student_id": "S004",
        "allowed_classes": [],
        "capabilities": ["校内政策咨询", "本人基础画像", "实训日志填写", "宿舍报修引导"],
    },
    "ST-60412403": {
        "role": "student",
        "display_role": "学生",
        "display_name": "S604124移动班学生演示账号",
        "student_id": "60412403",
        "allowed_classes": [],
        "capabilities": ["校内政策咨询", "本人基础画像", "本人跟进记录", "实训日志填写", "宿舍报修引导"],
    },
    "HT-S604124": {
        "role": "head_teacher",
        "display_role": "班主任",
        "display_name": "S604124移动班班主任演示账号",
        "student_id": "",
        "allowed_classes": ["S604124移动"],
        "capabilities": ["本班态势", "本班学生画像", "班级通知", "学风与实训督导"],
    },
    "CO-DIGITAL": {
        "role": "counselor",
        "display_role": "辅导员",
        "display_name": "数字技术学院辅导员演示账号",
        "student_id": "",
        "allowed_classes": ["P603124数媒", "P603223数媒", "S603323数媒", "S604124移动", "W602325网络", "W602425网络"],
        "capabilities": ["学生画像", "关注名单", "帮扶任务", "班级周报", "协同触达"],
    },
    "AD-DIGITAL": {
        "role": "administrator",
        "display_role": "行政管理人员",
        "display_name": "学生工作管理演示账号",
        "student_id": "",
        "allowed_classes": ["*"],
        "capabilities": ["匿名班级看板", "工作周报", "制度与流程咨询", "运行状态查看"],
    },
    "OP-TRAINING": {
        "role": "service_staff",
        "display_role": "实训室管理人员",
        "display_name": "实训中心演示账号",
        "student_id": "",
        "allowed_classes": ["*"],
        "capabilities": ["场地空闲查询", "设备报修", "安全巡检", "借还待办"],
    },
}


ROLE_DEMO_ACCOUNTS = {
    "学生": "ST-60412403",
    "班主任": "HT-S604124",
    "辅导员": "CO-DIGITAL",
    "行政人员": "AD-DIGITAL",
}


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(TOKEN_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _decode(token: str) -> dict[str, Any]:
    try:
        encoded, signature = token.strip().split(".", 1)
    except ValueError as exc:
        raise ValueError("角色令牌格式无效，请重新完成身份核验。") from exc
    expected = hmac.new(TOKEN_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("角色令牌校验失败，请重新完成身份核验。")
    padded = encoded + "=" * (-len(encoded) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("角色令牌已过期，请重新完成身份核验。")
    return payload


def verify_demo_identity(account_id: str, verification_code: str) -> dict[str, Any]:
    identity = DEMO_IDENTITIES.get(account_id.strip().upper())
    if not identity or not hmac.compare_digest(verification_code.strip(), DEMO_CODE):
        return {
            "verified": False,
            "message": "账号或演示验证码不正确。正式上线时应接入学校统一身份认证，不使用演示验证码。",
        }
    payload = {
        "account_id": account_id.strip().upper(),
        "role": identity["role"],
        "display_role": identity["display_role"],
        "display_name": identity["display_name"],
        "student_id": identity["student_id"],
        "allowed_classes": identity["allowed_classes"],
        "capabilities": identity["capabilities"],
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    return {
        "verified": True,
        "session_token": _encode(payload),
        "expires_in_minutes": TOKEN_TTL_SECONDS // 60,
        "identity": {key: value for key, value in payload.items() if key not in {"exp"}},
        "note": "当前为比赛演示身份核验。生产环境请将账号校验接入学校统一身份认证，并通过平台第三方认证地址回调。",
    }


def start_demo_role_session(role_name: str) -> dict[str, Any]:
    """Create a short-lived demo session for one of the four portal roles."""
    account_id = ROLE_DEMO_ACCOUNTS.get(role_name.strip())
    if not account_id:
        return {
            "verified": False,
            "message": "仅支持学生、班主任、辅导员、行政人员四类演示角色。",
        }
    return verify_demo_identity(account_id, DEMO_CODE)


def require_role_session(
    session_token: str,
    allowed_roles: tuple[str, ...],
    target_student_id: str = "",
    target_class_name: str = "",
) -> dict[str, Any]:
    payload = _decode(session_token)
    role = payload.get("role", "")
    if role not in allowed_roles:
        raise PermissionError(f"当前角色“{payload.get('display_role', '未知')}”无权执行该操作。")
    if role == "student" and target_student_id and payload.get("student_id") != target_student_id:
        raise PermissionError("学生角色只能查看本人信息，不能查询其他学生的治理数据。")
    allowed_classes = set(payload.get("allowed_classes", []))
    if target_class_name and "*" not in allowed_classes and allowed_classes and target_class_name not in allowed_classes:
        raise PermissionError("当前账号不在该班级的数据授权范围内。")
    return payload
