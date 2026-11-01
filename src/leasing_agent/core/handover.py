"""交接字段校验：生成补问清单，阻止不合格需求进入"交接完成"。

校验分四层：
1. 必填层：按客户用途配置的必填/条件必填字段是否有明确取值
2. 语义层：已明确字段是否有原文证据；层高/净高、kW/kVA 口径是否被混用
3. 专业层：需专业确认的字段是否已由具备权限的角色确认
4. 流程层：复核状态是否允许交接；阻断项是否已解除

阻断项（blocking）必须清零才能交接完成。提示项（advisory）不阻断但要留痕。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..schemas.fields import (
    FIELDS,
    STATE_LABELS,
    STATE_NONE_CONFIRMED,
    STATE_NOT_REQUESTED,
    STATE_SPECIFIED,
    STATE_UNKNOWN,
    label_of,
    unit_of,
)

#: 交接状态机
STATUS_DRAFT = 'draft'            # 草稿
STATUS_PENDING = 'pending'        # 待交接（校验通过，待复核）
STATUS_HANDOVER = 'handover'      # 已交接
STATUS_RETURNED = 'returned'      # 已退回
STATUS_ESCALATED = 'escalated'    # 已升级

HANDOVER_STATUSES = (STATUS_DRAFT, STATUS_PENDING, STATUS_HANDOVER, STATUS_RETURNED, STATUS_ESCALATED)

#: 阻断级别
BLOCKING = 'blocking'
ADVISORY = 'advisory'

#: 问题类型
ISSUE_MISSING_VALUE = 'missing_value'
ISSUE_MISSING_EVIDENCE = 'missing_evidence'
ISSUE_NO_PROFESSIONAL_CONFIRM = 'no_professional_confirm'
ISSUE_UNIT_CONFLICT = 'unit_conflict'
ISSUE_UNKNOWN_PURPOSE = 'unknown_purpose'
ISSUE_NOT_REQUESTED = 'not_requested'
ISSUE_CONFIRMATION_VOID = 'confirmation_void'


class HandoverError(ValueError):
    """交接校验输入不合法。"""


def _purpose_rules(config: Dict[str, Any], purpose: str) -> Dict[str, Any]:
    purposes = (config or {}).get('purposes') or {}
    if purpose and purpose in purposes:
        return purposes[purpose]
    return purposes.get('通用') or {'required': [], 'conditional': {}, 'professional': [], 'questions': []}


def validate_handover(card: Dict[str, Any], config: Dict[str, Any],
                      confirmations: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """执行交接校验。

    card 需含：purpose、items（字段->条目）、evidence_map（可选）、confirmed_at
    confirmations：已生效的专业确认记录 [{field, by, role, at, note}]
    """
    if not isinstance(card, dict):
        raise HandoverError('需求卡必须为对象')
    items = card.get('items')
    if not isinstance(items, dict):
        raise HandoverError('需求卡缺少字段条目')

    purpose = card.get('purpose') or ''
    rules = _purpose_rules(config, purpose)
    required = list(rules.get('required') or [])
    conditional = dict(rules.get('conditional') or {})
    professional = list(rules.get('professional') or [])
    questions = list(rules.get('questions') or [])

    confirmations = confirmations or []
    confirmed_fields = {c.get('field') for c in confirmations if c.get('valid', True)}

    issues: List[Dict[str, Any]] = []

    # --- 用途未选 ---
    if not purpose:
        issues.append({
            'level': BLOCKING,
            'type': ISSUE_UNKNOWN_PURPOSE,
            'field': None,
            'message': '未选择客户用途，必填项与专业确认项无法确定',
            'question': '客户实际业态与主要作业内容是什么？',
        })

    # --- 必填层 ---
    for key in required:
        item = items.get(key)
        state = item.get('state') if item else STATE_UNKNOWN
        if state == STATE_SPECIFIED:
            continue
        if state == STATE_UNKNOWN:
            issues.append({
                'level': BLOCKING,
                'type': ISSUE_MISSING_VALUE,
                'field': key,
                'message': f'{label_of(key)}为未知待补，需补齐后才能交接',
                'question': f'客户对{label_of(key)}的具体要求是多少？（单位{unit_of(key) or "枚举"}）',
            })
        elif state == STATE_NOT_REQUESTED:
            issues.append({
                'level': BLOCKING,
                'type': ISSUE_NOT_REQUESTED,
                'field': key,
                'message': f'{label_of(key)}客户本次未提出，但按"通用/当前用途"属于必填，需客户明确确认无此要求',
                'question': f'客户确认本次确实不涉及{label_of(key)}吗？如不涉及请回复"确认无此要求"。',
            })
        elif state == STATE_NONE_CONFIRMED:
            issues.append({
                'level': ADVISORY,
                'type': ISSUE_NOT_REQUESTED,
                'field': key,
                'message': f'{label_of(key)}已确认无此要求，按必填规则记为提示项，不阻断交接',
                'question': '',
            })

    # --- 条件必填层 ---
    for key, condition in conditional.items():
        item = items.get(key)
        state = item.get('state') if item else STATE_UNKNOWN
        if state == STATE_SPECIFIED:
            continue
        if state == STATE_NONE_CONFIRMED:
            continue
        issues.append({
            'level': ADVISORY,
            'type': ISSUE_MISSING_VALUE,
            'field': key,
            'message': f'{label_of(key)}待确认（触发条件：{condition}）',
            'question': f'是否触发以下条件：{condition}？若触发，{label_of(key)}要求为多少？',
        })

    # --- 语义层：证据 ---
    for key, item in items.items():
        if item.get('state') != STATE_SPECIFIED:
            continue
        evidence = (item.get('evidence') or '').strip()
        if not evidence:
            issues.append({
                'level': BLOCKING,
                'type': ISSUE_MISSING_EVIDENCE,
                'field': key,
                'message': f'{label_of(key)}有取值但缺少原文证据',
                'question': f'请提供{label_of(key)}的原文表述，用于留痕核对。',
            })
        if key == 'height' and item.get('unit') and '层高' in evidence and '净高' not in evidence:
            issues.append({
                'level': BLOCKING,
                'type': ISSUE_UNIT_CONFLICT,
                'field': key,
                'message': '净高的证据中出现"层高"，两者口径不同，不能相互替代',
                'question': '请确认该数值是梁下净高还是楼层层高？',
            })
        if key == 'power' and 'kw' in evidence.lower() and 'kva' not in evidence.lower():
            issues.append({
                'level': BLOCKING,
                'type': ISSUE_UNIT_CONFLICT,
                'field': key,
                'message': '供电容量的证据为设备功率 kW，不可直接作为 kVA',
                'question': '请提供供电容量（kVA）口径的原文，或由工程出具换算说明。',
            })

    # --- 专业层：仅对已明确取值的字段要求专业确认 ---
    # 字段本身没填时，由"必填层/条件必填层"处理，不在此重复阻断
    for key in professional:
        if key in confirmed_fields:
            continue
        item = items.get(key)
        if not item or item.get('state') != STATE_SPECIFIED:
            continue
        issues.append({
            'level': BLOCKING,
            'type': ISSUE_NO_PROFESSIONAL_CONFIRM,
            'field': key,
            'message': f'{label_of(key)}已填写但需专业确认，尚未由具备权限的角色确认实测口径',
            'question': f'请工程或复核角色确认{label_of(key)}的实测口径与数值。',
        })

    blocking = [i for i in issues if i['level'] == BLOCKING]
    advisory = [i for i in issues if i['level'] == ADVISORY]

    return {
        'passed': not blocking,
        'purpose': purpose,
        'issues': issues,
        'blocking_count': len(blocking),
        'advisory_count': len(advisory),
        'blocking': blocking,
        'advisory': advisory,
        'questions': questions,
        'required_fields': required,
        'conditional_fields': conditional,
        'professional_fields': professional,
        'confirmed_fields': sorted(confirmed_fields),
        'checklist': [i['question'] for i in issues if i.get('question')],
    }


def can_submit(status: str) -> bool:
    """当前状态是否允许发起交接提交。"""
    return status in (STATUS_DRAFT, STATUS_RETURNED)


def can_handover(status: str, validation: Dict[str, Any]) -> bool:
    """是否可以把需求置为已交接。"""
    if status == STATUS_ESCALATED:
        return False
    if status == STATUS_HANDOVER:
        return False
    return bool(validation.get('passed'))


def next_status_after_return(status: str) -> str:
    """退回后的状态。"""
    return STATUS_DRAFT


def summarize(validation: Dict[str, Any]) -> str:
    """一句话摘要，给前端与报告使用。"""
    if validation.get('passed'):
        return f"校验通过（{validation.get('advisory_count', 0)} 项提示待留痕）"
    return f"校验未通过：{validation.get('blocking_count', 0)} 项阻断待处理"
