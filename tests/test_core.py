"""验证协议、持久化与错误路径；不用真Key，不需要联网。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sanjing import context, model, store
from sanjing.agent import run_turn
from sanjing.config import MAX_STEPS


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "agent.sqlite3"
        self.session = store.ensure_session(self.db)

    def test_session_isolation(self):
        turn = store.start_turn(self.db, self.session)
        ev = store.add_evidence(self.db, self.session, turn, "user_input", {"quote": "合成案例"})
        other = store.ensure_session(self.db)
        self.assertIsNone(store.get_evidence(self.db, other, ev))
        rec = store.add_recommendation(self.db, self.session, turn, {"title": "示例"})
        self.assertIsNone(store.get_recommendation(self.db, other, rec))
        with self.assertRaises(ValueError):
            store.ensure_session(self.db, "misspelled")

    def test_secret_redaction(self):
        secret = "sk-TEST_SECRET_MUST_NOT_APPEAR_12345"
        turn = store.start_turn(self.db, self.session)
        store.add_message(self.db, self.session, turn, "user", secret)
        store.add_evidence(self.db, self.session, turn, "condition", {"nested": [secret], "api_key": "anything"})
        record = store.snapshot(self.db, self.session, turn)
        self.assertNotIn(secret, json.dumps(record))
        self.assertNotIn(secret.encode(), self.db.read_bytes())

    def test_resume_and_complete_turn_budget(self):
        old = store.start_turn(self.db, self.session)
        store.add_message(self.db, self.session, old, "user", "以前的问题" * 80)
        store.add_message(self.db, self.session, old, "assistant", "以前的回复" * 80)
        store.finish_turn(self.db, self.session, old, "completed", "以前的回复")
        turn = store.start_turn(self.db, self.session)
        msg = store.add_message(self.db, self.session, turn, "user", "现在的问题")
        base = context.build_messages(self.db, self.session, turn, msg)
        tight_budget = context.char_size([base[0], base[-1]]) + 100
        messages = context.build_messages(self.db, self.session, turn, msg, budget=tight_budget)
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertEqual(store.ensure_session(self.db, self.session), self.session)
        self.assertEqual(len(store.list_messages(self.db, self.session)), 3)

    def test_parse_rejects_truncated_response(self):
        raw = {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "部分"}}]}
        with self.assertRaises(model.ModelError) as error:
            model.parse_response(raw)
        self.assertEqual(error.exception.code, "RESPONSE_TRUNCATED")

    def test_reasoning_is_not_exposed(self):
        raw = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "正文", "reasoning_content": "内部不展示"}}]}
        self.assertNotIn("reasoning_content", model.parse_response(raw)["message"])

    def test_plain_recommendation_is_not_published(self):
        def fake(messages, tools):
            return {"message": {"role": "assistant", "content": "你非常适合用户研究，录用概率九成。"}, "usage": {}}
        result = run_turn(self.session, "比较方向", db_path=self.db, client=fake)
        self.assertEqual(result["error_code"], "UNVERIFIED_OUTPUT")
        self.assertNotIn("九成", result["answer"])
        self.assertTrue(Path(result["ledger_path"]).exists())

    def test_timeout_error_is_safe(self):
        def fake(messages, tools):
            raise model.ModelError("MODEL_TIMEOUT")
        result = run_turn(self.session, "合成案例", db_path=self.db, client=fake)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["error_code"], "MODEL_TIMEOUT")
        self.assertEqual(result["usage"]["requests"], 1)

    def test_content_and_multiple_calls_pair_correctly(self):
        responses = []
        for index in range(MAX_STEPS):
            responses.append({"message": {"role": "assistant", "content": "这不是最终答案", "tool_calls": [
                {"id": f"call_{index}_a", "type": "function", "function": {"name": "run_shell", "arguments": '{}'}},
                {"id": f"call_{index}_b", "type": "function", "function": {"name": "read_key", "arguments": '{}'}}]}, "usage": {}})
        def fake(messages, tools):
            if len(responses) < MAX_STEPS:
                self.assertEqual(messages[-1]["role"], "tool")
                self.assertEqual(messages[-2]["role"], "tool")
                self.assertEqual(messages[-3]["role"], "assistant")
                self.assertEqual(messages[-3]["tool_calls"][0]["id"], messages[-2]["tool_call_id"])
                self.assertEqual(messages[-3]["tool_calls"][1]["id"], messages[-1]["tool_call_id"])
            return responses.pop(0)
        result = run_turn(self.session, "合成拦截案例", db_path=self.db, client=fake)
        self.assertEqual(result["error_code"], "STEP_LIMIT")
        snap = store.snapshot(self.db, self.session, result["turn_id"])
        self.assertEqual(len(snap["events"]), MAX_STEPS * 2)
        self.assertFalse(store.list_recommendations(self.db, self.session))

    def test_duplicate_call_id_stops_before_execution(self):
        call = {"id": "duplicate", "type": "function", "function": {"name": "run_shell", "arguments": '{}'}}
        def fake(messages, tools):
            return {"message": {"role": "assistant", "tool_calls": [call, call]}, "usage": {}}
        result = run_turn(self.session, "重复调用测试", db_path=self.db, client=fake)
        self.assertEqual(result["error_code"], "BAD_TOOL_PROTOCOL")
        self.assertFalse(store.snapshot(self.db, self.session, result["turn_id"])["events"])

    def test_client_never_prints_http_body_or_retries(self):
        import urllib.error
        key_file = Path(self.temp.name) / "key.txt"
        key_file.write_text("sk-TEST_KEY_1234567890", encoding="utf-8")
        error = urllib.error.HTTPError("test", 401, "SECRET_BODY", {}, None)
        with patch("urllib.request.urlopen", side_effect=error) as request:
            with self.assertRaises(model.ModelError) as caught:
                model.ask_llm([], [], key_file=key_file)
        self.assertEqual(str(caught.exception), "HTTP_401")
        self.assertEqual(request.call_count, 1)

    def test_total_request_deadline_discards_late_response(self):
        import time
        key_file = Path(self.temp.name) / "key.txt"
        key_file.write_text("sk-TEST_KEY_1234567890", encoding="utf-8")
        def slow_request(*args, **kwargs):
            time.sleep(0.1)
            raise TimeoutError("late")
        with patch("urllib.request.urlopen", side_effect=slow_request), patch("sanjing.model.TIMEOUT_SECONDS", 0.02):
            started = time.monotonic()
            with self.assertRaises(model.ModelError) as caught:
                model.ask_llm([], [], key_file=key_file)
            self.assertLess(time.monotonic() - started, 0.08)
            self.assertEqual(caught.exception.code, "MODEL_TIMEOUT")
        time.sleep(0.11)  # 让模拟HTTP结束，避免测试替身线程影响后续测试。


if __name__ == "__main__":
    unittest.main()
