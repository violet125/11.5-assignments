"""空间匹配引擎。

硬条件用确定性规则：满足 / 不满足 / 待核实 三态。
房源参数未知不等于满足；台账溯源不完整的房源不能给出"满足"结论。
结果附带逐项依据与缺口，供人工复核。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ..inventory import LedgerError, normalize_ledger
from ..schemas.fields import (
    FIELDS,
    STATE_SPECIFIED,
    compare,
    compare_label,
    is_number_field,
    label_of,
    unit_of,
)

STATUS_MATCH = '满足'
STATUS_MISS = '不满足'
STATUS_UNVERIFIED = '待核实'

#: 整体结论
CONCLUSION_PASS = '已填条件满足'
CONCLUSION_UNVERIFIED = '待核实'
CONCLUSION_FAIL = '不匹配'
CONCLUSION_INCOMPLETE_TRACE = '条件满足但溯源不全'

CONCLUSION_ORDER = {
    CONCLUSION_PASS: 0,
    CONCLUSION_INCOMPLETE_TRACE: 1,
    CONCLUSION_UNVERIFIED: 2,
    CONCLUSION_FAIL: 3,
}


class MatchError(ValueError):
    """匹配输入不合法。"""


def _requirement_entries(requirements: Any) -> List[Dict[str, Any]]:
    """兼容两种输入：新的 items 列表，或旧的 {field: value} 字典。"""
    if requirements is None:
        raise MatchError('请至少确认一项需求，不能以空需求匹配')
    if isinstance(requirements, dict):
        if 'items' in requirements and isinstance(requirements['items'], list):
            return _requirement_entries(requirements['items'])
        entries = []
        for key, value in requirements.items():
            if key not in FIELDS:
                continue
            if value is None:
                continue
            entries.append({'field': key, 'state': STATE_SPECIFIED, 'value': value})
        return entries
    if isinstance(requirements, list):
        entries = []
        for item in requirements:
            if not isinstance(item, dict):
                raise MatchError('需求条目必须为对象')
            key = item.get('field')
            if key not in FIELDS:
                continue
            state = item.get('state', STATE_SPECIFIED)
            if state != STATE_SPECIFIED:
                continue
            value = item.get('value')
            if value is None:
                continue
            entries.append({'field': key, 'state': state, 'value': value})
        return entries
    raise MatchError('需求格式无效')


def _validate_requirement_value(key: str, value: Any) -> Any:
    """需求侧数值必须是正数，不允许 0 或负数。"""
    if not is_number_field(key):
        return str(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatchError(f'{label_of(key)}必须为数值')
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise MatchError(f'{label_of(key)}必须大于零')
    return number


def _coerce(value: Any, key: str) -> Any:
    if is_number_field(key):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MatchError(f'{label_of(key)}必须为数值') from exc
        if not math.isfinite(number):
            raise MatchError(f'{label_of(key)}必须为有限数值')
        return number
    return str(value)


def match(requirements: Any, spaces: Any, manual_checks: Optional[List[str]] = None) -> Dict[str, Any]:
    """执行硬条件匹配。

    requirements：需求条目列表（推荐）或 {field: value} 字典（兼容旧接口）
    spaces：房源台账数组
    """
    entries = _requirement_entries(requirements)
    if not entries:
        raise MatchError('请至少确认一项需求，不能以空需求匹配')

    ledger = normalize_ledger(spaces)
    results = []
    for record in ledger:
        checks = []
        for entry in entries:
            key = entry['field']
            required = _validate_requirement_value(key, entry['value'])
            actual = record['values'].get(key)
            if actual is None:
                status = STATUS_UNVERIFIED
                basis = f'房源未登记{label_of(key)}，未知不等于满足'
            else:
                actual = _coerce(actual, key)
                status = STATUS_MATCH if compare(key, actual, required) else STATUS_MISS
                basis = compare_label(key, actual, required)
            checks.append({
                'field': key,
                'label': label_of(key),
                'unit': unit_of(key),
                'required': required,
                'actual': actual,
                'status': status,
                'basis': basis,
            })

        statuses = {check['status'] for check in checks}
        if STATUS_MISS in statuses:
            conclusion = CONCLUSION_FAIL
        elif STATUS_UNVERIFIED in statuses:
            conclusion = CONCLUSION_UNVERIFIED
        elif not record['trace_complete']:
            conclusion = CONCLUSION_INCOMPLETE_TRACE
        else:
            conclusion = CONCLUSION_PASS

        results.append({
            'name': record['name'],
            'status': conclusion,
            'checks': checks,
            'trace': record['trace'],
            'trace_complete': record['trace_complete'],
            'meta': record['meta'],
            'gaps': [f"{c['label']}：{c['basis']}" for c in checks if c['status'] != STATUS_MATCH],
        })

    # 同状态内保持台账原始顺序，不按字段数值跨维度比较（跨维度求和无业务含义）
    results.sort(key=lambda item: CONCLUSION_ORDER.get(item['status'], 9))

    missing = []
    for key in FIELDS:
        if key in {entry['field'] for entry in entries}:
            continue
        missing.append(label_of(key))

    return {
        'results': results,
        'missing_requirements': missing,
        'manual_checks': list(manual_checks or []),
        'matched_count': sum(1 for r in results if r['status'] == CONCLUSION_PASS),
        'unverified_count': sum(1 for r in results if r['status'] == CONCLUSION_UNVERIFIED),
        'failed_count': sum(1 for r in results if r['status'] == CONCLUSION_FAIL),
    }
