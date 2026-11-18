from unittest.mock import patch
from tests.test_turn_recall import Fixture, SCOPE, NOW, TODAY, turn


class LiteralRecallTests(Fixture):
    def recall(self, text, **scope):
        with patch.object(turn.time, 'time', return_value=NOW):
            return turn.TurnRecall(self.memory).recall({**SCOPE, **scope, 'text': text})

    def test_real_1035_user_sentence_is_not_an_assistant_speech_query(self):
        text = '可以 下次问题你可以直接告诉我用什么模型'
        ref = self.raw(rid='1794383082030-2294', text=text, assign=True)
        mid = self.remember('下次问题直接告诉用户用什么模型', [ref])
        out = self.recall(text)
        self.assertEqual([h['id'] for h in out['hits']], [mid])
        self.assertEqual(out['hits'][0]['source_role'], 'user')
        self.assertEqual(out['hits'][0]['source_refs'][0]['quote'], text)

    def test_exact_long_original_can_find_a_memory_with_a_short_citation(self):
        text = ''.join(f'模型验证项目第{i}项需要逐一核对。' for i in range(60))
        ref = self.raw(text=text, assign=True)
        ref['quote'] = '模型验证项目第40项需要逐一核对。'
        mid = self.remember('逐一核对模型验证项目', [ref])
        out = self.recall(text)
        self.assertEqual([h['id'] for h in out['hits']], [mid])
        self.assertLessEqual(len(out['hits'][0]['source_refs'][0]['quote']), turn.MAX_QUOTE_CHARS)
        self.assertIn(out['hits'][0]['source_refs'][0]['quote'], text)

    def test_date_prefixed_user_imperative_is_not_filtered_as_assistant(self):
        text = '可以 下次问题你可以直接告诉我用什么模型'
        ref = self.raw(text=text, assign=True)
        mid = self.remember('下次问题用什么模型', [ref])
        out = self.recall('2026-11-11 原话：' + text)
        self.assertIn(mid, [h['id'] for h in out['hits']])
        self.assertEqual(self.recall('2026-11-10 原话：' + text)['hits'], [])

    def test_literal_never_bypasses_scope_ownership_or_source_proof(self):
        text = '可以 下次问题你可以直接告诉我用什么模型'
        ref = self.raw(text=text, assign=True)
        self.remember('下次问题用什么模型', [ref])
        for scope in ({'persona': '另一位'}, {'conv_key': 'private:2228628155'}, {'bot_id': '99999'}):
            self.assertEqual(self.recall(text, **scope)['hits'], [])
        self.db.conn.execute("UPDATE memory_pipeline_raw SET sha256='changed'")
        self.assertEqual(self.recall(text)['hits'], [])

    def test_explicit_recall_question_does_not_literal_match_itself_on_the_wrong_day(self):
        text = '我昨天跟你说过什么事情，请你帮我回想一下。'
        ref = self.raw(text=text, ts=TODAY, assign=True)
        self.remember('用户请求回查昨日发言', [ref])
        self.assertEqual(self.recall(text)['hits'], [])