"""载体参数台账测试：溯源校验与单位口径拦截。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from leasing_agent.inventory import (  # noqa: E402
    LedgerError,
    ledger_issues,
    ledger_template,
    normalize_ledger,
    normalize_space_record,
)


class LedgerTests(unittest.TestCase):
    def test_name_required(self):
        with self.assertRaises(LedgerError):
            normalize_space_record({'area': 100})

    def test_trace_fields_are_tracked(self):
        record = normalize_space_record({'name': 'A', 'area': 100}, 0)
        self.assertFalse(record['trace_complete'])
        self.assertIn('资料来源', record['missing_trace'])
        self.assertIn('核实人', record['missing_trace'])

    def test_complete_trace_passes(self):
        record = normalize_space_record({
            'name': 'A', 'area': 100,
            'source': '图纸', 'verifier': '张工',
            'verified_at': '2026-01-01', 'version': 'v1',
        }, 0)
        self.assertTrue(record['trace_complete'])
        self.assertEqual(record['missing_trace'], [])

    def test_verified_at_format_checked(self):
        with self.assertRaises(LedgerError):
            normalize_space_record({
                'name': 'A', 'verified_at': '2026年1月',
            }, 0)

    def test_unit_text_rejected(self):
        with self.assertRaises(LedgerError):
            normalize_space_record({'name': 'A', 'area': '500㎡'}, 0)

    def test_kw_rejected_as_power(self):
        with self.assertRaises(LedgerError):
            normalize_space_record({'name': 'A', 'power': '160kW'}, 0)

    def test_negative_rejected(self):
        with self.assertRaises(LedgerError):
            normalize_space_record({'name': 'A', 'area': -5}, 0)

    def test_enum_validated(self):
        with self.assertRaises(LedgerError):
            normalize_space_record({'name': 'A', 'redundancy': '三路'}, 0)

    def test_null_allowed(self):
        record = normalize_space_record({'name': 'A', 'area': None, 'power': ''}, 0)
        self.assertIsNone(record['values']['area'])
        self.assertIsNone(record['values']['power'])

    def test_duplicate_names_rejected(self):
        with self.assertRaises(LedgerError):
            normalize_ledger([{'name': 'A'}, {'name': 'A'}])

    def test_issues_reported(self):
        records = normalize_ledger([{'name': 'A', 'area': 100}])
        issues = ledger_issues(records)
        self.assertTrue(any('溯源' in issue for issue in issues))

    def test_template_is_valid(self):
        records = normalize_ledger(ledger_template())
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['trace_complete'])


if __name__ == '__main__':
    unittest.main()
