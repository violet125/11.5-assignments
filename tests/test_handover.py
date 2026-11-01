"""交接校验测试：必填、语义、专业确认与阻断判定。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from leasing_agent.core.handover import (  # noqa: E402
    BLOCKING,
    can_handover,
    can_submit,
    validate_handover,
)
from leasing_agent.api.context import get_config  # noqa: E402
from leasing_agent.schemas.fields import (  # noqa: E402
    STATE_NONE_CONFIRMED,
    STATE_NOT_REQUESTED,
    STATE_SPECIFIED,
    STATE_UNKNOWN,
    blank_item,
)


def item(key, value=None, state=STATE_SPECIFIED, evidence='原文'):
    entry = blank_item(key, state, evidence, 'manual')
    if state == STATE_SPECIFIED:
        entry['value'] = value
    return entry


def card(purpose='研发办公', **overrides):
    items = {
        'area': item('area', 600),
        'rent': item('rent', 50),
        'power': item('power', 160),
        'height': item('height', 4),
    }
    items.update(overrides)
    return {'purpose': purpose, 'items': items}


CONFIG = get_config()


class HandoverTests(unittest.TestCase):
    def test_missing_required_blocks(self):
        items = {'area': item('area', 600)}
        result = validate_handover({'purpose': '研发办公', 'items': items}, CONFIG)
        self.assertFalse(result['passed'])
        self.assertGreater(result['blocking_count'], 0)

    def test_not_requested_on_required_field_blocks(self):
        """必填字段标"未提出要求"仍然阻断，必须由客户确认。"""
        items = {
            'area': item('area', 600), 'rent': item('rent', 50),
            'power': item('power', 160), 'height': item('height', 4),
        }
        items['layer_height'] = item('layer_height', state=STATE_NOT_REQUESTED, evidence='')
        result = validate_handover({'purpose': '生产制造', 'items': items}, CONFIG)
        self.assertFalse(result['passed'])

    def test_professional_confirmation_required(self):
        result = validate_handover(card(), CONFIG, confirmations=[])
        blocking = [i for i in result['issues'] if i['type'] == 'no_professional_confirm']
        self.assertTrue(blocking)

    def test_professional_confirmation_clears_block(self):
        confirmations = [{'field': 'height', 'by': '张工', 'role': 'engineer', 'valid': True}]
        result = validate_handover(card(), CONFIG, confirmations)
        blocking = [i for i in result['issues'] if i['type'] == 'no_professional_confirm']
        self.assertFalse(blocking)

    def test_missing_evidence_blocks(self):
        items = {
            'area': item('area', 600, evidence=''), 'rent': item('rent', 50),
            'power': item('power', 160), 'height': item('height', 4),
        }
        result = validate_handover({'purpose': '研发办公', 'items': items}, CONFIG)
        self.assertFalse(result['passed'])
        self.assertTrue(any(i['type'] == 'missing_evidence' for i in result['issues']))

    def test_layer_height_evidence_on_net_height_blocks(self):
        items = {
            'area': item('area', 600), 'rent': item('rent', 50),
            'power': item('power', 160),
            'height': item('height', 4, evidence='层高4米'),
        }
        result = validate_handover({'purpose': '研发办公', 'items': items}, CONFIG)
        self.assertTrue(any(i['type'] == 'unit_conflict' for i in result['issues']))

    def test_kw_evidence_on_power_blocks(self):
        items = {
            'area': item('area', 600), 'rent': item('rent', 50),
            'power': item('power', 160, evidence='功率160kW'),
            'height': item('height', 4),
        }
        result = validate_handover({'purpose': '研发办公', 'items': items}, CONFIG)
        self.assertTrue(any(i['type'] == 'unit_conflict' for i in result['issues']))

    def test_unknown_purpose_blocks(self):
        result = validate_handover({'purpose': '', 'items': {}}, CONFIG)
        self.assertFalse(result['passed'])
        self.assertTrue(any(i['type'] == 'unknown_purpose' for i in result['issues']))

    def test_checklist_is_generated(self):
        result = validate_handover({'purpose': '研发办公', 'items': {}}, CONFIG)
        self.assertTrue(result['checklist'])

    def test_advisory_does_not_block(self):
        """条件必填项缺失只是提示，不阻断交接。"""
        items = {
            'area': item('area', 600), 'rent': item('rent', 50),
            'power': item('power', 160), 'height': item('height', 4),
        }
        result = validate_handover({'purpose': '研发办公', 'items': items}, CONFIG,
                                   confirmations=[{'field': 'height', 'valid': True}])
        self.assertTrue(result['passed'])
        self.assertGreaterEqual(result['advisory_count'], 0)

    def test_state_machine(self):
        self.assertTrue(can_submit('draft'))
        self.assertTrue(can_submit('returned'))
        self.assertFalse(can_submit('handover'))
        self.assertFalse(can_submit('pending'))
        self.assertTrue(can_handover('draft', {'passed': True}))
        self.assertFalse(can_handover('draft', {'passed': False}))
        self.assertFalse(can_handover('escalated', {'passed': True}))


if __name__ == '__main__':
    unittest.main()
