"""智能助手：基于当前工作状态的问答与内容生成。

- 已配置 AGENT_API_KEY 时调用对话模型生成回复，发送内容先脱敏；
- 未配置时退回本地规则助手：围绕当前需求卡生成补问清单、状态总结、
  报价口径说明、字段口径说明等结构化回复。
- 助手只读取数据并生成文本，不修改任何业务数据，因此不写审计留痕。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from ..api.context import get_config
from ..core.handover import validate_handover
from ..core.matching import MatchError, match
from ..extractors import ModelServiceError
from ..inventory import LedgerError, flatten_records
from ..schemas.fields import FIELDS, FIELD_KEYS, STATE_LABELS, label_of, unit_of
from ..security import redact
from ..storage import get_card, list_confirmations, list_quotes, list_spaces

STATUS_TEXT = {
    'draft': '草稿',
    'pending': '待交接',
    'handover': '已交接',
    'returned': '已退回',
    'escalated': '已升级',
}

QUOTE_STATUS_TEXT = {'draft': '草稿', 'issued': '已发出', 'voided': '已作废'}

CONCLUSION_TEXT = {
    '已填条件满足': '已填条件满足',
    '条件满足但溯源不全': '条件满足但溯源不全',
    '部分满足待核实': '部分满足待核实',
    '待核实': '待核实',
    '不满足': '不满足',
}

MAX_MESSAGE_LENGTH = 4000

HELP_TEXT = '''我可以围绕当前需求卡帮你生成与解答，试试这样问：
1. 生成补问清单 —— 按字段状态与校验结果产出向客户追问的问题
2. 总结当前需求卡 —— 字段裁定、校验、匹配与报价概况
3. 报价口径说明 —— 报价单 v2 的费用构成与三年总占用成本
4. 字段口径说明 —— 各需求字段含义、单位与填写要求
5. 匹配结论说明 —— 当前台账下的匹配结果解读

当前为本地规则模式（未配置模型密钥），回复基于系统内数据模板生成；
配置 AGENT_API_KEY / AGENT_MODEL 后可切换为 AI 模式自由问答。'''


def ai_enabled() -> bool:
    """是否已配置模型密钥。"""
    return bool(os.getenv('AGENT_API_KEY'))


# ---------------------------------------------------------------------------
# 上下文组装（只读）
# ---------------------------------------------------------------------------

def _fmt_value(item: Dict[str, Any]) -> str:
    value = item.get('value')
    if value is None or value == '':
        return '—'
    return str(value)


def _card_items_summary(card: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = []
    raw = card.get('items') or {}
    for key in FIELD_KEYS:
        entry = raw.get(key) or {}
        state = entry.get('state') or 'not_requested'
        items.append({
            'field': key,
            'label': label_of(key),
            'state': state,
            'state_label': STATE_LABELS.get(state, state),
            'value': _fmt_value(entry),
            'unit': unit_of(key) or '',
            'note': entry.get('note') or '',
        })
    return items


def build_context(card_id: str) -> Dict[str, Any]:
    """组装助手上下文。需求卡不存在时抛 KeyError。"""
    context: Dict[str, Any] = {}
    spaces = list_spaces()

    if card_id:
        card = get_card(card_id)
        items = _card_items_summary(card)
        validation = validate_handover(
            {'purpose': card.get('purpose'), 'items': dict(card.get('items') or {})},
            get_config(),
            list_confirmations(card_id),
        )
        context['card'] = {
            'id': card['id'],
            'title': card.get('title'),
            'customer': card.get('customer'),
            'purpose': card.get('purpose'),
            'status': STATUS_TEXT.get(card.get('status'), card.get('status')),
            'version': card.get('version'),
            'items': items,
        }
        context['validation'] = {
            'passed': bool(validation.get('passed')),
            'blocking_count': validation.get('blocking_count', 0),
            'advisory_count': validation.get('advisory_count', 0),
            'checklist': list(validation.get('checklist') or []),
            'questions': list(validation.get('questions') or []),
        }
        quotes = list_quotes(card_id)
        context['quotes'] = [{
            'quote_no': q.get('quote_no'),
            'space': q.get('space_name'),
            'unit_price': q.get('unit_price'),
            'rentable_area': (q.get('terms') or {}).get('rentable_area'),
            'total_3y': (q.get('terms') or {}).get('total_3y'),
            'status': QUOTE_STATUS_TEXT.get(q.get('status'), q.get('status')),
            'round': q.get('round'),
        } for q in quotes]

        # 匹配结论基于当前台账重算（与报告口径一致）
        specified = []
        for key, entry in (card.get('items') or {}).items():
            if (isinstance(entry, dict) and entry.get('state') == 'specified'
                    and entry.get('value') is not None):
                specified.append({'field': key, 'state': 'specified', 'value': entry['value']})
        if specified:
            try:
                result = match(specified, flatten_records(spaces),
                               get_config().get('manual_checks'))
                results = result.get('results') or []
                context['match'] = {
                    'total': len(results),
                    'conclusion_counts': _count_conclusions(results),
                    'top': [{'name': r.get('name'),
                             'conclusion': CONCLUSION_TEXT.get(r.get('conclusion'), r.get('conclusion'))}
                            for r in results[:5]],
                }
            except (MatchError, LedgerError):
                context['match'] = None
        else:
            context['match'] = None

    context['ledger'] = {
        'space_count': len(spaces),
        'trace_complete': sum(1 for s in spaces if s.get('trace_complete')),
    }
    return context


def _count_conclusions(results: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in results:
        label = CONCLUSION_TEXT.get(row.get('conclusion'), str(row.get('conclusion')))
        counts[label] = counts.get(label, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 本地规则助手
# ---------------------------------------------------------------------------

def _questions_from_context(context: Dict[str, Any]) -> List[str]:
    """从校验与字段状态生成补问问题。"""
    questions: List[str] = []
    validation = context.get('validation') or {}
    for q in (validation.get('questions') or []):
        questions.append(q if isinstance(q, str) else str(q.get('question') or ''))
    questions.extend(str(q) for q in (validation.get('checklist') or []))
    for entry in (context.get('card') or {}).get('items') or []:
        if entry['state'] == 'unknown':
            unit = f"（单位：{entry['unit']}）" if entry['unit'] else ''
            questions.append(f"{entry['label']}仍为未知待补，请向客户确认具体数值{unit}。")
    # 去重保序
    seen, output = set(), []
    for q in questions:
        if q and q not in seen:
            seen.add(q)
            output.append(q)
    return output


def _summarize(context: Dict[str, Any]) -> str:
    card = context.get('card')
    if not card:
        ledger = context.get('ledger') or {}
        return ('当前未选择需求卡。'
                f"台账现有房源 {ledger.get('space_count', 0)} 条，其中溯源完整 "
                f"{ledger.get('trace_complete', 0)} 条。选择需求卡后我可以生成补问清单、"
                '状态总结与报价口径说明。')
    lines = [f"【{card['title']}】（{card['status']}，版本 v{card['version']}）"]
    if card.get('purpose'):
        lines.append(f"客户用途：{card['purpose']}。")
    items = card.get('items') or []
    specified = [i for i in items if i['state'] == 'specified']
    unknown = [i for i in items if i['state'] == 'unknown']
    lines.append('已明确字段：' + ('、'.join(f"{i['label']} {i['value']}{i['unit']}" for i in specified) or '无'))
    lines.append('待补字段：' + ('、'.join(i['label'] for i in unknown) or '无'))
    validation = context.get('validation') or {}
    lines.append(f"交接校验：{'已通过' if validation.get('passed') else '未通过'}"
                 f"（阻断 {validation.get('blocking_count', 0)} 项 / 提示 {validation.get('advisory_count', 0)} 项）。")
    match_info = context.get('match')
    if match_info:
        counts = '、'.join(f"{k} {v} 条" for k, v in (match_info.get('conclusion_counts') or {}).items())
        lines.append(f"当前台账匹配：共 {match_info.get('total', 0)} 条（{counts or '无结果'}）。")
    quotes = context.get('quotes') or []
    if quotes:
        lines.append(f"报价单 {len(quotes)} 张：" + '；'.join(
            f"{q['quote_no']} {q['space']}（{q['status']}）" for q in quotes[:3]) + '。')
    else:
        lines.append('暂无报价单。')
    return '\n'.join(lines)


def _quote_explain(context: Dict[str, Any]) -> str:
    lines = [
        '报价单 v2 口径：',
        '- 计租面积 = 登记面积 × 得房率（台账缺得房率按 100%）',
        '- 三年总占用成本 = 三年租金（含年递增）+ 三年物业费 + 装修投入 − 补贴',
        '- 装修投入按交付标准查表估算（L1 300–600、L2 100–300、L3 0–30、毛坯 300–800 元/㎡ 取中值），行业参照，需园区确认',
        '- 报价有效期默认 15 天；免租超 92 天或单价折扣超 5% 须管理员授权后才能发出',
        '- 需求卡明确了承重或供电容量时，发出前须有对应字段的专业确认',
    ]
    quotes = context.get('quotes') or []
    if quotes:
        lines.append('当前需求卡报价单：')
        for q in quotes:
            price = f"，单价 {q['unit_price']} 元/㎡/月" if q.get('unit_price') is not None else ''
            total = f"，三年总占用成本 {q['total_3y']} 元" if q.get('total_3y') is not None else ''
            lines.append(f"- {q['quote_no']} {q['space']}（{q['status']}{price}{total}）")
    return '\n'.join(lines)


def _fields_explain() -> str:
    lines = ['需求字段口径：']
    for key in FIELD_KEYS:
        unit = f"，单位 {unit_of(key)}" if unit_of(key) else '，枚举字段'
        note = f"：{FIELDS[key].note}" if FIELDS[key].note else '。'
        lines.append(f"- {label_of(key)}（{key}）{unit}{note}")
    return '\n'.join(lines)


def _match_explain(context: Dict[str, Any]) -> str:
    match_info = context.get('match')
    if not match_info:
        return ('当前需求卡没有已明确数值的字段，无法给出匹配结论。'
                '请先在"02 访谈抽取 / 03 字段裁定"中明确至少一个字段（如面积、租金）。')
    lines = [f"基于当前台账重算，共 {match_info['total']} 条房源结果："]
    for name, count in (match_info.get('conclusion_counts') or {}).items():
        lines.append(f"- {name}：{count} 条")
    top = match_info.get('top') or []
    if top:
        lines.append('前几条：' + '；'.join(f"{t['name']}（{t['conclusion']}）" for t in top))
    lines.append('注意：只有"已填条件满足"或"条件满足但溯源不全"的房源可以生成报价单。')
    return '\n'.join(lines)


def rule_reply(message: str, context: Dict[str, Any]) -> str:
    """本地规则助手：按关键词意图生成结构化回复。"""
    text = message.strip()
    if any(k in text for k in ('补问', '追问', '问题清单', '还要问')):
        questions = _questions_from_context(context)
        if questions:
            return '建议向客户补问以下内容：\n' + '\n'.join(
                f"{i}. {q}" for i, q in enumerate(questions, 1))
        return '当前需求卡没有需要补问的未知字段；如需重新梳理，可先在访谈抽取中补充内容。'
    if any(k in text for k in ('总结', '摘要', '概况', '现状')):
        return _summarize(context)
    if any(k in text for k in ('报价', '三年', '总占用成本', '物业费', '得房率', '装修')):
        return _quote_explain(context)
    if any(k in text for k in ('字段', '口径', '什么意思', '怎么填', '含义')):
        return _fields_explain()
    if any(k in text for k in ('匹配', '房源', '哪个适合', '推荐')):
        return _match_explain(context)
    if any(k in text for k in ('你好', '您好', 'hi', 'hello', '你是谁', '你能')):
        return HELP_TEXT
    return '我暂时只能识别固定意图（本地规则模式）。\n\n' + HELP_TEXT


# ---------------------------------------------------------------------------
# AI 模式
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = '''你是园区招商工作台内嵌的智能助手。依据"工作台上下文"中的真实数据回答用户问题，帮助用户生成补问清单、需求总结、报价口径说明等内容。

要求：
- 全部用中文，简洁分点，直接给出可用的文字成果。
- 只使用上下文中给出的数据，不得编造数值；上下文没有的信息明确说明"系统中暂无"。
- 涉及报价口径时遵循：计租面积=登记面积×得房率；三年总占用成本=三年租金（含递增）+三年物业费+装修投入−补贴；装修与物业估算均为行业参照，需园区确认。
- 你只能生成文字，不能修改系统数据；涉及发报价、审批等操作时提示用户在工作台对应面板操作。'''


def ai_reply(message: str, context: Dict[str, Any]) -> str:
    """AI 模式：调用对话模型生成回复。发送内容已脱敏。"""
    api_key = os.getenv('AGENT_API_KEY')
    model = os.getenv('AGENT_MODEL')
    if not api_key or not model:
        raise ModelServiceError('未配置 AGENT_API_KEY / AGENT_MODEL，无法使用 AI 模式')

    user_content = (message + '\n\n[工作台上下文]\n'
                    + json.dumps(redact(context), ensure_ascii=False, default=str))
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        'temperature': 0.3,
    }
    url = os.getenv('AGENT_BASE_URL', 'https://api.openai.com/v1').rstrip('/') + '/chat/completions'
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            envelope = json.load(response)
        content = envelope['choices'][0]['message']['content']
    except urllib.error.HTTPError as exc:  # pragma: no cover - 依赖外部服务
        raise ModelServiceError(f'模型服务返回 {exc.code}：{exc.read(300).decode("utf-8", "ignore")}') from exc
    except urllib.error.URLError as exc:  # pragma: no cover - 依赖外部服务
        raise ModelServiceError(f'无法连接模型服务：{exc.reason}') from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelServiceError('模型返回格式无效') from exc
    if not isinstance(content, str) or not content.strip():
        raise ModelServiceError('模型未返回有效内容')
    return content.strip()


def assistant_reply(message: str, card_id: str = '') -> Any:
    """生成助手回复。返回 (reply, mode)。

    AI 模式调用失败时自动回落到本地规则回复，保证助手始终可用。
    """
    if not isinstance(message, str) or not message.strip():
        raise ValueError('请输入问题')
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError(f'问题不得超过 {MAX_MESSAGE_LENGTH} 字符')
    try:
        context = build_context(card_id)
    except KeyError as exc:
        raise ValueError('需求卡不存在，请先选择需求卡') from exc

    if ai_enabled():
        try:
            return ai_reply(message, context), 'ai'
        except ModelServiceError:
            pass  # 回落本地规则
    return rule_reply(message, context), 'rule'
