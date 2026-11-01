"""脱敏与字段权限。

- 脱敏：模型调用前处理敏感信息（手机号、身份证、统一社会信用代码、邮箱、
  详细门牌等），保留可核对的掩码，避免把客户身份信息送到外部服务。
- 字段权限：后端按角色控制字段可见性与操作权限。专业确认阻断项只能由
  具备相应权限的角色解除，业务人员不能自行解除。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# --- 脱敏规则 ---
REDACTION_RULES = (
    (re.compile(r'(?<!\d)(1[3-9]\d)[ -]?\d{4}[ -]?\d{4}(?!\d)'), r'\1****\1'),
    (re.compile(r'(?<!\d)(\d{3})\*{0,2}\d{4}(\d{4}[\dXx]{0,4})(?!\d)'), r'\1********\2'),
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), '***@***'),
    (re.compile(r'(?<![0-9A-Za-z])[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}(?![0-9A-Za-z])'), '统一社会信用代码已脱敏'),
    (re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)'), '身份证号已脱敏'),
    (re.compile(r'(?<!\d)\d{16,19}(?!\d)'), '卡号已脱敏'),
    (re.compile(r'(\d{3,4}-)?\d{7,8}(?!\d)'), '电话已脱敏'),
)

#: 角色 -> 允许执行的动作。admin 用 '*' 表示全部。
DEFAULT_ROLE_PERMISSIONS = {
    'business': {'card.create', 'card.edit', 'card.submit', 'card.confirm_business',
                 'space.import', 'space.view', 'match.run', 'report.export'},
    'engineer': {'card.view', 'card.edit', 'card.confirm_professional', 'card.comment',
                 'space.import', 'space.view', 'space.verify', 'match.run', 'report.export'},
    'reviewer': {'card.view', 'card.edit', 'card.submit', 'card.review',
                 'card.confirm_professional', 'space.view', 'space.verify',
                 'match.run', 'report.export'},
    'admin': {'*'},
}

ROLE_LABELS = {
    'business': '业务（招商）',
    'engineer': '工程',
    'reviewer': '复核',
    'admin': '管理员',
}

#: 只有这些角色可以解除专业确认阻断项
PROFESSIONAL_ROLES = ('engineer', 'reviewer', 'admin')


class PermissionError_(ValueError):
    """权限不足。"""


def redact(text: str) -> str:
    """对文本做脱敏。"""
    if not isinstance(text, str) or not text:
        return ''
    result = text
    for pattern, replacement in REDACTION_RULES:
        result = pattern.sub(replacement, result)
    return result


def redact_payload(payload: Any) -> Any:
    """递归脱敏结构化数据中的字符串。"""
    if isinstance(payload, str):
        return redact(payload)
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, dict):
        return {key: redact_payload(value) for key, value in payload.items()}
    return payload


def load_role_permissions(config: Dict[str, Any]) -> Dict[str, set]:
    """从配置读取角色权限，缺省回落到内置定义。"""
    roles = (config or {}).get('roles') or {}
    permissions: Dict[str, set] = {}
    for role, spec in roles.items():
        can = spec.get('can') or []
        permissions[role] = {'*'} if '*' in can else set(can)
    for role, can in DEFAULT_ROLE_PERMISSIONS.items():
        permissions.setdefault(role, set(can))
    return permissions


def has_permission(role: str, action: str, permissions: Optional[Dict[str, set]] = None) -> bool:
    """角色是否具备某动作权限。"""
    permissions = permissions or load_role_permissions(None)
    allowed = permissions.get(role)
    if not allowed:
        return False
    if '*' in allowed:
        return True
    return action in allowed


def require_permission(role: str, action: str, permissions: Optional[Dict[str, set]] = None) -> None:
    """无权限则抛错。"""
    if not has_permission(role, action, permissions):
        raise PermissionError_(f'角色"{ROLE_LABELS.get(role, role)}"无权限执行：{action}')


def can_clear_professional(role: str, config: Dict[str, Any]) -> bool:
    """该角色是否可以解除专业确认阻断项。"""
    roles = (config or {}).get('roles') or {}
    spec = roles.get(role) or {}
    if 'cannot_clear_professional' in spec:
        return not spec['cannot_clear_professional']
    return role in PROFESSIONAL_ROLES


def require_professional_clearing(role: str, config: Dict[str, Any]) -> None:
    """业务人员不得自行解除专业审核阻断。"""
    if not can_clear_professional(role, config):
        raise PermissionError_('专业确认阻断项只能由工程或复核角色解除，业务人员不能自行解除')


def visible_fields(role: str, fields: List[str], config: Dict[str, Any]) -> List[str]:
    """按角色过滤可见字段。"""
    masked = set((config or {}).get('redaction', {}).get('masked_for_business') or [])
    if role != 'business':
        return list(fields)
    return [f for f in fields if f not in masked]


def mask_value(role: str, field: str, value: Any, config: Dict[str, Any]) -> Any:
    """业务角色看到脱敏后的敏感字段值。"""
    masked = set((config or {}).get('redaction', {}).get('masked_for_business') or [])
    if role == 'business' and field in masked and isinstance(value, str) and value:
        return redact(value)
    return value
