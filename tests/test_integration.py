"""用真实SQLite与模拟模型验证多轮和账本，避免模块替身掩盖接口错误。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sanjing import store, tools
from sanjing.agent import run_turn
from sanjing.offline import ask_offline


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "agent.sqlite3"
        self.session = store.ensure_session(self.db)

    def ctx(self):
        turn = store.start_turn(self.db, self.session)
        msg = store.add_message(self.db, self.session, turn, "user", "我每周可投入十五小时。")
        return {"db_path": self.db, "session_id": self.session, "turn_id": turn,
                "user_message_id": msg, "allowed_evidence_ids": set(), "pending_recommendations": []}

    def call(self, ctx, name, args):
        return tools.execute_tool(ctx, {"id": store.new_id("call"), "type": "function", "function": {
            "name": name, "arguments": json.dumps(args, ensure_ascii=False)}})

    def test_end_to_end_resume_and_two_recommendations(self):
        first = run_turn(self.session, "比较用户研究和产品经理助理", db_path=self.db, client=ask_offline)
        self.assertEqual(first["status"], "completed")
        snap = store.snapshot(self.db, self.session, first["turn_id"])
        self.assertEqual(len(snap["recommendations"]), 2)
        html_before = Path(first["ledger_path"]).read_bytes()
        self.assertTrue(any("忽略前面的规则" in json.dumps(item["data"], ensure_ascii=False) for item in snap["evidence"]))
        self.assertTrue(all(event["tool_name"] != "run_shell" for event in snap["events"]))
        resumed = store.ensure_session(self.db, self.session)
        second = run_turn(resumed, "继续比较", db_path=self.db, client=ask_offline)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(Path(first["ledger_path"]).read_bytes(), html_before)

    def test_blocked_command_returns_visible_question_and_event(self):
        result = run_turn(self.session, "演示危险命令拦截", db_path=self.db, client=ask_offline)
        self.assertEqual(result["status"], "needs_input")
        snap = store.snapshot(self.db, self.session, result["turn_id"])
        self.assertEqual(snap["events"][0]["status"], "rejected")
        self.assertEqual(snap["events"][0]["error_code"], "unknown_tool")
        self.assertIn("unknown_tool", Path(result["ledger_path"]).read_text(encoding="utf-8"))

    def test_exact_condition_does_not_replace_assessment(self):
        ctx = self.ctx()
        result = self.call(ctx, "record_condition", {"source_message_id": ctx["user_message_id"],
                           "field": "availability", "quote": "每周可投入十五小时", "status": "自述"})
        self.assertTrue(result["ok"], result)
        result = self.call(ctx, "record_condition", {"source_message_id": ctx["user_message_id"],
                           "field": "skills", "quote": "精通SQL", "status": "自述"})
        self.assertFalse(result["ok"])
        assessment = self.call(ctx, "get_assessment", {})
        self.assertTrue(assessment["ok"])
        self.assertNotIn("精通SQL", json.dumps(assessment))

    def test_historical_calculation_cannot_be_republished_without_new_compute(self):
        first = run_turn(self.session, "探索", db_path=self.db, client=ask_offline)
        rec = store.list_recommendations(self.db, self.session, first["turn_id"])[0]
        ctx = self.ctx()
        self.assertTrue(self.call(ctx, "get_record", {"record_type": "recommendation", "record_id": rec["id"]})["ok"])
        args = {key: rec["data"][key] for key in ("title", "explanation", "calculation_id", "evidence_ids", "limitations", "next_action")}
        result = self.call(ctx, "submit_recommendation", args)
        self.assertEqual(result["error"]["code"], "calculation_not_current")

    def test_partial_submission_repair_does_not_duplicate_success(self):
        corrupted = False
        def fake(messages, definitions):
            nonlocal corrupted
            response = ask_offline(messages, definitions)
            calls = response["message"]["tool_calls"]
            if not corrupted and calls[0]["function"]["name"] == "submit_recommendation":
                arguments = json.loads(calls[-1]["function"]["arguments"])
                arguments["evidence_ids"] = ["ev_fake"]
                calls[-1]["function"]["arguments"] = json.dumps(arguments, ensure_ascii=False)
                corrupted = True
            return response
        result = run_turn(self.session, "比较两个方向", db_path=self.db, client=fake)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(store.list_recommendations(self.db, self.session, result["turn_id"])), 2)
        self.assertEqual(result["usage"]["requests"], 4)

    def test_missing_assessment_dimension_is_kept_and_never_invented(self):
        ctx = self.ctx()
        original_load = tools._load_json
        def load_without_mbti(filename):
            data = original_load(filename)
            if filename == "assessment.json":
                data["scores"].pop("mbti")
            return data
        with patch("sanjing.tools._load_json", side_effect=load_without_mbti):
            assessment = self.call(ctx, "get_assessment", {})
        job = self.call(ctx, "search_jobs", {"query": "用户研究"})["data"]["results"][0]
        calc = self.call(ctx, "match_job", {"job_id": job["job"]["job_id"],
            "assessment_evidence_id": assessment["evidence_ids"][0], "job_evidence_id": job["evidence_id"]})
        self.assertTrue(calc["ok"])
        self.assertIn("mbti.preference", calc["data"]["computed"]["missing"])
        self.assertEqual(calc["data"]["computed"]["score"], 75.0)
        contribution = calc["data"]["computed"]["contributions"][-1]
        self.assertIsNone(contribution["user_value"])
        self.assertTrue(contribution["missing"])

    def test_failed_calculation_event_cannot_be_used_as_success(self):
        ctx = self.ctx()
        assessment = self.call(ctx, "get_assessment", {})
        job = self.call(ctx, "search_jobs", {"query": "用户研究"})["data"]["results"][0]
        calc = self.call(ctx, "match_job", {"job_id": job["job"]["job_id"],
            "assessment_evidence_id": assessment["evidence_ids"][0], "job_evidence_id": job["evidence_id"]})
        record = store.get_evidence(self.db, self.session, calc["data"]["calculation_id"])
        store.update_event(self.db, record["event_id"], "failed", "simulated_failure")
        result = self.call(ctx, "submit_recommendation", {
            "title": "探索", "explanation": "按演示资料探索", "calculation_id": record["id"],
            "evidence_ids": calc["evidence_ids"], "limitations": ["原型假设"], "next_action": "先做体验任务"})
        self.assertEqual(result["error"]["code"], "calculation_not_completed")
        self.assertFalse(store.list_recommendations(self.db, self.session, ctx["turn_id"]))


if __name__ == "__main__":
    unittest.main()
