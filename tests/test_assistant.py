"""智能助手测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

_TMP = tempfile.mkdtemp(prefix='leasing-test-')
os.environ['AGENT_DB'] = str(Path(_TMP) / 'assistant.db')

from leasing_agent.core.assistant import (  # noqa: E402
    build_context,
    rule_reply,
)
from leasing_agent.storage import create_card, save_spaces, seed_spaces_if_empty  # noqa: E402


class AssistantRuleTests(unittest.TestCase):
    def test_help_reply_without_card(self):
        context = build_context('')
        reply = rule_reply('总结当前需求卡', context)
        self.assertIn('未选择需求卡', reply)

    def test_summarize_with_card(self):
        card = create_card('助手总结卡', purpose='研发办公', customer='测试客户')
        context = build_context(card['id'])
        reply = rule_reply('总结当前需求卡', context)
        self.assertIn('助手总结卡', reply)
        self.assertIn('草稿', reply)

    def test_followup_questions_from_unknown_fields(self):
        card = create_card('补问测试卡')
        from leasing_agent.storage import update_card
        update_card(card['id'], items={
            'area': {'state': 'specified', 'value': 500, 'evidence': '', 'note': ''},
            'height': {'state': 'unknown', 'value': None, 'evidence': '', 'note': ''},
        })
        context = build_context(card['id'])
        reply = rule_reply('生成补问清单', context)
        self.assertIn('补问', reply)
        self.assertIn('净高', reply)

    def test_quote_explain_mentions_3y_cost(self):
        card = create_card('报价口径卡')
        context = build_context(card['id'])
        reply = rule_reply('报价口径说明', context)
        self.assertIn('三年总占用成本', reply)
        self.assertIn('行业参照', reply)

    def test_fields_explain(self):
        context = build_context('')
        reply = rule_reply('字段口径说明', context)
        self.assertIn('最高月单价', reply)
        self.assertIn('kVA', reply)

    def test_match_explain_without_specified(self):
        card = create_card('匹配口径卡')
        context = build_context(card['id'])
        reply = rule_reply('匹配结论说明', context)
        self.assertIn('没有已明确数值的字段', reply)

    def test_unknown_intent_falls_back_to_help(self):
        context = build_context('')
        reply = rule_reply('今天天气怎么样', context)
        self.assertIn('本地规则模式', reply)

    def test_context_ledger_summary(self):
        seed_spaces_if_empty()
        context = build_context('')
        self.assertGreaterEqual(context['ledger']['space_count'], 1)

    def test_context_with_match(self):
        seed_spaces_if_empty()
        card = create_card('带匹配的卡')
        from leasing_agent.storage import update_card
        update_card(card['id'], items={
            'area': {'state': 'specified', 'value': 500, 'evidence': '', 'note': ''},
        })
        context = build_context(card['id'])
        self.assertIsNotNone(context['match'])
        self.assertGreaterEqual(context['match']['total'], 0)

    def test_missing_card_raises_keyerror(self):
        with self.assertRaises(KeyError):
            build_context('no-such-card')

    def test_ledger_roundtrip_for_context(self):
        save_spaces([{
            'name': '助手台账房源', 'values': {'area': 400}, 'trace': {'source': '图纸'},
            'meta': {}, 'trace_complete': False,
        }])
        context = build_context('')
        self.assertGreaterEqual(context['ledger']['space_count'], 1)


if __name__ == '__main__':
    unittest.main()
