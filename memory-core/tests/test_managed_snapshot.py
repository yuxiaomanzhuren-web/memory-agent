from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import threading

import pytest

from memory_core.memory_evidence import RETIRED_RAW, raw_session, scope_key
from memory_core.memory_pipeline import MemoryPipeline
from memory_core.rawlog import RawLog
from memory_core.store import Store


@pytest.fixture
def sample(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    db = source / "index.db"
    raw = source / "archive"
    store = Store(db)
    log = RawLog(raw)
    pipeline = MemoryPipeline(SimpleNamespace(store=store, raw=log, lock=threading.RLock()), model=lambda *_: None)
    scope = dict(bot_id="111", persona="synthetic-source", conv_key="private:222")
    foreign = dict(scope, persona="other-persona")
    target = dict(bot_id="333", persona="synthetic-target", conv_key="private:444")
    start = 1700000000.123
    ids = [
        log.append(
            raw_session(scope), role, text, start + pos, strict=True,
            extra=dict(ingest_source_kind="forward", observed_at="2023-11-15T07:13:20.123+09:00"),
        )
        for pos, (role, text) in enumerate([
            ("user", "SYNTHETIC \u539f\u6587 A\nsecond line"),
            ("user", "unapproved record in the same scope"),
            ("assistant", "SYNTHETIC original B"),
            ("user", "foreign persona with the same raw session"),
        ])
    ]
    pipeline.notice({**scope, "raw_ids": ids[:3]})
    pipeline.notice({**foreign, "raw_ids": ids[3:]})
    store.conn.execute("PRAGMA journal_mode=DELETE")
    store.conn.close()
    isolated = tmp_path / "isolated"
    area = isolated / "case" / "snapshot"
    area.mkdir(parents=True)
    request = dict(source_scope=scope, test_scope=target, area="production_snapshot_copy",
                   raw_ids=[ids[2], ids[0]])
    registry = dict(
        version=1, source_index=str(db), archive_root=str(raw), isolation_root=str(isolated),
        areas={"production_snapshot_copy": dict(test_scope=target, directory="case/snapshot")},
        approval=dict(request),
    )
    credential = "synthetic-scope-credential-only-000000000"
    creds = tmp_path / "scope.json"
    creds.write_text(json.dumps(dict(version=1, credentials=[
        dict(sha256=hashlib.sha256(credential.encode()).hexdigest(), scopes=[scope]),
    ])), encoding="utf-8")
    registry_path = tmp_path / "registration.json"
    monkeypatch.setenv("MEMORY_SCOPE_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("MEMORY_SNAPSHOT_REGISTRY_FILE", str(registry_path))
    monkeypatch.setenv("MEMORY_SNAPSHOT_SCOPE_CREDENTIAL", credential)
    return SimpleNamespace(db=db, raw=raw, area=area, ids=ids, request=request, registry=registry,
                           registry_path=registry_path, credential=credential)


def command(sample, request=None, script=None):
    sample.registry_path.write_text(json.dumps(sample.registry), encoding="utf-8")
    return run_cli(sample.request if request is None else request, script)


def run_cli(request, script=None):
    args = ["-c", script] if script else ["-m", "memory_core.managed_snapshot"]
    return subprocess.run(
        [sys.executable, *args], input=json.dumps(request),
        text=True, capture_output=True, check=False,
    )


def receipt(run, code=0):
    assert run.returncode == code
    assert not run.stderr
    return json.loads(run.stdout)


def originals(sample):
    return {
        json.loads(line)["id"]: line
        for shard in sample.raw.glob("*.jsonl")
        for line in shard.read_bytes().splitlines(keepends=True)
    }


def test_cli_preserves_bytes_scope_and_hash_and_retry(sample):
    before = {p: p.read_bytes() for p in (sample.db, *sample.raw.iterdir())}
    result = receipt(command(sample))
    assert set(result) == {"status", "record_count", "byte_count", "sha256"}
    assert result["status"] == "complete"
    assert result["record_count"] == 2
    (copy,) = sample.area.iterdir()
    assert copy.name == "production_snapshot_copy.jsonl"
    contents = copy.read_bytes()
    assert len(contents) == result["byte_count"]
    assert hashlib.sha256(contents).hexdigest() == result["sha256"]
    *lines, manifest = contents.splitlines(keepends=True)
    expected = originals(sample)
    assert lines == [expected[rid] for rid in sample.request["raw_ids"]]
    binding = json.loads(manifest)
    assert binding["source_scope"] == sample.request["source_scope"]
    assert binding["test_scope"] == sample.request["test_scope"]
    assert binding["area"] == sample.request["area"]
    assert binding["payload_sha256"] == hashlib.sha256(b"".join(lines)).hexdigest()
    assert [tags["ingest_source_kind"] for tags in binding["source_tags"]] == ["forward", "forward"]
    assert receipt(command(sample)) == result
    assert list(sample.area.iterdir()) == [copy]
    assert {p: p.read_bytes() for p in before} == before
    assert set(sample.db.parent.iterdir()) == {sample.db, sample.raw}


def test_archive_reads_only_approved_byte_ranges(sample, monkeypatch):
    from memory_core import managed_snapshot

    with sqlite3.connect(sample.db) as conn:
        locations = [
            conn.execute("SELECT file, offset, size FROM memory_pipeline_raw WHERE raw_id=?", (rid,)).fetchone()
            for rid in sample.request["raw_ids"]
        ]
    actual = []
    open_path = Path.open

    class TrackedReader:
        def __init__(self, file, stream):
            self.file, self.stream = file, stream

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def seek(self, offset):
            self.stream.seek(offset)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            actual.append((self.file, self.stream.tell(), size))
            return self.stream.read(size)

    def open_exact(path, mode="r", buffering=-1, **kwargs):
        stream = open_path(path, mode, buffering, **kwargs)
        if path.parent == sample.raw:
            assert mode == "rb" and buffering == 0
            return TrackedReader(path.name, stream)
        return stream

    monkeypatch.setattr(Path, "open", open_exact)
    assert managed_snapshot.export(sample.request, sample.registry, sample.credential)["record_count"] == 2
    assert actual == locations


@pytest.mark.parametrize("change", [
    "incomplete_source", "incomplete_test", "wrong_credential", "unregistered", "other_scope",
    "unapproved_id", "absolute", "parent", "alias", "overlaps_source",
])
def test_reject_before_opening_source(sample, tmp_path, monkeypatch, change):
    if change == "incomplete_source":
        sample.request["source_scope"] = {"bot_id": "111", "conv_key": "private:222"}
    elif change == "incomplete_test":
        sample.request["test_scope"] = {"bot_id": "333", "conv_key": "private:444"}
    elif change == "wrong_credential":
        monkeypatch.setenv("MEMORY_SNAPSHOT_SCOPE_CREDENTIAL", "x" * 40)
    elif change == "unregistered":
        sample.registry["areas"].clear()
    elif change == "other_scope":
        sample.registry["areas"]["production_snapshot_copy"]["test_scope"] = dict(
            sample.request["test_scope"], persona="other-target",
        )
    elif change == "unapproved_id":
        sample.request["raw_ids"] = [sample.ids[1]]
    else:
        area = sample.registry["areas"]["production_snapshot_copy"]
        if change == "absolute":
            area["directory"] = str(sample.area)
        elif change == "parent":
            area["directory"] = "../source"
        elif change == "overlaps_source":
            sample.registry["isolation_root"] = str(tmp_path)
            area["directory"] = "source"
        else:
            link = sample.area.parent / "alias"
            try:
                link.symlink_to(sample.raw, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(sample.raw)],
                               capture_output=True, check=True)
            area["directory"] = "case/alias"
    unavailable = "import sqlite3; sqlite3.connect = lambda *a,**k: (_ for _ in ()).throw(AssertionError());"
    result = receipt(command(sample, script=unavailable +
                             "from memory_core.managed_snapshot import main; raise SystemExit(main())"), 1)
    assert result["status"] == "rejected"
    assert list(sample.area.iterdir()) == []


@pytest.mark.parametrize("fault", ["missing", "foreign", "conflict", "retired", "bad_hash", "bad_time", "bad_role"])
def test_batch_failure_leaves_no_copy_and_can_retry(sample, fault):
    last = sample.ids[0]
    with sqlite3.connect(sample.db) as conn:
        saved = conn.execute("SELECT * FROM memory_pipeline_raw WHERE raw_id=?", (last,)).fetchone()
        if fault == "missing":
            conn.execute("DELETE FROM memory_pipeline_raw WHERE raw_id=?", (last,))
        elif fault == "foreign":
            conn.execute("UPDATE memory_pipeline_raw SET scope=? WHERE raw_id=?",
                         (scope_key(dict(sample.request["source_scope"], persona="other-persona")), last))
        elif fault == "conflict":
            conn.execute("INSERT INTO memory_pipeline_conflicts VALUES (?)", (last,))
        elif fault == "retired":
            conn.execute("UPDATE memory_pipeline_raw SET processed=? WHERE raw_id=?", (RETIRED_RAW, last))
        else:
            column, bad = {"bad_hash": ("sha256", "0" * 64), "bad_time": ("ts", 123),
                           "bad_role": ("role", "assistant")}[fault]
            conn.execute(f"UPDATE memory_pipeline_raw SET {column}=? WHERE raw_id=?", (bad, last))
    run = command(sample)
    outcome = receipt(run, 1)
    assert set(outcome) == {"status", "reason", "record_count"}
    assert outcome["record_count"] == 0
    assert all(rid not in run.stdout for rid in sample.ids)
    assert "SYNTHETIC" not in run.stdout
    assert list(sample.area.iterdir()) == []
    with sqlite3.connect(sample.db) as conn:
        conn.execute("DELETE FROM memory_pipeline_conflicts WHERE raw_id=?", (last,))
        conn.execute("INSERT OR REPLACE INTO memory_pipeline_raw VALUES (" + ",".join("?" * len(saved)) + ")", saved)
    assert receipt(command(sample))["record_count"] == 2
    assert len(list(sample.area.iterdir())) == 1


def test_process_exit_cleans_partial_write_and_retry_publishes_once(sample):
    script = """
import os
from memory_core import managed_snapshot
original = managed_snapshot._read_record
calls = 0
def stop_after_first(*args):
    global calls
    calls += 1
    if calls == 2:
        os._exit(73)
    return original(*args)
managed_snapshot._read_record = stop_after_first
raise SystemExit(managed_snapshot.main())
"""
    crash = command(sample, script=script)
    assert crash.returncode == 73
    assert list(sample.area.iterdir()) == []
    first = receipt(command(sample))
    assert receipt(command(sample)) == first
    assert len(list(sample.area.iterdir())) == 1


def test_existing_different_copy_is_never_overwritten(sample):
    copy = sample.area / "production_snapshot_copy.jsonl"
    copy.write_bytes(b"existing synthetic content")
    assert receipt(command(sample), 1)["reason"] == "copy_conflict"
    assert copy.read_bytes() == b"existing synthetic content"
    assert list(sample.area.iterdir()) == [copy]


def test_damaged_archive_bytes_fail_and_retry(sample):
    (shard,) = sample.raw.iterdir()
    saved = shard.read_bytes()
    shard.write_bytes(saved.replace(b"original B", b"original C", 1))
    assert receipt(command(sample), 1)["reason"] == "record_invalid"
    assert list(sample.area.iterdir()) == []
    shard.write_bytes(saved)
    assert receipt(command(sample))["record_count"] == 2


def test_closed_wal_index_does_not_create_sidecars(sample):
    with sqlite3.connect(sample.db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    assert receipt(command(sample), 1)["reason"] == "index_unavailable"
    assert set(sample.db.parent.iterdir()) == {sample.db, sample.raw}
    assert list(sample.area.iterdir()) == []


def test_active_wal_index_checks_committed_state(sample):
    with sqlite3.connect(sample.db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO memory_pipeline_conflicts VALUES (?)", (sample.ids[0],))
        conn.commit()
        wal = sample.db.with_name(sample.db.name + "-wal")
        before = {path: path.read_bytes() for path in (sample.db, wal)}
        assert receipt(command(sample), 1)["reason"] == "record_unavailable"
        assert list(sample.area.iterdir()) == []
        assert {path: path.read_bytes() for path in before} == before
        conn.execute("DELETE FROM memory_pipeline_conflicts")
        conn.commit()
        assert receipt(command(sample))["record_count"] == 2
    conn.close()


def test_existing_hardlink_cannot_act_as_registered_copy(sample, tmp_path):
    outside = tmp_path / "other-data"
    outside.write_bytes(b"outside")
    os.link(outside, sample.area / "production_snapshot_copy.jsonl")
    assert receipt(command(sample), 1)["reason"] == "unsafe_path"
    assert outside.read_bytes() == b"outside"


def test_concurrent_attempts_leave_one_verified_copy(sample):
    sample.registry_path.write_text(json.dumps(sample.registry), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(run_cli, [sample.request, sample.request]))
    first = next(receipt(run) for run in runs if run.returncode == 0)
    assert receipt(command(sample)) == first
    (copy,) = sample.area.iterdir()
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == first["sha256"]
