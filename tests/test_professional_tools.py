"""专业资料工具边界；完全使用内存存储与伪造客户端，不访问网络或密钥。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sanjing import tools


def call(name, arguments, call_id="call-test"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class MemoryStore:
    def __init__(self):
        self.events = {}
        self.evidence = {}
        self.recommendations = {}
        self.messages = {}
        self.sequence = 0

    def _id(self, prefix):
        self.sequence += 1
        return f"{prefix}-{self.sequence}"

    def add_event(self, db_path, session_id, turn_id, tool_name, call_id, status,
                  error_code=None, duration_ms=0):
        event_id = self._id("event")
        self.events[event_id] = {
            "id": event_id, "session_id": session_id, "turn_id": turn_id,
            "tool_name": tool_name, "call_id": call_id, "status": status,
            "error_code": error_code, "duration_ms": duration_ms,
        }
        return event_id

    def update_event(self, db_path, event_id, status, error_code=None, duration_ms=0):
        self.events[event_id].update(status=status, error_code=error_code, duration_ms=duration_ms)

    def get_event(self, db_path, session_id, event_id):
        event = self.events.get(event_id)
        return dict(event) if event and event["session_id"] == session_id else None

    def add_evidence(self, db_path, session_id, turn_id, kind, data, event_id=None):
        evidence_id = self._id("ev")
        self.evidence[evidence_id] = {
            "id": evidence_id, "session_id": session_id, "turn_id": turn_id,
            "kind": kind, "data": data, "event_id": event_id,
        }
        return evidence_id

    def get_evidence(self, db_path, session_id, evidence_id):
        item = self.evidence.get(evidence_id)
        return dict(item) if item and item["session_id"] == session_id else None

    def list_evidence(self, db_path, session_id, turn_id=None):
        return [dict(item) for item in self.evidence.values()
                if item["session_id"] == session_id and (turn_id is None or item["turn_id"] == turn_id)]

    def get_recommendation(self, db_path, session_id, record_id):
        item = self.recommendations.get(record_id)
        return dict(item) if item and item["session_id"] == session_id else None

    def add_recommendation(self, db_path, session_id, turn_id, data):
        record_id = self._id("rec")
        self.recommendations[record_id] = {
            "id": record_id, "session_id": session_id, "turn_id": turn_id, "data": data,
        }
        return record_id

    def get_message(self, db_path, session_id, message_id):
        item = self.messages.get(message_id)
        return dict(item) if item and item["session_id"] == session_id else None


class ProfessionalToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore()
        self.store_patch = patch("sanjing.tools._store", return_value=self.store)
        self.store_patch.start()
        self.calls = []
        self.ctx = {
            "db_path": Path(self.temp.name) / "agent.sqlite3",
            "session_id": "session-a",
            "turn_id": "turn-a",
            "user_message_id": "msg-a",
            "allowed_evidence_ids": set(),
            "pending_recommendations": [],
            "deadline": time.monotonic() + 5,
            "data_usage": {"requests": 0, "success": 0, "failed": 0, "timeouts": 0, "cache_hits": 0},
            "data_cache": {},
            "data_failures": [],
            "data_client": self.client,
        }
        self.store.messages["msg-a"] = {
            "id": "msg-a", "session_id": "session-a", "turn_id": "turn-a",
            "role": "user", "content": "我正在考虑一家企业。",
        }
        self.payload = {
            "code": 0,
            "msg": "ok",
            "trace_id": "trace-a",
            "dataset_type": "academic_search",
            "items": [
                {"id": str(index), "title": f"研究记录{index}",
                 "abstract": "公开摘要内容" * 30, "url": f"https://example.test/{index}",
                 "published_at": "2025-01-01"}
                for index in range(12)
            ],
        }

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def client(self, query):
        self.calls.append(query)
        return self.payload

    def run_tool(self, name, arguments, ctx=None, call_id=None):
        return tools.execute_tool(ctx or self.ctx, call(name, arguments, call_id or f"call-{name}-{len(self.calls)}"))

    def query(self, *, category="research", query_text="模型证据"):
        return self.run_tool("query_professional_data", {"category": category, "query": query_text})

    def test_query_saves_every_candidate_but_grants_only_bounded_index(self):
        result = self.query()
        self.assertTrue(result["ok"], result)
        envelope = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(envelope), 1500)
        self.assertIn("查询研究与学术资料：模型证据", self.calls[0])
        self.assertEqual(self.ctx["data_usage"]["requests"], 1)
        self.assertEqual(self.ctx["data_usage"]["success"], 1)

        records = [item for item in self.store.evidence.values() if item["kind"] == "professional_record"]
        self.assertEqual(len(records), len(self.payload["items"]))
        snapshot_id = result["data"]["query_evidence_id"]
        self.assertEqual(self.store.evidence[snapshot_id]["kind"], "professional_query")
        self.assertTrue(self.store.evidence[snapshot_id]["data"]["response_sha256"])
        self.assertTrue(all(item["data"]["query_evidence_id"] == snapshot_id for item in records))

        indexed_ids = {item["evidence_id"] for item in result["data"]["items"]}
        self.assertTrue(indexed_ids)
        self.assertLess(len(indexed_ids), len(records))
        self.assertEqual(set(result["evidence_ids"]), indexed_ids | {snapshot_id})
        self.assertEqual(self.ctx["allowed_evidence_ids"], indexed_ids | {snapshot_id})
        self.assertNotIn("公开摘要内容", envelope)

    def test_macro_source_keeps_month_unit_and_original_date_meaning(self):
        self.payload = {"code": 0, "dataset_type": "macro", "items": [{
            "series_name": "规模以上工业增加值:当月同比", "indicator_name": "当月同比",
            "source_system": "INDUSTRY_CHAIN", "dataset_id": "sample-macro",
            "year": 2025, "time_raw": "2025-12-31", "time_grain": "month",
            "value": 5.9, "unit_raw": "%", "p_date": "2026-01-20",
            "date": "2025-12-31",
        }]}
        result = self.query(category="industry", query_text="工业指标的时间口径")
        self.assertTrue(result["ok"], result)
        evidence_id = result["data"]["items"][0]["evidence_id"]
        data = self.store.evidence[evidence_id]["data"]
        self.assertTrue(data["route_verified"])
        self.assertEqual(data["source"]["unit"], "%")
        self.assertEqual(data["source"]["statistical_period"], "2025-12-31")
        self.assertEqual(data["source"]["source_system"], "INDUSTRY_CHAIN")
        self.assertIsNone(data["source"]["published_at"])
        self.assertIn("/record/time_raw", result["data"]["items"][0]["field_paths"])
        self.assertEqual(data["record"]["time_grain"], "month")

    def test_every_candidate_is_persisted_even_when_index_is_compact(self):
        self.payload["items"] = [
            {"id": str(index), "title": f"record-{index}", "summary": "brief"}
            for index in range(105)
        ]
        result = self.query(query_text="large candidate batch")
        self.assertTrue(result["ok"], result)
        records = [item for item in self.store.evidence.values() if item["kind"] == "professional_record"]
        self.assertEqual(len(records), 105)
        self.assertLess(len(result["data"]["items"]), len(records))

    def test_snapshot_hash_matches_the_saved_redacted_response(self):
        self.payload["credential_token"] = "sentinel-test-value"
        result = self.query()
        self.assertTrue(result["ok"], result)
        snapshot = self.store.evidence[result["data"]["query_evidence_id"]]["data"]
        saved = snapshot["response"]
        self.assertNotEqual(saved["credential_token"], "sentinel-test-value")
        expected = hashlib.sha256(
            json.dumps(saved, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(snapshot["response_sha256"], expected)

    def test_company_aliases_source_times_and_preview_priority(self):
        self.assertEqual(tools._normalized_dataset_category("enterprise_info"), "company_profile")
        self.assertEqual(tools._normalized_dataset_category("enterprise_risk"), "company_risk")
        item = {
            "generic_note": "generic first field", "公司名称": "示例企业", "企业状态": "存续",
            "成立日期": "2011-03-01", "统一社会信用代码": "USCC-TEST",
            "核准日期": "2025-01-02", "有效期起": "2024-01-01", "year": "2024",
        }
        preview = tools._professional_preview(item)
        self.assertIn("/record/公司名称", preview["field_paths"])
        self.assertIn("/record/企业状态", preview["field_paths"])
        source = tools._professional_source(item)
        self.assertEqual(source["record_id"], "USCC-TEST")
        self.assertIsNone(source["published_at"])
        self.assertEqual(source["year"], "2024")
        times = {entry["field"]: entry for entry in source["time_fields"]}
        self.assertEqual(times["核准日期"]["value"], "2025-01-02")
        self.assertEqual(times["有效期起"]["value"], "2024-01-01")

    def test_query_cache_uses_category_and_stripped_query_without_second_call(self):
        first = self.query(query_text="  模型证据  ")
        second = self.query(query_text="模型证据")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ctx["data_usage"]["requests"], 1)
        self.assertEqual(self.ctx["data_usage"]["cache_hits"], 1)
        self.assertEqual(first["evidence_ids"], second["evidence_ids"])

    def test_failed_query_is_reused_without_another_provider_attempt(self):
        self.payload["code"] = 403
        first = self.query(query_text="相同的失败查询")
        second = self.query(query_text="相同的失败查询")
        self.assertEqual(first["error"]["code"], "data_business_error")
        self.assertEqual(second["error"]["code"], "data_business_error")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ctx["data_usage"]["requests"], 1)
        self.assertEqual(self.ctx["data_usage"]["failed"], 1)
        self.assertEqual(self.ctx["data_usage"]["cache_hits"], 1)

    def test_bad_provider_responses_never_create_successful_evidence(self):
        cases = [
            ({**self.payload, "code": 9}, "data_business_error"),
            ({**self.payload, "items": []}, "data_empty"),
            ({**self.payload, "dataset_type": "financial_report"}, "data_wrong_type"),
            ({**self.payload, "items": [{"body": "x" * 1_048_576}]}, "data_response_too_large"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                self.payload = payload
                result = self.query(query_text="唯一查询" + code)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], code)
                self.assertFalse(self.store.evidence)
        self.assertEqual(self.ctx["data_usage"]["failed"], len(cases))
        self.assertEqual(self.ctx["data_usage"]["success"], 0)

    def test_expired_deadline_does_not_call_provider_or_save_evidence(self):
        self.ctx["deadline"] = time.monotonic() - 1
        result = self.query()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "data_timeout")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ctx["data_usage"]["timeouts"], 0)
        self.assertEqual(self.ctx["data_usage"]["failed"], 0)
        self.assertEqual(self.ctx["data_usage"]["blocked"], 1)
        self.assertFalse(self.store.evidence)

    def test_timeout_is_passed_when_client_supports_it(self):
        observed = {}

        def timed_client(query, *, timeout_seconds):
            observed["query"] = query
            observed["timeout_seconds"] = timeout_seconds
            return self.payload

        self.ctx["data_client"] = timed_client
        self.ctx["deadline"] = time.monotonic() + 10
        result = self.query()
        self.assertTrue(result["ok"], result)
        self.assertGreater(observed["timeout_seconds"], 0)
        self.assertLessEqual(observed["timeout_seconds"], 10)

    def test_late_provider_result_after_timeout_is_discarded(self):
        def slow_client(query):
            time.sleep(0.05)
            return self.payload

        self.ctx["data_client"] = slow_client
        self.ctx["deadline"] = time.monotonic() + 0.01
        result = self.query()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "data_timeout")
        time.sleep(0.07)
        self.assertFalse(self.store.evidence)
        self.assertEqual(self.ctx["data_usage"]["requests"], 1)
        self.assertEqual(self.ctx["data_usage"]["timeouts"], 1)

    def test_per_turn_external_request_budget_is_enforced_without_retry(self):
        for index in range(16):
            result = self.query(query_text=f"查询-{index}")
            self.assertTrue(result["ok"], result)
        before = len(self.calls)
        rejected = self.query(query_text="第十七次")
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "data_budget_exceeded")
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.ctx["data_usage"]["requests"], 16)
        self.assertEqual(self.ctx["data_usage"]["failed"], 0)
        self.assertEqual(self.ctx["data_usage"]["blocked"], 1)

    def test_unknown_route_can_support_inference_but_not_fact_claim(self):
        self.payload["dataset_type"] = "future_unmapped"
        result = self.query()
        self.assertTrue(result["ok"], result)
        record_id = result["data"]["items"][0]["evidence_id"]
        fact = self.submit_claim(record_id, "fact")
        self.assertFalse(fact["ok"])
        self.assertEqual(fact["error"]["code"], "unverified_data_route")

        inference = self.submit_claim(record_id, "inference")
        self.assertTrue(inference["ok"], inference)
        data = inference["data"]["recommendation"]
        self.assertEqual(data["kind"], "career_decision")
        self.assertIn("资料类型未核实", "；".join(data["limitations"]))
        claim = data["claims"][0]
        self.assertEqual(claim["origin"], "model_generated")
        self.assertEqual(claim["resolved"][0]["value"], "研究记录0")

    def test_decision_requires_the_matching_successful_query_event(self):
        query = self.query()
        record_id = query["data"]["items"][0]["evidence_id"]
        snapshot_id = query["data"]["query_evidence_id"]
        fake_event = self.store.add_event(
            self.ctx["db_path"], self.ctx["session_id"], self.ctx["turn_id"],
            "search_jobs", "other-call", "success",
        )
        self.store.evidence[snapshot_id]["event_id"] = fake_event
        result = self.submit_claim(record_id, "inference")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "source_event_incomplete")

    def submit_claim(self, evidence_id, kind):
        read = self.run_tool("get_record", {"record_type": "evidence", "record_id": evidence_id,
                                            "field_path": "/record/title"}, call_id="call-read-title")
        self.assertTrue(read["ok"], read)
        return self.run_tool("submit_decision", {
            "title": "继续核实资料",
            "options": ["继续核实", "暂缓决定"],
            "claims": [{
                "kind": kind,
                "text": "当前材料支持继续核验" if kind == "inference" else "研究标题",
                "citations": [{"evidence_id": evidence_id, "field_path": "/record/title"}],
            }],
            "unknowns": ["资料来源仍需人工检查"],
            "next_actions": ["核对原始来源"],
        }, call_id=f"call-submit-{kind}")

    def test_fact_claim_resolves_source_value_and_numeric_text_is_rejected(self):
        query = self.query()
        evidence_id = query["data"]["items"][0]["evidence_id"]
        self.run_tool("get_record", {"record_type": "evidence", "record_id": evidence_id,
                                     "field_path": "/record/abstract"})
        result = self.run_tool("submit_decision", {
            "title": "比较资料选项",
            "options": ["进一步核对", "暂缓决定"],
            "claims": [{
                "kind": "fact", "text": "公开摘要",
                "citations": [{"evidence_id": evidence_id, "field_path": "/record/abstract"}],
            }],
            "unknowns": [], "next_actions": ["检查发布者"],
        })
        self.assertTrue(result["ok"], result)
        claim = result["data"]["recommendation"]["claims"][0]
        self.assertEqual(claim["resolved"][0]["value"], self.payload["items"][0]["abstract"])
        self.assertEqual(claim["origin"], "source_derived")
        self.assertEqual(claim["text"], "引用字段：abstract")
        self.assertNotEqual(claim["generated_label"], claim["text"])

        numeric = self.run_tool("submit_decision", {
            "title": "Option 2 comparison",
            "options": ["优先方案一"],
            "claims": [{
                "kind": "fact", "text": "摘要有2项",
                "citations": [{"evidence_id": evidence_id, "field_path": "/record/abstract"}],
            }],
            "unknowns": [], "next_actions": ["检查来源"],
        }, call_id="call-numeric")
        self.assertFalse(numeric["ok"])
        self.assertEqual(numeric["error"]["code"], "numeric_claim_not_allowed")

    def test_user_statement_cannot_be_labeled_from_a_professional_record(self):
        query = self.query()
        evidence_id = query["data"]["items"][0]["evidence_id"]
        result = self.run_tool("submit_decision", {
            "title": "核验后再决定", "options": ["继续核验"],
            "claims": [{"kind": "user_statement", "text": "用户提供的研究标题",
                        "citations": [{"evidence_id": evidence_id, "field_path": "/record/title"}]}],
            "unknowns": [], "next_actions": ["核对用户情况"],
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "wrong_citation_kind")

    def test_user_statement_display_is_fixed_to_the_cited_user_quote(self):
        query = self.query()
        professional_id = query["data"]["items"][0]["evidence_id"]
        self.run_tool("get_record", {"record_type": "evidence", "record_id": professional_id,
                                     "field_path": "/record/title"})
        user_id = tools._save_evidence(self.ctx, "user_input", {"quote": "我会写Python"})
        result = self.run_tool("submit_decision", {
            "title": "职业方向比较", "options": ["继续核实"],
            "claims": [
                {"kind": "fact", "text": "公司业务必然增长",
                 "citations": [{"evidence_id": professional_id, "field_path": "/record/title"}]},
                {"kind": "user_statement", "text": "用户已经具备商业分析能力",
                 "citations": [{"evidence_id": user_id, "field_path": "/quote"}]},
            ],
            "unknowns": [], "next_actions": ["复核原始信息"],
        })
        self.assertTrue(result["ok"], result)
        claims = result["data"]["recommendation"]["claims"]
        self.assertEqual(claims[0]["text"], "引用字段：title")
        self.assertEqual(claims[0]["generated_label"], "公司业务必然增长")
        self.assertEqual(claims[1]["text"], "用户原话")
        self.assertEqual(claims[1]["resolved"][0]["value"], "我会写Python")
        self.assertNotIn("generated_label", claims[1])

    def test_chinese_numeric_claims_are_rejected_by_shared_validator(self):
        for text in ("利润增长九成", "收益达到百分之九十", "估值增加三倍", "成本为两万元"):
            with self.subTest(text=text):
                self.assertTrue(tools._contains_free_text_number([text]))
        self.assertFalse(tools._contains_free_text_number(["业务方向仍待核实"]))

    def test_long_abstract_and_short_scalar_author_array_are_citable(self):
        abstract = "背景与结论" * 50
        authors = ["Alice", "Bob", "Chen"]
        self.payload["items"] = [{"title": "Paper", "abstract": abstract, "authors": authors}]
        query = self.query(query_text="paper abstract and authors")
        self.assertIn("/record/authors", query["data"]["items"][0]["field_paths"])
        evidence_id = query["data"]["items"][0]["evidence_id"]
        read = self.run_tool("get_record", {"record_type": "evidence", "record_id": evidence_id})
        self.assertTrue(read["ok"], read)
        result = self.run_tool("submit_decision", {
            "title": "核对研究证据", "options": ["继续核验"],
            "claims": [
                {"kind": "fact", "text": "论文发表于2024年",
                 "citations": [{"evidence_id": evidence_id, "field_path": "/record/abstract"}]},
                {"kind": "fact", "text": "作者包括若干研究者",
                 "citations": [{"evidence_id": evidence_id, "field_path": "/record/authors"}]},
            ],
            "unknowns": [], "next_actions": ["核对出版记录"],
        })
        self.assertTrue(result["ok"], result)
        claims = result["data"]["recommendation"]["claims"]
        self.assertEqual(claims[0]["resolved"][0]["value"], abstract)
        self.assertEqual(claims[1]["resolved"][0]["value"], authors)

    def test_scalar_author_array_over_600_bytes_is_rejected(self):
        self.payload["items"] = [{"title": "Paper", "authors": ["x" * 700]}]
        query = self.query(query_text="oversized authors")
        evidence_id = query["data"]["items"][0]["evidence_id"]
        result = self.run_tool("submit_decision", {
            "title": "核对作者", "options": ["继续核验"],
            "claims": [{"kind": "fact", "text": "作者字段",
                        "citations": [{"evidence_id": evidence_id, "field_path": "/record/authors"}]}],
            "unknowns": [], "next_actions": ["核对作者列表"],
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "citation_too_broad")

    def test_query_index_does_not_authorize_unread_field_citations(self):
        query = self.query()
        evidence_id = query["data"]["items"][0]["evidence_id"]
        args = {"title": "逐条核对字段", "options": ["继续核实"],
                "claims": [{"kind": "fact", "text": "摘要",
                            "citations": [{"evidence_id": evidence_id, "field_path": "/record/abstract"}]}],
                "unknowns": [], "next_actions": ["核查资料原文"]}
        # 即使 ID 合法、字段存在，没通过 get_record 读取也不能直接引用。
        rejected = self.run_tool("submit_decision", args)
        self.assertEqual(rejected["error"]["code"], "field_not_read")
        self.run_tool("get_record", {"record_type": "evidence", "record_id": evidence_id,
                                     "field_path": "/record/title"})
        still_rejected = self.run_tool("submit_decision", args)
        self.assertEqual(still_rejected["error"]["code"], "field_not_read")
        self.run_tool("get_record", {"record_type": "evidence", "record_id": evidence_id,
                                     "field_path": "/record/abstract"})
        accepted = self.run_tool("submit_decision", args)
        self.assertTrue(accepted["ok"], accepted)

    def test_large_record_preview_is_not_treated_as_complete_read(self):
        self.payload["items"] = [{"title": "记录标题", "summary": "x" * 7000}]
        query = self.query()
        evidence_id = query["data"]["items"][0]["evidence_id"]
        preview = self.run_tool("get_record", {"record_type": "evidence", "record_id": evidence_id})
        self.assertTrue(preview["ok"], preview)
        self.assertNotIn("data", preview["data"]["record"])
        self.assertFalse(tools._professional_field_was_read(self.ctx, evidence_id, "/record/title"))
        self.run_tool("get_record", {"record_type": "evidence", "record_id": evidence_id,
                                     "field_path": "/record/title"})
        self.assertTrue(tools._professional_field_was_read(self.ctx, evidence_id, "/record/title"))

    def test_snapshot_read_hides_raw_and_can_index_an_unlisted_candidate(self):
        query = self.query()
        snapshot_id = query["data"]["query_evidence_id"]
        metadata = self.run_tool("get_record", {"record_type": "evidence", "record_id": snapshot_id})
        self.assertTrue(metadata["ok"], metadata)
        self.assertNotIn("response", metadata["data"]["record"])
        self.assertNotIn("公开摘要内容", json.dumps(metadata, ensure_ascii=False))

        selected = self.run_tool("get_record", {
            "record_type": "evidence", "record_id": snapshot_id,
            "field_path": "/response/items/11",
        }, call_id="call-index-more")
        self.assertTrue(selected["ok"], selected)
        record = selected["data"]["record"]
        self.assertEqual(record["kind"], "professional_record")
        self.assertIn(record["id"], self.ctx["allowed_evidence_ids"])
        self.assertIn(record["id"], selected["evidence_ids"])

    def test_get_record_caps_the_complete_professional_response(self):
        self.payload["items"] = [{"title": "long", "body": "x" * 5_900}]
        query = self.query(query_text="large selected field")
        record_id = query["data"]["items"][0]["evidence_id"]
        preview = self.run_tool("get_record", {"record_type": "evidence", "record_id": record_id})
        self.assertTrue(preview["ok"], preview)
        envelope = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(envelope), 6_000)
        selected = self.run_tool("get_record", {
            "record_type": "evidence", "record_id": record_id, "field_path": "/record/body",
        }, call_id="call-large-field")
        self.assertFalse(selected["ok"])
        self.assertEqual(selected["error"]["code"], "field_too_large")

    def test_condition_supersession_links_and_preserves_old_record(self):
        first = self.run_tool("record_condition", {
            "source_message_id": "msg-a", "field": "企业意向", "quote": "考虑一家企业",
            "status": "自述",
        })
        self.assertTrue(first["ok"], first)
        old_id = first["evidence_ids"][0]
        second = self.run_tool("record_condition", {
            "source_message_id": "msg-a", "field": "企业意向", "quote": "考虑一家企业",
            "status": "待确认", "supersedes_evidence_id": old_id,
        }, call_id="call-condition-update")
        self.assertTrue(second["ok"], second)
        self.assertEqual(self.store.evidence[old_id]["data"]["status"], "自述")
        new = self.store.evidence[second["evidence_ids"][0]]["data"]
        self.assertEqual(new["supersedes_evidence_id"], old_id)

    def test_unknown_tool_schema_is_visible_and_registers_explicit_categories(self):
        definitions = {item["function"]["name"]: item for item in tools.tool_definitions()}
        professional = definitions["query_professional_data"]["function"]["parameters"]["properties"]
        self.assertEqual(professional["category"]["enum"], [
            "company_profile", "company_risk", "industry", "research", "public_financials",
        ])
        self.assertIn("submit_decision", definitions)
        self.assertIn("academic_search", tools._DATASET_TYPE_ALIASES)


if __name__ == "__main__":
    unittest.main()
