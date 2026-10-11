"""模拟并发调用与网页重发；所有网络客户端都由测试替身替换。"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sanjing import store
from sanjing.agent import run_turn
from sanjing.offline import ask_offline, _reply


class SessionRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        self.session = store.ensure_session(self.db)

    def test_repeated_id_returns_original_result_without_second_model_call(self):
        count = []
        def fake(messages, definitions):
            count.append(1)
            return ask_offline(messages, definitions)
        first = run_turn(self.session, '探索用户研究', db_path=self.db, client=fake, request_id='web-1')
        original_count = len(count)
        second = run_turn(self.session, '探索用户研究', db_path=self.db, client=fake, request_id='web-1')
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(second['turn_id'], first['turn_id'])
        self.assertEqual(second['answer'], first['answer'])
        self.assertTrue(second['replayed'])
        self.assertEqual(len(count), original_count)
        self.assertEqual(len(store.list_turns(self.db, self.session)), 1)

    def test_id_cannot_change_input_or_cross_session(self):
        run_turn(self.session, '探索用户研究', db_path=self.db, client=ask_offline, request_id='same')
        result = run_turn(self.session, '更改问题', db_path=self.db, client=lambda *_: self.fail(), request_id='same')
        self.assertEqual(result['error_code'], 'REQUEST_ID_CONFLICT')
        other = store.ensure_session(self.db)
        result = run_turn(other, '探索用户研究', db_path=self.db, client=ask_offline, request_id='same')
        self.assertEqual(result['session_id'], other)
        self.assertEqual(result['status'], 'completed')

    def test_running_session_refuses_other_request_but_other_session_runs(self):
        entered, release = threading.Event(), threading.Event()
        received = []
        def waiting(messages, definitions):
            entered.set()
            self.assertTrue(release.wait(3))
            return _reply([('ask_user', {'fields': ['skills']})])
        worker = threading.Thread(target=lambda: received.append(run_turn(
            self.session, '合成问题', db_path=self.db, client=waiting, request_id='first')))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            for req in ('first', 'second'):
                refused = run_turn(self.session, '合成问题', db_path=self.db,
                                   client=lambda *_: self.fail(), request_id=req)
                self.assertEqual(refused['error_code'], 'SESSION_BUSY')
                self.assertEqual(refused['usage']['requests'], 0)
            other = store.ensure_session(self.db)
            independent = run_turn(other, '合成问题', db_path=self.db,
                                   client=lambda *_: _reply([('ask_user', {'fields': ['skills']})]))
            self.assertEqual(independent['status'], 'needs_input')
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(received[0]['status'], 'needs_input')
        self.assertFalse(store.owns_lease(self.db, self.session, received[0]['turn_id']))

    def test_expired_owner_cannot_release_new_lease(self):
        first = store.begin_request(self.db, self.session, '旧输入', 'old', lease_seconds=-1)
        second = store.begin_request(self.db, self.session, '新输入', 'new')
        self.assertEqual(second['state'], 'owned')
        self.assertEqual(store.get_turn(self.db, self.session, first['turn_id'])['error_code'], 'INTERRUPTED')
        store.complete_request(self.db, self.session, first['turn_id'])
        self.assertTrue(store.owns_lease(self.db, self.session, second['turn_id']))
        repeat = store.begin_request(self.db, self.session, '旧输入', 'old')
        self.assertEqual(repeat['state'], 'interrupted')

    def test_snapshot_failure_still_releases_and_does_not_retry_old_request(self):
        with patch('sanjing.viewer.write_snapshot', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                run_turn(self.session, '探索方向', db_path=self.db, client=ask_offline, request_id='page-failure')
        repeated = run_turn(self.session, '探索方向', db_path=self.db,
                            client=lambda *_: self.fail(), request_id='page-failure')
        self.assertEqual(repeated['error_code'], 'INTERRUPTED')
        newer = run_turn(self.session, '继续探索', db_path=self.db, client=ask_offline, request_id='newer')
        self.assertEqual(newer['status'], 'completed')

    def test_same_expired_id_becomes_interrupted_without_paid_retry(self):
        first = store.begin_request(self.db, self.session, '旧输入', 'old', lease_seconds=-1)
        repeated = run_turn(self.session, '旧输入', db_path=self.db,
                            client=lambda *_: self.fail(), request_id='old')
        self.assertEqual(repeated['error_code'], 'INTERRUPTED')
        self.assertEqual(repeated['usage']['requests'], 0)
        self.assertEqual(store.get_turn(self.db, self.session, first['turn_id'])['status'], 'incomplete')

    def test_private_continuation_is_only_in_memory(self):
        from sanjing import model
        calls = []
        def fake(messages, definitions):
            if not calls:
                calls.append(1)
                return model.parse_responses_response({'status': 'completed', 'output': [
                    {'id': 'rs_example', 'type': 'reasoning', 'encrypted_content': 'opaque-private-example',
                     'summary': [{'type': 'summary_text', 'text': 'internal-example-do-not-store'}]},
                    {'id': 'fc_example', 'type': 'function_call', 'call_id': 'call_example',
                     'name': 'get_assessment', 'arguments': '{}'},
                    {'id': 'fc_search', 'type': 'function_call', 'call_id': 'call_search',
                     'name': 'search_jobs', 'arguments': '{"query":"用户研究 产品经理","limit":2}'}]})
            self.assertTrue(any(row.get('_responses_output') for row in messages))
            return ask_offline(messages, definitions)
        result = run_turn(self.session, '探索用户研究', db_path=self.db, client=fake)
        self.assertEqual(result['status'], 'completed')
        for content in (self.db.read_bytes(), Path(result['ledger_path']).read_bytes()):
            self.assertNotIn(b'opaque-private-example', content)
            self.assertNotIn(b'internal-example-do-not-store', content)
            self.assertNotIn(b'_responses_output', content)

    def test_stale_worker_does_not_resurrect_or_cache_after_tool_returns(self):
        from sanjing import tools
        entered, release = threading.Event(), threading.Event()
        original_begin, original_execute = store.begin_request, tools.execute_tool
        results = []
        def short_lease(*args, **kwargs):
            if args[3] == 'old':
                args = (*args[:4], 0.1)
            return original_begin(*args, **kwargs)
        def blocking(ctx, call):
            if call['id'] == 'old-call':
                entered.set()
                self.assertTrue(release.wait(4))
            return original_execute(ctx, call)
        def fake(*_):
            return {'message': {'role': 'assistant', 'tool_calls': [
                {'id': 'old-call', 'type': 'function', 'function': {'name': 'ask_user', 'arguments': '{"fields":["skills"]}'}}]}, 'usage': {}}
        with patch.object(store, 'begin_request', side_effect=short_lease), patch.object(tools, 'execute_tool', side_effect=blocking):
            worker = threading.Thread(target=lambda: results.append(run_turn(
                self.session, '旧问题', db_path=self.db, client=fake, request_id='old')))
            worker.start()
            try:
                self.assertTrue(entered.wait(4))
                time.sleep(0.15)
                newer = run_turn(self.session, '新问题', db_path=self.db,
                                 client=lambda *_: _reply([('ask_user', {'fields': ['goal']})]), request_id='new')
                self.assertEqual(newer['status'], 'needs_input')
            finally:
                release.set()
                worker.join(5)
        self.assertEqual(results[0]['error_code'], 'INTERRUPTED')
        old = store.get_turn(self.db, self.session, results[0]['turn_id'])
        self.assertEqual(old['status'], 'incomplete')
        self.assertEqual(old['error_code'], 'INTERRUPTED')
        self.assertFalse(any(row['role'] == 'assistant' and row['turn_id'] == old['id']
                             for row in store.list_messages(self.db, self.session)))
        repeat = run_turn(self.session, '旧问题', db_path=self.db, client=lambda *_: self.fail(), request_id='old')
        self.assertEqual(repeat['error_code'], 'INTERRUPTED')
        self.assertFalse(repeat['replayed'])

    def test_reported_token_details_are_persisted_as_numbers(self):
        def fake(*_):
            reply = _reply([('ask_user', {'fields': ['skills']})])
            reply['usage'] = {'prompt_tokens': 40, 'completion_tokens': 12, 'total_tokens': 52,
                              'input_tokens_details': {'cached_tokens': 10},
                              'output_tokens_details': {'reasoning_tokens': 3}}
            return reply
        result = run_turn(self.session, '合成问题', db_path=self.db, client=fake)
        usage = store.get_turn(self.db, self.session, result['turn_id'])['usage']
        self.assertEqual(usage['cached_input_tokens'], 10)
        self.assertEqual(usage['reasoning_tokens'], 3)
        self.assertEqual(usage['reported_details_requests'], 1)


if __name__ == '__main__':
    unittest.main()
