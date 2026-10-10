"""Automated checks for the synthetic replay fixture; semantic review stays pending."""

from __future__ import annotations

import json
import unittest

from sanjing.config import PROJECT_DIR
from verification.factual_check import evaluate, fixture_payload


class FactualCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(
            (PROJECT_DIR / "verification" / "factual_cases.json").read_text(encoding="utf-8")
        )
        cls.report = evaluate()

    def test_thirty_cases_have_distinct_reviewable_source_and_claim(self) -> None:
        cases = self.catalog["cases"]
        self.assertEqual(len(cases), 30)
        fingerprints = set()
        for case in cases:
            self.assertTrue(case.get("proposed_inference"), case["id"])
            self.assertTrue(case.get("human_check"), case["id"])
            self.assertIn("source_items", case, case["id"])
            self.assertIsInstance(case["source_items"], list, case["id"])
            self.assertTrue(case.get("read_fields"), case["id"])
            fingerprints.add(json.dumps(case["source_items"], ensure_ascii=False, sort_keys=True))
        self.assertEqual(len(fingerprints), 30)

    def test_runner_uses_real_query_record_reads_and_keeps_review_pending(self) -> None:
        self.assertEqual(self.report["count"], 30)
        self.assertEqual(self.report["structure_passed"], 30)
        self.assertGreater(self.report["field_reads_total"], 0)
        self.assertEqual(self.report["field_reads_passed"], self.report["field_reads_total"])
        rows = {row["id"]: row for row in self.report["cases"]}
        self.assertTrue(rows["company_exact"]["query_metadata_read_passed"])
        self.assertTrue(rows["company_exact"]["source_field_copy_passed"])
        self.assertTrue(rows["source_missing"]["missing_source_fields"])
        self.assertTrue(all(
            item["handled_as_missing"]
            for item in rows["source_missing"]["missing_source_fields"]
        ))
        self.assertFalse(rows["provider_unknown_type"]["route_verified"])
        self.assertEqual(rows["risk_none"]["query_error_code"], "data_empty")
        self.assertEqual(rows["provider_business_error"]["query_error_code"], "data_business_error")
        for row in self.report["cases"]:
            self.assertEqual(row["human_check_status"], "pending_human_review")
            self.assertEqual(row["citation_support_review"], "pending_human_review")
            self.assertEqual(row["factual_scope_review"], "pending_human_review")
        self.assertIn("pending_human_review", self.report["notice"])

    def test_one_decision_only_exercises_the_citation_mechanism(self) -> None:
        demo = self.report["citation_mechanism_demo"]
        self.assertTrue(demo["attempted"])
        self.assertTrue(demo["passed"])
        self.assertEqual(demo["citation_field"], "/record/status")
        self.assertEqual(demo["program_resolved_value"], "存续")
        self.assertIn("不评价", demo["scope"])

    def test_error_variants_do_not_return_synthetic_records_as_success(self) -> None:
        cases = {case["id"]: case for case in self.catalog["cases"]}
        for case_id, code in (
            ("risk_none", 0),
            ("provider_business_error", 403),
        ):
            payload = fixture_payload(cases[case_id])
            self.assertEqual(payload["code"], code)
        self.assertEqual(fixture_payload(cases["provider_wrong_type"])["dataset_type"], "academic_search")
        self.assertEqual(fixture_payload(cases["provider_unknown_type"])["dataset_type"], "unmapped_provider_type")
        self.assertEqual(fixture_payload(cases["provider_bad_shape"])["items"], [
            "这是一条字符串候选，不是记录对象。"
        ])


if __name__ == "__main__":
    unittest.main()
