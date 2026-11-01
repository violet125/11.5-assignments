"""脱敏、字段权限与职责分离测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from leasing_agent.security import (  # noqa: E402
    PermissionError_,
    has_permission,
    load_role_permissions,
    mask_value,
    redact,
    redact_payload,
    require_permission,
    require_professional_clearing,
    visible_fields,
)

CONFIG = {
    'roles': {
        'business': {'can': ['card.create', 'card.edit', 'card.submit'], 'cannot_clear_professional': True},
        'engineer': {'can': ['card.confirm_professional'], 'cannot_clear_professional': False},
        'reviewer': {'can': ['card.review', 'card.confirm_professional'], 'cannot_clear_professional': False},
        'admin': {'can': ['*'], 'cannot_clear_professional': False},
    },
    'redaction': {'masked_for_business': ['customer_contact']},
}


class RedactionTests(unittest.TestCase):
    def test_phone_masked(self):
        masked = redact('联系电话13812345678')
        self.assertNotIn('13812345678', masked)

    def test_id_card_masked(self):
        masked = redact('身份证11010119900307561X')
        self.assertNotIn('11010119900307561X', masked)

    def test_email_masked(self):
        masked = redact('邮箱 zhang@example.com')
        self.assertNotIn('zhang@example.com', masked)

    def test_credit_code_masked(self):
        masked = redact('统一社会信用代码91310000MA1FL1234X')
        self.assertNotIn('91310000MA1FL1234X', masked)

    def test_plain_text_unchanged(self):
        text = '面积至少600平方米，净高不低于4米。'
        self.assertEqual(redact(text), text)

    def test_nested_payload_masked(self):
        payload = {'card': {'contact': '13812345678', 'items': [{'note': '联系13700001111'}]}}
        masked = redact_payload(payload)
        self.assertNotIn('13812345678', str(masked))
        self.assertNotIn('13700001111', str(masked))


class PermissionTests(unittest.TestCase):
    def test_business_cannot_confirm_professional(self):
        with self.assertRaises(PermissionError_):
            require_professional_clearing('business', CONFIG)

    def test_engineer_can_clear_professional(self):
        require_professional_clearing('engineer', CONFIG)

    def test_reviewer_can_clear_professional(self):
        require_professional_clearing('reviewer', CONFIG)

    def test_admin_has_all(self):
        permissions = load_role_permissions(CONFIG)
        self.assertTrue(has_permission('admin', 'anything', permissions))

    def test_permission_enforced(self):
        permissions = load_role_permissions(CONFIG)
        with self.assertRaises(PermissionError_):
            require_permission('business', 'card.review', permissions)

    def test_business_can_create(self):
        permissions = load_role_permissions(CONFIG)
        require_permission('business', 'card.create', permissions)

    def test_masked_field_visible_only_to_others(self):
        fields = ['customer_contact', 'customer_name']
        self.assertEqual(visible_fields('business', fields, CONFIG), ['customer_name'])
        self.assertEqual(visible_fields('engineer', fields, CONFIG), fields)

    def test_mask_value_for_business(self):
        masked = mask_value('business', 'customer_contact', '13812345678', CONFIG)
        self.assertNotEqual(masked, '13812345678')
        self.assertEqual(mask_value('engineer', 'customer_contact', '13812345678', CONFIG),
                         '13812345678')


if __name__ == '__main__':
    unittest.main()
