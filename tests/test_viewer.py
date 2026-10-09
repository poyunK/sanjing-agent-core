"""viewer 的静态页面测试；用模拟快照避免依赖 SQLite 内部表结构。"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sanjing.viewer import write_snapshot


class ViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name) / "ledger"
        self.session_id = "session-demo"
        self.turn_id = str(uuid.uuid4())

    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn": {
                "id": self.turn_id,
                "status": "completed",
                "answer": "建议先探索用户研究。",
                "error_code": None,
                "created_at": "2026-10-08T12:00:00Z",
            },
            "recommendations": [{
                "id": "rec-1",
                "turn_id": self.turn_id,
                "data": {
                    "title": "用户研究方向",
                    "summary": "这是团队原型假设，供演示。",
                    "calculation_inputs": {"兴趣维度": "S"},
                    "rule_version": "prototype-v1",
                    "demo_label": "团队演示假设",
                    "computed": {"score": 0.6, "demo": True, "demo_label": "团队演示假设"},
                    "contributions": [{"factor": "S", "value": 0.6}],
                    "evidence_ids": ["ev-1"],
                    "limitations": ["尚未评估实际访谈能力"],
                },
            }],
            "evidence": [{
                "id": "ev-1",
                "kind": "job_profile",
                "event_id": "event-1",
                "turn_id": self.turn_id,
                "data": {
                    "job": {
                        "title": "用户研究实习生（演示岗位）",
                        "summary": "岗位包含访谈与整理发现。",
                        "requirements": ["访谈和整理发现"],
                    },
                    "source": "演示资料第 2 段",
                    "demo": True,
                },
            }],
            "events": [{
                "id": "event-1",
                "tool_name": "search_roles",
                "call_id": "call-1",
                "status": "success",
                "error_code": None,
                "duration_ms": 4,
            }],
            "messages": [
                {"id": "m1", "role": "user", "content": "我对用户研究好奇。", "turn_id": self.turn_id},
                {"id": "m2", "role": "assistant", "content": "PRIVATE_REASONING_SHOULD_NOT_APPEAR", "turn_id": self.turn_id},
                {"id": "m3", "role": "tool", "content": "raw tool output should not appear", "turn_id": self.turn_id},
            ],
        }

    def test_writes_readable_static_page_with_linked_evidence_and_event(self) -> None:
        with patch("sanjing.viewer.store.snapshot", return_value=self.snapshot()) as snapshot:
            page_path = write_snapshot("demo.db", self.session_id, self.turn_id, self.output_dir)

        self.assertEqual(page_path, self.output_dir.resolve() / f"turn-{self.turn_id}.html")
        self.assertTrue(page_path.is_file())
        snapshot.assert_called_once_with("demo.db", self.session_id, self.turn_id)
        page = page_path.read_text(encoding="utf-8")
        self.assertIn("用户研究方向", page)
        self.assertIn("prototype-v1", page)
        self.assertIn("演示资料第 2 段", page)
        self.assertIn("程序计算分数", page)
        self.assertIn("团队演示假设", page)
        self.assertIn("call-1", page)
        self.assertNotIn("PRIVATE_REASONING_SHOULD_NOT_APPEAR", page)
        self.assertNotIn("raw tool output should not appear", page)
        self.assertIn("引用有效不等于内容一定支持结论", page)
        self.assertIn("Content-Security-Policy", page)

    def test_escapes_untrusted_html_in_all_displayed_fields(self) -> None:
        data = self.snapshot()
        data["messages"][0]["content"] = '<img src=x onerror="alert(1)">'
        data["evidence"][0]["data"]["job"]["summary"] = "<script>alert('evidence')</script>"
        data["evidence"][0]["data"]["source"] = '<a href="javascript:alert(1)">source</a>'
        data["recommendations"][0]["data"]["title"] = '<b onclick="bad()">title</b>'
        with patch("sanjing.viewer.store.snapshot", return_value=data):
            page_path = write_snapshot("demo.db", self.session_id, self.turn_id, self.output_dir)
        page = page_path.read_text(encoding="utf-8")

        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", page)
        self.assertIn("&lt;script&gt;alert(&#x27;evidence&#x27;)&lt;/script&gt;", page)
        self.assertIn("&lt;a href=&quot;javascript:alert(1)&quot;&gt;source&lt;/a&gt;", page)
        self.assertIn("&lt;b onclick=&quot;bad()&quot;&gt;title&lt;/b&gt;", page)
        self.assertNotIn("<script>alert('evidence')</script>", page)
        self.assertNotIn('<img src=x onerror="alert(1)">', page)

    def test_each_turn_gets_its_own_uuid_named_page(self) -> None:
        other_turn_id = str(uuid.uuid4())
        data = self.snapshot()
        data["turn"]["id"] = other_turn_id
        with patch("sanjing.viewer.store.snapshot", return_value=data):
            first = write_snapshot("demo.db", self.session_id, self.turn_id, self.output_dir)
            second = write_snapshot("demo.db", self.session_id, other_turn_id, self.output_dir)
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.output_dir.glob("turn-*.html"))), 2)

    def test_rejects_unsafe_turn_id_before_reading_snapshot(self) -> None:
        with patch("sanjing.viewer.store.snapshot") as snapshot:
            with self.assertRaises(ValueError):
                write_snapshot("demo.db", self.session_id, "../../outside", self.output_dir)
        snapshot.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_accepts_the_stores_turn_prefix_format(self) -> None:
        turn_id = "turn_0123456789abcdef0123456789abcdef"
        data = self.snapshot()
        data["turn"]["id"] = turn_id
        with patch("sanjing.viewer.store.snapshot", return_value=data):
            page_path = write_snapshot("demo.db", self.session_id, turn_id, self.output_dir)
        self.assertEqual(page_path.name, "turn_0123456789abcdef0123456789abcdef.html")

    def test_without_recommendation_does_not_promote_plain_answer(self) -> None:
        data = self.snapshot()
        data["recommendations"] = []
        data["turn"]["answer"] = "你应该选择用户研究。"
        with patch("sanjing.viewer.store.snapshot", return_value=data):
            page_path = write_snapshot("demo.db", self.session_id, self.turn_id, self.output_dir)
        page = page_path.read_text(encoding="utf-8")
        self.assertIn("没有已验证的建议记录", page)
        self.assertIn("不会把它标记为已验证建议", page)

    def test_incomplete_recommendation_is_marked_as_unpublished(self) -> None:
        data = self.snapshot()
        data["turn"]["status"] = "incomplete"
        with patch("sanjing.viewer.store.snapshot", return_value=data):
            page_path = write_snapshot("demo.db", self.session_id, self.turn_id, self.output_dir)
        page = page_path.read_text(encoding="utf-8")
        self.assertIn("未发布草稿", page)
        self.assertNotIn("<p class=\"status\">已发布建议</p>", page)

    def test_needs_input_page_shows_user_condition_evidence(self) -> None:
        data = self.snapshot()
        data["turn"]["status"] = "needs_input"
        data["recommendations"] = []
        data["evidence"].append({
            "id": "ev-condition",
            "kind": "user_condition",
            "event_id": "event-condition",
            "turn_id": self.turn_id,
            "data": {
                "field": "availability",
                "quote": "我每周只能实习三天。",
                "status": "自述",
                "source_message_id": "message-1",
            },
        })
        data["turn"]["answer"] = "你每周可以实习几天？"
        with patch("sanjing.viewer.store.snapshot", return_value=data):
            page_path = write_snapshot("demo.db", self.session_id, self.turn_id, self.output_dir)
        page = page_path.read_text(encoding="utf-8")
        self.assertIn("用户自述 / 待确认条件", page)
        self.assertIn("我每周只能实习三天。", page)
        self.assertIn("availability", page)


if __name__ == "__main__":
    unittest.main()
