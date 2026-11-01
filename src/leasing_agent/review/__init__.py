"""退回复核闭环：接受、修改等级、修改解释、驳回、升级。

五种动作参考报告的人工作业定义。每次动作都必须留痕：
操作者、角色、时间、理由、引用版本、最终处理内容。
引用版本用于回答"当时交接的是哪一版需求"。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 五种复核动作
ACTION_ACCEPT = 'accept'            # 接受
ACTION_CHANGE_LEVEL = 'change_level'  # 修改等级
ACTION_CHANGE_EXPLANATION = 'change_explanation'  # 修改解释
ACTION_REJECT = 'reject'            # 驳回
ACTION_ESCALATE = 'escalate'        # 升级

ACTIONS = (ACTION_ACCEPT, ACTION_CHANGE_LEVEL, ACTION_CHANGE_EXPLANATION, ACTION_REJECT, ACTION_ESCALATE)

ACTION_LABELS = {
    ACTION_ACCEPT: '接受',
    ACTION_CHANGE_LEVEL: '修改等级',
    ACTION_CHANGE_EXPLANATION: '修改解释',
    ACTION_REJECT: '驳回',
    ACTION_ESCALATE: '升级',
}

#: 动作是否必须填写理由
REASON_REQUIRED = (ACTION_CHANGE_LEVEL, ACTION_CHANGE_EXPLANATION, ACTION_REJECT, ACTION_ESCALATE)

#: 动作的目标状态
ACTION_TARGET_STATUS = {
    ACTION_ACCEPT: 'handover',
    ACTION_CHANGE_LEVEL: 'draft',
    ACTION_CHANGE_EXPLANATION: 'draft',
    ACTION_REJECT: 'returned',
    ACTION_ESCALATE: 'escalated',
}

#: 修改等级可用的等级
LEVELS = ('blocking', 'advisory', 'info')

#: 退回/驳回原因枚举：代码 → 业务文案（对接审计口径，避免自由文本难统计）
RETURN_REASON_CODES = {
    'HW_MISSING': '硬件条件缺失（层高/承重/配电等未登记）',
    'HW_MISMATCH': '硬件条件不匹配',
    'HW_UNVERIFIED': '硬件条件未核实',
    'CAPACITY_MISSING': '容量参数缺失（供电容量等）',
    'AREA_MISMATCH': '面积不匹配',
    'DELIVERY_UNCONFIRMED': '交付条件未确认',
    'PRICE_OVER_LIMIT': '价格超限',
    'INCENTIVE_ABOVE_AUTH': '让利超授权',
    'VERSION_CONFLICT': '版本冲突',
    'OLD_MATERIAL_SENT': '送错材料/旧版材料',
    'MISSING_BASIS': '缺少依据或溯源',
    'SYSTEM_UNAVAILABLE': '系统不可用（人工通道）',
}


class ReviewError(ValueError):
    """复核动作不合法。"""


def validate_action(action: str, reason: str = '', detail: Optional[Dict[str, Any]] = None,
                    actor_role: str = '') -> Dict[str, Any]:
    """校验一个复核动作，返回规范化结果。"""
    if action not in ACTIONS:
        raise ReviewError(f'不支持的复核动作：{action}')
    reason = (reason or '').strip()
    if action in REASON_REQUIRED and not reason:
        raise ReviewError(f'"{ACTION_LABELS[action]}"必须填写理由')
    detail = detail or {}
    if not isinstance(detail, dict):
        raise ReviewError('动作明细必须为对象')
    if action == ACTION_CHANGE_LEVEL:
        level = detail.get('level')
        if level not in LEVELS:
            raise ReviewError(f'修改等级需指定 level，取值：{"、".join(LEVELS)}')
        if not detail.get('field'):
            raise ReviewError('修改等级需指定 field')
    if action == ACTION_CHANGE_EXPLANATION:
        if not detail.get('field'):
            raise ReviewError('修改解释需指定 field')
        if not (detail.get('explanation') or '').strip():
            raise ReviewError('修改解释需填写 explanation')
    if action == ACTION_REJECT:
        reason_code = (detail.get('reason_code') or '').strip()
        if not reason_code:
            raise ReviewError('驳回需填写 reason_code，取值：' + '、'.join(RETURN_REASON_CODES))
        if reason_code not in RETURN_REASON_CODES:
            raise ReviewError(f'未知退回原因代码：{reason_code}，必须取枚举：'
                              + '、'.join(RETURN_REASON_CODES))
    if action == ACTION_ESCALATE:
        if not (detail.get('escalate_to') or '').strip():
            raise ReviewError('升级需填写 escalate_to')
    return {
        'action': action,
        'action_label': ACTION_LABELS[action],
        'reason': reason,
        'detail': detail,
        'actor_role': actor_role,
    }


def apply_action(card: Dict[str, Any], action_result: Dict[str, Any]) -> Dict[str, Any]:
    """把复核动作应用到需求卡，返回新的卡片状态与需要落库的动作记录。"""
    if not isinstance(card, dict):
        raise ReviewError('需求卡必须为对象')
    action = action_result['action']
    detail = action_result.get('detail') or {}
    version = card.get('version') or 1

    record = {
        'action': action,
        'action_label': action_result['action_label'],
        'reason': action_result['reason'],
        'detail': detail,
        'actor_role': action_result.get('actor_role') or '',
        'ref_version': version,
        'target_status': ACTION_TARGET_STATUS[action],
    }

    new_status = ACTION_TARGET_STATUS[action]
    if action == ACTION_CHANGE_LEVEL:
        # 记录等级调整：把该字段的阻断级别下调，交接校验时按调整后级别重算
        level = detail.get('level')
        field = detail.get('field')
        adjustments = dict(card.get('level_adjustments') or {})
        adjustments[field] = level
        card['level_adjustments'] = adjustments
        record['resolution'] = f"{label(field)} 的校验等级调整为 {level}"
    elif action == ACTION_CHANGE_EXPLANATION:
        field = detail.get('field')
        explanation = (detail.get('explanation') or '').strip()
        items = dict(card.get('items') or {})
        item = dict(items.get(field) or {})
        if item:
            item['explanation'] = explanation
            item['note'] = explanation
            items[field] = item
            card['items'] = items
        record['resolution'] = f'{label(field)} 的解释更新为：{explanation}'
    elif action == ACTION_REJECT:
        code = detail.get('reason_code')
        record['resolution'] = (f"驳回原因：{code}（{RETURN_REASON_CODES.get(code, '未知代码')}），"
                                f"处理说明：{detail.get('resolution') or action_result['reason']}")
    elif action == ACTION_ESCALATE:
        record['resolution'] = f"升级至 {detail.get('escalate_to')}：{action_result['reason']}"
    else:
        record['resolution'] = action_result['reason'] or '接受交接'

    return {
        'status': new_status,
        'record': record,
        'card': card,
    }


def label(field: Optional[str]) -> str:
    from ..schemas.fields import label_of
    if not field:
        return '未知字段'
    try:
        return label_of(field)
    except Exception:
        return str(field)


def review_history(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """整理复核历史，供前端展示。"""
    history = []
    for record in records or []:
        detail = record.get('detail') or {}
        reason_code = detail.get('reason_code') or ''
        history.append({
            'action': record.get('action'),
            'action_label': record.get('action_label') or ACTION_LABELS.get(record.get('action'), '未知动作'),
            'reason': record.get('reason') or '',
            'reason_code': reason_code,
            'reason_code_label': RETURN_REASON_CODES.get(reason_code, ''),
            'detail': detail,
            'actor': record.get('actor') or '',
            'actor_role': record.get('actor_role') or '',
            'ref_version': record.get('ref_version'),
            'created_at': record.get('created_at') or '',
            'resolution': record.get('resolution') or '',
        })
    return history
