"""Regression checks for the one citation validator."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from memory_core.citation import check_reply


RAW = "会议周五下午开始"
FAKE = "每人先交一千元"
GOOD = f"你之前说过「{RAW}」"
BAD = f"你之前说过「{FAKE}」"


def source(raw_id, quote, role="user", **tags):
    return dict(raw_id=raw_id, quote=quote, role=role, **{
        "stance": "他说" if role == "user" else "助手自述",
        "support_scope": "可作主张依据", **tags,
    })


LIST = {"allowed_raw_ids": ["official"], "sources": [source("official", RAW)]}


class CitationTests(unittest.TestCase):
    def test_exact_quote_and_normal_reply(self):
        for text in (GOOD, "我想喝水。"):
            self.assertTrue(check_reply(text, LIST)["allowed"])

    def test_ids_and_contiguous_quote_required(self):
        snapshot = deepcopy(LIST)
        snapshot["sources"].append(source("hidden", FAKE))
        self.assertEqual(check_reply(BAD, snapshot)["violations"][0]["reason"], "no_allowed_raw_match")
        snapshot["allowed_raw_ids"].append("hidden")
        self.assertFalse(check_reply(f"你之前说过「{RAW}，{FAKE}」", snapshot)["allowed"])

    def test_strip_only_the_bad_citation_and_recheck(self):
        text = f"🫖我上一句说错了，{BAD}。{GOOD}。我先准备场地。"
        verdict = check_reply(text, LIST)
        self.assertFalse(verdict["allowed"])
        self.assertNotIn(FAKE, verdict["stripped"])
        self.assertIn("我上一句说错了", verdict["stripped"])
        self.assertIn(GOOD, verdict["stripped"])
        self.assertIn("我先准备场地", verdict["stripped"])
        self.assertTrue(check_reply(verdict["stripped"], LIST)["allowed"])

    def test_quote_does_not_approve_an_attached_claim(self):
        for text in (GOOD + "说明你答应给我买船。", GOOD + "，另外你答应给我买船。"):
            verdict = check_reply(text, LIST)
            self.assertFalse(verdict["allowed"])
            if verdict["stripped"]:
                self.assertNotIn("买船", verdict["stripped"])
        self.assertEqual(check_reply(GOOD + "说明你答应给我买船。", LIST)["violations"][0]["reason"],
                         "claim_object_mismatch")

    def test_assistant_and_user_roles_are_independent(self):
        own = "我会提前安排会场"
        snapshot = {"allowed_raw_ids": ["official", "self"],
                    "sources": [*LIST["sources"], source("self", own, "assistant")]}
        self.assertTrue(check_reply(f"{GOOD}。我刚才说过「{own}」", snapshot)["allowed"])
        self.assertFalse(check_reply(f"你之前说过「{own}」", snapshot)["allowed"])

    def test_meta_only_stays_restricted_under_a_transcript_alias(self):
        first = source("official", RAW, meta_only=True)
        second = source("history", RAW, source_kind="transcript", archive_raw_id="official")
        snapshot = {"allowed_raw_ids": ["official", "history"], "sources": [first, second]}
        self.assertTrue(check_reply(GOOD, snapshot)["allowed"])
        self.assertEqual(check_reply(GOOD + "所以会议周五开始", snapshot)["violations"][0]["reason"],
                         "meta_only_cannot_support_claim")

    def test_missing_labels_do_not_upgrade_the_source_or_do_io(self):
        snapshot = {"allowed_raw_ids": ["official"], "sources": [
            {"raw_id": "official", "role": "user", "quote": RAW},
        ]}
        with patch("builtins.open", side_effect=AssertionError("I/O")), patch("builtins.print") as p:
            verdict = check_reply(GOOD, snapshot)
        p.assert_not_called()
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["violations"][0]["reason"], "source_requires_qualification")

    def test_qualified_clue_stays_weaker_when_mixed_with_an_authoritative_source(self):
        clue = source("clue", FAKE, stance="他转述", support_scope="仅可作线索")
        snapshot = {"allowed_raw_ids": ["official", "clue"], "sources": [*LIST["sources"], clue]}
        mixed = check_reply(f"{GOOD}。仅作线索，{BAD}", snapshot)
        self.assertTrue(mixed["allowed"])
        self.assertEqual(mixed["source_state"], "suspected")

    def test_malformed_snapshot_and_foreign_source_fail_closed(self):
        self.assertEqual(check_reply("我想喝水。", {})["violations"][0]["reason"], "invalid_snapshot")
        snapshot = {**LIST, "scope": {"conversation_id": "one"}, "sources": [
            {**LIST["sources"][0], "scope": {"conversation_id": "other"}},
        ]}
        self.assertFalse(check_reply(GOOD, snapshot)["allowed"])


if __name__ == "__main__":
    unittest.main()
