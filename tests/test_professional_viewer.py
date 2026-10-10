"""professional evidence and career decision rendering stays selective and escaped."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sanjing.viewer import write_snapshot


class ProfessionalViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name) / "ledger"
        self.session_id = "session-professional-demo"
        self.turn_id = str(uuid.uuid4())

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn": {
                "id": self.turn_id,
                "status": "completed",
                "answer": "比较岗位和企业背景。",
                "error_code": None,
                "data_usage": {"input_tokens": 120, "output_tokens": 48, "total_tokens": 168},
            },
            "recommendations": [{
                "id": "rec-career-1",
                "turn_id": self.turn_id,
                "data": {
                    "kind": "career_decision",
                    "title": "比较两个职业选择",
                    "options": [
                        {"title": "用户研究", "summary": "访谈与整理发现"},
                        {"title": "产品助理", "summary": "协助需求与交付"},
                    ],
                    "claims": [
                        {
                            "kind": "fact",
                            "origin": "source_derived",
                            "text": "企业状态字段显示为存续。",
                            "citations": [{"evidence_id": "ev-record", "field_path": "/record/status"}],
                            "resolved": [{"evidence_id": "ev-record", "field_path": "/record/status", "value": "存续"}],
                        },
                        {
                            "kind": "fact",
                            "origin": "source_derived",
                            "text": "登记资本字段记录为一百万元。",
                            "citations": [{"evidence_id": "ev-record", "field_path": "/record/registered_capital"}],
                            "resolved": [{"evidence_id": "ev-record", "field_path": "/record/registered_capital",
                                          "value": 100, "unit": "万元"}],
                        },
                        {
                            "kind": "user_statement",
                            "origin": "user_statement",
                            "text": "我希望留在北京。",
                            "citations": [{"evidence_id": "ev-user", "field_path": "/quote"}],
                            "resolved": [{"evidence_id": "ev-user", "field_path": "/quote", "value": "我希望留在北京"}],
                        },
                        {
                            "kind": "inference",
                            "origin": "model_generated",
                            "text": "两项选择都需要进一步核验实际招聘状态。",
                            "citations": [{"evidence_id": "ev-query", "field_path": "/query"}],
                            "resolved": [],
                        },
                    ],
                    "unknowns": ["实时招聘状态未核验"],
                    "next_actions": ["核对当前岗位描述和招聘状态"],
                    "evidence_ids": ["ev-record"],
                    "candidate_evidence_ids": ["ev-query", "ev-bad-link"],
                    "limitations": ["工商登记状态不代表岗位质量。"],
                },
            }],
            "evidence": [
                {
                    "id": "ev-query", "kind": "professional_query", "turn_id": self.turn_id,
                    "data": {
                        "category": "企业工商与风险", "dataset_type": "company_registry",
                        "query": "查询演示企业登记状态", "trace_id": "trace-17",
                        "route_verified": True, "fetched_at": "2026-10-10T04:00:00Z",
                        "response_sha256": "abcdef0123456789abcdef0123456789",
                        "candidate_indexes": [0, 2],
                        "payload": {"private_payload_marker": "RAW_PAYLOAD_DO_NOT_RENDER"},
                        "snapshot": {"private_snapshot_marker": "RAW_SNAPSHOT_DO_NOT_RENDER"},
                        "response": {"private_response_marker": "RAW_RESPONSE_DO_NOT_RENDER"},
                        "headers": {"Authorization": "Bearer HEADER_SECRET_DO_NOT_RENDER"},
                        "limitations": ["仅用于演示"],
                    },
                },
                {
                    "id": "ev-record", "kind": "professional_record", "turn_id": self.turn_id,
                    "data": {
                        "category": "企业工商与风险", "dataset_type": "company_registry",
                        "query": "查询演示企业登记状态", "trace_id": "trace-17",
                        "fetched_at": "2026-10-10T04:00:00Z", "route_verified": True,
                        "record": {
                            "company_name": "<img src=x onerror=alert(1)>",
                            "status": "存续", "registered_capital": "100万元",
                            "raw_detail": "RAW_RECORD_DO_NOT_RENDER",
                            "api_key": "RECORD_SECRET_DO_NOT_RENDER",
                        },
                        "selected_fields": ["company_name", "status", "registered_capital"],
                        "source": {
                            "url": "https://example.org/company/123", "provider": "演示来源",
                            "record_id": "company-123", "published_at": "2026-09-01",
                            "missing_fields": ["招聘状态"],
                        },
                        "query_evidence_id": "ev-query",
                        "dependency_evidence_ids": ["snapshot-abc"],
                        "limitations": ["登记状态不代表经营质量。"],
                    },
                },
                {
                    "id": "ev-user", "kind": "user_condition", "turn_id": self.turn_id,
                    "data": {"field": "city", "quote": "我希望留在北京。", "status": "自述"},
                },
                {
                    "id": "ev-bad-link", "kind": "professional_record", "turn_id": self.turn_id,
                    "data": {
                        "category": "演示", "record": {"name": "示例记录"},
                        "source": {"url": "https://example.org/record?api_key=URL_SECRET_DO_NOT_RENDER"},
                    },
                },
            ],
            "events": [{
                "id": "event-1", "tool_name": "professional_query", "call_id": "call-1",
                "status": "error", "error_code": "TIMEOUT", "duration_ms": 3000,
                "headers": {"Authorization": "Bearer EVENT_HEADER_SECRET_DO_NOT_RENDER"},
            }],
            "messages": [{"id": "m1", "role": "user", "content": "我希望留在北京。", "turn_id": self.turn_id}],
        }

    def render(self, data: dict | None = None) -> str:
        snapshot = data if data is not None else self.snapshot()
        with patch("sanjing.viewer.store.snapshot", return_value=snapshot):
            path = write_snapshot("demo.db", self.session_id, self.turn_id, self.output_dir)
        return path.read_text(encoding="utf-8")

    def test_professional_query_shows_metadata_and_suppresses_raw_response(self) -> None:
        page = self.render()
        for expected in (
            "企业工商与风险", "company_registry", "trace-17",
            "abcdef0123456789abcdef0123456789", "候选索引", "0", "2",
        ):
            self.assertIn(expected, page)
        for secret in (
            "RAW_PAYLOAD_DO_NOT_RENDER", "RAW_SNAPSHOT_DO_NOT_RENDER", "RAW_RESPONSE_DO_NOT_RENDER",
            "HEADER_SECRET_DO_NOT_RENDER", "Authorization", "RECORD_SECRET_DO_NOT_RENDER",
            "RAW_RECORD_DO_NOT_RENDER", "EVENT_HEADER_SECRET_DO_NOT_RENDER",
        ):
            self.assertNotIn(secret, page)

    def test_career_claims_label_origin_and_show_program_resolved_values(self) -> None:
        page = self.render()
        self.assertIn("来源字段事实（由程序解析）", page)
        self.assertIn("程序解析值：", page)
        self.assertIn("存续", page)
        self.assertIn("用户自述", page)
        self.assertIn("我希望留在北京", page)
        self.assertIn("模型生成推断", page)
        self.assertIn("没有与此引用对应的程序解析值", page)
        self.assertIn("采用的依据", page)
        self.assertIn("本轮候选依据", page)
        self.assertIn("ev-record", page)
        self.assertIn("ev-query", page)

    def test_record_source_provenance_time_unit_limitations_and_safe_fields(self) -> None:
        page = self.render()
        self.assertIn("演示来源", page)
        self.assertIn("company-123", page)
        self.assertIn("2026-09-01", page)
        self.assertIn("2026-10-10T04:00:00Z", page)
        self.assertIn("登记状态不代表经营质量", page)
        self.assertIn("100万元", page)
        self.assertIn("展开可读源字段", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn("<img src=x onerror=alert(1)>", page)

    def test_only_valid_non_secret_http_links_are_rendered(self) -> None:
        page = self.render()
        self.assertIn('href="https://example.org/company/123"', page)
        self.assertNotIn('href="javascript:', page)
        self.assertNotIn("URL_SECRET_DO_NOT_RENDER", page)
        self.assertNotIn("api_key=", page)
        self.assertIn("来源链接已隐藏", page)

    def test_query_metadata_or_orphaned_values_cannot_be_promoted_to_facts(self) -> None:
        data = self.snapshot()
        data["recommendations"][0]["data"] = {
            "kind": "career_decision", "title": "待核验事实",
            "claims": [{
                "kind": "fact", "text": "这是一条未经来源记录支持的事实。",
                "citations": [{"evidence_id": "ev-query", "field_path": "/response/result"}],
                "resolved": [{"evidence_id": "ev-query", "field_path": "/response/result",
                              "value": "ORPHANED_FACT_VALUE"}],
            }],
        }
        data["evidence"] = [next(item for item in data["evidence"] if item["id"] == "ev-query")]
        page = self.render(data)
        self.assertIn("事实声明待核验", page)
        self.assertNotIn("来源字段事实（由程序解析）", page)
        self.assertNotIn("ORPHANED_FACT_VALUE", page)
        self.assertIn("查询元数据不是源记录", page)

    def test_timeout_events_and_usage_show_cost_uncertainty(self) -> None:
        page = self.render()
        self.assertIn("展开查看工具执行记录", page)
        self.assertIn("输入 token 数", page)
        self.assertIn("168", page)
        self.assertIn("该次请求是否产生费用目前未知", page)
        self.assertIn("超时不代表请求已取消", page)

    def test_optional_empty_schema_does_not_fail(self) -> None:
        data = self.snapshot()
        data["recommendations"][0]["data"] = {
            "kind": "career_decision", "title": "只有标题的旧记录", "options": None,
            "claims": None, "evidence_ids": [], "unknowns": None,
        }
        data["evidence"] = [{
            "id": "ev-old", "kind": "job_profile", "turn_id": self.turn_id,
            "data": {"job": {"title": "旧演示岗位", "summary": "只用已有字段渲染。"}},
        }]
        page = self.render(data)
        self.assertIn("只有标题的旧记录", page)
        self.assertIn("没有结构化结论", page)

    def test_macro_citation_keeps_unit_period_and_model_usage(self) -> None:
        data = self.snapshot()
        record = next(row for row in data["evidence"] if row["id"] == "ev-record")
        record["data"]["record"] = {"value": 5.9, "unit_raw": "%", "time_raw": "2025-12-31"}
        record["data"]["source"].update(unit="%", statistical_period="2025-12-31", source_system="INDUSTRY_CHAIN")
        claim = data["recommendations"][0]["data"]["claims"][0]
        claim["citations"] = [{"evidence_id": "ev-record", "field_path": "/record/value"}]
        claim["resolved"] = [{"evidence_id": "ev-record", "field_path": "/record/value", "value": 5.9}]
        data["turn"].pop("data_usage", None)
        data["turn"]["usage"] = {"requests": 3, "prompt_tokens": 271, "completion_tokens": 49,
                                  "total_tokens": 320, "data_requests": {"requests": 1, "success": 1}}
        page = self.render(data)
        self.assertIn("统计期（原字段）", page)
        self.assertIn("2025-12-31", page)
        self.assertIn("INDUSTRY_CHAIN", page)
        self.assertIn("单位（原字段）", page)
        self.assertIn("320", page)
        self.assertIn("271", page)
        self.assertIn("专业查询客户端尝试次数", page)
        self.assertIn("返回有效候选次数", page)


if __name__ == "__main__":
    unittest.main()
