"""适用性提示由可观察字段产生；不暗示程序证明了推论。"""
import unittest
from sanjing.evidence_quality import quality_flags
from sanjing import viewer


class EvidenceQualityTests(unittest.TestCase):
    def codes(self, data):
        return {row['code'] for row in quality_flags(data)}

    def test_fetch_timestamp_does_not_replace_publication(self):
        flags = self.codes({'fetched_at': '2026-10-11', 'record': {}, 'source': {}, 'route_verified': True})
        self.assertIn('time_unknown', flags)
        self.assertNotIn('check_time_scope', flags)

    def test_abstract_is_limited_and_fulltext_does_not_imply_validity(self):
        data = {'category': 'research', 'record': {'abstract': '合成摘要'}, 'source': {}, 'route_verified': True}
        self.assertIn('summary_only', self.codes(data))
        data['record']['full_text'] = '合成全文'
        self.assertNotIn('summary_only', self.codes(data))
        self.assertIn('scope_not_proven', self.codes(data))

    def test_row_id_is_not_enterprise_identity_and_empty_risk_is_not_safe(self):
        data = {'category': 'company_risk', 'record': {'id': 'row-1', 'risk': []}, 'source': {'record_id': 'row-1'}}
        self.assertIn('entity_unresolved', self.codes(data))
        self.assertIn('risk_coverage_unknown', self.codes(data))
        data['record']['统一社会信用代码'] = 'synthetic-entity'
        self.assertNotIn('entity_unresolved', self.codes(data))

    def test_period_unit_and_html_escaping_for_old_record(self):
        data = {'category': 'industry', 'record': {'title': '<script>bad()</script>'},
                'source': {}, 'route_verified': True}
        self.assertIn('period_missing', self.codes(data))
        self.assertIn('unit_missing', self.codes(data))
        html = viewer._professional_record_card({'id': 'ev-old'}, data)
        self.assertIn('适用性提示', html)
        self.assertNotIn('<script>', html)
        self.assertEqual(data['source'], {})  # 渲染不改写旧账目
