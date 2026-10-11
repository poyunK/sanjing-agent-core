"""历史量增长时，不先加载完整历史，也不拆开一个旧回合。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sanjing import context, store, tools


class BoundedContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        self.session = store.ensure_session(self.db)

    def test_context_uses_bounded_sql_reads_and_old_ids_can_be_paged(self):
        for index in range(70):
            turn = store.start_turn(self.db, self.session)
            store.add_message(self.db, self.session, turn, 'user', '旧问题'+str(index))
            store.add_message(self.db, self.session, turn, 'assistant', '旧回复'+str(index))
            store.add_evidence(self.db, self.session, turn, 'user_condition',
                               {'quote': '条件'+str(index), 'field': '目标', 'status': '自述'})
            store.finish_turn(self.db, self.session, turn, 'completed', '旧回复')
        turn = store.start_turn(self.db, self.session)
        message = store.add_message(self.db, self.session, turn, 'user', '现在的问题')
        with (patch.object(store, 'list_messages', side_effect=AssertionError('full history')),
             patch.object(store, 'list_turns', side_effect=AssertionError('full history')),
             patch.object(store, 'list_evidence', side_effect=AssertionError('full history')),
             patch.object(store, 'list_recommendations', side_effect=AssertionError('full history'))):
            result = context.build_messages(self.db, self.session, turn, message)
        self.assertLessEqual(context.char_size(result), context.CONTEXT_CHAR_BUDGET)
        self.assertLessEqual(len(result), 82)
        roles = [row['role'] for row in result[1:-1]]
        self.assertEqual(roles, ['user', 'assistant'] * (len(roles)//2))
        first = store.history_index(self.db, self.session, 'evidence', limit=20)
        second = store.history_index(self.db, self.session, 'evidence', first['next_before_seq'], limit=20)
        self.assertFalse({row['id'] for row in first['items']} & {row['id'] for row in second['items']})
        self.assertEqual(len(second['items']), 20)

    def test_superseding_old_condition_is_checked_before_limit(self):
        turn = store.start_turn(self.db, self.session)
        old = store.add_evidence(self.db, self.session, turn, 'user_condition', {'field': '地点', 'quote': '北京', 'status': '自述'})
        store.add_evidence(self.db, self.session, turn, 'user_condition',
                           {'field': '地点', 'quote': '上海', 'status': '自述', 'supersedes_evidence_id': old})
        rows = store.recent_conditions(self.db, self.session)
        self.assertNotIn(old, [row['id'] for row in rows])
        self.assertIsNotNone(store.get_evidence(self.db, self.session, old))

    def test_history_tool_is_scoped_and_index_does_not_grant_evidence(self):
        turn = store.start_turn(self.db, self.session)
        evidence = store.add_evidence(self.db, self.session, turn, 'user_input', {'quote': '自己的'})
        other = store.ensure_session(self.db)
        other_turn = store.start_turn(self.db, other)
        hidden = store.add_evidence(self.db, other, other_turn, 'user_input', {'quote': '别人的'})
        ctx = {'db_path': self.db, 'session_id': self.session, 'turn_id': turn, 'allowed_evidence_ids': set()}
        call = {'id': 'history', 'type': 'function', 'function': {'name': 'list_history', 'arguments': '{"record_type":"evidence"}'}}
        result = tools.execute_tool(ctx, call)
        self.assertTrue(result['ok'], result)
        ids = [row['id'] for row in result['data']['items']]
        self.assertIn(evidence, ids)
        self.assertNotIn(hidden, ids)
        self.assertFalse(ctx['allowed_evidence_ids'])
        self.assertEqual(result['evidence_ids'], [])

    def test_recommendation_pages_reach_end_without_unpublished_or_missing_rows(self):
        expected = set()
        for index in range(23):
            turn = store.start_turn(self.db, self.session)
            record_id = store.add_recommendation(self.db, self.session, turn, {'title': '合成建议'+str(index)})
            if index % 5:
                store.finish_turn(self.db, self.session, turn, 'completed', '完成')
                expected.add(record_id)
        collected, cursor = [], None
        while True:
            page = store.history_index(self.db, self.session, 'recommendation', cursor, limit=3)
            collected.extend(row['id'] for row in page['items'])
            cursor = page['next_before_seq']
            if cursor is None:
                break
        self.assertEqual(set(collected), expected)
        self.assertEqual(len(collected), len(expected))
