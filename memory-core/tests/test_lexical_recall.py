"""Complete-word retrieval and its unchanged fact/source boundaries."""
import unittest
from unittest.mock import patch

from memory_core.gate1_recall_units import analyze, _lexical, lexical_match
from memory_core.turn_recall import _tokens
from test_single_content_constraints import ask, make_gates, record, STAMP


def plan(text):
    return analyze(text, text, _tokens)


class LexicalRecallTests(unittest.TestCase):
    def test_content_word_and_attributive_phrase_survive(self):
        self.assertTrue({'吃', '吃的'} <= set(plan('晚上吃的什么')['units']))
        self.assertTrue(lexical_match(plan('晚上吃的什么'), '吃了，红烧肉拌饭'))

    def test_disk_and_complete_action_object_survive(self):
        self.assertTrue({'清理', '盘', '清理盘'} <= set(plan('清理哪个盘')['units']))

    def test_noun_phrase_on_source_keeps_word_boundaries(self):
        words, phrases, _ = _lexical('吃了，红烧肉拌饭')
        self.assertIn('红烧肉拌饭', phrases)
        self.assertTrue({'红烧肉', '拌', '饭'} <= words)

    def test_code_identifier_and_phrase_survive_without_cross_word_grams(self):
        units = set(plan('_validate() 提前校验连线，后来怎么处理的')['units'])
        self.assertTrue({'_validate()', '校验', '连线', '校验连线'} <= units)
        self.assertFalse(units & {'前校', '验连', '来怎', 'validate'})

    def test_actual_retrieval_cannot_depend_on_legacy_cross_word_overlap(self):
        result = ask('你还记得晚上吃的什么吗', make_gates('吃了，红烧肉拌饭'))
        self.assertEqual(result['state'], 'suspected')
        self.assertIn('红烧肉拌饭', result['evidence']['quote'])
        self.assertEqual(result['evidence']['match_method'], 'lexical_units')
        self.assertFalse(set(result['searched']['query_tokens']) & {'上吃', '的什'})

    def test_imperative_is_a_clue_without_inventing_disk(self):
        # 2026-11-21 口径变更（主人裁定）：「我是不是说过要做某动作」时，
        # 开放的疑问对象（哪个盘）是待填项，原话能确认「说过清理」就算 found，
        # 但必须标成 meta_only（只说存在这句话），不能替「具体哪个盘」背书。
        result = ask('我上次是不是说过要清理哪个盘', make_gates('清理这个吧'))
        self.assertEqual(result['state'], 'found')
        self.assertEqual(result['evidence']['quote'], '清理这个吧')
        self.assertTrue(result['evidence'].get('meta_only'))

    def test_code_and_punctuation_retrieve_same_complete_words(self):
        result = ask('上次那个 `_validate()` 提前校验连线，后来怎么处理的？',
                     make_gates('_validate() 校验连线时逐边隔离'))
        self.assertEqual(result['state'], 'suspected')
        self.assertIn('校验连线', result['evidence']['quote'])
        self.assertFalse(set(result['searched']['query_units']) & {'前校', '验连', '来怎'})

    def test_no_promotion_of_paste_or_unknown(self):
        result = ask('你还记得晚上吃的什么吗', make_gates('吃了，红烧肉拌饭', source_clue=True))
        self.assertEqual(result['state'], 'absent')
        self.assertTrue(result['source_clues'])
        self.assertFalse(result.get('evidence'))

    def test_an_unrelated_object_still_does_not_trigger(self):
        self.assertEqual(ask('你知道怎么用飞梭给鲛人织网吗')['state'], 'not_checked')

    def test_generic_action_cannot_replace_a_named_object(self):
        self.assertFalse(lexical_match(plan('去北海道玩'), '我去问一下别人'))
        self.assertFalse(lexical_match(plan('马拉松跑了多少'), '你跑啊'))
        self.assertFalse(lexical_match(plan('家猫叫什么名字'), '我忘了名字'))
        self.assertFalse(lexical_match(plan('想让我放弃你'), '我让他评估'))

    def test_a_repeated_action_does_not_answer_why(self):
        result = ask('为什么之前不挂', make_gates('都没挂'))
        self.assertEqual(result['state'], 'absent')

    def test_dates_remain_independent_of_word_recovery(self):
        # 原问句「2026-11-13 晚上吃的什么」只对上别的日子的通用动作「吃」，
        # 无法定位所问那顿饭 —— 那种情况按判据应给 absent，不是 suspected。
        # 要测「日期独立于词面恢复」，问句里必须带上那顿饭本身（红烧肉拌饭），
        # 才能让另一日的原话沾边而时间对不上，从而落到 suspected。
        result = ask('2026-11-13 晚上我吃的是红烧肉拌饭吗', make_gates('吃了，红烧肉拌饭'))
        self.assertEqual(result['state'], 'suspected')
        self.assertIn('时间对不上', result['missing'])

    def test_missing_library_fails_explicitly_instead_of_empty_absent(self):
        from memory_core import gate1_recall_units as units
        original_import = __import__
        def missing(name, *args, **kwargs):
            if name == 'jieba':
                raise ImportError('isolated missing dependency')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=missing):
            with self.assertRaisesRegex(RuntimeError, 'gate1_tokenizer_missing'):
                units._segmenter.__wrapped__()


if __name__ == '__main__':
    unittest.main()
