"""报价单草稿测试：条款计算、状态流转、存储与列表分页。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

_TMP = tempfile.mkdtemp(prefix='leasing-quote-')
os.environ['AGENT_DB'] = str(Path(_TMP) / 'quote.db')

from leasing_agent.core.quotation import (  # noqa: E402
    QUOTE_DRAFT,
    QUOTE_ISSUED,
    QUOTE_VOIDED,
    QuoteError,
    authority_breach,
    build_terms,
    quote_caveats,
    quote_summary,
    validate_quote_action,
    validate_revision,
)
from leasing_agent.storage import (  # noqa: E402
    count_cards,
    create_card,
    create_quote,
    get_quote,
    list_cards,
    list_quotes,
    update_quote_approval,
    update_quote_status,
)

CONFIG_QUOTATION = {
    'deposit_months': 3,
    'free_rent_days': 30,
    'lease_term_months': 36,
}


class BuildTermsTests(unittest.TestCase):
    def test_defaults_from_ledger_and_config(self):
        terms = build_terms({'rent': 42, 'area': 1200}, None, CONFIG_QUOTATION)
        self.assertEqual(terms['unit_price'], 42)
        self.assertEqual(terms['area'], 1200)
        self.assertEqual(terms['monthly_total'], 50400.0)
        self.assertEqual(terms['deposit_months'], 3)
        self.assertEqual(terms['free_rent_days'], 30)
        self.assertEqual(terms['lease_term_months'], 36)
        self.assertEqual(terms['total_revenue'], 1814400.0)

    def test_overrides_take_precedence(self):
        terms = build_terms(
            {'rent': 42, 'area': 1200},
            {'unit_price': 50, 'area': 1000, 'deposit_months': 6,
             'free_rent_days': 0, 'lease_term_months': 12, 'note': '季付'},
            CONFIG_QUOTATION)
        self.assertEqual(terms['unit_price'], 50)
        self.assertEqual(terms['area'], 1000)
        self.assertEqual(terms['monthly_total'], 50000.0)
        self.assertEqual(terms['deposit_months'], 6)
        self.assertEqual(terms['free_rent_days'], 0)
        self.assertEqual(terms['lease_term_months'], 12)
        self.assertEqual(terms['total_revenue'], 600000.0)
        self.assertEqual(terms['note'], '季付')

    def test_missing_ledger_value_requires_override(self):
        with self.assertRaises(QuoteError):
            build_terms({'rent': None, 'area': 1200}, None, CONFIG_QUOTATION)
        terms = build_terms({'rent': None, 'area': 1200}, {'unit_price': 30}, CONFIG_QUOTATION)
        self.assertEqual(terms['unit_price'], 30)

    def test_zero_is_not_a_valid_price(self):
        with self.assertRaises(QuoteError):
            build_terms({'rent': 0, 'area': 1200}, None, CONFIG_QUOTATION)
        with self.assertRaises(QuoteError):
            build_terms({'rent': 42, 'area': 1200}, {'unit_price': 0}, CONFIG_QUOTATION)

    def test_bounds_reject_nonsense(self):
        with self.assertRaises(QuoteError):
            build_terms({'rent': 42, 'area': 1200}, {'lease_term_months': 999}, CONFIG_QUOTATION)
        with self.assertRaises(QuoteError):
            build_terms({'rent': 42, 'area': 1200}, {'deposit_months': 13}, CONFIG_QUOTATION)
        with self.assertRaises(QuoteError):
            build_terms({'rent': 42, 'area': 1200}, {'free_rent_days': -1}, CONFIG_QUOTATION)

    def test_note_length_limited(self):
        with self.assertRaises(QuoteError):
            build_terms({'rent': 42, 'area': 1200}, {'note': 'x' * 501}, CONFIG_QUOTATION)

    def test_config_fallback_when_missing(self):
        terms = build_terms({'rent': 10, 'area': 100}, None, None)
        self.assertEqual(terms['deposit_months'], 3)
        self.assertEqual(terms['lease_term_months'], 36)


class BuildTermsV2Tests(unittest.TestCase):
    def test_three_year_cost_with_zero_escalation(self):
        terms = build_terms({'rent': 42, 'area': 1200}, None, CONFIG_QUOTATION)
        # 月租金 50400，递增 0% → 三年租金 = 50400 × 36
        self.assertEqual(terms['rent_3y'], 1814400.0)
        self.assertEqual(terms['property_3y'], 0)
        self.assertEqual(terms['renovation_input'], 0)
        self.assertEqual(terms['total_3y'], 1814400.0)
        self.assertTrue(any('需园区确认' in a for a in terms['assumptions']))

    def test_rentable_area_uses_efficiency_ratio(self):
        terms = build_terms({'rent': 42, 'area': 1200, 'efficiency_ratio': 80},
                            None, CONFIG_QUOTATION)
        self.assertEqual(terms['rentable_area'], 960.0)
        self.assertEqual(terms['monthly_total'], 40320.0)
        self.assertEqual(terms['rent_3y'], 1451520.0)

    def test_property_fee_and_escalation(self):
        terms = build_terms(
            {'rent': 40, 'area': 1000},
            {'property_fee': 8, 'escalation_pct': 10},
            CONFIG_QUOTATION)
        # 三年租金 = 12 × 40000 × (1 + 1.1 + 1.21) = 12 × 40000 × 3.31
        self.assertEqual(terms['rent_3y'], 1588800.0)
        self.assertEqual(terms['property_3y'], 8 * 1000 * 36)
        self.assertEqual(terms['total_3y'], 1588800.0 + 288000.0)

    def test_renovation_lookup_by_delivery_status(self):
        config = dict(CONFIG_QUOTATION)
        config['renovation_cost_per_sqm'] = {'L1': [300, 600]}
        terms = build_terms({'rent': 40, 'area': 1000, 'delivery_status': 'L1'},
                            None, config)
        self.assertEqual(terms['renovation_cost_per_sqm'], 450.0)
        self.assertEqual(terms['renovation_input'], 450000.0)
        self.assertTrue(any('L1' in a for a in terms['assumptions']))

    def test_renovation_override_disables_estimate(self):
        terms = build_terms({'rent': 40, 'area': 1000, 'delivery_status': 'L1'},
                            {'renovation_cost_per_sqm': 500}, CONFIG_QUOTATION)
        self.assertEqual(terms['renovation_cost_per_sqm'], 500.0)
        self.assertEqual(terms['renovation_input'], 500000.0)

    def test_subsidies_reduce_total(self):
        terms = build_terms({'rent': 40, 'area': 1000},
                            {'renovation_subsidy': 100000, 'moving_subsidy': 20000},
                            CONFIG_QUOTATION)
        self.assertEqual(terms['subsidies_total'], 120000.0)
        self.assertEqual(terms['total_3y'], terms['rent_3y'] + terms['property_3y'] - 120000.0)

    def test_discount_and_requires_approval(self):
        # 台账价 42，报价 30 → 折扣 28.57% > 5%
        terms = build_terms({'rent': 42, 'area': 1200},
                            {'unit_price': 30}, CONFIG_QUOTATION)
        self.assertEqual(terms['discount_pct'], 28.57)
        self.assertTrue(terms['requires_approval'])
        breaches = authority_breach(terms, CONFIG_QUOTATION)
        self.assertEqual(len(breaches), 1)
        self.assertIn('折扣', breaches[0])

    def test_free_rent_over_authority(self):
        terms = build_terms({'rent': 42, 'area': 1200},
                            {'free_rent_days': 120}, CONFIG_QUOTATION)
        self.assertTrue(terms['requires_approval'])
        breaches = authority_breach(terms, CONFIG_QUOTATION)
        self.assertIn('免租', breaches[0])

    def test_within_authority_needs_no_approval(self):
        terms = build_terms({'rent': 42, 'area': 1200}, None, CONFIG_QUOTATION)
        self.assertFalse(terms['requires_approval'])
        self.assertEqual(authority_breach(terms, CONFIG_QUOTATION), [])

    def test_no_ledger_rent_skips_discount(self):
        terms = build_terms({'rent': None, 'area': 1000}, {'unit_price': 30}, CONFIG_QUOTATION)
        self.assertIsNone(terms['ledger_rent'])
        self.assertEqual(terms['discount_pct'], 0)
        self.assertFalse(terms['requires_approval'])

    def test_validity_days_default(self):
        terms = build_terms({'rent': 40, 'area': 1000}, None, CONFIG_QUOTATION)
        self.assertEqual(terms['validity_days'], 15)
        terms = build_terms({'rent': 40, 'area': 1000}, {'validity_days': 7}, CONFIG_QUOTATION)
        self.assertEqual(terms['validity_days'], 7)
        with self.assertRaises(QuoteError):
            build_terms({'rent': 40, 'area': 1000}, {'validity_days': 0}, CONFIG_QUOTATION)

    def test_revision_validation(self):
        with self.assertRaises(QuoteError):
            validate_revision(2, '')
        with self.assertRaises(QuoteError):
            validate_revision(2, '改价')
        self.assertEqual(validate_revision(2, '客户要求单价下调至 35 元'), '客户要求单价下调至 35 元')
        self.assertEqual(validate_revision(1, ''), '')


class QuoteFlowTests(unittest.TestCase):
    def test_status_flow(self):
        self.assertEqual(validate_quote_action(QUOTE_DRAFT, 'issue'), QUOTE_ISSUED)
        self.assertEqual(validate_quote_action(QUOTE_DRAFT, 'void'), QUOTE_VOIDED)
        self.assertEqual(validate_quote_action(QUOTE_ISSUED, 'void'), QUOTE_VOIDED)
        with self.assertRaises(QuoteError):
            validate_quote_action(QUOTE_ISSUED, 'issue')
        with self.assertRaises(QuoteError):
            validate_quote_action(QUOTE_VOIDED, 'void')
        with self.assertRaises(QuoteError):
            validate_quote_action(QUOTE_DRAFT, 'delete')

    def test_caveats_always_present(self):
        caveats = quote_caveats('已填条件满足')
        self.assertTrue(any('草稿素材' in c for c in caveats))
        self.assertTrue(any('元/㎡/月' in c for c in caveats))
        extra = quote_caveats('条件满足但溯源不全')
        self.assertTrue(any('溯源' in c for c in extra))
        self.assertGreater(len(extra), len(caveats))

    def test_summary_format(self):
        quote = {'quote_no': 'RJ-202611-001', 'space_name': '某房源',
                 'terms': {'unit_price': 42, 'unit_label': '元/㎡/月', 'area': 1200,
                           'rentable_area': 1200, 'area_label': '㎡', 'monthly_total': 50400,
                           'total_3y': 1814400.0}}
        text = quote_summary(quote)
        self.assertIn('RJ-202611-001', text)
        self.assertIn('某房源', text)
        self.assertIn('42元/㎡/月', text)
        self.assertIn('三年总占用成本 1814400.0 元', text)


class QuoteStorageTests(unittest.TestCase):
    def test_quote_crud_and_card_filter(self):
        card = create_card(title='报价存储测试卡', purpose='通用')
        quote = create_quote(card['id'], '测试房源 A', {'unit_price': 40, 'area': 500,
                                                       'monthly_total': 20000,
                                                       'validity_days': 15},
                             '已填条件满足', '张三', 'business')
        self.assertEqual(quote['status'], 'draft')
        self.assertEqual(quote['card_title'], '报价存储测试卡')
        self.assertEqual(quote['actor'], '张三')
        self.assertEqual(quote['terms']['unit_price'], 40)
        # v2：编号 / 版本 / 轮次 / 有效期
        self.assertRegex(quote['quote_no'], r'^RJ-\d{6}-\d{3}$')
        self.assertEqual(quote['quote_version'], 1)
        self.assertEqual(quote['round'], '首次报价')
        self.assertTrue(quote['valid_until'])

        fetched = get_quote(quote['id'])
        self.assertEqual(fetched['space_name'], '测试房源 A')

        in_card = list_quotes(card['id'])
        self.assertTrue(any(q['id'] == quote['id'] for q in in_card))

        updated = update_quote_status(quote['id'], 'issued')
        self.assertEqual(updated['status'], 'issued')
        self.assertEqual(get_quote(quote['id'])['status'], 'issued')

    def test_quote_versioning_and_supersede(self):
        card = create_card(title='版本测试卡', purpose='通用')
        first = create_quote(card['id'], '版本房源', {'unit_price': 40, 'area': 500,
                                                      'monthly_total': 20000,
                                                      'validity_days': 15},
                             '已填条件满足', '张三', 'business')
        # 修订必须给原因
        with self.assertRaises(ValueError):
            create_quote(card['id'], '版本房源', {'unit_price': 38, 'area': 500,
                                                  'monthly_total': 19000,
                                                  'validity_days': 15},
                         '已填条件满足', '张三', 'business')
        second = create_quote(card['id'], '版本房源', {'unit_price': 38, 'area': 500,
                                                       'monthly_total': 19000,
                                                       'validity_days': 15},
                              '已填条件满足', '张三', 'business',
                              revision_reason='客户要求下调单价')
        self.assertEqual(second['quote_version'], 2)
        self.assertEqual(second['round'], '修订报价')
        self.assertEqual(second['revision_reason'], '客户要求下调单价')
        self.assertEqual(second['seq'], first['seq'] + 1)
        self.assertEqual(second['superseded'], [first['id']])
        # 旧版被自动置为 voided
        self.assertEqual(get_quote(first['id'])['status'], 'voided')

    def test_quote_approval_record(self):
        card = create_card(title='审批测试卡', purpose='通用')
        quote = create_quote(card['id'], '审批房源', {'unit_price': 30, 'area': 500,
                                                      'monthly_total': 15000,
                                                      'requires_approval': True,
                                                      'validity_days': 15},
                             '已填条件满足', '张三', 'business')
        self.assertEqual(quote['approved_by'], '')
        approved = update_quote_approval(quote['id'], '管理员')
        self.assertEqual(approved['approved_by'], '管理员')
        self.assertTrue(approved['approved_at'])

    def test_card_list_search_filter_paging(self):
        marker = f'分页测试{os.urandom(3).hex()}'
        for index in range(3):
            create_card(title=f'{marker}卡{index}', customer=marker,
                        purpose='通用' if index % 2 == 0 else '研发办公')
        total = count_cards(q=marker)
        self.assertEqual(total, 3)
        page_one = list_cards(q=marker, page=1, page_size=2)
        self.assertEqual(len(page_one), 2)
        page_two = list_cards(q=marker, page=2, page_size=2)
        self.assertEqual(len(page_two), 1)

        filtered = list_cards(q=marker, status='draft', page=1, page_size=10)
        self.assertEqual(len(filtered), 3)
        # 未命中关键词时计数为 0
        self.assertEqual(count_cards(q='不存在关键词xyz'), 0)

        # 兼容旧调用：不带分页参数时返回全部
        legacy = list_cards()
        self.assertGreaterEqual(len(legacy), 3)


if __name__ == '__main__':
    unittest.main()
