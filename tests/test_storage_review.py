"""复核动作与存储测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

_TMP = tempfile.mkdtemp(prefix='leasing-test-')
os.environ['AGENT_DB'] = str(Path(_TMP) / 'test.db')

from leasing_agent.review import (  # noqa: E402
    ACTION_ACCEPT,
    ACTION_CHANGE_EXPLANATION,
    ACTION_CHANGE_LEVEL,
    ACTION_ESCALATE,
    ACTION_REJECT,
    RETURN_REASON_CODES,
    ReviewError,
    apply_action,
    review_history,
    validate_action,
)
from leasing_agent.storage import (  # noqa: E402
    add_audit,
    add_confirmation,
    add_review,
    create_card,
    get_card,
    list_audit,
    list_cards,
    list_reviews,
    list_spaces,
    save_spaces,
    update_card,
)


class ReviewActionTests(unittest.TestCase):
    def test_five_actions_exist(self):
        details = {
            ACTION_ACCEPT: {},
            ACTION_CHANGE_LEVEL: {'field': 'area', 'level': 'advisory'},
            ACTION_CHANGE_EXPLANATION: {'field': 'area', 'explanation': '客户确认无此要求'},
            ACTION_REJECT: {'reason_code': 'HW_MISSING'},
            ACTION_ESCALATE: {'escalate_to': '工程负责人'},
        }
        for action, detail in details.items():
            reason = '' if action == ACTION_ACCEPT else '理由'
            result = validate_action(action, reason, detail)
            self.assertEqual(result['action'], action)
            self.assertTrue(result['action_label'])

    def test_unknown_action_rejected(self):
        with self.assertRaises(ReviewError):
            validate_action('delete', 'x')

    def test_reason_required(self):
        with self.assertRaises(ReviewError):
            validate_action(ACTION_REJECT, '')

    def test_change_level_needs_field_and_level(self):
        with self.assertRaises(ReviewError):
            validate_action(ACTION_CHANGE_LEVEL, 'x', {'level': 'advisory'})
        with self.assertRaises(ReviewError):
            validate_action(ACTION_CHANGE_LEVEL, 'x', {'field': 'area'})

    def test_change_explanation_needs_text(self):
        with self.assertRaises(ReviewError):
            validate_action(ACTION_CHANGE_EXPLANATION, 'x', {'field': 'area'})

    def test_reject_needs_reason_code(self):
        with self.assertRaises(ReviewError):
            validate_action(ACTION_REJECT, 'x', {})

    def test_reject_reason_code_must_be_enum(self):
        with self.assertRaises(ReviewError):
            validate_action(ACTION_REJECT, 'x', {'reason_code': '自由文本'})
        result = validate_action(ACTION_REJECT, 'x', {'reason_code': 'HW_MISSING'})
        self.assertIn('reason_code', result['detail'])

    def test_return_reason_codes_have_labels(self):
        self.assertIn('HW_MISSING', RETURN_REASON_CODES)
        self.assertIn('INCENTIVE_ABOVE_AUTH', RETURN_REASON_CODES)
        for code, label in RETURN_REASON_CODES.items():
            self.assertTrue(code)
            self.assertTrue(label)

    def test_history_carries_reason_code_label(self):
        result = validate_action(ACTION_REJECT, '信息不足',
                                 {'reason_code': 'PRICE_OVER_LIMIT'}, 'reviewer')
        applied = apply_action({'items': {}, 'version': 1}, result)
        history = review_history([applied['record']])
        self.assertEqual(history[0]['reason_code'], 'PRICE_OVER_LIMIT')
        self.assertIn('价格超限', history[0]['reason_code_label'])
        self.assertIn('PRICE_OVER_LIMIT', history[0]['resolution'])

    def test_escalate_needs_target(self):
        with self.assertRaises(ReviewError):
            validate_action(ACTION_ESCALATE, 'x', {})

    def test_apply_reject_moves_to_returned(self):
        card = {'items': {}, 'version': 3}
        result = validate_action(ACTION_REJECT, '信息不足', {'reason_code': 'HW_MISSING'}, 'reviewer')
        applied = apply_action(card, result)
        self.assertEqual(applied['status'], 'returned')
        self.assertEqual(applied['record']['ref_version'], 3)

    def test_apply_accept_moves_to_handover(self):
        card = {'items': {}, 'version': 1}
        result = validate_action(ACTION_ACCEPT, '', {}, 'reviewer')
        applied = apply_action(card, result)
        self.assertEqual(applied['status'], 'handover')

    def test_apply_escalate(self):
        card = {'items': {}, 'version': 1}
        result = validate_action(ACTION_ESCALATE, '超权限', {'escalate_to': '工程负责人'}, 'business')
        applied = apply_action(card, result)
        self.assertEqual(applied['status'], 'escalated')

    def test_change_level_records_adjustment(self):
        card = {'items': {'area': {'state': 'unknown'}}, 'version': 1}
        result = validate_action(ACTION_CHANGE_LEVEL, '客户确认无此要求',
                                  {'field': 'area', 'level': 'advisory'}, 'reviewer')
        applied = apply_action(card, result)
        self.assertEqual(applied['card']['level_adjustments']['area'], 'advisory')


class StorageTests(unittest.TestCase):
    def test_create_and_fetch_card(self):
        card = create_card('测试需求卡', purpose='研发办公')
        fetched = get_card(card['id'])
        self.assertEqual(fetched['title'], '测试需求卡')
        self.assertEqual(fetched['status'], 'draft')
        self.assertEqual(fetched['version'], 1)

    def test_update_card(self):
        card = create_card('可更新')
        updated = update_card(card['id'], title='新标题', purpose='仓储物流')
        self.assertEqual(updated['title'], '新标题')
        self.assertEqual(updated['purpose'], '仓储物流')

    def test_list_cards(self):
        create_card('列表卡1')
        create_card('列表卡2')
        self.assertGreaterEqual(len(list_cards()), 2)

    def test_reviews_persisted(self):
        card = create_card('留痕卡')
        result = validate_action(ACTION_ACCEPT, '', {}, 'reviewer')
        add_review(card['id'], {
            'action': result['action'],
            'action_label': result['action_label'],
            'reason': result['reason'],
            'detail': result['detail'],
            'actor_role': result['actor_role'],
            'ref_version': 1,
        }, actor='复核员')
        reviews = list_reviews(card['id'])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['actor'], '复核员')
        self.assertEqual(reviews[0]['action'], ACTION_ACCEPT)

    def test_confirmations_persisted(self):
        card = create_card('确认卡')
        add_confirmation(card['id'], 'height', '张工', 'engineer', '实测4.2米')
        card = get_card(card['id'])
        self.assertEqual(len(card['confirmations']), 1)
        self.assertEqual(card['confirmations'][0]['field'], 'height')

    def test_audit_persisted(self):
        card = create_card('审计卡')
        add_audit('card.create', card['id'], '李四', 'business', {'x': 1})
        rows = list_audit(card['id'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['actor'], '李四')

    def test_spaces_roundtrip(self):
        records = [{
            'name': '房源X', 'values': {'area': 500}, 'trace': {'source': '图纸'},
            'meta': {}, 'trace_complete': False,
        }]
        save_spaces(records)
        loaded = list_spaces()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]['name'], '房源X')
        self.assertFalse(loaded[0]['trace_complete'])


if __name__ == '__main__':
    unittest.main()
