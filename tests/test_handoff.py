"""在临时项目测试协议边界，既不派真实任务，也不访问 API。"""

import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / ".agents/skills/local-agent-handoff/scripts/handoff.py"
spec = importlib.util.spec_from_file_location("local_handoff", SCRIPT)
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "project"
        self.root.mkdir()
        (self.root / "README.md").write_text("合成测试项目\n", encoding="utf-8")
        (self.root / "code.py").write_text("answer = 1\n", encoding="utf-8")
        self.drafts = self.root / ".handoff-local/drafts"
        self.drafts.mkdir(parents=True)
        self.task_spec = {
            "sender": "codex", "receiver": "deepseek-harness", "mode": "edit",
            "goal": "修改合成测试常量", "user_authorization": "用户要求在测试目录修改常量。",
            "context": "用于协议离线验证。", "confirmed_decisions": ["不调用网络"],
            "read_first": ["code.py"], "write_scope": ["code.py"],
            "acceptance": ["常量等于二"], "out_of_scope": ["不增加服务"], "external_actions": [],
        }
        self.report = {"status": "completed", "summary": "合成改动完成",
                       "decisions": ["使用常量"], "remaining": [], "context_requests": [],
                       "checks": [{"command": "读取合成常量", "outcome": "passed", "evidence": "实际值为二", "exit_code": 0}]}
        self.review = {"decision": "accepted", "summary": "派单方复核字节符合目标",
                       "checks": [{"command": "复核合成常量", "outcome": "passed", "evidence": "重新读取值为二"}]}

    def tearDown(self):
        self.temp.cleanup()

    def json_file(self, name, value):
        path = self.drafts / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def run_command(self, command, *args):
        return handoff.main([command, "--project", str(self.root), *map(str, args)])

    def pack(self, spec=None):
        return self.run_command("pack", "--spec", self.json_file("spec.json", spec or self.task_spec))

    def claim(self, task):
        return self.run_command("claim", "--task", task["task"], "--actor", self.task_spec["receiver"])

    def finish(self, task, report=None):
        return self.run_command("finish", "--task", task["task"], "--actor", self.task_spec["receiver"],
                                "--report", self.json_file("report.json", report or self.report))

    def complete(self):
        task = self.pack()
        self.claim(task)
        (self.root / "code.py").write_text("answer = 2\n", encoding="utf-8")
        result = self.finish(task)
        return task, result

    def test_roundtrip_arbitrary_agents_and_durable_board(self):
        self.task_spec.update(sender="claude-code", receiver="another-vendor-agent")
        task, result = self.complete()
        self.assertEqual(self.run_command("check", "--message", result["result"])["semantic_acceptance"], "not_checked")
        receipt = self.run_command("accept", "--result", result["result"], "--review", self.json_file("review.json", self.review))
        self.assertIsNone(self.run_command("status")["active"])
        state = self.run_command("status", "--task-id", task["task_id"])
        self.assertEqual([event["stage"] for event in state["history"]], ["backlog", "offered", "running", "returned", "accepted"])
        self.assertIn("已验收", Path(receipt["board"]).read_text(encoding="utf-8"))
        self.assertIn("another-vendor-agent", Path(receipt["board"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(receipt["receipt"]).exists())

    def test_readonly_review_and_plan(self):
        for mode in ("review", "plan"):
            with self.subTest(mode=mode):
                task_spec = dict(self.task_spec, mode=mode, write_scope=[])
                task = self.pack(task_spec)
                self.claim(task)
                report = dict(self.report, status="proposed" if mode == "plan" else "completed")
                result = self.finish(task, report)
                self.assertEqual(handoff.message_read(result["result"])["changed_files"], [])
                self.run_command("accept", "--result", result["result"], "--review", self.json_file("review.json", self.review))

    def test_readonly_change_is_rejected(self):
        task = self.pack(dict(self.task_spec, mode="review", write_scope=[]))
        self.claim(task)
        (self.root / "code.py").write_text("oops", encoding="utf-8")
        with self.assertRaisesRegex(handoff.ProtocolError, "out_of_scope_change"):
            self.finish(task)

    def test_stale_snapshot_and_protected_new_file(self):
        task = self.pack()
        (self.root / "new.py").write_text("new", encoding="utf-8")
        with self.assertRaisesRegex(handoff.ProtocolError, "stale_snapshot"):
            self.claim(task)
        (self.root / "new.py").unlink()
        self.claim(task)
        (self.root / "README.md").write_text("not allowed", encoding="utf-8")
        with self.assertRaisesRegex(handoff.ProtocolError, "out_of_scope_change"):
            self.finish(task)

    def test_scope_and_delete(self):
        task = self.pack()
        self.claim(task)
        (self.root / "code.py").unlink()
        with self.assertRaisesRegex(handoff.ProtocolError, "delete_not_authorized"):
            self.finish(task)

    def test_duplicate_claim_and_other_dispatch(self):
        task = self.pack()
        self.claim(task)
        with self.assertRaisesRegex(handoff.ProtocolError, "already_claimed"):
            self.claim(task)
        with self.assertRaisesRegex(handoff.ProtocolError, "active_task"):
            self.pack()

    def test_wrong_actor(self):
        task = self.pack()
        with self.assertRaisesRegex(handoff.ProtocolError, "wrong_actor"):
            self.run_command("claim", "--task", task["task"], "--actor", "intruder")

    def test_interrupted_claim_needs_recovery_not_second_claim(self):
        task = self.pack()
        handoff.save_json(self.root / ".handoff-local/ACTIVE.json", {"task_id": task["task_id"], "phase": "running", "owner": self.task_spec["receiver"]})
        with self.assertRaisesRegex(handoff.ProtocolError, "phase_mismatch"):
            self.claim(task)
        self.run_command("recover", "--task-id", task["task_id"], "--note", "合成中断：已确认领取进程停止，源码未改。")
        self.assertEqual(self.run_command("status")["active"]["phase"], "running")

    def test_forged_task_and_result(self):
        task = self.pack()
        value = handoff.message_read(task["task"])
        value["goal"] = "伪造扩大目标"
        forged = self.drafts / "forged.md"
        handoff.write_message(forged, value, "伪造任务", "用于测试")
        with self.assertRaisesRegex(handoff.ProtocolError, "not_matching_archive"):
            self.run_command("claim", "--task", forged, "--actor", self.task_spec["receiver"])
        self.claim(task)
        result = self.finish(task)
        value = handoff.message_read(result["result"])
        value["in_reply_to"] = "m-forged"
        forged_result = self.drafts / "forged-result.md"
        handoff.write_message(forged_result, value, "伪造结果", "用于测试")
        with self.assertRaisesRegex(handoff.ProtocolError, "not_matching_archive"):
            self.run_command("check", "--message", forged_result)

    def test_file_changed_after_result(self):
        task, result = self.complete()
        (self.root / "code.py").write_text("answer = 3\n", encoding="utf-8")
        with self.assertRaisesRegex(handoff.ProtocolError, "files_changed_after_result"):
            self.run_command("check", "--message", result["result"])

    def test_repeated_accept_is_idempotent_after_later_changes(self):
        task, result = self.complete()
        review = self.json_file("review.json", self.review)
        first = self.run_command("accept", "--result", result["result"], "--review", review)
        (self.root / "code.py").write_text("later task\n", encoding="utf-8")
        second = self.run_command("accept", "--result", result["result"], "--review", review)
        self.assertTrue(second["already_processed"])
        self.assertEqual(first["receipt"], second["receipt"])
        self.assertEqual((self.root / "code.py").read_text(), "later task\n")

    def test_pending_board_revisions_and_dependency(self):
        first = self.run_command("add", "--spec", self.json_file("spec.json", self.task_spec))
        dependent = dict(self.task_spec, depends_on=[first["task_id"]], priority="high")
        second = self.run_command("add", "--spec", self.json_file("second.json", dependent))
        with self.assertRaisesRegex(handoff.ProtocolError, "dependency_not_accepted"):
            self.run_command("pack", "--task-id", second["task_id"])
        cyclic = dict(self.task_spec, depends_on=[second["task_id"]])
        with self.assertRaisesRegex(handoff.ProtocolError, "dependency_cycle"):
            self.run_command("revise", "--task-id", first["task_id"], "--spec", self.json_file("cyclic.json", cyclic))
        revised = dict(self.task_spec, title="更新的测试标题")
        self.run_command("revise", "--task-id", first["task_id"], "--spec", self.json_file("revised.json", revised))
        folder = self.root / ".handoff-local/tasks" / first["task_id"]
        self.assertTrue((folder / "spec-0001.json").exists())
        self.assertTrue((folder / "spec-0002.json").exists())
        board = Path(self.run_command("board")["board"]).read_text(encoding="utf-8")
        self.assertIn("前置任务尚未验收", board)
        self.assertIn("更新的测试标题", board)

    def test_dependency_can_dispatch_after_acceptance(self):
        first, result = self.complete()
        self.run_command("accept", "--result", result["result"], "--review", self.json_file("review.json", self.review))
        spec = dict(self.task_spec, depends_on=[first["task_id"]])
        self.assertIn("task", self.pack(spec))

    def test_secret_paths_traversal_and_plaintext_key(self):
        for unsafe in ("../escape.py", "C:/secrets.txt", "runtime/a.py", ".env", "火山keyUTF8.txt", "secrets/one.py", "*.py"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(handoff.ProtocolError):
                    self.pack(dict(self.task_spec, write_scope=[unsafe]))
        with self.assertRaisesRegex(handoff.ProtocolError, "possible_credential"):
            self.pack(dict(self.task_spec, context="synthetic sk-123456789012345678901234"))

    def test_duplicate_json_and_multiple_blocks(self):
        with self.assertRaisesRegex(handoff.ProtocolError, "duplicate_json_key"):
            json.loads('{"a":1,"a":2}', object_pairs_hook=handoff.no_duplicate_keys)
        task = self.pack()
        duplicate = self.drafts / "duplicated.md"
        duplicate.write_text(Path(task["task"]).read_text(encoding="utf-8") * 2, encoding="utf-8")
        with self.assertRaisesRegex(handoff.ProtocolError, "need_one_lah"):
            handoff.message_read(duplicate)

    def test_cancel_retains_files_and_releases_occupancy(self):
        task = self.pack()
        self.claim(task)
        (self.root / "code.py").write_text("partial change\n", encoding="utf-8")
        cancelled = self.run_command("cancel", "--task", task["task"], "--note", "用户取消；合成执行方已停止，派单方已核对改动。")
        self.assertFalse(cancelled["files_rolled_back"])
        self.assertIsNone(self.run_command("status")["active"])
        self.assertEqual((self.root / "code.py").read_text(), "partial change\n")

    def test_needs_context_must_explain_and_cannot_be_accepted(self):
        task = self.pack()
        self.claim(task)
        missing = dict(self.report, status="needs_context")
        with self.assertRaisesRegex(handoff.ProtocolError, "missing_context_requests"):
            self.finish(task, missing)
        result = self.finish(task, dict(missing, context_requests=["需要明确预期值"]))
        with self.assertRaisesRegex(handoff.ProtocolError, "incomplete_result_not_accepted"):
            self.run_command("accept", "--result", result["result"], "--review", self.json_file("review.json", self.review))
        rework = dict(self.review, decision="rework")
        self.run_command("accept", "--result", result["result"], "--review", self.json_file("review.json", rework))
        self.assertEqual(self.run_command("status", "--task-id", task["task_id"])["stage"], "rework")

    def test_acceptance_requires_actual_check(self):
        task, result = self.complete()
        with self.assertRaisesRegex(handoff.ProtocolError, "acceptance_requires_evidence"):
            self.run_command("accept", "--result", result["result"], "--review", self.json_file("review.json", dict(self.review, checks=[])))

    def test_operation_lock_not_automatically_stolen(self):
        lock = self.root / ".handoff-local/OPERATION.lock"
        lock.mkdir()
        with self.assertRaisesRegex(handoff.ProtocolError, "operation_busy"):
            self.pack()

    def test_cancel_backlog_while_other_task_runs(self):
        task = self.pack()
        self.claim(task)
        waiting = self.run_command("add", "--spec", self.json_file("waiting.json", self.task_spec))
        self.run_command("cancel", "--task-id", waiting["task_id"], "--note", "用户取消尚未派出的测试待办。")
        self.assertEqual(self.run_command("status")["active"]["task_id"], task["task_id"])

    def test_recover_interrupted_return_without_reapplying_code(self):
        task, result = self.complete()
        folder = self.root / ".handoff-local/tasks" / task["task_id"]
        state = handoff.load_json(folder / "state.json")
        state["stage"] = "running"
        handoff.save_json(folder / "state.json", state)
        handoff.save_json(self.root / ".handoff-local/ACTIVE.json", {"task_id": task["task_id"], "phase": "running", "owner": self.task_spec["receiver"]})
        (self.root / ".handoff-local/OPERATION.lock").mkdir()
        before = (self.root / "code.py").read_bytes()
        recovered = self.run_command("recover", "--task-id", task["task_id"], "--note", "合成故障：已核对所有执行均停止。")
        self.assertEqual(recovered["stage"], "returned")
        self.assertEqual((self.root / "code.py").read_bytes(), before)
        self.assertEqual(self.run_command("check", "--message", result["result"])["structural_check"], "passed")

    def test_board_html_does_not_execute_task_text(self):
        value = dict(self.task_spec, title='<script>alert("injection")</script>')
        self.run_command("add", "--spec", self.json_file("html.json", value))
        page = (self.root / ".handoff-local/BOARD.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Content-Security-Policy", page)

    def test_runtime_and_credentials_not_in_snapshot(self):
        (self.root / "runtime").mkdir()
        (self.root / "runtime/private.txt").write_text("not packed")
        (self.root / "deepseekKey.txt").write_text("not packed")
        task = self.pack()
        files = handoff.message_read(task["task"])["snapshot"]["files"]
        self.assertEqual(set(files), {"README.md", "code.py"})

    @unittest.skipUnless(shutil.which("git"), "Git unavailable")
    def test_git_dirty_baseline_and_excluded_local_state(self):
        def git(*args):
            result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        git("init", "-q")
        (self.root / ".gitignore").write_text(".handoff-local/\n", encoding="utf-8")
        git("add", ".")
        git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "synthetic base")
        (self.root / "code.py").write_text("answer = 10\n", encoding="utf-8")
        actual_before = (self.root / "code.py").read_bytes()
        task = self.pack()
        base = handoff.message_read(task["task"])["snapshot"]["files"]["code.py"]
        self.assertEqual(base, handoff.digest(actual_before))
        self.claim(task)
        (self.root / "code.py").write_text("answer = 2\n", encoding="utf-8")
        result = self.finish(task)
        self.assertEqual(handoff.message_read(result["result"])["changed_files"][0]["before"], base)
        status = subprocess.run(["git", "-C", str(self.root), "status", "--short"], capture_output=True).stdout
        self.assertNotIn(b".handoff-local", status)


if __name__ == "__main__":
    unittest.main()
