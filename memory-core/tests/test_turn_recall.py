"""Evidence/turn recall contracts; all archive and SQLite fixtures stay local."""
import hashlib
import importlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from memory_core.store import Store


def optional_module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None


evidence = optional_module("memory_core.memory_evidence")
turn = optional_module("memory_core.turn_recall")
SCOPE = {"bot_id": "10001", "persona": "小满", "conv_key": "private:20002"}
KEY = '{"bot_id":"10001","conv_key":"private:20002","persona":"小满"}'
SESSION = "10001|private:20002"
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 11, 0, 15, tzinfo=TZ).timestamp()
TODAY = datetime(2026, 11, 11, 0, 5, tzinfo=TZ).timestamp()
YESTERDAY = datetime(2026, 11, 10, 23, 59, tzinfo=TZ).timestamp()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(evidence, "Task1 evidence helpers are not implemented")
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "raw"
        self.root.mkdir()
        self.db = Store(Path(self.tmp.name) / "brain.db")
        self.addCleanup(self.db.close)
        self.db.conn.executescript("""
            CREATE TABLE memory_pipeline_owners(memory_id INTEGER PRIMARY KEY, scope TEXT);
            CREATE TABLE memory_pipeline_raw(
                raw_id TEXT PRIMARY KEY, session TEXT, role TEXT, ts REAL,
                file TEXT, offset INTEGER, size INTEGER, sha256 TEXT,
                scope TEXT DEFAULT '', historic INTEGER, processed INTEGER DEFAULT 0
            );
        """)
        self.db.conn.commit()
        self.memory = SimpleNamespace(store=self.db, raw=SimpleNamespace(root=str(self.root)))

    def raw(self, rid="raw-1", text="今天一起研究量子计算", ts=TODAY,
            session=SESSION, role="user", file="2026-11.jsonl", assign=False,
            scope=KEY, complete=True):
        rec = {"id": rid, "ts": ts, "s": session, "r": role, "t": text}
        path = self.root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = (json.dumps(rec, ensure_ascii=False) + ("\n" if complete else "")).encode("utf-8")
        offset = path.stat().st_size if path.exists() else 0
        with path.open("ab") as stream:
            stream.write(blob)
        if assign:
            self.db.conn.execute(
                "INSERT INTO memory_pipeline_raw VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rid, session, role, ts, file, offset, len(blob),
                 hashlib.sha256(blob).hexdigest(), scope, 0, 0),
            )
            self.db.conn.commit()
        return {"raw_id": rid, "raw_ts": ts, "quote": text}

    def remember(self, text="一起研究量子计算", refs=None, scope=SCOPE, importance=0.5,
                 created=TODAY, legacy=False, source="", status="active"):
        meta = {}
        if scope is not None:
            meta["pipeline_scope"] = scope
        if refs is not None:
            if legacy:
                meta.update(refs[0])
            else:
                meta["source_refs"] = refs
        mid = self.db.insert_memory(text=text, importance=importance, created_at=created, source=source, meta=meta)
        if status != "active":
            self.db.update_memory(mid, status=status)
        return mid

    def adopt(self, mid, scope=KEY):
        self.db.conn.execute("INSERT INTO memory_pipeline_owners VALUES(?,?)", (mid, scope))
        self.db.conn.commit()

    def recall(self, text="量子计算", **extra):
        self.assertIsNotNone(turn, "Task1 turn recall is not implemented")
        with patch.object(turn.time, "time", return_value=NOW):
            return turn.TurnRecall(self.memory).recall({**SCOPE, "text": text, **extra})

    def nohit(self, out):
        self.assertEqual(out["hits"], [], out)
        self.assertEqual(out["block"], "", out)


class EvidenceTests(Fixture):
    def test_scope_canonical_key_and_session(self):
        scope = evidence.scope_from({**SCOPE, "bot_id": 10001, "text": "ignored"})
        self.assertEqual(scope, SCOPE)
        self.assertEqual(evidence.scope_key(scope), KEY)
        self.assertEqual(evidence.raw_session(scope), SESSION)

    def test_invalid_scope_rejected(self):
        for field, value in (
            ("bot_id", "1x"), ("bot_id", True), ("bot_id", 1.5), ("bot_id", "0"),
            ("persona", " "), ("persona", "a" * 121), ("persona", 123),
            ("conv_key", "private"), ("conv_key", "private:2|other"), ("conv_key", "group:0"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                evidence.scope_from({**SCOPE, field: value})

    def test_binary_provenance_and_original_fields(self):
        self.raw(rid="first", text="保留原话\n以及换行", file="old.jsonl")
        self.raw(rid="second", text="第二行", ts=YESTERDAY, file="old.jsonl")
        self.raw(rid="third", file="nested/older.jsonl")
        path = self.root / "old.jsonl"
        before = path.read_bytes()
        records = evidence.raw_records(self.root)
        self.assertEqual(set(records), {"first", "second", "third"})
        rec = records["second"]
        blob = before[rec["offset"]:rec["offset"] + rec["size"]]
        self.assertEqual(rec["id"], "second")
        self.assertEqual((rec["ts"], rec["s"], rec["r"], rec["t"]),
                         (YESTERDAY, SESSION, "user", "第二行"))
        self.assertEqual(rec["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual((self.root / rec["file"]).resolve(), path.resolve())
        self.assertEqual(path.read_bytes(), before)

    def test_incomplete_valid_json_tail_is_not_evidence(self):
        self.raw(rid="good")
        self.raw(rid="incomplete", complete=False)
        self.assertEqual(set(evidence.raw_records(self.root)), {"good"})
        with self.assertRaises(ValueError):
            evidence.verify_ref(self.root, SCOPE, {"raw_id": "incomplete", "quote": "量子计算"})

    def test_malformed_complete_line_does_not_hide_later_evidence(self):
        (self.root / "2026-11.jsonl").write_bytes(b'{bad json}\n')
        self.raw()
        self.assertEqual(set(evidence.raw_records(self.root)), {"raw-1"})

    def test_exact_session_filter(self):
        self.raw(rid="mine")
        self.raw(rid="other", session="10001|private:200020")
        self.assertEqual(set(evidence.raw_records(self.root, SESSION)), {"mine"})

    def test_conflicting_duplicate_ids_rejected_even_across_sessions(self):
        self.raw(rid="same", file="a.jsonl")
        self.raw(rid="same", text="different", session="9|group:1", file="b.jsonl")
        with self.assertRaisesRegex(ValueError, "duplicate|conflict"):
            evidence.raw_records(self.root, SESSION)

    def test_identical_duplicate_ids_have_stable_provenance(self):
        self.raw(file="a.jsonl")
        self.raw(file="b.jsonl")
        self.assertEqual(evidence.raw_records(self.root)["raw-1"]["file"], "a.jsonl")

    def test_parsed_equal_duplicate_with_different_raw_bytes_is_a_conflict(self):
        self.raw(file="a.jsonl")
        blob = (self.root / "a.jsonl").read_bytes()
        (self.root / "b.jsonl").write_bytes(blob.replace(b'"id": ', b'"id":  '))
        with self.assertRaisesRegex(ValueError, "duplicate|conflict"):
            evidence.raw_records(self.root)

    def test_verify_exact_quote_and_canonical_provenance(self):
        self.raw(text="  原样的量子计算  ")
        ref = evidence.verify_ref(self.root, SCOPE, {"raw_id": "raw-1", "quote": "  原样"})
        self.assertEqual(ref["quote"], "  原样")
        self.assertEqual(ref["raw_ts"], TODAY)
        self.assertEqual(ref["role"], "user")
        # 来源资格/立场字段是后加的（判「谁说的、站在什么立场」用），必须一起钉住：
        # 少一个就说明溯源信息退化了，多一个就说明有人往里塞了没约定的字段。
        self.assertEqual(set(ref), {"raw_id", "raw_ts", "quote", "role", "raw_file", "raw_offset",
                                    "raw_sha256", "send_role", "stance", "stance_reason",
                                    "support_scope", "classifier_version"})
        self.assertEqual(evidence.verify_ref(self.root, SCOPE, ref), ref)

    def test_verify_rejects_fabrication_wrong_session_time_and_provenance(self):
        ref = self.raw()
        for change in (
            {"quote": "量子 计算"}, {"quote": ""}, {"quote": " "},
            {"raw_id": "missing"}, {"raw_ts": YESTERDAY}, {"role": "assistant"},
            {"raw_file": "other.jsonl"}, {"raw_offset": 1}, {"raw_sha256": "fake"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                evidence.verify_ref(self.root, SCOPE, {**ref, **change})
        with self.assertRaises(ValueError):
            evidence.verify_ref(self.root, {**SCOPE, "bot_id": "10002"}, ref)

    def test_owner_is_explicit_and_exact(self):
        mid = self.remember(scope=None, source="qq:10001|private:20002")
        row = self.db.get_memory(mid)
        self.assertFalse(evidence.owns_memory(self.memory, row, SCOPE))
        self.adopt(mid)
        self.assertTrue(evidence.owns_memory(self.memory, row, SCOPE))
        for field, value in (("persona", "other"), ("bot_id", "10002"), ("conv_key", "group:20002")):
            self.assertFalse(evidence.owns_memory(self.memory, row, {**SCOPE, field: value}))

    def test_metadata_owner_must_match_without_legacy_table(self):
        mid = self.remember()
        self.db.conn.execute("DROP TABLE memory_pipeline_owners")
        self.assertTrue(evidence.owns_memory(self.memory, self.db.get_memory(mid), SCOPE))
        self.assertFalse(evidence.owns_memory(self.memory, self.db.get_memory(mid), {**SCOPE, "persona": "other"}))

    def test_mismatched_metadata_not_overridden_by_owner_row(self):
        mid = self.remember(scope={**SCOPE, "persona": "other"})
        self.adopt(mid)
        self.assertFalse(evidence.owns_memory(self.memory, self.db.get_memory(mid), SCOPE))


class RecallTests(Fixture):
    def test_existing_narrow_synonyms_keep_the_source_gate(self):
        ref = self.raw(text="今天修好了记忆混乱的问题", assign=True)
        self.remember("修好了记忆混乱的问题", [ref])
        out = self.recall("记忆串")
        self.assertEqual(len(out['hits']), 1)
        self.assertIn('同义词回查', out['hits'][0]['why'])
        self.nohit(self.recall("记忆串", persona="other"))

    def test_unrelated_max_importance_is_not_a_hit(self):
        ref = self.raw(text="春天种植兰花")
        self.remember("春天种植兰花", [ref], importance=1.0)
        self.nohit(self.recall("量子计算"))

    def test_strong_overlap_gates_before_importance(self):
        unrelated = self.raw(rid="unrelated", text="春天种植兰花", assign=True)
        relevant = self.raw(rid="relevant", text="量子计算需要量子比特", assign=True)
        self.remember("春天种植兰花", [unrelated], importance=1.0)
        mid = self.remember("量子计算需要量子比特", [relevant], importance=0.01)
        out = self.recall()
        self.assertEqual([hit["id"] for hit in out["hits"]], [mid])
        self.assertIn("量子计算", out["block"])
        self.assertTrue(out["hits"][0]["why"])
        self.assertEqual(out["hits"][0]["source_refs"][0]["raw_id"], "relevant")

    def test_english_overlap_uses_words_not_character_fragments(self):
        ref = self.raw(text="The office timetable is finished", assign=True)
        self.remember("The office timetable is finished", [ref], importance=1.0)
        self.nohit(self.recall("coffee"))
        self.assertEqual(len(self.recall("timetable")["hits"]), 1)

    def test_generic_recall_question_does_not_match_generic_text(self):
        ref = self.raw(text="你还记得我说过什么吗")
        self.remember("你还记得我说过什么吗", [ref], importance=1.0)
        self.nohit(self.recall("你还记得量子计算吗"))
        self.nohit(self.recall("还记得吗"))

    def test_unowned_unbacked_invalid_and_inactive_memories_are_excluded(self):
        ref = self.raw()
        self.remember(refs=[ref], scope=None, source="qq:10001|private:20002")
        self.remember(refs=None)
        self.remember(refs=[{**ref, "quote": "编造的量子计算"}])
        self.remember(refs=[ref], status="archived")
        self.nohit(self.recall())

    def test_same_raw_session_different_persona_is_not_ownership(self):
        ref = self.raw(assign=True)
        mid = self.remember(refs=[ref])
        self.assertEqual(self.recall()["hits"][0]["id"], mid)
        for field, value in (("persona", "other"), ("bot_id", "10002"), ("conv_key", "group:20002")):
            self.nohit(self.recall(**{field: value}))
            self.nohit(self.recall("今天聊了什么", **{field: value}))

    def test_legacy_1034_requires_explicit_adoption(self):
        ref = self.raw(assign=True, scope="")
        mid = self.remember(refs=[ref], scope=None, legacy=True, source="qq:10001|private:20002")
        # Preserve the production legacy identifier without opening the live DB.
        self.db.conn.execute("UPDATE memories SET id=1034 WHERE id=?", (mid,))
        self.db.conn.commit()
        self.nohit(self.recall())
        self.adopt(1034)
        self.nohit(self.recall())
        self.db.conn.execute("UPDATE memory_pipeline_raw SET scope=?", (KEY,))
        self.db.conn.commit()
        out = self.recall()
        self.assertEqual(out["hits"][0]["id"], 1034)
        self.assertEqual(out["hits"][0]["source_refs"][0]["raw_id"], "raw-1")

    def test_max_three_and_deterministic_reasons(self):
        for i in range(5):
            ref = self.raw(rid=f"raw-{i}", text=f"量子计算研究记录{i}", assign=True)
            self.remember(f"量子计算研究记录{i}", [ref])
        first = self.recall(limit=100, k=100)
        self.assertEqual(len(first["hits"]), 3)
        self.assertEqual(first, self.recall(limit=100, k=100))

    def test_general_query_never_falls_back_to_raw_rows(self):
        self.raw(assign=True)
        self.nohit(self.recall())

    def test_today_and_yesterday_follow_source_time_in_shanghai(self):
        old = self.raw(rid="old", ts=YESTERDAY, assign=True)
        new = self.raw(rid="new", ts=TODAY, assign=True)
        old_mid = self.remember(refs=[old], created=TODAY)
        new_mid = self.remember(refs=[new], created=YESTERDAY)
        today = self.recall("今天量子计算")
        yesterday = self.recall("昨天量子计算")
        self.assertEqual([h["id"] for h in today["hits"]], [new_mid])
        self.assertEqual([h["id"] for h in yesterday["hits"]], [old_mid])
        self.assertEqual(today["hits"][0]["source_refs"][0]["raw_ts"], TODAY)
        self.assertEqual(self.recall("2026-11-10 量子计算")["hits"], yesterday["hits"])

    def test_date_only_request_reads_assigned_raw_without_a_memory(self):
        self.raw(rid="mine", assign=True)
        self.raw(rid="unassigned")
        self.raw(rid="other-persona", assign=True, scope=KEY.replace("小满", "other"))
        self.raw(rid="yesterday", assign=True, ts=YESTERDAY)
        out = self.recall("今天聊了什么")
        self.assertEqual(len(out["hits"]), 1)
        self.assertEqual(out["hits"][0]["source_refs"][0]["raw_id"], "mine")
        self.assertEqual(self.recall("today")["hits"], out["hits"])

    def test_date_path_does_not_scan_unassigned_raw(self):
        self.raw(rid="mine", assign=True, file="owned.jsonl")
        self.raw(rid="other", file="unassigned.jsonl")
        opened = []
        original = Path.open

        def guarded(path, *args, **kwargs):
            if path.suffix == ".jsonl":
                opened.append(path.name)
                self.assertEqual(path.name, "owned.jsonl")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            out = self.recall("今天")
        self.assertTrue(out["hits"])
        self.assertTrue(opened)

    def test_date_index_is_required_even_for_owned_memory_refs(self):
        ref = self.raw()
        self.remember(refs=[ref])
        self.nohit(self.recall("今天量子计算"))
        self.db.conn.execute("DROP TABLE memory_pipeline_raw")
        self.nohit(self.recall("今天"))

    def test_date_topic_is_still_a_hard_relevance_gate(self):
        ref = self.raw(text="春天种植兰花", assign=True)
        self.remember("春天种植兰花", [ref], importance=1.0)
        self.nohit(self.recall("今天量子计算"))

    def test_out_of_date_sources_are_not_returned_in_refs(self):
        old = self.raw(rid="old", ts=YESTERDAY, assign=True)
        new = self.raw(rid="new", ts=TODAY, assign=True)
        self.remember(refs=[old, new])
        refs = self.recall("今天量子计算")["hits"][0]["source_refs"]
        self.assertEqual([r["raw_id"] for r in refs], ["new"])

    def test_corrupt_index_hash_or_scope_never_reads_as_owned_evidence(self):
        self.raw(assign=True)
        self.db.conn.execute("UPDATE memory_pipeline_raw SET sha256='fabricated'")
        self.nohit(self.recall("今天"))
        self.db.conn.execute("UPDATE memory_pipeline_raw SET file='../outside.jsonl'")
        self.nohit(self.recall("今天"))

    def test_index_cannot_relabel_a_wrong_session(self):
        self.raw(session="10002|private:20002", assign=True)
        self.db.conn.execute("UPDATE memory_pipeline_raw SET session=?", (SESSION,))
        self.nohit(self.recall("今天"))

    def test_nohit_and_errors_are_empty_without_side_effects(self):
        self.nohit(self.recall(""))
        self.nohit(self.recall(persona=""))
        self.nohit(self.recall("2026-99-99"))
        self.db.conn.execute("DROP TABLE memories")
        self.nohit(self.recall())

    def test_date_speaker_intent_filters_user_and_assistant(self):
        self.raw(rid="user", text="我喜欢量子计算", ts=YESTERDAY, assign=True)
        self.raw(rid="assistant", text="我会继续研究量子计算", role="assistant", ts=YESTERDAY, assign=True)
        for query, rid, role in (("我昨天跟你说过什么", "user", "user"),
                                 ("你昨天跟我说过什么", "assistant", "assistant")):
            with self.subTest(query=query):
                out = self.recall(query)
                self.assertEqual([r["raw_id"] for h in out["hits"] for r in h["source_refs"]], [rid])
                self.assertEqual(out["hits"][0]["source_role"], role)

    def test_date_speaker_filters_memory_evidence_and_preserves_self_review_role(self):
        user = self.raw(rid="user", text="量子计算需要学习", ts=YESTERDAY, assign=True)
        assistant = self.raw(rid="assistant", text="量子计算我之前没讲清楚", role="assistant", ts=YESTERDAY, assign=True)
        self.remember(refs=[user])
        mid = self.remember(refs=[assistant])
        meta = self.db.get_memory(mid)["meta"]
        meta.update(source_kind="self_review", source_role="assistant")
        self.db.set_meta(mid, meta)
        out = self.recall("你昨天跟我说过什么")
        self.assertEqual([h["id"] for h in out["hits"]], [mid])
        self.assertEqual(out["hits"][0]["source_role"], "assistant")
        self.assertEqual(out["hits"][0]["source_kind"], "self_review")
        self.assertNotIn(mid, [h["id"] for h in self.recall("我昨天跟你说过什么")["hits"]])

    def test_metadata_cannot_claim_a_different_personas_raw_even_with_same_session(self):
        ref = self.raw(assign=True, scope=KEY.replace("小满", "other"))
        self.remember(refs=[ref])
        self.nohit(self.recall())

    def test_same_text_different_persona_returns_only_the_owned_evidence(self):
        mine = self.raw(rid="mine", assign=True)
        other = self.raw(rid="other", assign=True, scope=KEY.replace("小满", "other"))
        mid = self.remember(refs=[mine])
        self.remember(refs=[other], scope={**SCOPE, "persona": "other"})
        self.remember(refs=[other])  # Forged metadata must not bridge personas.
        out = self.recall()
        self.assertEqual([h["id"] for h in out["hits"]], [mid])
        self.assertEqual(out["hits"][0]["source_refs"][0]["raw_id"], "mine")

    def test_source_quote_must_support_relevance_not_just_generated_summary(self):
        ref = self.raw(text="今天春天种植兰花", assign=True)
        self.remember("量子计算研究进展", [ref], importance=1.0)
        self.nohit(self.recall("量子计算"))
        self.nohit(self.recall("今天量子计算"))

    def test_relevance_outside_explicit_quote_cannot_support_memory(self):
        ref = self.raw(text="种植兰花。另有量子计算的话题。", assign=True)
        self.remember("量子计算研究进展", [{**ref, "quote": "种植兰花"}])
        self.nohit(self.recall("量子计算"))

    def test_actual_source_quote_can_match_without_generated_summary_overlap(self):
        ref = self.raw(text="量子计算需要量子比特", assign=True)
        mid = self.remember("一次有依据的学习交流", [ref])
        self.assertEqual([h["id"] for h in self.recall()["hits"]], [mid])

    def test_human_reason_names_matched_source_terms_and_source_date_in_chinese(self):
        ref = self.raw(assign=True)
        self.remember(refs=[ref])
        why = self.recall("今天量子计算")["hits"][0]["why"]
        self.assertIn("原话", why)
        self.assertIn("2026-11-11", why)
        self.assertIn("计算", why)

    def test_raw_date_context_and_quotes_are_bounded_exact_excerpts(self):
        texts = {}
        for i in range(5):
            text = "普通天气" * 1700 + f"最后讨论量子计算{i}"
            texts[f"raw-{i}"] = text
            self.raw(rid=f"raw-{i}", text=text, assign=True)
        out = self.recall("今天量子计算")
        self.assertEqual(len(out["hits"]), 3)
        quote_chars = 0
        for hit in out["hits"]:
            self.assertLessEqual(len(hit["text"]), 240)
            for ref in hit["source_refs"]:
                self.assertLessEqual(len(ref["quote"]), 240)
                self.assertIn(ref["quote"], texts[ref["raw_id"]])
                self.assertIn("量子计算", ref["quote"])
                quote_chars += len(ref["quote"])
        self.assertLessEqual(len(out["block"]) + quote_chars, 1500)

    def test_unrelated_no_match_is_distinct_from_database_outage(self):
        ref = self.raw(text="种植兰花", assign=True)
        self.remember("种植兰花", [ref], importance=1.0)
        out = self.recall()
        self.nohit(out)
        self.assertNotIn("error", out)
        self.db.conn.execute("DROP TABLE memories")
        error = self.recall()
        self.nohit(error)
        self.assertTrue(error.get("error"))
        self.assertNotEqual(error["why"], out["why"])

    def test_recall_is_read_only_for_raw_database_and_access_counters(self):
        ref = self.raw(assign=True)
        mid = self.remember(refs=[ref])
        before_row = self.db.get_memory(mid)
        before_bytes = (self.root / "2026-11.jsonl").read_bytes()
        before_changes = self.db.conn.total_changes
        before_tables = list(self.db.conn.execute("SELECT name FROM sqlite_master ORDER BY name"))
        self.assertTrue(self.recall()["hits"])
        self.assertTrue(self.recall("今天量子计算")["hits"])
        self.assertEqual(self.db.get_memory(mid), before_row)
        self.assertEqual(self.db.conn.total_changes, before_changes)
        # sqlite3.Row 没有 __eq__，直接比两个 list 等于比对象身份——必须转成 tuple 才能比内容。
        self.assertEqual([tuple(r) for r in self.db.conn.execute("SELECT name FROM sqlite_master ORDER BY name")],
                         [tuple(r) for r in before_tables])
        self.assertEqual((self.root / "2026-11.jsonl").read_bytes(), before_bytes)


if __name__ == "__main__":
    unittest.main()