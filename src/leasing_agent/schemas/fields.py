"""字段字典：需求字段、房源字段、单位、取值状态。

设计要点
--------
1. 空白不再是单一含义。需求字段取值状态分为四态：
   - ``specified``   已明确：有数值/枚举值，可参与硬条件匹配
   - ``unknown``     未知待补：客户提到过该维度但没给准数，必须补问
   - ``not_requested`` 未提出要求：客户本次访谈未涉及该维度（主动留白）
   - ``none_confirmed`` 确认无此要求：客户明确表示不需要，已确认
   交接校验据此区分"必须补问"与"不需要问"。
2. 层高与净高是两个独立字段，不可互相替代。设备功率 kW 不可当作供电容量 kVA。
3. 房源字段与需求字段同名字段共用单位口径，避免两侧换算口径不一致。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# 取值状态
# ---------------------------------------------------------------------------

STATE_SPECIFIED = 'specified'
STATE_UNKNOWN = 'unknown'
STATE_NOT_REQUESTED = 'not_requested'
STATE_NONE_CONFIRMED = 'none_confirmed'

ALL_STATES = (STATE_SPECIFIED, STATE_UNKNOWN, STATE_NOT_REQUESTED, STATE_NONE_CONFIRMED)

#: 可以参与硬条件匹配的状态
MATCHABLE_STATES = (STATE_SPECIFIED,)

STATE_LABELS = {
    STATE_SPECIFIED: '已明确',
    STATE_UNKNOWN: '未知待补',
    STATE_NOT_REQUESTED: '未提出要求',
    STATE_NONE_CONFIRMED: '确认无此要求',
}

#: 空白（未指定数值）的三种业务语义
BLANK_STATES = (STATE_UNKNOWN, STATE_NOT_REQUESTED, STATE_NONE_CONFIRMED)


class FieldError(ValueError):
    """字段取值不合法。"""


# ---------------------------------------------------------------------------
# 字段规格
# ---------------------------------------------------------------------------

COMPARISON_MIN = 'min'   # 房源实际值 >= 需求值 才满足
COMPARISON_MAX = 'max'   # 房源实际值 <= 需求值 才满足
COMPARISON_ORDINAL = 'ordinal'  # 枚举按等级比较，房源等级 >= 需求等级

#: 配电冗余等级（顺序即能力高低）
REDUNDANCY_LEVELS = ['单路', '双路', 'N+1']


class FieldSpec:
    """一个需求/房源字段的规格。"""

    __slots__ = ('key', 'label', 'unit', 'kind', 'comparison', 'choices', 'note',
                 'professional', 'dimension')

    def __init__(self, key: str, label: str, unit: str, kind: str = 'number',
                 comparison: str = COMPARISON_MIN, choices: Optional[List[str]] = None,
                 note: str = '', professional: bool = False, dimension: str = '') -> None:
        self.key = key
        self.label = label
        self.unit = unit
        self.kind = kind          # number | enum
        self.comparison = comparison
        self.choices = choices or []
        self.note = note
        self.professional = professional  # 是否需要专业确认
        self.dimension = dimension or label

    def to_dict(self) -> Dict[str, Any]:
        return {
            'key': self.key,
            'label': self.label,
            'unit': self.unit,
            'kind': self.kind,
            'comparison': self.comparison,
            'choices': list(self.choices),
            'note': self.note,
            'professional': self.professional,
        }


#: 需求字段与房源字段共用一套规格（台账侧再追加溯源元数据）
FIELDS: Dict[str, FieldSpec] = {
    'area': FieldSpec('area', '最小面积', '㎡', note='按所需面积下限筛选，面积上限人工核实'),
    'rent': FieldSpec('rent', '最高月单价', '元/㎡/月', comparison=COMPARISON_MAX,
                      note='年租金、总租金不得折算冒充月单价，需人工换算确认'),
    'power': FieldSpec('power', '最低供电容量', 'kVA', note='设备功率 kW 不可直接替代 kVA'),
    'load': FieldSpec('load', '最低楼面承重', 'kg/㎡', note='需区分楼面承重与集中荷载'),
    'height': FieldSpec('height', '最低净高', '米', professional=True,
                        note='净高为梁下或装修面以上净尺寸，不得用层高替代'),
    'layer_height': FieldSpec('layer_height', '最低层高', '米', note='层高为楼层高度，不等于净高'),
    'redundancy': FieldSpec('redundancy', '配电冗余', '', kind='enum',
                            comparison=COMPARISON_ORDINAL, choices=REDUNDANCY_LEVELS,
                            professional=True, note='双路与 N+1 的停电自愈能力不同，需工程确认'),
}

FIELD_KEYS = tuple(FIELDS.keys())

#: 抽取器与匹配引擎默认处理的字段集合（保留旧的五项核心字段语义）
CORE_FIELDS = ('area', 'rent', 'power', 'load', 'height')


def spec(key: str) -> FieldSpec:
    if key not in FIELDS:
        raise FieldError(f'未知字段：{key}')
    return FIELDS[key]


def label_of(key: str) -> str:
    return spec(key).label


def unit_of(key: str) -> str:
    return spec(key).unit


def is_number_field(key: str) -> bool:
    return spec(key).kind == 'number'


# ---------------------------------------------------------------------------
# 数值与枚举校验
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r'^\s*[-+]?\d+(?:\.\d+)?\s*$')


def parse_number(value: Any, key: str) -> Optional[float]:
    """把输入转成正数 float。空值返回 None；非法输入抛 FieldError。"""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise FieldError(f'{label_of(key)}必须为数值或空值')
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not _NUM_RE.match(text):
            raise FieldError(f'{label_of(key)}必须为纯数值，单位固定为 {unit_of(key)}，请勿把数值与单位混填')
        value = float(text)
    if not isinstance(value, (int, float)):
        raise FieldError(f'{label_of(key)}必须为数值或空值')
    number = float(value)
    if not math.isfinite(number):
        raise FieldError(f'{label_of(key)}必须为有限数值')
    if number <= 0:
        raise FieldError(f'{label_of(key)}必须大于零')
    return number


def parse_choice(value: Any, key: str) -> Optional[str]:
    """枚举字段取值校验。"""
    if value is None or value == '':
        return None
    text = str(value).strip()
    choices = spec(key).choices
    if text not in choices:
        raise FieldError(f'{label_of(key)}只能取：{"、".join(choices)}')
    return text


def parse_value(key: str, value: Any) -> Any:
    """按字段类型解析取值。"""
    if is_number_field(key):
        return parse_number(value, key)
    return parse_choice(value, key)


def compare(field_key: str, actual: Any, required: Any) -> bool:
    """比较房源实际值是否满足需求值。调用方需保证两者均已校验。"""
    rule = spec(field_key).comparison
    if rule == COMPARISON_MAX:
        return actual <= required
    if rule == COMPARISON_ORDINAL:
        try:
            return REDUNDANCY_LEVELS.index(actual) >= REDUNDANCY_LEVELS.index(required)
        except ValueError:
            return False
    return actual >= required


def compare_label(field_key: str, actual: Any, required: Any) -> str:
    """给前端看的一句话比较说明。"""
    rule = spec(field_key).comparison
    unit = spec(field_key).unit
    if rule == COMPARISON_MAX:
        return f'需 ≤ {required}{unit}，实际 {actual}{unit}'
    if rule == COMPARISON_ORDINAL:
        return f'需 ≥ {required}，实际 {actual}'
    return f'需 ≥ {required}{unit}，实际 {actual}{unit}'


# ---------------------------------------------------------------------------
# 需求条目（requirement item）
# ---------------------------------------------------------------------------

def blank_item(key: str, state: str = STATE_UNKNOWN, evidence: str = '',
               origin: str = 'manual', note: str = '') -> Dict[str, Any]:
    """构造一个需求条目。"""
    if state not in ALL_STATES:
        raise FieldError(f'未知取值状态：{state}')
    return {
        'field': key,
        'state': state,
        'value': None,
        'unit': unit_of(key),
        'evidence': evidence,
        'origin': origin,
        'note': note,
    }


def normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规整单个需求条目。"""
    if not isinstance(item, dict):
        raise FieldError('需求条目必须为对象')
    key = item.get('field')
    if key not in FIELDS:
        raise FieldError(f'未知字段：{key}')
    state = item.get('state', STATE_SPECIFIED)
    if state not in ALL_STATES:
        raise FieldError(f'{label_of(key)}的取值状态无效：{state}')
    value = item.get('value')
    if state == STATE_SPECIFIED:
        parsed = parse_value(key, value)
        if parsed is None:
            raise FieldError(f'{label_of(key)}标记为已明确，但数值为空')
        if is_number_field(key):
            parsed = float(parsed)
    else:
        if value not in (None, ''):
            raise FieldError(f'{label_of(key)}处于"{STATE_LABELS[state]}"状态，不应携带数值')
        parsed = None
    evidence = item.get('evidence') or ''
    if not isinstance(evidence, str):
        raise FieldError(f'{label_of(key)}的原文证据必须为文本')
    return {
        'field': key,
        'state': state,
        'value': parsed,
        'unit': unit_of(key),
        'evidence': evidence.strip(),
        'origin': item.get('origin') or 'manual',
        'note': (item.get('note') or '').strip(),
    }


def normalize_items(items: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """规整需求条目集合，返回 {字段: 条目}。重复字段以最后一条为准。"""
    result: Dict[str, Dict[str, Any]] = {}
    for item in items or []:
        normalized = normalize_item(item)
        result[normalized['field']] = normalized
    return result


def ensure_all_fields(items: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """补齐缺失字段，缺失字段按"未知待补"处理，便于人工裁定。"""
    for key in FIELD_KEYS:
        if key not in items:
            items[key] = blank_item(key, STATE_UNKNOWN)
    return items


def specified_items(items: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """取出已明确的条目。"""
    return {k: v for k, v in items.items() if v.get('state') == STATE_SPECIFIED and v.get('value') is not None}
