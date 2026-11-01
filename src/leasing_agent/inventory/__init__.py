"""载体参数台账。

台账记录 = 空间参数 + 溯源元数据。元数据不是可选项：
没有来源、核实人、核实日期、版本的房源，其参数不能进入匹配结论，
只能作为"待核实"素材。这是把"台账必须由业务及工程人员建设"落到代码上。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..schemas.fields import FIELDS, FIELD_KEYS, FieldError, is_number_field, parse_value

#: 台账必填溯源字段
TRACE_FIELDS = ('source', 'verifier', 'verified_at', 'version')

TRACE_LABELS = {
    'source': '资料来源',
    'verifier': '核实人',
    'verified_at': '核实日期',
    'version': '数据版本',
}

#: 允许的核实日期格式
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

#: 房源可选描述字段
OPTIONAL_META = ('park', 'building', 'floor', 'usable_area', 'delivery_date', 'notes')

# ---------------------------------------------------------------------------
# 报价参数（B-lite）：不参与匹配，仅供报价单带出与成本估算
# ---------------------------------------------------------------------------

#: 数值型报价参数：key → (名称, 单位, 下限, 上限)
QUOTE_PARAM_NUMBERS = {
    'efficiency_ratio': ('得房率', '%', 1.0, 100.0),
    'property_fee': ('物业费', '元/㎡/月', 0.0, 1000.0),
    'hvac_overtime_fee': ('空调加时费', '元/㎡/月', 0.0, 1000.0),
    'net_height': ('净高', '米', 0.1, 30.0),
}

#: 枚举型报价参数：key → (名称, 允许取值)
QUOTE_PARAM_ENUMS = {
    'delivery_status': ('交付标准', ('毛坯', 'L1', 'L2', 'L3')),
    'power_redundancy': ('供电冗余', ('单路', '双路', '双路+应急', 'N+1')),
}

QUOTE_PARAM_KEYS = tuple(QUOTE_PARAM_NUMBERS) + tuple(QUOTE_PARAM_ENUMS)


class LedgerError(ValueError):
    """台账数据不合法。"""


def normalize_space_record(record: Any, index: int = 0) -> Dict[str, Any]:
    """校验并规整一条房源台账记录。"""
    if not isinstance(record, dict):
        raise LedgerError(f'第 {index + 1} 条房源必须是对象')

    name = record.get('name')
    if not isinstance(name, str) or not name.strip():
        raise LedgerError(f'第 {index + 1} 条房源缺少名称')
    name = name.strip()
    if len(name) > 120:
        raise LedgerError(f'第 {index + 1} 条房源名称过长')

    values: Dict[str, Any] = {}
    for key in FIELD_KEYS:
        raw = record.get(key)
        if raw is None or raw == '':
            values[key] = None
            continue
        if isinstance(raw, str):
            text = raw.strip()
            if _carries_unit(key, text):
                raise LedgerError(
                    f'房源"{name}"的{_label(key)}填入了带单位的文本"{text}"，'
                    f'请只填数值，单位固定为{_unit(key)}；kW 不能当作 kVA，层高不能当作净高'
                )
        try:
            values[key] = parse_value(key, raw)
        except FieldError as exc:
            raise LedgerError(f'房源"{name}"的{_label(key)}不合法：{exc}') from exc
        if is_number_field(key) and values[key] is not None:
            values[key] = float(values[key])

    for key in QUOTE_PARAM_KEYS:
        raw = record.get(key)
        if raw is None or raw == '':
            values[key] = None
            continue
        if key in QUOTE_PARAM_NUMBERS:
            label, unit, low, high = QUOTE_PARAM_NUMBERS[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise LedgerError(f'房源"{name}"的{label}必须为数值')
            if isinstance(raw, str):
                text = raw.strip()
                if not re.fullmatch(r'[-+]?\d+(?:\.\d+)?', text):
                    raise LedgerError(
                        f'房源"{name}"的{label}必须为纯数值，单位固定为{unit}，请勿混填单位')
                raw = float(text)
            number = float(raw)
            if number < low or number > high:
                raise LedgerError(f'房源"{name}"的{label}必须在 {low} ~ {high} 之间（{unit}）')
            values[key] = number
        else:
            label, choices = QUOTE_PARAM_ENUMS[key]
            text = str(raw).strip()
            if text not in choices:
                raise LedgerError(f'房源"{name}"的{label}只能取：{"、".join(choices)}')
            values[key] = text

    trace: Dict[str, Any] = {}
    for key in TRACE_FIELDS:
        raw = record.get(key)
        if raw is None:
            trace[key] = ''
            continue
        if not isinstance(raw, (str, int, float)):
            raise LedgerError(f'房源"{name}"的{TRACE_LABELS[key]}必须为文本')
        text = str(raw).strip()
        if key == 'verified_at' and text and not DATE_RE.match(text):
            raise LedgerError(f'房源"{name}"的核实日期应为 YYYY-MM-DD 格式')
        trace[key] = text

    meta: Dict[str, Any] = {}
    for key in OPTIONAL_META:
        raw = record.get(key)
        if raw is None:
            meta[key] = ''
            continue
        if not isinstance(raw, (str, int, float)):
            raise LedgerError(f'房源"{name}"的{key}必须为文本')
        meta[key] = str(raw).strip()

    missing_trace = [TRACE_LABELS[k] for k in TRACE_FIELDS if not trace[k]]
    return {
        'name': name,
        'values': values,
        'trace': trace,
        'meta': meta,
        'trace_complete': not missing_trace,
        'missing_trace': missing_trace,
    }


def _label(key: str) -> str:
    return FIELDS[key].label


def _unit(key: str) -> str:
    return FIELDS[key].unit or '（枚举）'


def _carries_unit(key: str, text: str) -> bool:
    """判断文本是否夹带了单位。"""
    if not is_number_field(key):
        return False
    if re.search(r'[A-Za-z\u4e00-\u9fa5]', text):
        return True
    if any(symbol in text for symbol in ('/', '㎡', '米', '公斤', '元')):
        return True
    return False


def normalize_ledger(records: Any) -> List[Dict[str, Any]]:
    """校验整份台账。返回规整后的记录列表。"""
    if not isinstance(records, list):
        raise LedgerError('房源台账必须为数组')
    if not 1 <= len(records) <= 2000:
        raise LedgerError('房源台账必须包含 1—2000 条记录')
    result = []
    seen = set()
    for index, record in enumerate(records):
        normalized = normalize_space_record(record, index)
        if normalized['name'] in seen:
            raise LedgerError(f'房源名称重复：{normalized["name"]}')
        seen.add(normalized['name'])
        result.append(normalized)
    return result


def flatten_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把存储形态的记录还原成匹配引擎需要的扁平台账行。"""
    flat = []
    for record in records or []:
        row: Dict[str, Any] = {'name': record.get('name')}
        row.update(record.get('values') or {})
        row.update(record.get('trace') or {})
        row.update(record.get('meta') or {})
        flat.append(row)
    return flat


def ledger_issues(normalized: List[Dict[str, Any]]) -> List[str]:
    """汇总台账整体问题（不阻塞导入，但会提示）。"""
    issues: List[str] = []
    incomplete = [r['name'] for r in normalized if not r['trace_complete']]
    if incomplete:
        issues.append(f'{len(incomplete)} 条房源缺少溯源信息（{"、".join(incomplete[:3])}{"…" if len(incomplete) > 3 else ""}），其参数仅作待核实素材')
    empty_params = [r['name'] for r in normalized if all(v is None for v in r['values'].values())]
    if empty_params:
        issues.append(f'{len(empty_params)} 条房源没有任何参数（{"、".join(empty_params[:3])}）')
    return issues


def ledger_template() -> List[Dict[str, Any]]:
    """下载用模板：一个字段齐全的虚构样例。"""
    return [
        {
            'name': '示例房源 A（虚构）',
            'park': '示例园区',
            'building': 'A 栋',
            'floor': '3 层',
            'area': 500,
            'rent': 55,
            'power': 100,
            'load': 300,
            'height': 3.6,
            'layer_height': 4.8,
            'redundancy': '单路',
            'efficiency_ratio': 75,
            'property_fee': 8,
            'delivery_status': 'L1',
            'net_height': 3.3,
            'power_redundancy': '单路',
            'hvac_overtime_fee': 2,
            'source': '示例工程图纸（非真实资料）',
            'verifier': '示例工程负责人',
            'verified_at': '2026-01-15',
            'version': 'v1',
            'usable_area': 460,
            'delivery_date': '2026-06-30',
            'notes': '演示数据，不可用于真实招商',
        }
    ]


def ledger_fields_documentation() -> List[Dict[str, str]]:
    """台账字段说明，供前端与文档使用。"""
    rows = []
    for key in FIELD_KEYS:
        item = FIELDS[key]
        rows.append({
            'key': key,
            'label': item.label,
            'unit': item.unit or '枚举',
            'kind': item.kind,
            'note': item.note,
            'required': item.comparison in ('min', 'max'),
        })
    for key in TRACE_FIELDS:
        rows.append({
            'key': key,
            'label': TRACE_LABELS[key],
            'unit': '文本',
            'kind': 'trace',
            'note': '台账溯源信息，缺失时房源参数只作待核实素材',
            'required': True,
        })
    for key, (label, unit, _low, _high) in QUOTE_PARAM_NUMBERS.items():
        rows.append({
            'key': key,
            'label': label,
            'unit': unit,
            'kind': 'quote_param',
            'note': '报价参数，不参与匹配，仅报价单带出（行业参照，需园区确认）',
            'required': False,
        })
    for key, (label, choices) in QUOTE_PARAM_ENUMS.items():
        rows.append({
            'key': key,
            'label': label,
            'unit': '枚举',
            'kind': 'quote_param',
            'note': f'取值：{"、".join(choices)}；不参与匹配，仅报价单带出',
            'required': False,
        })
    return rows
