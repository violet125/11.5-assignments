"""匹配引擎测试：硬条件边界、未知参数、单位口径与溯源完整性。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from leasing_agent.core.matching import match  # noqa: E402


TRACE = {'source': '测试图纸', 'verifier': '张工', 'verified_at': '2026-01-01', 'version': 'v1'}


def space(name, **kwargs):
    record = {'name': name}
    record.update(kwargs)
    record.update(TRACE)
    return record


class MatchingTests(unittest.TestCase):
    def test_unknown_is_not_pass(self):
        result = match({'power': 100}, [space('A', power=None)])
        self.assertEqual(result['results'][0]['status'], '待核实')

    def test_violation_overrides_unknown(self):
        result = match({'power': 100, 'rent': 50},
                       [space('A', power=None, rent=60)])
        self.assertEqual(result['results'][0]['status'], '不匹配')

    def test_boundaries(self):
        result = match({'rent': 50, 'height': 4}, [space('A', rent=50, height=4)])
        self.assertEqual(result['results'][0]['status'], '已填条件满足')

    def test_empty_does_not_match(self):
        with self.assertRaises(ValueError):
            match({}, [space('A')])

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            match({'area': -1}, [space('A')])

    def test_only_specified_items_are_matched(self):
        """状态非"已明确"的字段不参与匹配。"""
        items = [
            {'field': 'area', 'state': 'specified', 'value': 500},
            {'field': 'height', 'state': 'unknown', 'value': None},
        ]
        result = match(items, [space('A', area=600, height=3.0)])
        self.assertEqual(result['results'][0]['status'], '已填条件满足')
        self.assertEqual(len(result['results'][0]['checks']), 1)

    def test_trace_incomplete_cannot_be_pass(self):
        """溯源不完整的房源不能判为"已填条件满足"。"""
        record = space('A', area=600)
        record['source'] = ''
        result = match({'area': 500}, [record])
        self.assertEqual(result['results'][0]['status'], '条件满足但溯源不全')

    def test_rent_is_upper_bound(self):
        result = match({'rent': 50}, [space('A', rent=40)])
        self.assertEqual(result['results'][0]['status'], '已填条件满足')

    def test_area_is_lower_bound(self):
        result = match({'area': 500}, [space('A', area=400)])
        self.assertEqual(result['results'][0]['status'], '不匹配')

    def test_redundancy_ordinal(self):
        """N+1 需求可由 N+1 房源满足，单路不满足双路需求。"""
        result = match({'redundancy': '双路'}, [space('A', redundancy='N+1')])
        self.assertEqual(result['results'][0]['status'], '已填条件满足')
        result = match({'redundancy': '双路'}, [space('B', redundancy='单路')])
        self.assertEqual(result['results'][0]['status'], '不匹配')

    def test_unit_bearing_text_is_rejected(self):
        with self.assertRaises(ValueError):
            match({'area': 500}, [{'name': 'A', 'area': '500㎡', **TRACE}])

    def test_kw_is_not_kva(self):
        """房源把设备功率 kW 填进供电容量应被拒绝。"""
        with self.assertRaises(ValueError):
            match({'power': 100}, [{'name': 'A', 'power': '100kW', **TRACE}])

    def test_missing_requirements_listed(self):
        result = match({'area': 500}, [space('A', area=600)])
        self.assertIn('最低净高', result['missing_requirements'])

    def test_gaps_are_explained(self):
        result = match({'area': 500}, [space('A', area=400)])
        self.assertTrue(result['results'][0]['gaps'])

    def test_sorting_puts_best_first(self):
        result = match(
            {'area': 500},
            [space('差', area=100), space('好', area=900), space('中', area=600)],
        )
        self.assertEqual([r['name'] for r in result['results']], ['好', '中', '差'])

    def test_duplicate_names_rejected(self):
        with self.assertRaises(ValueError):
            match({'area': 500}, [space('A'), space('A')])

    def test_empty_spaces_rejected(self):
        with self.assertRaises(ValueError):
            match({'area': 500}, [])


if __name__ == '__main__':
    unittest.main()
