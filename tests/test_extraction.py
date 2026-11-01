"""抽取准确性、证据与单位校验测试。"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from leasing_agent.extractors import extract  # noqa: E402
from leasing_agent.schemas.fields import (  # noqa: E402
    STATE_NONE_CONFIRMED,
    STATE_NOT_REQUESTED,
    STATE_SPECIFIED,
    STATE_UNKNOWN,
)


def rule_extract(text):
    old = os.environ.get('AGENT_API_KEY')
    os.environ.pop('AGENT_API_KEY', None)
    try:
        return extract(text)
    finally:
        if old is not None:
            os.environ['AGENT_API_KEY'] = old


class ExtractionTests(unittest.TestCase):
    def test_five_core_fields(self):
        result = rule_extract(
            '面积至少600平方米，租金不超过50元/㎡/月，供电容量不低于160kVA，'
            '承重不低于500kg/㎡，净高不低于4米。'
        )
        items = result['items']
        self.assertEqual(items['area']['value'], 600)
        self.assertEqual(items['rent']['value'], 50)
        self.assertEqual(items['power']['value'], 160)
        self.assertEqual(items['load']['value'], 500)
        self.assertEqual(items['height']['value'], 4)
        self.assertTrue(all(items[k]['state'] == STATE_SPECIFIED for k in
                            ('area', 'rent', 'power', 'load', 'height')))

    def test_units_are_not_invented(self):
        """层高不能当净高，kW 不能当 kVA，年租金不能当月单价。"""
        result = rule_extract('层高5米，功率200kW，租金每年100万元。')
        items = result['items']
        self.assertIsNone(items['height']['value'])
        self.assertIsNone(items['power']['value'])
        self.assertIsNone(items['rent']['value'])

    def test_layer_height_is_separate_field(self):
        result = rule_extract('层高不低于5米。')
        self.assertEqual(result['items']['layer_height']['value'], 5)
        self.assertIsNone(result['items']['height']['value'])

    def test_layer_height_does_not_imply_height(self):
        result = rule_extract('层高5米。')
        self.assertEqual(result['items']['height']['state'], STATE_UNKNOWN)
        self.assertIn('净高', result['items']['height']['note'])

    def test_conflicting_mentions_require_review(self):
        result = rule_extract('净高4米，也讨论过净高5米')
        self.assertIsNone(result['items']['height']['value'])
        self.assertEqual(result['items']['height']['state'], STATE_UNKNOWN)

    def test_blank_has_three_distinct_meanings(self):
        """未知待补、未提出要求、确认无此要求必须可区分。"""
        # 提到但没给数值 -> 未知待补
        mentioned = rule_extract('供电容量要比较大一些')
        self.assertEqual(mentioned['items']['power']['state'], STATE_UNKNOWN)

        # 明确表示不需要 -> 确认无此要求
        negated = rule_extract('客户明确表示不需要特殊地面加固。')
        self.assertEqual(negated['items']['load']['state'], STATE_NONE_CONFIRMED)

        # 本次访谈完全未涉及 -> 未提出要求
        untouched = rule_extract('今天先聊聊客户的基本情况。')
        self.assertEqual(untouched['items']['area']['state'], STATE_NOT_REQUESTED)

    def test_not_requested_state_exists(self):
        """未提及的维度标为"未提出要求"，不是"未知待补"。"""
        result = rule_extract('面积至少600平方米')
        self.assertEqual(result['items']['power']['state'], STATE_NOT_REQUESTED)
        self.assertEqual(result['items']['area']['state'], STATE_SPECIFIED)

    def test_evidence_is_verbatim(self):
        text = '面积至少600平方米，净高不低于4米。'
        result = rule_extract(text)
        for item in result['items'].values():
            if item['evidence']:
                self.assertIn(item['evidence'], text)

    def test_empty_input_rejected(self):
        with self.assertRaises(Exception):
            rule_extract('   ')

    def test_overlong_input_rejected(self):
        with self.assertRaises(Exception):
            rule_extract('啊' * 20001)

    def test_every_field_present_in_output(self):
        from leasing_agent.schemas.fields import FIELD_KEYS
        result = rule_extract('面积至少600平方米')
        for key in FIELD_KEYS:
            self.assertIn(key, result['items'])


if __name__ == '__main__':
    unittest.main()
