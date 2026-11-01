"""HTTP API 路由。

所有写操作都需要角色标识（X-Actor-Role 头或请求体 actor_role）。
权限检查在后端执行，前端隐藏按钮不构成权限控制。
"""

from __future__ import annotations

import json
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.assistant import assistant_reply
from ..core.handover import (
    BLOCKING,
    STATUS_DRAFT,
    STATUS_ESCALATED,
    STATUS_HANDOVER,
    STATUS_PENDING,
    STATUS_RETURNED,
    HandoverError,
    can_handover,
    can_submit,
    summarize,
    validate_handover,
)
from ..core.matching import MatchError, match
from ..core.quotation import (
    QUOTE_ELIGIBLE_CONCLUSIONS,
    QuoteError,
    authority_breach,
    build_terms,
    quote_caveats,
    quote_summary,
    validate_quote_action,
)
from ..core.report import build_markdown_report
from ..extractors import ModelServiceError, extract
from ..inventory import LedgerError, flatten_records, ledger_issues, ledger_template, normalize_ledger
from ..review import RETURN_REASON_CODES, ReviewError, review_history, validate_action
from ..review import apply_action as apply_review_action
from ..schemas.fields import (
    FIELDS,
    FIELD_KEYS,
    STATE_LABELS,
    STATE_NONE_CONFIRMED,
    STATE_NOT_REQUESTED,
    STATE_SPECIFIED,
    STATE_UNKNOWN,
    ensure_all_fields,
    label_of,
    normalize_item,
    unit_of,
)
from ..security import (
    PermissionError_,
    load_role_permissions,
    mask_value,
    redact,
    require_permission,
    require_professional_clearing,
    visible_fields,
)
from ..storage import (
    add_audit,
    add_confirmation,
    add_review,
    audit_chain_valid,
    count_cards,
    create_card,
    create_quote,
    delete_card,
    get_card,
    get_quote,
    list_audit,
    list_cards,
    list_confirmations,
    list_quotes,
    list_reviews,
    list_spaces,
    metrics,
    now,
    save_spaces,
    update_card,
    update_quote_approval,
    update_quote_status,
)
from .context import get_config


class ApiError(Exception):
    """API 业务错误。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _role(payload: Dict[str, Any], headers: Dict[str, str]) -> str:
    role = headers.get('x-actor-role') or (payload or {}).get('actor_role') or ''
    role = role.strip()
    if role not in ('business', 'engineer', 'reviewer', 'admin'):
        raise ApiError(400, '缺少或无效的角色标识（business / engineer / reviewer / admin）')
    return role


def _actor(payload: Dict[str, Any], headers: Dict[str, str]) -> str:
    actor = headers.get('x-actor') or (payload or {}).get('actor') or ''
    return str(actor).strip()


def _items_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get('items')
    if raw is None:
        return {}
    if isinstance(raw, dict):
        raw = [{'field': key, **value} if isinstance(value, dict) else {'field': key, 'value': value}
               for key, value in raw.items()]
    if not isinstance(raw, list):
        raise ApiError(400, 'items 必须为数组')
    items: Dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ApiError(400, '需求条目必须为对象')
        try:
            normalized = normalize_item(entry)
        except Exception as exc:
            raise ApiError(400, str(exc)) from exc
        items[normalized['field']] = normalized
    return ensure_all_fields(items)


# ---------------------------------------------------------------------------
# GET 路由
# ---------------------------------------------------------------------------

def get_config_endpoint(_payload: Dict[str, Any], _headers: Dict[str, str]) -> Dict[str, Any]:
    """前端启动所需配置：字段字典、用途规则、角色、当前模式。"""
    config = get_config()
    return {
        'fields': {key: spec.to_dict() for key, spec in FIELDS.items()},
        'state_labels': dict(STATE_LABELS),
        'purposes': config.get('purposes') or {},
        'roles': config.get('roles') or {},
        'manual_checks': config.get('manual_checks') or [],
        'return_reasons': dict(RETURN_REASON_CODES),
        'ai_enabled': bool(_config_ai_enabled()),
        'redaction_masked_fields': config.get('redaction', {}).get('masked_for_business') or [],
    }


def _config_ai_enabled() -> bool:
    import os
    return bool(os.getenv('AGENT_API_KEY'))


def get_spaces_endpoint(_payload: Dict[str, Any], _headers: Dict[str, str]) -> Dict[str, Any]:
    """当前台账。"""
    records = list_spaces()
    return {'spaces': records, 'count': len(records)}


def get_spaces_template_endpoint(_payload: Dict[str, Any], _headers: Dict[str, str]) -> Dict[str, Any]:
    """下载用模板。"""
    return {'spaces': ledger_template()}


def get_cards_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """需求卡列表：支持关键词搜索、状态筛选与分页。"""
    status = str(payload.get('status') or '')
    q = str(payload.get('q') or '').strip()
    try:
        page = int(payload.get('page') or 0)
        page_size = int(payload.get('page_size') or 0)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, 'page 与 page_size 必须为整数') from exc
    if page_size and not 1 <= page_size <= 100:
        raise ApiError(400, 'page_size 必须在 1 ~ 100 之间')
    if page and page < 1:
        raise ApiError(400, 'page 必须从 1 开始')
    if page_size and not page:
        page = 1
    cards = list_cards(status=status, q=q, page=page, page_size=page_size)
    role = headers.get('x-actor-role') or 'admin'
    for card in cards:
        card['customer'] = mask_value(role, 'customer', card.get('customer'), get_config())
    response: Dict[str, Any] = {'cards': cards}
    if page:
        response['total'] = count_cards(status=status, q=q)
        response['page'] = page
        response['page_size'] = page_size
    return response


def get_card_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """需求卡详情，含校验结果与复核历史。"""
    card_id = str(payload.get('card_id') or '')
    try:
        card = get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc
    role = headers.get('x-actor-role') or 'admin'
    card['customer'] = mask_value(role, 'customer', card.get('customer'), get_config())
    card['review_history'] = review_history(card.get('reviews') or [])
    card['field_dictionary'] = {key: FIELDS[key].to_dict() for key in FIELD_KEYS}
    card['visible_fields'] = visible_fields(role, list(FIELD_KEYS), get_config())
    return card


def get_audit_endpoint(payload: Dict[str, Any], _headers: Dict[str, str]) -> Dict[str, Any]:
    """审计留痕（含 hash 链校验结果）。"""
    card_id = str(payload.get('card_id') or '')
    limit = int(payload.get('limit') or 200)
    return {'audit': list_audit(card_id, limit), 'chain_valid': audit_chain_valid()}


def get_quotes_endpoint(payload: Dict[str, Any], _headers: Dict[str, str]) -> Dict[str, Any]:
    """报价单列表，可按需求卡过滤。"""
    card_id = str(payload.get('card_id') or '')
    return {'quotes': list_quotes(card_id)}


def get_card_report_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Markdown 报告：字段裁定、交接校验、复核、匹配（当前台账重算）与报价单。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'report.export', permissions)
    card_id = str(payload.get('card_id') or '')
    try:
        card = get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc

    items = ensure_all_fields(dict(card.get('items') or {}))
    validation = validate_handover(
        {'purpose': card.get('purpose'), 'items': items},
        get_config(),
        list_confirmations(card_id),
    )
    match_result: Optional[Dict[str, Any]] = None
    specified = [entry for entry in items.values()
                 if entry.get('state') == 'specified' and entry.get('value') is not None]
    if specified:
        try:
            match_result = match(specified, flatten_records(list_spaces()),
                                 get_config().get('manual_checks'))
        except (MatchError, LedgerError):
            match_result = None

    card['customer'] = mask_value(role, 'customer', card.get('customer'), get_config())
    card['review_history'] = review_history(card.get('reviews') or [])
    markdown = build_markdown_report(
        card, validation, match_result, list_quotes(card_id), now())
    return {'filename': f'需求报告_{card_id}.md', 'markdown': markdown}


def get_metrics_endpoint(_payload: Dict[str, Any], _headers: Dict[str, str]) -> Dict[str, Any]:
    """过程指标。"""
    return metrics()


# ---------------------------------------------------------------------------
# POST 路由
# ---------------------------------------------------------------------------

def post_extract_endpoint(payload: Dict[str, Any], _headers: Dict[str, str]) -> Dict[str, Any]:
    """访谈抽取。发往模型前先脱敏。"""
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, '请填写访谈内容')
    sanitized = redact(text)
    try:
        result = extract(sanitized)
    except ModelServiceError as exc:
        raise ApiError(502, str(exc)) from exc
    except Exception as exc:
        raise ApiError(400, str(exc)) from exc
    result['redacted'] = sanitized != text
    return result


def post_cards_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """新建需求卡。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'card.create', permissions)
    title = str(payload.get('title') or '').strip()
    if not title:
        raise ApiError(400, '请填写需求卡标题')
    items = _items_from_payload(payload)
    card = create_card(
        title=title,
        source=str(payload.get('source') or ''),
        purpose=str(payload.get('purpose') or ''),
        customer=str(payload.get('customer') or ''),
        items=items,
        mode=str(payload.get('mode') or ''),
    )
    add_audit('card.create', card['id'], _actor(payload, headers), role, {'title': title})
    return card


def put_card_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """更新需求卡字段。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'card.edit', permissions)
    card_id = str(payload.get('card_id') or '')
    try:
        card = get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc

    updates: Dict[str, Any] = {}
    for key in ('title', 'customer', 'purpose'):
        if key in payload:
            updates[key] = str(payload[key] or '').strip()
    if 'items' in payload:
        updates['items'] = _items_from_payload(payload)

    changed = sorted(updates.keys())
    card = update_card(card_id, **updates)
    add_audit('card.edit', card_id, _actor(payload, headers), role, {'fields': changed},
              card.get('version', 1))
    return card


def post_card_submit_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """交接提交：先跑校验，有阻断项则退回补问。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'card.submit', permissions)
    card_id = str(payload.get('card_id') or '')
    try:
        card = get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc

    if not can_submit(card['status']):
        raise ApiError(409, f'当前状态"{card["status"]}"不允许发起交接提交')

    items = ensure_all_fields(dict(card.get('items') or {}))
    confirmations = list_confirmations(card_id)
    validation = validate_handover(
        {'purpose': card.get('purpose'), 'items': items},
        get_config(),
        confirmations,
    )

    if not validation['passed']:
        card = update_card(card_id, items=items, validation=validation)
        add_audit('handover.blocked', card_id, _actor(payload, headers), role,
                  {'blocking_count': validation['blocking_count']}, card.get('version', 1))
        return {
            'submitted': False,
            'status': card['status'],
            'validation': validation,
            'summary': summarize(validation),
            'card': card,
        }

    new_status = STATUS_HANDOVER if payload.get('complete') else STATUS_PENDING
    if not can_handover(card['status'], validation):
        raise ApiError(409, '当前状态不允许完成交接')
    card = update_card(card_id, items=items, validation=validation, status=new_status,
                       version=int(card.get('version', 1)) + 1)
    add_audit('handover.submit', card_id, _actor(payload, headers), role,
              {'status': new_status}, card.get('version', 1))
    return {
        'submitted': True,
        'status': new_status,
        'validation': validation,
        'summary': summarize(validation),
        'card': card,
    }


def post_review_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """复核动作：接受 / 修改等级 / 修改解释 / 驳回 / 升级。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'card.review', permissions)
    card_id = str(payload.get('card_id') or '')
    try:
        card = get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc

    try:
        action_result = validate_action(
            str(payload.get('action') or ''),
            str(payload.get('reason') or ''),
            payload.get('detail') or {},
            role,
        )
    except ReviewError as exc:
        raise ApiError(400, str(exc)) from exc

    applied = apply_review_action(card, action_result)
    record = applied['record']
    actor = _actor(payload, headers)

    updated = update_card(card_id, status=applied['status'],
                          items=applied['card'].get('items'),
                          level_adjustments=applied['card'].get('level_adjustments'))
    saved = add_review(card_id, record, actor)
    add_audit(f"review.{record['action']}", card_id, actor, role,
              {'reason': record.get('reason', ''), 'detail': record.get('detail') or {}},
              record.get('ref_version', 1))

    return {
        'action': record['action'],
        'action_label': record['action_label'],
        'status': updated['status'],
        'review': saved,
        'card': updated,
    }


def post_confirmation_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """专业确认。业务角色不可解除专业阻断。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'card.confirm_professional', permissions)
    require_professional_clearing(role, get_config())
    card_id = str(payload.get('card_id') or '')
    try:
        get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc
    field = str(payload.get('field') or '')
    if field not in FIELDS:
        raise ApiError(400, f'未知字段：{field}')
    actor = _actor(payload, headers)
    confirmation = add_confirmation(card_id, field, actor, role, str(payload.get('note') or ''))
    add_audit('professional.confirm', card_id, actor, role, {'field': field},
              get_card(card_id).get('version', 1))
    return confirmation


def post_quotes_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """生成报价单草稿。

    服务端按需求卡当前字段重跑硬条件匹配：只有"已填条件满足"或
    "条件满足但溯源不全"的房源可以生成报价单。台账未登记租金或面积时
    必须人工覆盖，不能用 0 或估计值悄悄代替。
    """
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'quote.create', permissions)
    card_id = str(payload.get('card_id') or '')
    try:
        card = get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc
    space_name = str(payload.get('space_name') or '').strip()
    if not space_name:
        raise ApiError(400, '请指定房源名称')

    items = ensure_all_fields(dict(card.get('items') or {}))
    specified = [entry for entry in items.values()
                 if entry.get('state') == 'specified' and entry.get('value') is not None]
    if not specified:
        raise ApiError(400, '需求卡没有已明确的字段，请先完成字段裁定与匹配')
    ledger_records = list_spaces()
    spaces = flatten_records(ledger_records)
    try:
        match_result = match(specified, spaces, get_config().get('manual_checks'))
    except (MatchError, LedgerError) as exc:
        raise ApiError(400, str(exc)) from exc
    row = next((r for r in match_result['results'] if r['name'] == space_name), None)
    if row is None:
        raise ApiError(404, f'台账中不存在房源：{space_name}')
    if row['status'] not in QUOTE_ELIGIBLE_CONCLUSIONS:
        raise ApiError(409, f'房源"{space_name}"匹配结论为"{row["status"]}"，'
                            '只有"已填条件满足"或"条件满足但溯源不全"的房源可以生成报价单')

    space_values = next((r['values'] for r in ledger_records if r['name'] == space_name), {})
    try:
        terms = build_terms(space_values, payload.get('overrides'),
                            get_config().get('quotation'))
    except QuoteError as exc:
        raise ApiError(400, str(exc)) from exc

    actor = _actor(payload, headers)
    revision_reason = str(payload.get('revision_reason') or '')
    try:
        quote = create_quote(card_id, space_name, terms, row['status'], actor, role,
                             revision_reason=revision_reason)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    detail = {'quote_id': quote['id'], 'quote_no': quote.get('quote_no', ''),
              'quote_version': quote.get('quote_version', 1), 'round': quote.get('round', ''),
              'space': space_name, 'summary': quote_summary(quote)}
    if quote.get('superseded'):
        detail['superseded'] = quote['superseded']
    add_audit('quote.create', card_id, actor, role, detail, card.get('version', 1))
    return {'quote': quote, 'caveats': quote_caveats(row['status'])}


def post_quote_status_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """报价单状态流转：issue（草稿→已发出）与 void（作废）。

    issue 门槛（融景园区口径）：
    1. 让利超授权（免租超 92 天或折扣超 5%）须管理员先授权审批；
    2. 需求卡明确了 load/power 时，发出前须有对应字段的有效专业确认附页。
    """
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'quote.create', permissions)
    quote_id = str(payload.get('quote_id') or '')
    try:
        quote = get_quote(quote_id)
    except KeyError as exc:
        raise ApiError(404, '报价单不存在') from exc
    action = str(payload.get('action') or '')
    try:
        new_status = validate_quote_action(quote['status'], action)
    except QuoteError as exc:
        raise ApiError(409, str(exc)) from exc

    if action == 'issue':
        terms = quote.get('terms') or {}
        breaches = authority_breach(terms, get_config().get('quotation'))
        if breaches and not quote.get('approved_by'):
            raise ApiError(409, '让利超出授权，须管理员先授权审批后才能发出：' + '；'.join(breaches))
        card = get_card(quote['card_id'])
        items = card.get('items') or {}
        need_confirm = [key for key in ('load', 'power')
                        if (items.get(key) or {}).get('state') == 'specified'
                        and (items.get(key) or {}).get('value') is not None]
        if need_confirm:
            confirmed = {c['field'] for c in (card.get('confirmations') or [])
                         if c.get('valid') is not False}
            missing = [key for key in need_confirm if key not in confirmed]
            if missing:
                labels = '、'.join(FIELDS[key].label for key in missing)
                raise ApiError(409, f'需求卡明确了{labels}，发出前须完成对应字段的专业确认（专业确认附页）')

    quote = update_quote_status(quote_id, new_status)
    add_audit(f'quote.{action}', quote['card_id'], _actor(payload, headers), role,
              {'quote_id': quote_id, 'quote_no': quote.get('quote_no', ''),
               'space': quote['space_name'], 'summary': quote_summary(quote)},
              quote.get('card_version', 1))
    return {'quote': quote}


def post_quote_approve_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """让利授权审批：仅管理员。免租/折扣超授权的报价单发出前必须先审批。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'quote.approve', permissions)
    quote_id = str(payload.get('quote_id') or '')
    try:
        quote = get_quote(quote_id)
    except KeyError as exc:
        raise ApiError(404, '报价单不存在') from exc
    if quote['status'] != 'draft':
        raise ApiError(409, '只有草稿状态的报价单需要授权审批')
    actor = _actor(payload, headers)
    quote = update_quote_approval(quote_id, actor or role)
    add_audit('quote.approve', quote['card_id'], actor, role,
              {'quote_id': quote_id, 'quote_no': quote.get('quote_no', ''),
               'space': quote['space_name'],
               'breaches': authority_breach(quote.get('terms') or {},
                                            get_config().get('quotation'))},
              quote.get('card_version', 1))
    return {'quote': quote}


def post_spaces_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """导入房源台账。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'space.import', permissions)
    raw = payload.get('spaces')
    try:
        normalized = normalize_ledger(raw)
    except LedgerError as exc:
        raise ApiError(400, str(exc)) from exc
    count = save_spaces(normalized)
    issues = ledger_issues(normalized)
    add_audit('space.import', '', _actor(payload, headers), role, {'count': count, 'issues': issues})
    return {'count': count, 'issues': issues, 'spaces': list_spaces()}


def post_match_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """空间匹配。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, 'match.run', permissions)
    requirements = payload.get('requirements')
    if requirements is None:
        requirements = payload.get('items')
    spaces = payload.get('spaces')
    if spaces is None:
        spaces = flatten_records(list_spaces())
    elif spaces and isinstance(spaces[0], dict) and 'values' in spaces[0]:
        # 前端回传的是存储形态，先还原为扁平台账行再校验
        spaces = flatten_records(spaces)
    try:
        result = match(requirements, spaces, get_config().get('manual_checks'))
    except (MatchError, LedgerError) as exc:
        raise ApiError(400, str(exc)) from exc
    return result


def post_validate_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """仅跑交接校验，不改状态。"""
    card_id = str(payload.get('card_id') or '')
    try:
        card = get_card(card_id)
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在') from exc
    items = ensure_all_fields(dict(card.get('items') or {}))
    validation = validate_handover(
        {'purpose': card.get('purpose'), 'items': items},
        get_config(),
        list_confirmations(card_id),
    )
    return {'validation': validation, 'summary': summarize(validation)}


def post_delete_card_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """删除需求卡（仅管理员）。"""
    role = _role(payload, headers)
    permissions = load_role_permissions(get_config())
    require_permission(role, '*', permissions)
    card_id = str(payload.get('card_id') or '')
    delete_card(card_id)
    add_audit('card.delete', card_id, _actor(payload, headers), role, {})
    return {'deleted': card_id}


GET_ROUTES: Dict[str, Callable[[Dict[str, Any], Dict[str, str]], Dict[str, Any]]] = {
    '/api/config': get_config_endpoint,
    '/api/spaces': get_spaces_endpoint,
    '/api/spaces/template': get_spaces_template_endpoint,
    '/api/cards': get_cards_endpoint,
    '/api/card': get_card_endpoint,
    '/api/card/report': get_card_report_endpoint,
    '/api/audit': get_audit_endpoint,
    '/api/quotes': get_quotes_endpoint,
    '/api/metrics': get_metrics_endpoint,
}

def post_assistant_endpoint(payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """智能助手：只读生成回复，不修改业务数据，不写审计。"""
    _role(payload, headers)  # 仅要求带角色标识，助手为只读能力
    message = payload.get('message')
    if not isinstance(message, str) or not message.strip():
        raise ApiError(400, '请输入问题')
    card_id = str(payload.get('card_id') or '')
    try:
        reply, mode = assistant_reply(message, card_id)
    except ModelServiceError as exc:
        raise ApiError(502, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except KeyError as exc:
        raise ApiError(404, '需求卡不存在，请先选择需求卡') from exc
    return {'reply': reply, 'mode': mode}


POST_ROUTES: Dict[str, Callable[[Dict[str, Any], Dict[str, str]], Dict[str, Any]]] = {
    '/api/extract': post_extract_endpoint,
    '/api/cards': post_cards_endpoint,
    '/api/card': put_card_endpoint,
    '/api/card/submit': post_card_submit_endpoint,
    '/api/card/validate': post_validate_endpoint,
    '/api/card/review': post_review_endpoint,
    '/api/card/confirm': post_confirmation_endpoint,
    '/api/card/delete': post_delete_card_endpoint,
    '/api/spaces': post_spaces_endpoint,
    '/api/match': post_match_endpoint,
    '/api/quotes': post_quotes_endpoint,
    '/api/quote/status': post_quote_status_endpoint,
    '/api/quote/approve': post_quote_approve_endpoint,
    '/api/assistant/chat': post_assistant_endpoint,
}
