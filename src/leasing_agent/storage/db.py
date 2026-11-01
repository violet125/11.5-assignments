"""持久化：SQLite 存储需求卡、需求条目、房源台账、交接与复核记录、审计日志。

存储用标准库 sqlite3，不引入第三方依赖。所有写入走同一连接工厂，
默认落在项目内 data/agent.db，可通过 AGENT_DB 环境变量改写。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import calendar
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / 'data' / 'agent.db'

_LOCK = threading.Lock()
_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    customer TEXT DEFAULT '',
    purpose TEXT DEFAULT '',
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT DEFAULT '',
    mode TEXT DEFAULT '',
    items TEXT NOT NULL DEFAULT '{}',
    level_adjustments TEXT NOT NULL DEFAULT '{}',
    validation TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spaces (
    name TEXT PRIMARY KEY,
    field_values TEXT NOT NULL DEFAULT '{}',
    trace TEXT NOT NULL DEFAULT '{}',
    meta TEXT NOT NULL DEFAULT '{}',
    trace_complete INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    action TEXT NOT NULL,
    action_label TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    actor TEXT DEFAULT '',
    actor_role TEXT DEFAULT '',
    ref_version INTEGER DEFAULT 1,
    resolution TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmations (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    field TEXT NOT NULL,
    by TEXT DEFAULT '',
    role TEXT DEFAULT '',
    note TEXT DEFAULT '',
    valid INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id TEXT PRIMARY KEY,
    card_id TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    actor_role TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    ref_version INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    prev_hash TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS quotations (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    card_title TEXT DEFAULT '',
    card_version INTEGER DEFAULT 1,
    space_name TEXT NOT NULL,
    terms TEXT NOT NULL DEFAULT '{}',
    match_conclusion TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    actor TEXT DEFAULT '',
    actor_role TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    quote_no TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL DEFAULT 0,
    quote_version INTEGER NOT NULL DEFAULT 1,
    round TEXT NOT NULL DEFAULT '',
    revision_reason TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews(card_id);
CREATE INDEX IF NOT EXISTS idx_confirmations_card ON confirmations(card_id);
CREATE INDEX IF NOT EXISTS idx_audit_card ON audit(card_id);
CREATE INDEX IF NOT EXISTS idx_quotes_card ON quotations(card_id);
"""


def db_path() -> Path:
    """当前使用的数据库路径。"""
    env = os.getenv('AGENT_DB')
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_DB


#: 旧库自动补列：{表: {列: 默认 DDL 片段}}
_MIGRATION_COLUMNS = {
    'audit': {
        'prev_hash': "TEXT NOT NULL DEFAULT ''",
        'hash': "TEXT NOT NULL DEFAULT ''",
    },
    'quotations': {
        'quote_no': "TEXT NOT NULL DEFAULT ''",
        'seq': 'INTEGER NOT NULL DEFAULT 0',
        'quote_version': 'INTEGER NOT NULL DEFAULT 1',
        'round': "TEXT NOT NULL DEFAULT ''",
        'revision_reason': "TEXT NOT NULL DEFAULT ''",
        'valid_until': "TEXT NOT NULL DEFAULT ''",
        'approved_by': "TEXT NOT NULL DEFAULT ''",
        'approved_at': "TEXT NOT NULL DEFAULT ''",
    },
}


def _migrate(connection: sqlite3.Connection) -> None:
    """为旧库补齐新增列，避免 CREATE TABLE IF NOT EXISTS 不生效。"""
    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
        for column, ddl in columns.items():
            if column not in existing:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')


def connect() -> sqlite3.Connection:
    """获取线程内连接并确保表结构存在。"""
    connection = getattr(_LOCAL, 'connection', None)
    if connection is not None:
        return connection
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA journal_mode=WAL')
    connection.executescript(SCHEMA)
    _migrate(connection)
    connection.commit()
    _LOCAL.connection = connection
    return connection


def now() -> str:
    """UTC ISO 时间戳。"""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:12]}'


def _json_loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 需求卡
# ---------------------------------------------------------------------------

def create_card(title: str, source: str = '', purpose: str = '', customer: str = '',
                items: Optional[Dict[str, Any]] = None, mode: str = '') -> Dict[str, Any]:
    """新建需求卡。"""
    connection = connect()
    card_id = new_id('card')
    timestamp = now()
    normalized_items = items or {}
    connection.execute(
        'INSERT INTO cards (id, title, customer, purpose, status, version, source, mode, items,'
        ' level_adjustments, validation, created_at, updated_at)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (card_id, title, customer, purpose, 'draft', 1, source, mode,
         json.dumps(normalized_items, ensure_ascii=False), '{}', '{}', timestamp, timestamp),
    )
    connection.commit()
    return get_card(card_id)


def update_card(card_id: str, **fields: Any) -> Dict[str, Any]:
    """更新需求卡字段。items / level_adjustments / validation 存为 JSON。"""
    connection = connect()
    allowed = {'title', 'customer', 'purpose', 'status', 'version', 'source', 'mode',
               'items', 'level_adjustments', 'validation'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_card(card_id)
    assignments = []
    values: List[Any] = []
    for key, value in updates.items():
        if key in ('items', 'level_adjustments', 'validation'):
            assignments.append(f'{key} = ?')
            values.append(json.dumps(value, ensure_ascii=False))
        else:
            assignments.append(f'{key} = ?')
            values.append(value)
    assignments.append('updated_at = ?')
    values.append(now())
    values.append(card_id)
    with _LOCK:
        connection.execute(f'UPDATE cards SET {", ".join(assignments)} WHERE id = ?', values)
        connection.commit()
    return get_card(card_id)


def get_card(card_id: str) -> Dict[str, Any]:
    connection = connect()
    row = connection.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if row is None:
        raise KeyError('需求卡不存在')
    card = dict(row)
    card['items'] = _json_loads(card['items'], {})
    card['level_adjustments'] = _json_loads(card['level_adjustments'], {})
    card['validation'] = _json_loads(card['validation'], {})
    card['reviews'] = list_reviews(card_id)
    card['confirmations'] = list_confirmations(card_id)
    return card


def list_cards(status: str = '', q: str = '', page: int = 0, page_size: int = 0) -> List[Dict[str, Any]]:
    """需求卡列表。支持按状态筛选、按标题/客户搜索、分页。

    page <= 0 表示不分页（兼容旧调用）。q 为空时不过滤。
    """
    connection = connect()
    conditions = []
    values: List[Any] = []
    if status:
        conditions.append('status = ?')
        values.append(status)
    if q:
        conditions.append('(title LIKE ? OR customer LIKE ?)')
        like = f'%{q}%'
        values.extend([like, like])
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ''
    order = ' ORDER BY updated_at DESC'
    limit_offset = ''
    if page and page > 0 and page_size and page_size > 0:
        limit_offset = ' LIMIT ? OFFSET ?'
        values.extend([page_size, (page - 1) * page_size])
    rows = connection.execute(f'SELECT id FROM cards{where}{order}{limit_offset}', values).fetchall()
    cards = []
    for row in rows:
        card = get_card(row['id'])
        card.pop('reviews', None)
        card.pop('confirmations', None)
        cards.append(card)
    return cards


def count_cards(status: str = '', q: str = '') -> int:
    """需求卡计数（与 list_cards 同口径），用于分页。"""
    connection = connect()
    conditions = []
    values: List[Any] = []
    if status:
        conditions.append('status = ?')
        values.append(status)
    if q:
        conditions.append('(title LIKE ? OR customer LIKE ?)')
        like = f'%{q}%'
        values.extend([like, like])
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ''
    return connection.execute(f'SELECT COUNT(*) AS c FROM cards{where}', values).fetchone()['c']


def delete_card(card_id: str) -> None:
    connection = connect()
    with _LOCK:
        connection.execute('DELETE FROM reviews WHERE card_id = ?', (card_id,))
        connection.execute('DELETE FROM confirmations WHERE card_id = ?', (card_id,))
        connection.execute('DELETE FROM quotations WHERE card_id = ?', (card_id,))
        connection.execute('DELETE FROM audit WHERE card_id = ?', (card_id,))
        connection.execute('DELETE FROM cards WHERE id = ?', (card_id,))
        connection.commit()


# ---------------------------------------------------------------------------
# 房源台账
# ---------------------------------------------------------------------------

def save_spaces(records: List[Dict[str, Any]]) -> int:
    """整份覆盖房源台账。"""
    connection = connect()
    timestamp = now()
    with _LOCK:
        connection.execute('DELETE FROM spaces')
        for record in records:
            connection.execute(
                'INSERT INTO spaces (name, field_values, trace, meta, trace_complete, updated_at)'
                ' VALUES (?,?,?,?,?,?)',
                (record['name'],
                 json.dumps(record['values'], ensure_ascii=False),
                 json.dumps(record['trace'], ensure_ascii=False),
                 json.dumps(record['meta'], ensure_ascii=False),
                 1 if record['trace_complete'] else 0,
                 timestamp),
            )
        connection.commit()
    return len(records)


def list_spaces() -> List[Dict[str, Any]]:
    connection = connect()
    rows = connection.execute('SELECT * FROM spaces ORDER BY name').fetchall()
    result = []
    for row in rows:
        result.append({
            'name': row['name'],
            'values': _json_loads(row['field_values'], {}),
            'trace': _json_loads(row['trace'], {}),
            'meta': _json_loads(row['meta'], {}),
            'trace_complete': bool(row['trace_complete']),
        })
    return result


# ---------------------------------------------------------------------------
# 报价单
# ---------------------------------------------------------------------------

def create_quote(card_id: str, space_name: str, terms: Dict[str, Any],
                 match_conclusion: str, actor: str = '', actor_role: str = '',
                 revision_reason: str = '') -> Dict[str, Any]:
    """新建报价单：编号 RJ-{YYYYMM}-{seq}，同卡同房源自动升版。

    同一需求卡 + 房源已有非作废报价单时，版本号取历史最大版本 +1，
    轮次标记为"修订报价"（必须附修订原因），旧版自动置为 voided，
    被作废的旧版 id 列表放在返回值的 'superseded' 键（不入库）。
    """
    from ..core.quotation import QUOTE_ROUND_FIRST, QUOTE_ROUND_REVISED, validate_revision

    connection = connect()
    card = get_card(card_id)
    with _LOCK:
        row = connection.execute(
            'SELECT COALESCE(MAX(seq), 0) AS s FROM quotations').fetchone()
        seq = int(row['s']) + 1
        row = connection.execute(
            'SELECT COALESCE(MAX(quote_version), 0) AS v FROM quotations'
            ' WHERE card_id = ? AND space_name = ?',
            (card_id, space_name)).fetchone()
        quote_version = int(row['v']) + 1
        reason = validate_revision(quote_version, revision_reason)
        timestamp = now()
        created = timestamp
        year_month = time.strftime('%Y%m', time.gmtime())
        quote_no = f'RJ-{year_month}-{seq:03d}'
        round_label = QUOTE_ROUND_FIRST if quote_version == 1 else QUOTE_ROUND_REVISED
        validity_days = int((terms or {}).get('validity_days') or 15)
        base = time.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')
        valid_until = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                    time.gmtime(calendar.timegm(base) + validity_days * 86400))
        quote_id = new_id('quote')
        connection.execute(
            'INSERT INTO quotations (id, card_id, card_title, card_version, space_name, terms,'
            ' match_conclusion, status, actor, actor_role, created_at, updated_at,'
            ' quote_no, seq, quote_version, round, revision_reason, valid_until)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (quote_id, card_id, card.get('title', ''), int(card.get('version', 1)), space_name,
             json.dumps(terms, ensure_ascii=False), match_conclusion, 'draft',
             actor, actor_role, created, timestamp,
             quote_no, seq, quote_version, round_label, reason, valid_until),
        )
        superseded: List[str] = []
        if quote_version > 1:
            olds = connection.execute(
                "SELECT id FROM quotations WHERE card_id = ? AND space_name = ?"
                " AND id != ? AND status != 'voided'",
                (card_id, space_name, quote_id)).fetchall()
            superseded = [r['id'] for r in olds]
            if superseded:
                marks = ','.join('?' for _ in superseded)
                connection.execute(
                    f"UPDATE quotations SET status = 'voided', updated_at = ?"
                    f" WHERE id IN ({marks})",
                    [timestamp, *superseded])
        connection.commit()
    quote = get_quote(quote_id)
    quote['superseded'] = superseded
    return quote


def get_quote(quote_id: str) -> Dict[str, Any]:
    connection = connect()
    row = connection.execute('SELECT * FROM quotations WHERE id = ?', (quote_id,)).fetchone()
    if row is None:
        raise KeyError('报价单不存在')
    quote = dict(row)
    quote['terms'] = _json_loads(quote['terms'], {})
    return quote


def list_quotes(card_id: str = '') -> List[Dict[str, Any]]:
    connection = connect()
    if card_id:
        rows = connection.execute(
            'SELECT * FROM quotations WHERE card_id = ? ORDER BY created_at DESC, rowid DESC',
            (card_id,)).fetchall()
    else:
        rows = connection.execute(
            'SELECT * FROM quotations ORDER BY created_at DESC, rowid DESC').fetchall()
    result = []
    for row in rows:
        quote = dict(row)
        quote['terms'] = _json_loads(quote['terms'], {})
        result.append(quote)
    return result


def update_quote_status(quote_id: str, status: str) -> Dict[str, Any]:
    connection = connect()
    with _LOCK:
        connection.execute('UPDATE quotations SET status = ?, updated_at = ? WHERE id = ?',
                           (status, now(), quote_id))
        connection.commit()
    return get_quote(quote_id)


def update_quote_approval(quote_id: str, approved_by: str) -> Dict[str, Any]:
    """记录让利授权审批（仅管理员可发起，调用方负责权限）。"""
    connection = connect()
    timestamp = now()
    with _LOCK:
        connection.execute(
            'UPDATE quotations SET approved_by = ?, approved_at = ?, updated_at = ? WHERE id = ?',
            (approved_by, timestamp, timestamp, quote_id))
        connection.commit()
    return get_quote(quote_id)


# ---------------------------------------------------------------------------
# 复核与专业确认
# ---------------------------------------------------------------------------

def add_review(card_id: str, record: Dict[str, Any], actor: str = '') -> Dict[str, Any]:
    connection = connect()
    review_id = new_id('rev')
    timestamp = now()
    connection.execute(
        'INSERT INTO reviews (id, card_id, action, action_label, reason, detail, actor, actor_role,'
        ' ref_version, resolution, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (review_id, card_id, record['action'], record.get('action_label', ''), record.get('reason', ''),
         json.dumps(record.get('detail') or {}, ensure_ascii=False), actor,
         record.get('actor_role', ''), record.get('ref_version', 1),
         record.get('resolution', ''), timestamp),
    )
    connection.commit()
    return {'id': review_id, **record, 'actor': actor, 'created_at': timestamp}


def list_reviews(card_id: str) -> List[Dict[str, Any]]:
    connection = connect()
    rows = connection.execute('SELECT * FROM reviews WHERE card_id = ? ORDER BY created_at, rowid', (card_id,)).fetchall()
    reviews = []
    for row in rows:
        review = dict(row)
        review['detail'] = _json_loads(review['detail'], {})
        reviews.append(review)
    return reviews


def add_confirmation(card_id: str, field: str, by: str, role: str, note: str = '') -> Dict[str, Any]:
    connection = connect()
    confirmation_id = new_id('cf')
    timestamp = now()
    connection.execute(
        'INSERT INTO confirmations (id, card_id, field, by, role, note, valid, created_at)'
        ' VALUES (?,?,?,?,?,?,1,?)',
        (confirmation_id, card_id, field, by, role, note, timestamp),
    )
    connection.commit()
    return {'id': confirmation_id, 'card_id': card_id, 'field': field, 'by': by,
            'role': role, 'note': note, 'valid': True, 'created_at': timestamp}


def list_confirmations(card_id: str) -> List[Dict[str, Any]]:
    connection = connect()
    rows = connection.execute('SELECT * FROM confirmations WHERE card_id = ? ORDER BY created_at, rowid', (card_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['valid'] = bool(item['valid'])
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def seed_spaces_if_empty() -> int:
    """首次启动时载入虚构样例台账。台账非空时不覆盖业务导入的数据。"""
    connection = connect()
    count = connection.execute('SELECT COUNT(*) AS c FROM spaces').fetchone()['c']
    if count:
        return 0
    sample = ROOT / 'data' / 'spaces.example.json'
    if not sample.is_file():
        return 0
    try:
        payload = json.loads(sample.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, list):
        return 0
    from ..inventory import normalize_ledger
    try:
        records = normalize_ledger(payload)
    except Exception:
        return 0
    return save_spaces(records)

def _audit_hash(prev_hash: str, payload: Dict[str, Any]) -> str:
    """审计 hash：sha256(prev_hash + 规范化 JSON)。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(f'{prev_hash}{canonical}'.encode('utf-8')).hexdigest()


def add_audit(action: str, card_id: str = '', actor: str = '', actor_role: str = '',
              detail: Optional[Dict[str, Any]] = None, ref_version: int = 0) -> Dict[str, Any]:
    """写审计并接入 hash 链：每条记录含前一条 hash，形成防篡改链条。"""
    connection = connect()
    audit_id = new_id('aud')
    timestamp = now()
    with _LOCK:
        row = connection.execute('SELECT hash FROM audit ORDER BY rowid DESC LIMIT 1').fetchone()
        prev_hash = (row['hash'] if row else '') or ''
        payload = {
            'id': audit_id,
            'card_id': card_id,
            'actor': actor,
            'actor_role': actor_role,
            'action': action,
            'detail': detail or {},
            'ref_version': ref_version,
            'created_at': timestamp,
        }
        entry_hash = _audit_hash(prev_hash, payload)
        connection.execute(
            'INSERT INTO audit (id, card_id, actor, actor_role, action, detail, ref_version,'
            ' created_at, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (audit_id, card_id, actor, actor_role, action,
             json.dumps(detail or {}, ensure_ascii=False), ref_version, timestamp,
             prev_hash, entry_hash),
        )
        connection.commit()
    return {**payload, 'prev_hash': prev_hash, 'hash': entry_hash}


def audit_chain_valid(limit: int = 0) -> bool:
    """重算整条审计 hash 链，验证是否被篡改。limit>0 时只校验最近 N 条。"""
    connection = connect()
    query = 'SELECT * FROM audit ORDER BY rowid'
    if limit and limit > 0:
        query += f' LIMIT {int(limit)}'
    rows = connection.execute(query).fetchall()
    prev = ''
    for row in rows:
        if not row['hash']:
            prev = ''  # 迁移前的历史留痕无 hash，不参与链校验
            continue
        payload = {
            'id': row['id'],
            'card_id': row['card_id'],
            'actor': row['actor'],
            'actor_role': row['actor_role'],
            'action': row['action'],
            'detail': _json_loads(row['detail'], {}),
            'ref_version': row['ref_version'],
            'created_at': row['created_at'],
        }
        expected = _audit_hash(row['prev_hash'] or prev, payload)
        if row['hash'] != expected:
            return False
        prev = row['hash']
    return True


def list_audit(card_id: str = '', limit: int = 200) -> List[Dict[str, Any]]:
    connection = connect()
    if card_id:
        rows = connection.execute(
            'SELECT * FROM audit WHERE card_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?',
            (card_id, limit)).fetchall()
    else:
        rows = connection.execute(
            'SELECT * FROM audit ORDER BY created_at DESC, rowid DESC LIMIT ?', (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['detail'] = _json_loads(item['detail'], {})
        result.append(item)
    return result


def metrics() -> Dict[str, Any]:
    """交接遗漏相关的过程指标。统计口径需在试点前与业务确认。"""
    connection = connect()
    total = connection.execute('SELECT COUNT(*) AS c FROM cards').fetchone()['c']
    by_status = {row['status']: row['c'] for row in connection.execute(
        'SELECT status, COUNT(*) AS c FROM cards GROUP BY status')}
    reviews = connection.execute('SELECT COUNT(*) AS c FROM reviews').fetchone()['c']
    rejects = connection.execute("SELECT COUNT(*) AS c FROM reviews WHERE action = 'reject'").fetchone()['c']
    escalations = connection.execute("SELECT COUNT(*) AS c FROM reviews WHERE action = 'escalate'").fetchone()['c']
    confirmations = connection.execute('SELECT COUNT(*) AS c FROM confirmations').fetchone()['c']
    spaces = connection.execute('SELECT COUNT(*) AS c FROM spaces').fetchone()['c']
    trace_complete = connection.execute('SELECT COUNT(*) AS c FROM spaces WHERE trace_complete = 1').fetchone()['c']
    blocking_total = connection.execute(
        "SELECT COUNT(*) AS c FROM cards WHERE json_extract(validation, '$.blocking_count') > 0").fetchone()['c']
    quotes_total = connection.execute('SELECT COUNT(*) AS c FROM quotations').fetchone()['c']
    quotes_issued = connection.execute(
        "SELECT COUNT(*) AS c FROM quotations WHERE status = 'issued'").fetchone()['c']
    return {
        'cards_total': total,
        'cards_by_status': by_status,
        'reviews_total': reviews,
        'rejects_total': rejects,
        'escalations_total': escalations,
        'confirmations_total': confirmations,
        'spaces_total': spaces,
        'spaces_trace_complete': trace_complete,
        'cards_with_blocking': blocking_total,
        'quotes_total': quotes_total,
        'quotes_issued': quotes_issued,
        'caveat': '这些是系统过程计数，不等于业务交接遗漏率；遗漏率的统计单位、分母、抽样范围、时间窗口与复核责任人须在试点前确认。',
    }
