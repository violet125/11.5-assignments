"""Markdown 报告导出。

把需求卡的字段裁定、交接校验、复核与专业确认、空间匹配结论与报价单
汇总为一份 Markdown 文本，供人工存档与传阅。报告在服务端生成，
空间匹配基于当前台账重算，不等于历史匹配时刻的结论。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..schemas.fields import FIELD_KEYS, STATE_LABELS, label_of, unit_of

STATUS_LABELS = {
    'draft': '草稿',
    'pending': '待交接',
    'handover': '已交接',
    'returned': '已退回',
    'escalated': '已升级',
}

QUOTE_STATUS_LABELS = {
    'draft': '草稿',
    'issued': '已发出',
    'voided': '已作废',
}


def _fmt(value: Any) -> str:
    if value is None or value == '':
        return '—'
    return str(value)


def build_markdown_report(card: Dict[str, Any], validation: Dict[str, Any],
                          match_result: Optional[Dict[str, Any]],
                          quotes: List[Dict[str, Any]],
                          generated_at: str) -> str:
    """汇总需求卡数据为 Markdown 文本。"""
    lines: List[str] = []
    lines.append(f"# 需求与匹配报告：{_fmt(card.get('title'))}")
    lines.append('')
    lines.append(f"- 报告生成时间：{generated_at}（UTC）")
    lines.append(f"- 需求卡：{card.get('id')}（v{card.get('version', 1)}）")
    lines.append(f"- 客户：{_fmt(card.get('customer'))}")
    lines.append(f"- 客户用途：{_fmt(card.get('purpose'))}")
    lines.append(f"- 当前状态：{STATUS_LABELS.get(card.get('status'), card.get('status'))}")

    # 一、字段裁定
    lines.append('')
    lines.append('## 一、字段裁定')
    lines.append('')
    lines.append('| 字段 | 状态 | 数值 | 原文证据 |')
    lines.append('| --- | --- | --- | --- |')
    items = card.get('items') or {}
    for key in FIELD_KEYS:
        item = items.get(key) or {}
        state = item.get('state') or 'unknown'
        value = item.get('value')
        text = f"{value}{unit_of(key)}" if value is not None else '—'
        evidence = (item.get('evidence') or '—').replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {label_of(key)} | {STATE_LABELS.get(state, state)} | {text} | {evidence} |")

    # 二、交接校验
    lines.append('')
    lines.append('## 二、交接校验')
    lines.append('')
    if validation and validation.get('issues') is not None:
        verdict = '通过' if validation.get('passed') else '未通过'
        lines.append(f"- 结论：**{verdict}**（阻断 {validation.get('blocking_count', 0)} 项，"
                     f"提示 {validation.get('advisory_count', 0)} 项）")
        blocking = validation.get('blocking') or []
        if blocking:
            lines.append('- 阻断项：')
            for issue in blocking:
                lines.append(f"  - {_fmt(issue.get('message'))}")
        advisory = validation.get('advisory') or []
        if advisory:
            lines.append('- 提示项：')
            for issue in advisory:
                lines.append(f"  - {_fmt(issue.get('message'))}")
        questions = validation.get('questions') or []
        if questions:
            lines.append('- 补问清单：')
            for question in questions:
                lines.append(f'  - {question}')
    else:
        lines.append('- 尚未校验。')

    # 三、复核与专业确认
    lines.append('')
    lines.append('## 三、复核与专业确认')
    lines.append('')
    history = card.get('review_history') or card.get('reviews') or []
    if history:
        lines.append('### 复核记录')
        lines.append('')
        for entry in history:
            lines.append(f"- {entry.get('action_label', entry.get('action', ''))} · "
                         f"{entry.get('actor') or '未署名'}（{entry.get('actor_role') or '—'}） · "
                         f"引用 v{entry.get('ref_version', 1)} · {entry.get('created_at', '')}")
            if entry.get('reason'):
                lines.append(f"  - 理由：{entry['reason']}")
            if entry.get('resolution'):
                lines.append(f"  - 处理：{entry['resolution']}")
    else:
        lines.append('- 暂无复核记录。')
    confirmations = card.get('confirmations') or []
    if confirmations:
        lines.append('')
        lines.append('### 专业确认')
        lines.append('')
        for entry in confirmations:
            if entry.get('valid') is False:
                continue
            lines.append(f"- {label_of(entry.get('field', ''))} · "
                         f"{entry.get('by') or '未署名'}（{entry.get('role') or '—'}） · "
                         f"{entry.get('created_at', '')}"
                         + (f" · 备注：{entry['note']}" if entry.get('note') else ''))

    # 四、空间匹配
    lines.append('')
    lines.append('## 四、空间匹配（基于当前台账重算）')
    lines.append('')
    if match_result is None:
        lines.append('- 需求卡没有已明确的字段，未执行匹配。')
    else:
        lines.append(f"- 匹配结论：满足 {match_result.get('matched_count', 0)}，"
                     f"待核实 {match_result.get('unverified_count', 0)}，"
                     f"不匹配 {match_result.get('failed_count', 0)}。")
        missing = match_result.get('missing_requirements') or []
        if missing:
            lines.append(f"- 未参与匹配的字段：{'、'.join(missing)}")
        lines.append('')
        for row in match_result.get('results') or []:
            lines.append(f"### {row.get('name')} — {row.get('status')}")
            lines.append('')
            for check in row.get('checks') or []:
                actual = check.get('actual')
                actual_text = '未知' if actual is None else f"{actual}{check.get('unit') or ''}"
                required = check.get('required')
                required_text = '—' if required is None else f"{required}{check.get('unit') or ''}"
                lines.append(f"- {check.get('label')}:需求 {required_text} / 房源 {actual_text}"
                             f" → {check.get('status')}（{check.get('basis')}）")
            if not row.get('trace_complete'):
                lines.append('- 溯源信息不完整，该结论仅为待核实素材，不可作为交付依据。')
            lines.append('')
    manual = match_result.get('manual_checks') if match_result else None
    if manual:
        lines.append('### 交接前待人工核实')
        lines.append('')
        for item in manual:
            lines.append(f'- {item}')

    # 五、报价单
    lines.append('')
    lines.append('## 五、报价单')
    lines.append('')
    if quotes:
        for quote in quotes:
            terms = quote.get('terms') or {}
            status = quote.get('status')
            head = quote.get('quote_no') or quote.get('id', '')
            version = quote.get('quote_version')
            round_label = quote.get('round') or ('首次报价' if version == 1 else '修订报价')
            lines.append(f"### {head} {quote.get('space_name')} — "
                         f"{QUOTE_STATUS_LABELS.get(status, status)}"
                         f"（v{version or 1} · {round_label}）")
            lines.append('')
            lines.append(f"- 月单价：{terms.get('unit_price', '—')}{terms.get('unit_label', '')}"
                         f"（台账价 {terms.get('ledger_rent') if terms.get('ledger_rent') is not None else '—'}，"
                         f"折扣 {terms.get('discount_pct', 0)}%）")
            lines.append(f"- 登记面积：{terms.get('area', '—')}{terms.get('area_label', '')} × "
                         f"得房率 {terms.get('efficiency_ratio', '—')}% = "
                         f"计租面积 {terms.get('rentable_area', '—')}㎡")
            lines.append(f"- 月租金合计：{terms.get('monthly_total', '—')} 元/月 · "
                         f"物业费：{terms.get('property_fee', '—')} {terms.get('property_fee_unit', '元/㎡/月')} · "
                         f"年递增：{terms.get('escalation_pct', 0)}%")
            lines.append(f"- 押金：{terms.get('deposit_months', '—')} 个月 · "
                         f"免租：{terms.get('free_rent_days', '—')} 天 · "
                         f"租期：{terms.get('lease_term_months', '—')} 个月 · "
                         f"有效期：{terms.get('validity_days', '—')} 天（至 {quote.get('valid_until') or '—'}）")
            lines.append(f"- 租期总收入估算：{terms.get('total_revenue', '—')} 元")
            lines.append(f"- 三年总占用成本：{terms.get('total_3y', '—')} 元"
                         f"（三年租金 {terms.get('rent_3y', '—')} + 三年物业费 {terms.get('property_3y', '—')}"
                         f" + 装修投入 {terms.get('renovation_input', '—')}"
                         f" − 补贴 {terms.get('subsidies_total', '—')}）"
                         f"〔装修单价 {terms.get('renovation_cost_per_sqm', 0)} 元/㎡"
                         f" × 计租面积，交付标准 {terms.get('delivery_status') or '未登记'}；行业参照，需园区确认〕")
            if terms.get('renovation_subsidy') or terms.get('moving_subsidy'):
                lines.append(f"- 补贴：装修 {terms.get('renovation_subsidy', 0)} 元 · "
                             f"搬迁 {terms.get('moving_subsidy', 0)} 元")
            if terms.get('requires_approval'):
                approved = quote.get('approved_by')
                lines.append(f"- 让利授权：**超出授权额度**"
                             + (f"，已由 {approved} 审批（{quote.get('approved_at', '')}）" if approved
                                else '，**尚未授权审批，不得发出**'))
            for assumption in terms.get('assumptions') or []:
                lines.append(f"- 口径：{assumption}")
            if terms.get('note'):
                lines.append(f"- 备注：{terms['note']}")
            lines.append(f"- 生成：{quote.get('actor') or '未署名'} · {quote.get('created_at', '')}")
    else:
        lines.append('- 暂无报价单。')

    # 口径提示
    lines.append('')
    lines.append('## 口径提示')
    lines.append('')
    lines.append('- 报价单是草稿素材，不是正式承诺；租金口径为元/㎡/月，计租面积 = 登记面积 × 得房率。')
    lines.append('- 三年总占用成本与装修/物业估算为行业参照，需园区确认后方可对外。')
    lines.append('- 让利超授权（免租超 92 天或折扣超 5%，按配置）的报价单须经管理员授权审批后才能发出。')
    lines.append('- 审计留痕带 hash 链，可通过 /api/audit 的 chain_valid 校验是否被篡改。')
    lines.append('- 本报告的空间匹配结论基于生成时刻的台账重算，不代表历史匹配时刻的结论。')
    lines.append('- "已填条件满足"仅表示已录入的数值条件通过，不等于可交付承诺；'
                 '行业准入、消防、交付日期等仍需人工核实。')
    lines.append('- 本报告不替代独立审计；交接遗漏率等业务口径须在试点前确认。')
    lines.append('')
    return '\n'.join(lines)
