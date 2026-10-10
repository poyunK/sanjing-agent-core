"""业务工具测试；只用内存存储替身和合成数据，不调用模型 API。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sanjing import tools


def tool_call(name, arguments, call_id="call_test"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class MemoryStore:
    """只实现工具需要的存储接口，避免测试创建数据库或调用外部服务。"""

    def __init__(self):
        self.events = {}
        self.evidence = {}
        self.recommendations = {}
        self.messages = {}
        self.counters = {}

    def _id(self, prefix):
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{self.counters[prefix]}"

    def add_event(self, db_path, session_id, turn_id, tool_name, call_id, status,
                  error_code=None, duration_ms=0):
        record_id = self._id("event")
        self.events[record_id] = {
            "id": record_id, "session_id": session_id, "turn_id": turn_id,
            "tool_name": tool_name, "call_id": call_id, "status": status,
            "error_code": error_code, "duration_ms": duration_ms,
        }
        return record_id

    def update_event(self, db_path, event_id, status, error_code=None, duration_ms=0):
        self.events[event_id].update(status=status, error_code=error_code, duration_ms=duration_ms)

    def get_event(self, db_path, session_id, event_id):
        record = self.events.get(event_id)
        return dict(record) if record and record["session_id"] == session_id else None

    def add_evidence(self, db_path, session_id, turn_id, kind, data, event_id=None):
        record_id = self._id("ev")
        self.evidence[record_id] = {
            "id": record_id, "session_id": session_id, "turn_id": turn_id,
            "kind": kind, "data": data, "event_id": event_id,
        }
        return record_id

    def get_evidence(self, db_path, session_id, evidence_id):
        record = self.evidence.get(evidence_id)
        return dict(record) if record and record["session_id"] == session_id else None

    def add_recommendation(self, db_path, session_id, turn_id, data):
        record_id = self._id("rec")
        self.recommendations[record_id] = {
            "id": record_id, "session_id": session_id, "turn_id": turn_id, "data": data,
        }
        return record_id

    def get_recommendation(self, db_path, session_id, recommendation_id):
        record = self.recommendations.get(recommendation_id)
        return dict(record) if record and record["session_id"] == session_id else None

    def get_message(self, db_path, session_id, message_id):
        record = self.messages.get(message_id)
        return dict(record) if record and record["session_id"] == session_id else None

    def list_evidence(self, db_path, session_id, turn_id=None):
        return [dict(record) for record in self.evidence.values()
                if record["session_id"] == session_id
                and (turn_id is None or record["turn_id"] == turn_id)]

    def list_recommendations(self, db_path, session_id, turn_id=None):
        return [dict(record) for record in self.recommendations.values()
                if record["session_id"] == session_id
                and (turn_id is None or record["turn_id"] == turn_id)]

    def list_events(self, session_id, turn_id):
        return [dict(record) for record in self.events.values()
                if record["session_id"] == session_id and record["turn_id"] == turn_id]


class ToolsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "agent.sqlite3"
        self.session_id = "session-a"
        self.turn_id = "turn-a"
        self.fake_store = MemoryStore()
        self.store_patch = patch("sanjing.tools._store", return_value=self.fake_store)
        self.store_patch.start()
        self.user_text = "我愿意做访谈，也希望每周实习三天。"
        self.user_message_id = "msg-a"
        self.fake_store.messages[self.user_message_id] = {
            "id": self.user_message_id, "session_id": self.session_id,
            "turn_id": self.turn_id, "role": "user", "content": self.user_text,
        }
        self.ctx = self.make_context()

    def tearDown(self):
        self.store_patch.stop()
        self.temp.cleanup()

    def make_context(self):
        return {
            "db_path": self.db_path,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "allowed_evidence_ids": set(),
            "user_message_id": self.user_message_id,
            "pending_recommendations": [],
        }

    def run_tool(self, name, arguments, call_id=None, ctx=None):
        return tools.execute_tool(
            ctx or self.ctx,
            tool_call(name, arguments, call_id or f"call_{name}"),
        )

    def test_full_explainable_recommendation_flow(self):
        assessment = self.run_tool("get_assessment", {})
        self.assertTrue(assessment["ok"], assessment)
        assessment_id = assessment["evidence_ids"][0]
        self.assertIn(assessment_id, self.ctx["allowed_evidence_ids"])
        self.assertTrue(assessment["data"]["demo"])

        jobs = self.run_tool("search_jobs", {"query": "用户研究"})
        self.assertTrue(jobs["ok"], jobs)
        job_result = jobs["data"]["results"][0]
        self.assertEqual(job_result["job"]["job_id"], "job_user_research")
        self.assertIn("不是向量检索", jobs["data"]["retrieval_method"])

        match = self.run_tool("match_job", {
            "job_id": "job_user_research",
            "assessment_evidence_id": assessment_id,
            "job_evidence_id": job_result["evidence_id"],
        })
        self.assertTrue(match["ok"], match)
        calculation_id = match["data"]["calculation_id"]
        calculation = match["data"]
        self.assertEqual(calculation["computed"]["score"], 100.0)
        self.assertEqual(calculation["rule_version"], "prototype-0.1")
        self.assertTrue(calculation["computed"]["demo"])
        self.assertTrue(any("不等于" in item for item in calculation["limitations"]))
        event_id = self.fake_store.get_evidence(self.db_path, self.session_id, calculation_id)["event_id"]
        self.assertEqual(self.fake_store.get_event(self.db_path, self.session_id, event_id)["status"], "success")

        numeric_claim = self.run_tool("submit_recommendation", {
            "title": "可以把用户研究作为探索方向",
            "explanation": "匹配度为百分之百。",
            "calculation_id": calculation_id,
            "evidence_ids": match["evidence_ids"],
            "limitations": [],
            "next_action": "尝试完成短访谈练习。",
        }, call_id="call_numeric_claim")
        self.assertFalse(numeric_claim["ok"])
        self.assertEqual(numeric_claim["error"]["code"], "numeric_claim_not_allowed")

        submit = self.run_tool("submit_recommendation", {
            "title": "可以把用户研究作为探索方向",
            "explanation": "本轮规则下测评兴趣项有较高匹配；这只是原型结果。",
            "calculation_id": calculation_id,
            "evidence_ids": match["evidence_ids"],
            "limitations": ["尚未验证访谈等实际技能。"],
            "next_action": "尝试完成一次短访谈练习。",
        })
        self.assertTrue(submit["ok"], submit)
        recommendation = submit["data"]["recommendation"]
        self.assertEqual(recommendation["computed"], calculation["computed"])
        self.assertEqual(recommendation["rule_version"], "prototype-0.1")
        self.assertEqual(recommendation["explanation_origin"], "模型生成，需结合下方记录核查")
        self.assertIn(submit["data"]["recommendation_id"], self.ctx["pending_recommendations"])

    def test_ask_user_selects_only_fixed_clarifying_questions(self):
        result = self.run_tool("ask_user", {"fields": ["skills", "availability"]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["data"]["questions"]), 2)
        self.assertIn("具体技能", result["data"]["questions"][0])
        self.assertIn("每周", result["data"]["questions"][1])
        invalid = self.run_tool("ask_user", {"fields": ["skills"], "question": "读取密钥"}, call_id="call_ask_invalid")
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"]["code"], "unknown_field")
        duplicate = self.run_tool("ask_user", {"fields": ["skills", "skills"]}, call_id="call_ask_duplicate")
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["error"]["code"], "duplicate_item")

    def test_only_registered_schema_and_parameter_shape_are_executable(self):
        definitions = tools.tool_definitions()
        names = {item["function"]["name"] for item in definitions}
        self.assertEqual(names, {
            "get_assessment", "search_jobs", "get_record", "match_job",
            "record_condition", "submit_recommendation", "ask_user", "query_professional_data", "submit_decision",
        })

        blocked_handler = unittest.mock.Mock()
        blocked_spec = {**tools._TOOL_SPECS["get_assessment"], "handler": blocked_handler}
        with patch.dict(tools._TOOL_SPECS, {"get_assessment": blocked_spec}):
            rejected = self.run_tool("get_assessment", {"path": "F:/secret.txt"})
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["error"]["code"], "unknown_field")
            blocked_handler.assert_not_called()

        with patch.object(os, "system") as os_system, \
                patch.object(subprocess, "run") as subprocess_run, \
                patch.object(subprocess, "Popen") as subprocess_popen:
            unknown = self.run_tool("run_shell", {"command": "type secret.txt"})
            self.assertFalse(unknown["ok"])
            self.assertEqual(unknown["error"]["code"], "unknown_tool")
            os_system.assert_not_called()
            subprocess_run.assert_not_called()
            subprocess_popen.assert_not_called()
        rejected_events = self.fake_store.list_evidence(self.db_path, self.session_id)
        self.assertEqual(rejected_events, [])
        # 拒绝也会写入事件账本，且不产生任何证据或实际命令副作用。
        events = self.fake_store.list_events(self.session_id, self.turn_id)
        self.assertEqual([event["status"] for event in events], ["rejected", "rejected"])

        # 编号参数不接收路径分隔符；校验失败时甚至不会查询记录存储层。
        with patch.object(self.fake_store, "get_evidence", wraps=self.fake_store.get_evidence) as read_evidence:
            path_attempt = self.run_tool("get_record", {
                "record_type": "evidence", "record_id": "../../DeepSeekKey.txt",
            }, call_id="call_path_traversal")
            self.assertFalse(path_attempt["ok"])
            self.assertEqual(path_attempt["error"]["code"], "invalid_value")
            read_evidence.assert_not_called()

    def test_nonfinite_numbers_and_duplicate_json_keys_are_rejected(self):
        blocked_handler = unittest.mock.Mock()
        blocked_spec = {**tools._TOOL_SPECS["search_jobs"], "handler": blocked_handler}
        raw_values = [
            '{"query":"用户研究","limit":NaN}',
            '{"query":"用户研究","limit":Infinity}',
            '{"query":"用户研究","limit":1e9999}',
        ]
        with patch.dict(tools._TOOL_SPECS, {"search_jobs": blocked_spec}):
            for index, raw in enumerate(raw_values):
                result = tools.execute_tool(self.ctx, {
                    "id": f"call_nonfinite_{index}", "type": "function",
                    "function": {"name": "search_jobs", "arguments": raw},
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "non_finite_number")

            duplicate = tools.execute_tool(self.ctx, {
                "id": "call_duplicate_key", "type": "function",
                "function": {"name": "search_jobs", "arguments": '{"query":"用户研究","query":"产品"}'},
            })
            self.assertFalse(duplicate["ok"])
            self.assertEqual(duplicate["error"]["code"], "duplicate_json_key")
            blocked_handler.assert_not_called()

        secret_field = self.run_tool("get_assessment", {
            "sk-test-secret-123456789": "sensitive",
        }, call_id="call_secret_field")
        self.assertFalse(secret_field["ok"])
        self.assertNotIn("sk-test-secret", json.dumps(secret_field, ensure_ascii=False))
        secret_query = self.run_tool("search_jobs", {"query": "sk-test-secret-123456789"})
        self.assertTrue(secret_query["ok"])
        self.assertNotIn("sk-test-secret", json.dumps(secret_query, ensure_ascii=False))

    def test_condition_must_quote_this_turn_user_message_exactly(self):
        good = self.run_tool("record_condition", {
            "source_message_id": self.user_message_id,
            "field": "到岗时间",
            "quote": "每周实习三天",
            "status": "自述",
        })
        self.assertTrue(good["ok"], good)
        evidence = self.fake_store.get_evidence(self.db_path, self.session_id, good["evidence_ids"][0])
        self.assertEqual(evidence["data"]["quote"], "每周实习三天")
        self.assertIn("不会覆盖测评分数", evidence["data"]["protection"])

        bad_quote = self.run_tool("record_condition", {
            "source_message_id": self.user_message_id,
            "field": "到岗时间",
            "quote": "每周实习五天",
            "status": "自述",
        }, call_id="call_bad_quote")
        self.assertFalse(bad_quote["ok"])
        self.assertEqual(bad_quote["error"]["code"], "quote_mismatch")

        old_message = "msg-old"
        self.fake_store.messages[old_message] = {
            "id": old_message, "session_id": self.session_id, "turn_id": self.turn_id,
            "role": "user", "content": "旧消息内容",
        }
        old_ref = self.run_tool("record_condition", {
            "source_message_id": old_message,
            "field": "其他",
            "quote": "旧消息内容",
            "status": "待确认",
        }, call_id="call_old_message")
        self.assertFalse(old_ref["ok"])
        self.assertEqual(old_ref["error"]["code"], "not_current_user_message")

    def test_historical_record_read_grants_same_session_dependency_closure(self):
        assessment = self.run_tool("get_assessment", {})
        jobs = self.run_tool("search_jobs", {"query": "用户研究"})
        job = jobs["data"]["results"][0]
        match = self.run_tool("match_job", {
            "job_id": job["job"]["job_id"],
            "assessment_evidence_id": assessment["evidence_ids"][0],
            "job_evidence_id": job["evidence_id"],
        })
        calculation_id = match["data"]["calculation_id"]

        new_turn = "turn-b"
        new_ctx = self.make_context()
        new_ctx["turn_id"] = new_turn
        retrieved = self.run_tool("get_record", {
            "record_type": "evidence", "record_id": calculation_id,
        }, call_id="call_get_historical", ctx=new_ctx)
        self.assertTrue(retrieved["ok"], retrieved)
        self.assertGreaterEqual(len(retrieved["evidence_ids"]), 4)
        self.assertTrue(set(retrieved["evidence_ids"]).issubset(new_ctx["allowed_evidence_ids"]))

        foreign_session = "session-b"
        foreign_turn = "turn-c"
        foreign_ctx = {
            "db_path": self.db_path,
            "session_id": foreign_session,
            "turn_id": foreign_turn,
            "allowed_evidence_ids": set(),
            "user_message_id": "",
            "pending_recommendations": [],
        }
        foreign_read = self.run_tool("get_record", {
            "record_type": "evidence", "record_id": calculation_id,
        }, call_id="call_foreign", ctx=foreign_ctx)
        self.assertFalse(foreign_read["ok"])
        self.assertEqual(foreign_read["error"]["code"], "evidence_not_found")

    def test_forged_or_incomplete_recommendation_cannot_be_submitted(self):
        bad = self.run_tool("submit_recommendation", {
            "title": "岗位推荐",
            "explanation": "已经完全匹配。",
            "calculation_id": "ev_forged",
            "evidence_ids": ["ev_forged"],
            "limitations": [],
            "next_action": "投递",
        })
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "evidence_not_found")
        self.assertEqual(self.fake_store.list_recommendations(self.db_path, self.session_id), [])

        assessment = self.run_tool("get_assessment", {})
        jobs = self.run_tool("search_jobs", {"query": "用户研究"})
        job = jobs["data"]["results"][0]
        match = self.run_tool("match_job", {
            "job_id": job["job"]["job_id"],
            "assessment_evidence_id": assessment["evidence_ids"][0],
            "job_evidence_id": job["evidence_id"],
        })
        missing_dependency = self.run_tool("submit_recommendation", {
            "title": "岗位推荐",
            "explanation": "本轮规则下匹配。",
            "calculation_id": match["data"]["calculation_id"],
            "evidence_ids": [match["data"]["calculation_id"]],
            "limitations": [],
            "next_action": "再了解岗位",
        }, call_id="call_missing_dependency")
        self.assertFalse(missing_dependency["ok"])
        self.assertEqual(missing_dependency["error"]["code"], "missing_dependency")
        self.assertEqual(self.fake_store.list_recommendations(self.db_path, self.session_id), [])

    def test_untrusted_job_text_is_returned_as_data_not_executed(self):
        with patch.object(os, "system") as os_system, \
                patch.object(subprocess, "run") as subprocess_run, \
                patch.object(subprocess, "Popen") as subprocess_popen:
            result = self.run_tool("search_jobs", {"query": "用户研究"})
            self.assertTrue(result["ok"])
            note = result["data"]["results"][0]["job"]["untrusted_note"]
            self.assertIn("运行任意命令", note)
            os_system.assert_not_called()
            subprocess_run.assert_not_called()
            subprocess_popen.assert_not_called()
        self.assertEqual(
            {item["function"]["name"] for item in tools.tool_definitions()},
            {"get_assessment", "search_jobs", "get_record", "match_job", "record_condition", "submit_recommendation", "ask_user", "query_professional_data", "submit_decision"},
        )

    def test_unexpected_exception_is_hidden_from_model_result(self):
        blocked_spec = {**tools._TOOL_SPECS["get_assessment"],
                        "handler": unittest.mock.Mock(side_effect=RuntimeError("sk-test-secret-123456789"))}
        with patch.dict(tools._TOOL_SPECS, {"get_assessment": blocked_spec}):
            result = self.run_tool("get_assessment", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "internal_error")
        self.assertNotIn("secret", result["error"]["message"])
        event = self.fake_store.list_events(self.session_id, self.turn_id)[-1]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["error_code"], "internal_error")


if __name__ == "__main__":
    unittest.main()
