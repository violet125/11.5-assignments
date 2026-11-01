"""报价单草稿（v2）。

报价单是空间匹配的下游产物：只允许基于"已填条件满足"或"条件满足但溯源
不全"的房源生成。它是草稿素材，不是正式承诺——租金口径为元/㎡/月，
计租面积 = 台账登记面积 × 得房率（台账缺得房率按 100% 计）。

v2 增强（参照融景园区报价单设计方案）：
- 报价编号 RJ-{YYYYMM}-{序号}，全局递增；同卡同房源重复生成自动升版。
- 轮次：首次报价 / 修订报价（有前版时必须填写修订原因）。
- 费用扩展：物业费、年递增、装修补贴、搬迁补贴、押金、免租、租期。
- 三年总占用成本：三年租金（含递增）+ 三年物业费 + 装修投入 − 补贴。
- 让利授权：免租超期或折扣超限的报价单须管理员审批后才能发出。
- 有效期默认 15 天；装修/物业等估算均标注"行业参照，需园区确认"。

生成时服务端会重跑一次硬条件匹配，不信任前端传来的匹配结论。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

#: 报价单状态与流转
QUOTE_DRAFT = 'draft'
QUOTE_ISSUED = 'issued'
QUOTE_VOIDED = 'voided'

QUOTE_STATUS_LABELS = {
    QUOTE_DRAFT: '草稿',
    QUOTE_ISSUED: '已发出',
    QUOTE_VOIDED: '已作废',
}

#: 允许生成报价单的匹配结论（"待核实"与"不匹配"不允许）
QUOTE_ELIGIBLE_CONCLUSIONS = ('已填条件满足', '条件满足但溯源不全')

#: 报价轮次
QUOTE_ROUND_FIRST = '首次报价'
QUOTE_ROUND_REVISED = '修订报价'

#: 内置默认商务条款（config 缺省时回落）
DEFAULT_TERMS = {
    'deposit_months': 3,
    'free_rent_days': 30,
    'lease_term_months': 36,
    'escalation_pct': 0,
    'efficiency_ratio': 100,
    'property_fee': 0,
    'validity_days': 15,
    'renovation_subsidy': 0,
    'moving_subsidy': 0,
}

#: 条款上下限（防止把 0.01 元或 9999 个月当成手滑输入）
TERM_BOUNDS: Dict[str, Tuple[float, float]] = {
    'unit_price': (0.01, 100000.0),
    'area': (0.01, 1000000.0),
    'deposit_months': (0, 12),
    'free_rent_days': (0, 365),
    'lease_term_months': (1, 120),
    'property_fee': (0, 1000.0),
    'escalation_pct': (0, 50.0),
    'efficiency_ratio': (1, 100),
    'validity_days': (1, 180),
    'renovation_cost_per_sqm': (0, 10000.0),
    'renovation_subsidy': (0, 100000000.0),
    'moving_subsidy': (0, 100000000.0),
}

TERM_LABELS = {
    'unit_price': '月单价',
    'area': '登记面积',
    'deposit_months': '押金月数',
    'free_rent_days': '免租天数',
    'lease_term_months': '租期月数',
    'property_fee': '物业费',
    'escalation_pct': '年递增比例',
    'efficiency_ratio': '得房率',
    'validity_days': '有效期',
    'renovation_cost_per_sqm': '装修单价',
    'renovation_subsidy': '装修补贴',
    'moving_subsidy': '搬迁补贴',
}

#: 让利授权默认额度（config quotation.authority 可覆盖）
DEFAULT_AUTHORITY = {'max_free_rent_days': 92, 'max_discount_pct': 5.0}

#: 交付标准 → 装修投入参照区间（元/㎡，估算取中值；行业参照，需园区确认）
DEFAULT_RENOVATION_COST = {
    'L1': (300.0, 600.0),
    'L2': (100.0, 300.0),
    'L3': (0.0, 30.0),
    '毛坯': (300.0, 800.0),
}


class QuoteError(ValueError):
    """报价单生成或流转不合法。"""


def _coerce_number(name: str, value: Any, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QuoteError(f'{name}必须为数值')
    number = float(value)
    if not math.isfinite(number):
        raise QuoteError(f'{name}必须为有限数值')
    if number < lower or number > upper:
        raise QuoteError(f'{name}必须在 {lower} ~ {upper} 之间')
    return number


def _term(name: str, override: Any, fallback: Any, required: bool = True) -> Optional[float]:
    """取单个条款：优先覆盖值，其次台账/配置值。required 时缺失即报错。"""
    bounds = TERM_BOUNDS[name]
    value = override if override not in (None, '') else fallback
    if value in (None, ''):
        if required:
            raise QuoteError(f'缺少{TERM_LABELS[name]}：台账未登记该参数，需人工填写后生成')
        return None
    return _coerce_number(TERM_LABELS[name], value, bounds[0], bounds[1])


def _renovation_cost(space_values: Dict[str, Any], overrides: Dict[str, Any],
                     config: Dict[str, Any]) -> Tuple[float, bool, str]:
    """装修单价（元/㎡）：人工覆盖 > 交付标准查表（区间取中值）。

    返回 (单价, 是否为行业参照估算, 交付标准)。
    """
    override = overrides.get('renovation_cost_per_sqm')
    if override not in (None, ''):
        cost = _coerce_number(TERM_LABELS['renovation_cost_per_sqm'], override,
                              *TERM_BOUNDS['renovation_cost_per_sqm'])
        return cost, False, str(space_values.get('delivery_status') or '')
    delivery = overrides.get('delivery_status') or space_values.get('delivery_status') or ''
    table = config.get('renovation_cost_per_sqm') or DEFAULT_RENOVATION_COST
    entry = table.get(str(delivery))
    if not entry:
        return 0.0, True, str(delivery)
    low, high = float(entry[0]), float(entry[1])
    return round((low + high) / 2, 2), True, str(delivery)


def build_terms(space_values: Dict[str, Any], overrides: Optional[Dict[str, Any]],
                config_quotation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """根据台账参数与人工覆盖项计算报价条款与三年总占用成本。

    台账的 rent（元/㎡/月）与 area（㎡）是基准；两者缺失时必须人工覆盖，
    不能用 0 或估计值悄悄代替。装修与物业费缺登记时按 0 计并标注估算口径。
    """
    overrides = overrides or {}
    if not isinstance(overrides, dict):
        raise QuoteError('overrides 必须为对象')
    config = config_quotation or {}

    unit_price = _term('unit_price', overrides.get('unit_price'), space_values.get('rent'))
    area = _term('area', overrides.get('area'), space_values.get('area'))
    efficiency = _term('efficiency_ratio', overrides.get('efficiency_ratio'),
                       space_values.get('efficiency_ratio'),
                       required=False) or DEFAULT_TERMS['efficiency_ratio']
    rentable = round(area * efficiency / 100.0, 2)

    deposit = _term('deposit_months', overrides.get('deposit_months'),
                    config.get('deposit_months', DEFAULT_TERMS['deposit_months']))
    free_rent = _term('free_rent_days', overrides.get('free_rent_days'),
                      config.get('free_rent_days', DEFAULT_TERMS['free_rent_days']))
    term_months = _term('lease_term_months', overrides.get('lease_term_months'),
                        config.get('lease_term_months', DEFAULT_TERMS['lease_term_months']))
    escalation = _term('escalation_pct', overrides.get('escalation_pct'),
                       config.get('escalation_pct', DEFAULT_TERMS['escalation_pct']))
    property_fee = _term('property_fee', overrides.get('property_fee'),
                         space_values.get('property_fee'), required=False)
    property_fee_estimated = property_fee is None
    if property_fee is None:
        property_fee = DEFAULT_TERMS['property_fee']
    validity_days = _term('validity_days', overrides.get('validity_days'),
                          config.get('validity_days', DEFAULT_TERMS['validity_days']))
    renovation_cost, renovation_estimated, delivery_status = _renovation_cost(
        space_values, overrides, config)
    renovation_subsidy = _term('renovation_subsidy', overrides.get('renovation_subsidy'),
                               DEFAULT_TERMS['renovation_subsidy'], required=False) or 0.0
    moving_subsidy = _term('moving_subsidy', overrides.get('moving_subsidy'),
                           DEFAULT_TERMS['moving_subsidy'], required=False) or 0.0

    note = str(overrides.get('note') or '').strip()
    if len(note) > 500:
        raise QuoteError('报价备注不能超过 500 字')

    # --- 让利与授权 ---
    authority = dict(DEFAULT_AUTHORITY)
    authority.update(config.get('authority') or {})
    ledger_rent = space_values.get('rent')
    discount_pct = 0.0
    if isinstance(ledger_rent, (int, float)) and not isinstance(ledger_rent, bool) \
            and ledger_rent > 0 and unit_price < ledger_rent:
        discount_pct = round((ledger_rent - unit_price) / ledger_rent * 100.0, 2)
    requires_approval = (free_rent > authority['max_free_rent_days']
                         or discount_pct > authority['max_discount_pct'])

    # --- 三年总占用成本 ---
    monthly_rent = round(unit_price * rentable, 2)
    factor = 1 + (1 + escalation / 100.0) + (1 + escalation / 100.0) ** 2
    rent_3y = round(12 * unit_price * rentable * factor, 2)
    property_3y = round(property_fee * rentable * 36, 2)
    renovation_input = round(renovation_cost * rentable, 2)
    subsidies_total = round(renovation_subsidy + moving_subsidy, 2)
    total_3y = round(rent_3y + property_3y + renovation_input - subsidies_total, 2)

    monthly_total = monthly_rent
    total = round(monthly_total * term_months, 2)

    assumptions: List[str] = [
        f'计租面积 = 登记面积 {area:g}㎡ × 得房率 {efficiency:g}% = {rentable:g}㎡'
        '（台账缺得房率时按 100% 计）',
        f'三年租金按年递增 {escalation:g}% 估算：12 × 月租金 × (1 + (1+递增) + (1+递增)²)',
        '三年总占用成本 = 三年租金 + 三年物业费 + 装修投入 − 装修/搬迁补贴；'
        '均为估算口径，行业参照，需园区确认。',
    ]
    if property_fee_estimated:
        assumptions.append('台账未登记物业费，按 0 元/㎡/月计，正式报价前需园区确认。')
    if renovation_estimated:
        if delivery_status:
            assumptions.append(f'装修投入按交付标准"{delivery_status}"的行业参照区间估算，需园区确认。')
        else:
            assumptions.append('台账未登记交付标准，装修投入按 0 计，正式报价前需园区确认。')

    return {
        'unit_price': unit_price,
        'unit_label': '元/㎡/月',
        'area': area,
        'area_label': '㎡',
        'efficiency_ratio': efficiency,
        'rentable_area': rentable,
        'monthly_total': monthly_total,
        'property_fee': property_fee,
        'property_fee_unit': '元/㎡/月',
        'escalation_pct': escalation,
        'deposit_months': deposit,
        'free_rent_days': free_rent,
        'lease_term_months': term_months,
        'validity_days': validity_days,
        'renovation_cost_per_sqm': renovation_cost,
        'delivery_status': delivery_status,
        'renovation_input': renovation_input,
        'renovation_subsidy': renovation_subsidy,
        'moving_subsidy': moving_subsidy,
        'rent_3y': rent_3y,
        'property_3y': property_3y,
        'subsidies_total': subsidies_total,
        'total_3y': total_3y,
        'ledger_rent': ledger_rent if isinstance(ledger_rent, (int, float)) else None,
        'discount_pct': discount_pct,
        'requires_approval': requires_approval,
        'total_revenue': total,
        'assumptions': assumptions,
        'note': note,
    }


def quote_caveats(conclusion: str) -> List[str]:
    """报价单必须随附的口径提示。"""
    caveats = [
        '报价单是草稿素材，不是正式承诺；对外发出前需业务与审批确认。',
        '租金口径为元/㎡/月，计租面积 = 登记面积 × 得房率（缺得房率按 100%）。',
        '三年总占用成本为估算口径：三年租金（含递增）+ 三年物业费 + 装修投入 − 补贴，行业参照，需园区确认。',
        '押金、免租期、租期为默认商务条款，以最终合同为准；报价单自生成起 15 天内有效（可按配置调整）。',
    ]
    if conclusion == '条件满足但溯源不全':
        caveats.append('该房源台账溯源信息不完整，报价参数需工程核实后复核。')
    return caveats


def authority_breach(terms: Dict[str, Any], config_quotation: Optional[Dict[str, Any]]) -> List[str]:
    """让利是否超授权，返回超出项说明（空列表表示未超）。"""
    authority = dict(DEFAULT_AUTHORITY)
    authority.update((config_quotation or {}).get('authority') or {})
    breaches: List[str] = []
    free_rent = terms.get('free_rent_days') or 0
    if free_rent > authority['max_free_rent_days']:
        breaches.append(f"免租 {free_rent:g} 天超过授权上限 {authority['max_free_rent_days']:g} 天")
    discount = terms.get('discount_pct') or 0
    if discount > authority['max_discount_pct']:
        breaches.append(f"单价折扣 {discount:g}% 超过授权上限 {authority['max_discount_pct']:g}%")
    return breaches


def validate_quote_action(current_status: str, action: str) -> str:
    """校验状态流转：draft → issued，draft/issued → voided。"""
    if action == 'issue':
        if current_status != QUOTE_DRAFT:
            raise QuoteError(f'只有草稿状态可以发出，当前为"{QUOTE_STATUS_LABELS.get(current_status, current_status)}"')
        return QUOTE_ISSUED
    if action == 'void':
        if current_status == QUOTE_VOIDED:
            raise QuoteError('报价单已作废，不能重复操作')
        return QUOTE_VOIDED
    raise QuoteError('未知报价单动作：只支持 issue（发出）与 void（作废）')


def validate_revision(quote_version: int, revision_reason: str) -> str:
    """修订报价必须说明原因；首次报价忽略修订原因。"""
    if quote_version > 1:
        reason = (revision_reason or '').strip()
        if len(reason) < 4:
            raise QuoteError('修订报价必须填写修订原因（至少 4 个字），说明与上一版的差异')
        return reason
    return (revision_reason or '').strip()


def quote_summary(quote: Dict[str, Any]) -> str:
    """给审计与列表用的一句话摘要。"""
    terms = quote.get('terms') or {}
    return (f"{quote.get('quote_no', '')} {quote.get('space_name', '')}：{terms.get('unit_price', '—')}"
            f"{terms.get('unit_label', '')} × 计租 {terms.get('rentable_area', terms.get('area', '—'))}"
            f"{terms.get('area_label', '')} = {terms.get('monthly_total', '—')} 元/月，"
            f"三年总占用成本 {terms.get('total_3y', '—')} 元")
