"""接口测试：输入校验、权限与错误处理。"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

_TMP = tempfile.mkdtemp(prefix='leasing-api-')
os.environ['AGENT_DB'] = str(Path(_TMP) / 'api.db')

from leasing_agent.main import build_server  # noqa: E402
import threading  # noqa: E402

BASE = ''
_server = None
_thread = None


def setUpModule():
    global _server, _thread, BASE
    _server = build_server('127.0.0.1', 0)
    port = _server.server_address[1]
    BASE = f'http://127.0.0.1:{port}'
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()


def tearDownModule():
    if _server:
        _server.shutdown()
        _server.server_close()


def request(method, path, payload=None, role='admin', actor='tester'):
    url = BASE + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if role:
        # HTTP 头只能承载 latin-1，中文需按 UTF-8 字节写入
        req.add_header('X-Actor-Role', role.encode('utf-8').decode('latin-1'))
    if actor:
        req.add_header('X-Actor', actor.encode('utf-8').decode('latin-1'))
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8')
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {'error': body}


class ApiTests(unittest.TestCase):
    def test_config_endpoint(self):
        status, data = request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertIn('fields', data)
        self.assertIn('purposes', data)
        self.assertIn('state_labels', data)

    def test_index_page_served(self):
        with urllib.request.urlopen(BASE + '/', timeout=10) as response:
            body = response.read().decode('utf-8')
        self.assertEqual(response.status, 200)
        self.assertIn('招商', body)

    def test_extract_requires_text(self):
        status, data = request('POST', '/api/extract', {'text': ''})
        self.assertEqual(status, 400)
        self.assertIn('error', data)

    def test_extract_returns_items(self):
        status, data = request('POST', '/api/extract',
                               {'text': '面积至少600平方米，净高不低于4米。'})
        self.assertEqual(status, 200)
        self.assertIn('items', data)
        self.assertEqual(data['items']['area']['value'], 600)

    def test_extract_redacts_before_model(self):
        status, data = request('POST', '/api/extract',
                               {'text': '联系电话13812345678，面积至少600平方米。'})
        self.assertEqual(status, 200)
        self.assertTrue(data.get('redacted'))

    def test_card_lifecycle(self):
        status, card = request('POST', '/api/cards',
                               {'title': '接口需求卡', 'purpose': '研发办公'})
        self.assertEqual(status, 200)
        card_id = card['id']

        status, data = request('PUT', '/api/card', {
            'card_id': card_id,
            'items': [
                {'field': 'area', 'state': 'specified', 'value': 600, 'evidence': '面积至少600平方米'},
                {'field': 'rent', 'state': 'specified', 'value': 50, 'evidence': '租金不超过50元/㎡/月'},
                {'field': 'power', 'state': 'specified', 'value': 160, 'evidence': '供电容量不低于160kVA'},
                {'field': 'height', 'state': 'specified', 'value': 4, 'evidence': '净高不低于4米'},
            ],
        })
        self.assertEqual(status, 200)

        status, data = request('POST', '/api/card/validate', {'card_id': card_id})
        self.assertEqual(status, 200)
        self.assertIn('validation', data)

        status, data = request('POST', '/api/card/submit', {'card_id': card_id})
        self.assertEqual(status, 200)
        self.assertFalse(data['submitted'])
        self.assertGreater(data['validation']['blocking_count'], 0)

    def test_submit_requires_role(self):
        status, data = request('POST', '/api/cards', {'title': '无角色'}, role='')
        self.assertEqual(status, 400)

    def test_business_cannot_review(self):
        status, card = request('POST', '/api/cards',
                               {'title': '权限卡', 'purpose': '研发办公'}, role='business')
        status, data = request('POST', '/api/card/review', {
            'card_id': card['id'], 'action': 'accept',
        }, role='business')
        self.assertEqual(status, 403)

    def test_business_cannot_confirm_professional(self):
        status, card = request('POST', '/api/cards',
                               {'title': '专业确认卡', 'purpose': '研发办公'}, role='business')
        status, data = request('POST', '/api/card/confirm', {
            'card_id': card['id'], 'field': 'height',
        }, role='business')
        self.assertEqual(status, 403)

    def test_engineer_can_confirm_professional(self):
        """工程师对已有需求卡做专业确认：建卡由业务/管理员完成。"""
        status, card = request('POST', '/api/cards',
                               {'title': '工程确认卡', 'purpose': '研发办公'}, role='admin')
        self.assertEqual(status, 200)
        status, data = request('POST', '/api/card/confirm', {
            'card_id': card['id'], 'field': 'height', 'note': '实测4.2米',
        }, role='engineer')
        self.assertEqual(status, 200)
        self.assertEqual(data['field'], 'height')

    def test_spaces_import_and_match(self):
        spaces = [{
            'name': '接口房源A', 'area': 800, 'rent': 40, 'power': 200,
            'load': 600, 'height': 4.5,
            'source': '图纸', 'verifier': '张工',
            'verified_at': '2026-01-01', 'version': 'v1',
        }]
        status, data = request('POST', '/api/spaces', {'spaces': spaces})
        self.assertEqual(status, 200)
        self.assertEqual(data['count'], 1)

        status, data = request('POST', '/api/match', {
            'items': [{'field': 'area', 'state': 'specified', 'value': 500}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['status'], '已填条件满足')

    def test_spaces_import_rejects_unit_text(self):
        status, data = request('POST', '/api/spaces', {
            'spaces': [{'name': '坏房源', 'area': '500㎡'}],
        })
        self.assertEqual(status, 400)
        self.assertIn('error', data)

    def test_match_rejects_empty_requirements(self):
        status, data = request('POST', '/api/match', {'items': []})
        self.assertEqual(status, 400)

    def test_invalid_json_body(self):
        req = urllib.request.Request(BASE + '/api/extract', data=b'{bad', method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('X-Actor-Role', 'admin')
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail('应返回 400')
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_unknown_route(self):
        status, data = request('GET', '/api/nope')
        self.assertEqual(status, 404)

    def test_audit_and_metrics(self):
        status, data = request('GET', '/api/audit')
        self.assertEqual(status, 200)
        self.assertIn('audit', data)
        status, data = request('GET', '/api/metrics')
        self.assertEqual(status, 200)
        self.assertIn('cards_total', data)
        self.assertIn('caveat', data)

    def test_chinese_actor_name_stored_correctly(self):
        """中文操作人名必须按 UTF-8 正确入库，不能出现乱码。"""
        status, card = request('POST', '/api/cards',
                               {'title': '中文署名卡', 'purpose': '通用'},
                               role='admin', actor='李经理')
        self.assertEqual(status, 200)
        status, data = request('GET', '/api/audit?card_id=' + card['id'])
        self.assertEqual(status, 200)
        actors = [row['actor'] for row in data['audit'] if row['actor']]
        self.assertIn('李经理', actors)

    def test_cards_search_and_pagination(self):
        marker = f'接口分页{os.urandom(3).hex()}'
        for index in range(3):
            status, _ = request('POST', '/api/cards',
                                {'title': f'{marker}卡{index}', 'purpose': '通用'})
            self.assertEqual(status, 200)
        encoded = urllib.parse.quote(marker)
        status, data = request('GET', f'/api/cards?q={encoded}&page=1&page_size=2')
        self.assertEqual(status, 200)
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['page'], 1)
        self.assertEqual(len(data['cards']), 2)
        status, data = request('GET', f'/api/cards?q={encoded}&page=2&page_size=2')
        self.assertEqual(len(data['cards']), 1)
        status, data = request('GET', f'/api/cards?q={encoded}&status=draft&page=1&page_size=10')
        self.assertEqual(data['total'], 3)
        # 非法分页参数
        status, data = request('GET', '/api/cards?page=0&page_size=999')
        self.assertEqual(status, 400)

    def _make_quotable_card(self):
        """导入一套完整台账并建一张匹配通过的卡，返回 (card_id, space_name)。"""
        space_name = f'接口报价房源{os.urandom(3).hex()}'
        status, _ = request('POST', '/api/spaces', {'spaces': [{
            'name': space_name, 'area': 1200, 'rent': 42, 'power': 250,
            'load': 800, 'height': 5.2,
            'source': '图纸', 'verifier': '张工',
            'verified_at': '2026-01-01', 'version': 'v1',
        }]})
        self.assertEqual(status, 200)
        status, card = request('POST', '/api/cards',
                               {'title': '报价接口卡', 'purpose': '通用'})
        self.assertEqual(status, 200)
        status, _ = request('PUT', '/api/card', {
            'card_id': card['id'],
            'items': [
                {'field': 'area', 'state': 'specified', 'value': 1000, 'evidence': '面积至少1000'},
                {'field': 'rent', 'state': 'specified', 'value': 50, 'evidence': '租金不高于50'},
            ],
        })
        self.assertEqual(status, 200)
        return card['id'], space_name

    def test_quote_create_issue_void_flow(self):
        card_id, space_name = self._make_quotable_card()
        status, data = request('POST', '/api/quotes', {
            'card_id': card_id, 'space_name': space_name,
        })
        self.assertEqual(status, 200)
        quote = data['quote']
        self.assertEqual(quote['status'], 'draft')
        self.assertEqual(quote['terms']['unit_price'], 42)
        self.assertEqual(quote['terms']['area'], 1200)
        self.assertEqual(quote['terms']['monthly_total'], 50400.0)
        self.assertEqual(quote['terms']['lease_term_months'], 36)
        # v2：编号 / 版本 / 轮次 / 有效期 / 三年成本
        self.assertRegex(quote['quote_no'], r'^RJ-\d{6}-\d{3}$')
        self.assertEqual(quote['quote_version'], 1)
        self.assertEqual(quote['round'], '首次报价')
        self.assertTrue(quote['valid_until'])
        self.assertEqual(quote['terms']['rent_3y'], 1814400.0)
        self.assertEqual(quote['terms']['total_3y'], 1814400.0)
        self.assertFalse(quote['terms']['requires_approval'])
        self.assertTrue(any('需园区确认' in c for c in data['caveats']))

        status, data = request('POST', '/api/quote/status',
                               {'quote_id': quote['id'], 'action': 'issue'})
        self.assertEqual(status, 200)
        self.assertEqual(data['quote']['status'], 'issued')
        status, data = request('POST', '/api/quote/status',
                               {'quote_id': quote['id'], 'action': 'issue'})
        self.assertEqual(status, 409)
        status, data = request('POST', '/api/quote/status',
                               {'quote_id': quote['id'], 'action': 'void'})
        self.assertEqual(status, 200)
        self.assertEqual(data['quote']['status'], 'voided')

        status, data = request('GET', '/api/quotes?card_id=' + card_id)
        self.assertEqual(status, 200)
        self.assertTrue(any(q['id'] == quote['id'] for q in data['quotes']))

    def test_quote_revision_flow(self):
        card_id, space_name = self._make_quotable_card()
        status, first = request('POST', '/api/quotes',
                                {'card_id': card_id, 'space_name': space_name})
        self.assertEqual(status, 200)
        # 修订必须附原因
        status, data = request('POST', '/api/quotes', {
            'card_id': card_id, 'space_name': space_name,
            'overrides': {'unit_price': 40},
        })
        self.assertEqual(status, 400)
        self.assertIn('修订原因', data['error'])
        # 附原因后升版，旧版自动作废
        status, data = request('POST', '/api/quotes', {
            'card_id': card_id, 'space_name': space_name,
            'overrides': {'unit_price': 40},
            'revision_reason': '客户要求下调单价',
        })
        self.assertEqual(status, 200)
        second = data['quote']
        self.assertEqual(second['quote_version'], 2)
        self.assertEqual(second['round'], '修订报价')
        self.assertEqual(second['superseded'], [first['quote']['id']])
        status, data = request('GET', '/api/quotes?card_id=' + card_id)
        statuses = {q['id']: q['status'] for q in data['quotes']}
        self.assertEqual(statuses[first['quote']['id']], 'voided')
        self.assertEqual(statuses[second['id']], 'draft')

    def test_quote_issue_requires_approval_over_authority(self):
        card_id, space_name = self._make_quotable_card()
        # 折扣 (42-30)/42 = 28.57% > 5% → 需授权审批
        status, data = request('POST', '/api/quotes', {
            'card_id': card_id, 'space_name': space_name,
            'overrides': {'unit_price': 30},
        })
        self.assertEqual(status, 200)
        self.assertTrue(data['quote']['terms']['requires_approval'])
        quote_id = data['quote']['id']
        status, data = request('POST', '/api/quote/status',
                               {'quote_id': quote_id, 'action': 'issue'})
        self.assertEqual(status, 409)
        self.assertIn('授权审批', data['error'])
        # 业务角色无审批权限
        status, data = request('POST', '/api/quote/approve', {'quote_id': quote_id},
                               role='business')
        self.assertEqual(status, 403)
        # 管理员审批后可发出
        status, data = request('POST', '/api/quote/approve', {'quote_id': quote_id},
                               actor='审批员')
        self.assertEqual(status, 200)
        self.assertEqual(data['quote']['approved_by'], '审批员')
        status, data = request('POST', '/api/quote/status',
                               {'quote_id': quote_id, 'action': 'issue'})
        self.assertEqual(status, 200)

    def test_quote_issue_requires_professional_confirmation(self):
        space_name = f'接口专业确认房源{os.urandom(3).hex()}'
        status, _ = request('POST', '/api/spaces', {'spaces': [{
            'name': space_name, 'area': 1200, 'rent': 42, 'power': 250,
            'load': 800, 'height': 5.2,
            'source': '图纸', 'verifier': '张工',
            'verified_at': '2026-01-01', 'version': 'v1',
        }]})
        self.assertEqual(status, 200)
        status, card = request('POST', '/api/cards', {'title': '专业确认门槛卡', 'purpose': '通用'})
        status, _ = request('PUT', '/api/card', {
            'card_id': card['id'],
            'items': [
                {'field': 'area', 'state': 'specified', 'value': 1000, 'evidence': '面积至少1000'},
                {'field': 'rent', 'state': 'specified', 'value': 50, 'evidence': '租金不高于50'},
                {'field': 'load', 'state': 'specified', 'value': 500, 'evidence': '承重不低于500'},
            ],
        })
        status, data = request('POST', '/api/quotes',
                               {'card_id': card['id'], 'space_name': space_name})
        self.assertEqual(status, 200)
        quote_id = data['quote']['id']
        # 需求卡明确了承重 → issue 前 load 必须有专业确认
        status, data = request('POST', '/api/quote/status',
                               {'quote_id': quote_id, 'action': 'issue'})
        self.assertEqual(status, 409)
        self.assertIn('专业确认', data['error'])
        status, _ = request('POST', '/api/card/confirm',
                            {'card_id': card['id'], 'field': 'load', 'note': '结构核实'},
                            role='engineer')
        self.assertEqual(status, 200)
        status, data = request('POST', '/api/quote/status',
                               {'quote_id': quote_id, 'action': 'issue'})
        self.assertEqual(status, 200)

    def test_audit_chain_valid(self):
        status, card = request('POST', '/api/cards', {'title': '审计链卡', 'purpose': '通用'})
        self.assertEqual(status, 200)
        status, data = request('GET', '/api/audit?card_id=' + card['id'])
        self.assertEqual(status, 200)
        self.assertTrue(data['chain_valid'])
        self.assertTrue(any(row['hash'] for row in data['audit']))

    def test_spaces_accept_quote_params(self):
        marker = os.urandom(3).hex()
        status, data = request('POST', '/api/spaces', {'spaces': [{
            'name': f'报价参数房源{marker}', 'area': 1000, 'rent': 40,
            'efficiency_ratio': 80, 'property_fee': 8, 'delivery_status': 'L1',
            'net_height': 4.0, 'power_redundancy': '双路', 'hvac_overtime_fee': 2,
            'source': '图纸', 'verifier': '张工',
            'verified_at': '2026-01-01', 'version': 'v1',
        }]})
        self.assertEqual(status, 200)
        name = urllib.parse.quote(f'报价参数房源{marker}')
        # 参数应进入台账 values，报价时带出得房率与物业费
        status, card = request('POST', '/api/cards', {'title': '参数带出卡', 'purpose': '通用'})
        status, _ = request('PUT', '/api/card', {
            'card_id': card['id'],
            'items': [{'field': 'area', 'state': 'specified', 'value': 500,
                       'evidence': '面积至少500'}],
        })
        status, data = request('POST', '/api/quotes',
                               {'card_id': card['id'], 'space_name': f'报价参数房源{marker}'})
        self.assertEqual(status, 200)
        terms = data['quote']['terms']
        self.assertEqual(terms['efficiency_ratio'], 80)
        self.assertEqual(terms['rentable_area'], 800.0)
        self.assertEqual(terms['property_fee'], 8)
        self.assertEqual(terms['property_3y'], 8 * 800 * 36)
        self.assertEqual(terms['delivery_status'], 'L1')

    def test_spaces_reject_bad_quote_params(self):
        marker = os.urandom(3).hex()
        status, data = request('POST', '/api/spaces', {'spaces': [
            {'name': f'坏得房率{marker}', 'efficiency_ratio': 120},
        ]})
        self.assertEqual(status, 400)
        status, data = request('POST', '/api/spaces', {'spaces': [
            {'name': f'坏交付标准{marker}', 'delivery_status': '豪华装修'},
        ]})
        self.assertEqual(status, 400)

    def test_reject_review_requires_enum_reason_code(self):
        status, card = request('POST', '/api/cards', {'title': '驳回枚举卡', 'purpose': '通用'})
        self.assertEqual(status, 200)
        status, data = request('POST', '/api/card/review', {
            'card_id': card['id'], 'action': 'reject', 'reason': '信息不足',
            'detail': {'reason_code': '自由文本'},
        })
        self.assertEqual(status, 400)
        self.assertIn('枚举', data['error'])
        status, data = request('POST', '/api/card/review', {
            'card_id': card['id'], 'action': 'reject', 'reason': '信息不足',
            'detail': {'reason_code': 'HW_MISSING'},
        })
        self.assertEqual(status, 200)
        status, data = request('GET', '/api/card?card_id=' + card['id'])
        history = data['review_history']
        self.assertEqual(history[0]['reason_code'], 'HW_MISSING')
        self.assertIn('硬件条件缺失', history[0]['reason_code_label'])

    def test_config_exposes_return_reasons(self):
        status, data = request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertIn('HW_MISSING', data['return_reasons'])

    def test_assistant_chat_rule_mode(self):
        status, card = request('POST', '/api/cards', {'title': '助手测试卡', 'purpose': '通用'})
        self.assertEqual(status, 200)
        status, data = request('POST', '/api/assistant/chat',
                               {'message': '总结当前需求卡', 'card_id': card['id']})
        self.assertEqual(status, 200)
        self.assertIn('mode', data)
        self.assertIn('reply', data)
        self.assertTrue(data['reply'])
        self.assertIn(card['title'], data['reply'])

    def test_assistant_chat_requires_message(self):
        status, data = request('POST', '/api/assistant/chat', {'message': '  '})
        self.assertEqual(status, 400)
        self.assertIn('error', data)
        status, data = request('POST', '/api/assistant/chat', {'message': '总结', 'card_id': 'no-such'})
        self.assertEqual(status, 400)
        self.assertIn('error', data)

    def test_quote_blocked_for_non_matched_space(self):
        marker = os.urandom(3).hex()
        card_id, _ = self._make_quotable_card()
        # 追加一套"匹配通过 + 不匹配"两条房源的台账：save_spaces 是整份覆盖
        status, _ = request('POST', '/api/spaces', {'spaces': [
            {'name': f'可报价房源{marker}', 'area': 1200, 'rent': 42, 'power': 250,
             'load': 800, 'height': 5.2,
             'source': '图纸', 'verifier': '张工',
             'verified_at': '2026-01-01', 'version': 'v1'},
            {'name': f'不可报价房源{marker}', 'area': 500, 'rent': 60,
             'source': '图纸', 'verifier': '张工',
             'verified_at': '2026-01-01', 'version': 'v1'},
        ]})
        self.assertEqual(status, 200)
        status, data = request('POST', '/api/quotes',
                               {'card_id': card_id, 'space_name': f'不可报价房源{marker}'})
        self.assertEqual(status, 409)
        self.assertIn('error', data)
        # 匹配通过的那条仍然可以生成
        status, data = request('POST', '/api/quotes',
                               {'card_id': card_id, 'space_name': f'可报价房源{marker}'})
        self.assertEqual(status, 200)

    def test_quote_requires_override_when_ledger_incomplete(self):
        space_name = f'接口缺参房源{os.urandom(3).hex()}'
        status, _ = request('POST', '/api/spaces', {'spaces': [{
            'name': space_name, 'area': 900, 'power': 100,
            'source': '图纸', 'verifier': '张工',
            'verified_at': '2026-01-01', 'version': 'v1',
        }]})
        self.assertEqual(status, 200)
        status, card = request('POST', '/api/cards', {'title': '缺参报价卡', 'purpose': '通用'})
        status, _ = request('PUT', '/api/card', {
            'card_id': card['id'],
            'items': [{'field': 'area', 'state': 'specified', 'value': 500,
                       'evidence': '面积至少500'}],
        })
        # 需求只提面积，房源面积满足且溯源完整 → 匹配"已填条件满足"，
        # 但台账没登记租金：不能拿 0 或估计值悄悄代替，必须人工覆盖
        status, data = request('POST', '/api/quotes',
                               {'card_id': card['id'], 'space_name': space_name})
        self.assertEqual(status, 400)
        self.assertIn('月单价', data['error'])
        # 补上人工覆盖的租金后可以生成
        status, data = request('POST', '/api/quotes', {
            'card_id': card['id'], 'space_name': space_name,
            'overrides': {'unit_price': 30},
        })
        self.assertEqual(status, 200)
        self.assertEqual(data['quote']['terms']['unit_price'], 30)

    def test_quote_permission_and_validation(self):
        card_id, space_name = self._make_quotable_card()
        # 工程角色没有 quote.create 权限
        status, data = request('POST', '/api/quotes',
                               {'card_id': card_id, 'space_name': space_name}, role='engineer')
        self.assertEqual(status, 403)
        # 非法覆盖值
        status, data = request('POST', '/api/quotes', {
            'card_id': card_id, 'space_name': space_name,
            'overrides': {'unit_price': 0},
        })
        self.assertEqual(status, 400)
        # 不存在的房源
        status, data = request('POST', '/api/quotes',
                               {'card_id': card_id, 'space_name': '不存在的房源'})
        self.assertEqual(status, 404)

    def test_markdown_report_export(self):
        card_id, _ = self._make_quotable_card()
        status, data = request('GET', '/api/card/report?card_id=' + card_id)
        self.assertEqual(status, 200)
        markdown = data['markdown']
        for section in ('字段裁定', '交接校验', '复核与专业确认', '空间匹配', '口径提示'):
            self.assertIn(section, markdown)
        self.assertIn(card_id, data['filename'])


if __name__ == '__main__':
    unittest.main()
