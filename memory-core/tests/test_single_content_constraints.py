"""Behavioral checks with a frozen source pool. No DB, HTTP, model or QQ calls."""
import contextlib
from datetime import datetime
import hashlib
import io
import json
import unittest
from unittest.mock import Mock

from memory_core.memory_gates import MemoryGates, _gate1_query
from memory_core.source_provenance import classify_source
from memory_core.turn_recall import SHANGHAI

SCOPE = dict(bot_id='991111', persona='single-content-fixture', conv_key='private:992222')
NOW = datetime(2026, 11, 16, 12, tzinfo=SHANGHAI).timestamp()
STAMP = datetime(2026, 11, 12, 14, 47, tzinfo=SHANGHAI).timestamp()
FOOD = '一件事一条没问题 豆腐脑好像放了葱花和醋，所以好吃'


def record(rid, text, ts=STAMP, role='user'):
    rec = dict(id=rid, s='991111|private:992222', r=role, t=text, ts=ts, message_id=rid)
    blob = (json.dumps(rec, ensure_ascii=False) + '\n').encode('utf8')
    return {**rec, 'file': 'fixture.jsonl', 'offset': 0, 'size': len(blob),
            'sha256': hashlib.sha256(blob).hexdigest(), **classify_source(rec)}


def make_gates(food=FOOD, extra=(), source_clue=False):
    primary = record('fixture-food', food)
    if source_clue:
        primary.update(stance='来源不明', support_scope='仅可作线索')
    records = {r['id']: r for r in [primary, record('fixture-setting', '静默调成10秒 调用调三分钟'), *extra]}
    for n in range(12):
        rec = record('padding-' + str(n), '占位', STAMP + 100 + n, 'assistant')
        records[rec['id']] = rec
    gate = MemoryGates(None, clock=lambda: NOW)
    gate._load = Mock(return_value=([], records))
    return gate


def ask(text, gate=None):
    return (gate or make_gates()).one({**SCOPE, 'text': text})


def fixture_results():
    queries = ['豆腐脑放了糖吗', '豆腐脑放了盐吗', '豆腐脑放了醋吗',
               '豆腐脑是不是放了葱花和糖', '豆腐脑是不是放了葱花和醋和糖',
               '豆腐脑是不是放了葱花和盐', '豆腐脑是不是放了葱花和醋',
               '我说过『豆腐脑好像放了糖，所以好吃』吗',
               '我说过『豆腐脑好像放了葱花和醋，所以难吃』吗',
               '我说过『静默调成120秒』吗', '我说过『静默调成10秒』吗']
    with contextlib.redirect_stdout(io.StringIO()):
        return [dict(query=q, result=ask(q)) for q in queries]


class SingleContentConstraintsTests(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_short_questions_share_state_but_explain_the_missing_ingredient(self):
        sugar, salt, vinegar = [ask('豆腐脑放了' + word + '吗') for word in ('糖', '盐', '醋')]
        self.assertEqual([d['state'] for d in (sugar, salt, vinegar)], ['suspected'] * 3)
        for word, result in [('糖', sugar), ('盐', salt)]:
            self.assertIn('「' + word + '」', result['missing'])
            self.assertIn('葱花和醋', result['parts'][0])
            self.assertEqual(result['parts'][2], '所以我只能说到这儿')
        self.assertEqual(vinegar['missing'], '缺出处：原文没有覆盖所问的全部内容')
        self.assertNotEqual(sugar['missing'], salt['missing'])

    def test_unmentioned_single_character_nouns_never_borrow_partial_coverage(self):
        for noun in ('糖', '盐', '蒜', '姜', '辣', '酱'):
            with self.subTest(noun=noun):
                result = ask('豆腐脑是不是放了葱花和' + noun)
                self.assertNotEqual(result['state'], 'found')
                self.assertIn('「' + noun + '」', result['missing'])

    def test_true_and_false_ingredients_in_one_query_still_fail_full_coverage(self):
        result = ask('豆腐脑是不是放了葱花和醋和糖')
        self.assertEqual(result['state'], 'suspected')
        self.assertIn('「糖」', result['missing'])
        self.assertEqual(result['evidence']['raw_id'], 'fixture-food')

    def test_existing_positive_keeps_the_original_hedge(self):
        result = ask('豆腐脑是不是放了葱花和醋')
        self.assertEqual(result['state'], 'found')
        self.assertIn('好像', result['evidence']['quote'])

    def test_single_character_action_is_not_dropped(self):
        result = ask('豆腐脑是不是喝了葱花和醋')
        self.assertNotEqual(result['state'], 'found')
        self.assertIn('「喝」', result['missing'])

    def test_negation_must_not_match_an_unrelated_clause(self):
        gate = make_gates('我不着急，豆腐脑放了葱花和醋')
        result = ask('豆腐脑是不是不放了葱花和醋', gate)
        self.assertNotEqual(result['state'], 'found')
        self.assertIn('「不放」', result['missing'])

    def test_existing_literal_negative_predicate_can_still_match(self):
        gate = make_gates('豆腐脑好像不放葱花和醋')
        result = ask('豆腐脑是不是不放葱花和醋', gate)
        self.assertEqual(result['state'], 'found')

    def test_question_negation_shell_is_not_an_asserted_negative(self):
        self.assertEqual(ask('豆腐脑有没有放了葱花和醋')['state'], 'found')

    def test_removing_false_coverage_does_not_remove_original_ambiguity(self):
        extra = [record('fixture-later', '豆腐脑放了葱花和糖，所以好吃', STAMP + 1)]
        result = ask('上次豆腐脑是不是放了葱花和糖', make_gates(extra=extra))
        self.assertEqual(result['state'], 'suspected')
        self.assertEqual(result['evidence']['raw_id'], 'fixture-later')
        self.assertIn('多条相关原话', result['missing'])

    def test_selected_source_is_not_reranked_to_rescue_a_missing_term(self):
        extra = [record('fixture-later', '豆腐脑放了葱花和盐，所以好吃', STAMP + 1)]
        result = ask('豆腐脑是不是放了葱花和糖', make_gates(extra=extra))
        self.assertEqual(result['state'], 'suspected')
        self.assertEqual(result['evidence']['raw_id'], 'fixture-later')

    def test_explicit_utterance_questions_are_unchanged(self):
        for query in ['我说过『豆腐脑好像放了糖，所以好吃』吗',
                      '我说过『豆腐脑好像放了葱花和醋，所以难吃』吗',
                      '我说过『静默调成120秒』吗']:
            with self.subTest(query=query):
                self.assertEqual(ask(query)['state'], 'absent')
        for query in ['我说过『豆腐脑好像放了葱花和醋，所以好吃』吗',
                      '我说过『静默调成10秒』吗']:
            with self.subTest(query=query):
                result = ask(query)
                self.assertEqual(result['state'], 'found')
                self.assertTrue(result['meta_only'])

    def test_date_constraints_keep_their_original_verdict(self):
        self.assertEqual(ask('2026-11-12 豆腐脑是不是放了葱花和醋')['state'], 'found')
        wrong = ask('2026-11-13 豆腐脑是不是放了葱花和醋')
        self.assertEqual(wrong['state'], 'suspected')
        self.assertEqual(wrong['missing'], '时间对不上')

    def test_unqualified_material_is_not_promoted(self):
        result = ask('你还记得豆腐脑是不是放了葱花和糖', make_gates(source_clue=True))
        self.assertEqual(result['state'], 'absent')
        self.assertNotIn('evidence', result)
        self.assertTrue(result['source_clues'])

    def test_verdict_coverage_is_unchanged_but_displayed_units_have_word_boundaries(self):
        expected = {'豆腐', '腐脑', '葱花'}
        self.assertEqual(_gate1_query('豆腐脑是不是放了葱花和糖', NOW)[1], expected)
        result = ask('豆腐脑是不是放了葱花和糖')
        self.assertEqual(set(result['searched']['evidence_tokens']), {'豆腐脑', '放', '葱花', '糖'})

    def test_grammatical_remnants_are_not_invented_content_constraints(self):
        cases = [('下一步是这个吗', make_gates('开始下一步')),
                 ('现在豆腐脑是不是放了葱花和醋', make_gates())]
        for text, gate in cases:
            with self.subTest(text=text):
                self.assertEqual(ask(text, gate)['state'], 'found')

    def test_statements_still_do_not_load_sources(self):
        gate = make_gates()
        gate._load.side_effect = AssertionError('statement must not query')
        result = ask('今天早饭吃了豆腐脑', gate)
        self.assertEqual(result['state'], 'not_checked')
        self.assertIsNone(result['searched'])
        gate._load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
