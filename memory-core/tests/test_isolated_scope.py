"""MEMO-33 acceptance using generated scopes and local synthetic data only."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from memory_core import isolated_scope as cleanup
from memory_core.adapters.ingest import AgentRequestError
from memory_core.adapters.runtime import open_memory
from memory_core.memory_evidence import agent_conversation


OPERATOR = "isolated-operator-fixture-" + uuid4().hex
INGRESS = "isolated-ingress-fixture-" + uuid4().hex
INDUSTRIAL, COPY = "industrial_synthetic", "production_snapshot_copy"


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_ISOLATED_OPERATOR_TOKEN", OPERATOR)
    monkeypatch.setenv("MEMORY_AGENT_HTTP_TOKEN", INGRESS)
    root = tmp_path / "instance"
    enrollment = cleanup.initialize(root, OPERATOR)
    assert enrollment["areas"] == [INDUSTRIAL, COPY]
    targets = {area: {"instance_id": enrollment["instance_id"], "scope": enrollment["scope"], "area": area}
               for area in enrollment["areas"]}
    return root, targets, cleanup.ManagedInstance(root, OPERATOR)


def cli(root, command, request, *, extra=(), check=True):
    completed = subprocess.run([sys.executable, "-m", "memory_core.isolated_scope", "--root", str(root),
                                command, *extra], input=json.dumps(request), encoding="utf-8",
                               capture_output=True, timeout=20, check=check)
    return completed.returncode, json.loads(completed.stdout)


def post(url, path, data):
    request = Request(url + path, json.dumps(data).encode(),
                      {"Authorization": f"Bearer {INGRESS}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        with exc:
            return json.load(exc)


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.03)


@pytest.fixture
def serve():
    processes = []

    def start(root, target, hook=None):
        args = ["--root", str(root), "serve", "--port", "0"]
        entry = ["-m", "memory_core.isolated_scope"] if hook is None else ["-c", hook]
        proc = subprocess.Popen([sys.executable, *entry, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, encoding="utf-8")
        processes.append(proc)
        with proc.stdin:
            proc.stdin.write(json.dumps(target))
        lines = []
        threading.Thread(target=lambda: lines.append(proc.stdout.readline()), daemon=True).start()
        wait_for(lambda: lines)
        assert lines[0], proc.stderr.read() if proc.poll() is not None else "missing readiness"
        return proc, json.loads(lines[0])["url"]

    yield start
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


def remember(url, conversation, text, key):
    return post(url, "/ingest", {"conversation": conversation, "text": text, "speaker": "user",
                                 "at": "2025-04-01T00:00:00Z", "source_kind": "direct", "message_id": key})


def new_request(destination):
    return {**destination, "request_id": str(uuid4())}


EXTRAS = ("snapshots/fixture.json", "indexes/mock.index", "logs/fixture.log", "extra.sqlite3-journal")


def populate_extra(area):
    for relative in EXTRAS:
        path = area / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text("synthetic fixture", encoding="utf-8")


def test_full_scope_authorization_and_one_area_per_request(enrolled):
    root, targets, manager = enrolled
    with pytest.raises(cleanup.ScopeCleanupError, match="unauthorized"):
        cleanup.ManagedInstance(root, "different-operator-token-" + uuid4().hex)
    with pytest.raises(cleanup.ScopeCleanupError, match="root_already_exists"):
        cleanup.initialize(root, OPERATOR)
    original = targets[INDUSTRIAL]
    with manager.open_area(original) as area:
        sentinel = area / "sentinel"
        sentinel.touch()
    attempts = [
        ({"scope": {"bot_id": original["scope"]["bot_id"]}}, "invalid_scope"),
        ({"scope": {**original["scope"], "extra": True}}, "invalid_scope"),
        ({"scope": {"bot_id": "123", "persona": "production-fixture", "conv_key": "private:456"}},
         "target_not_registered"),
        ({"scope": {**original["scope"], "conv_key": "private:unenrolled"}}, "target_not_registered"),
        ({"instance_id": str(uuid4())}, "target_not_registered"),
        ({"area": "../" + COPY}, "target_not_registered"),
        ({"area": [INDUSTRIAL, COPY]}, "target_not_registered"),
        ({"path": str(sentinel.parent)}, "target_required"),
    ]
    for override, error in attempts:
        with pytest.raises(cleanup.ScopeCleanupError, match="^" + error + "$"):
            manager.delete(new_request({**original, **override}))
        assert sentinel.exists()


def test_two_independent_areas_return_matching_redacted_receipts(enrolled, serve):
    root, targets, manager = enrolled
    conversation = agent_conversation(targets[INDUSTRIAL]["scope"])
    labels = {INDUSTRIAL: "synthetic pump original", COPY: "synthetic snapshot stand-in"}
    services = {}
    for name, destination in targets.items():
        with manager.open_area(destination) as directory:
            populate_extra(directory)
        services[name] = serve(root, destination)
        _, url = services[name]
        assert remember(url, conversation, labels[name], name)["message_id"] == name
        assert [r["text"] for r in post(url, "/recent", {"conversation": conversation})["items"]] == [labels[name]]

    _, other_url = services[COPY]
    before = post(other_url, "/recent", {"conversation": conversation})
    pairs = []
    for name, destination in targets.items():
        request = new_request(destination)
        _, recorded = cli(root, "delete", request)
        audit, receipt = recorded["audit"], recorded["receipt"]
        assert audit["status"] == receipt["status"] == "complete"
        for key in ("request_id", "target", "status", "deleted_files", "verified_at", "remaining"):
            assert audit[key] == receipt[key]
        assert receipt["target"]["area"] == name
        assert len(receipt["target"]["scope_sha256"]) == 64
        assert receipt["verified_at"] >= audit["requested_at"]
        assert receipt["remaining"] == {"storage": 0, "index_entries": 0}
        for kind in ("originals", "sqlite", "indexes", "snapshots", "logs"):
            assert receipt["deleted_files"][kind] > 0
        assert services[name][0].wait(timeout=8) == 0
        area_root = root / "areas" / name
        assert not area_root.exists() and not (area_root / "agent.sqlite3").exists()
        assert not (area_root / "indexes").exists()
        assert cli(root, "status", request)[1] == recorded
        assert cli(root, "delete", request)[1] == recorded
        swapped = COPY if name == INDUSTRIAL else INDUSTRIAL
        assert cli(root, "delete", {**request, "area": swapped}, check=False)[1] == {"error": "request_target_conflict"}
        serialized = json.dumps(recorded)
        assert all(value not in serialized for value in (*labels.values(), str(root), OPERATOR, INGRESS,
                                                           destination["scope"]["conv_key"]))
        pairs.append(recorded)
        if name == INDUSTRIAL:
            assert post(other_url, "/recent", {"conversation": conversation}) == before
            for relative in EXTRAS:
                assert (root / "areas" / COPY / relative).read_text() == "synthetic fixture"

    assert list((root / "areas").iterdir()) == []
    if evidence := os.environ.get("MEMORY_ISOLATED_ACCEPTANCE_EVIDENCE"):
        report = {"production_content_reads": 0, "production_snapshots": 0, "audit_receipt_pairs": pairs}
        Path(evidence).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("after_erasure", [False, True])
def test_process_interruption_preserves_seal_and_original_counts(enrolled, after_erasure, monkeypatch):
    root, targets, manager = enrolled
    destination = targets[INDUSTRIAL]
    request = new_request(destination)
    with manager.open_area(destination) as area:
        populate_extra(area)
    monkeypatch.setenv("MEMO33_TEST_AFTER_ERASURE", str(int(after_erasure)))
    hook = """
import os
from memory_core import isolated_scope
original = isolated_scope.shutil.rmtree
def interrupt(path):
    if os.environ['MEMO33_TEST_AFTER_ERASURE'] == '1':
        original(path)
    else:
        (path / 'indexes' / 'mock.index').unlink()
    os._exit(75)
isolated_scope.shutil.rmtree = interrupt
raise SystemExit(isolated_scope.main())
"""
    result = subprocess.run([sys.executable, "-c", hook, "--root", str(root), "delete"],
                            input=json.dumps(request), text=True, timeout=15)
    assert result.returncode == 75
    before = cli(root, "status", request)[1]
    assert before["audit"]["status"] == "pending" and before["receipt"] is None
    assert before["audit"]["deleted_files"] is None
    assert not (area / "indexes" / "mock.index").exists()
    assert (area / "logs" / "fixture.log").exists() is not after_erasure
    with pytest.raises(cleanup.ScopeCleanupError, match="area_sealed"):
        with manager.open_area(destination):
            pytest.fail("interrupted delete reopened its area")
    after = cli(root, "delete", request)[1]
    assert after["receipt"]["deleted_files"]["indexes"] == 1
    assert after["receipt"]["status"] == "complete" and not area.exists()
    assert after == cli(root, "status", request)[1]


@pytest.mark.parametrize("directory_link", [False, True])
def test_cross_area_link_refuses_and_retry_finishes(enrolled, directory_link):
    _, targets, manager = enrolled
    with manager.open_area(targets[COPY]) as area:
        protected = area / "protected.db"
        protected.write_text("other area", encoding="utf-8")
    with manager.open_area(targets[INDUSTRIAL]) as area:
        keep, link = area / "keep", area / "linked"
        keep.touch()
        if not directory_link:
            os.link(protected, link)
        elif os.name == "nt":
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(protected.parent)],
                           capture_output=True, check=True)
        else:
            link.symlink_to(protected.parent, target_is_directory=True)
    try:
        request = new_request(targets[INDUSTRIAL])
        result = manager.delete(request)
        assert result["audit"]["status"] == "retryable" and result["audit"]["error"] == "unsafe_storage_path"
        assert result["receipt"] is None and link.exists() and keep.exists()
        assert protected.read_text() == "other area"
    finally:
        link.rmdir() if directory_link and os.name == "nt" else link.unlink()
    assert manager.delete(request)["receipt"]["status"] == "complete"
    assert protected.read_text() == "other area"


def test_replaced_area_is_rejected_and_legacy_launcher_cannot_recreate_it(enrolled, monkeypatch):
    root, targets, manager = enrolled
    destination = targets[INDUSTRIAL]
    request = new_request(destination)
    area = root / "areas" / INDUSTRIAL
    displaced = area.with_name("displaced")
    area.rename(displaced)
    area.mkdir()
    sentinel = area / "replacement"
    sentinel.touch()
    rejected = manager.delete(request)
    assert rejected["audit"]["error"] == "unsafe_storage_path"
    assert rejected["receipt"] is None and sentinel.exists()
    sentinel.unlink()
    area.rmdir()
    displaced.rename(area)
    assert manager.delete(request)["receipt"]["status"] == "complete"
    for field, value in agent_conversation(destination["scope"]).items():
        monkeypatch.setenv("MEMORY_AGENT_" + field.upper(), value)
    monkeypatch.setenv("MEMORY_AGENT_DATA_DIR", str(area / "nested"))
    with pytest.raises(AgentRequestError, match="managed_archive_requires_isolated_serve"):
        with open_memory():
            pytest.fail("legacy transport reopened a sealed area")
    assert not area.exists()


def test_seal_drains_an_admitted_write_before_removing_storage(enrolled, serve, tmp_path, monkeypatch):
    root, targets, manager = enrolled
    destination = targets[INDUSTRIAL]
    conversation = agent_conversation(destination["scope"])
    entered, release = tmp_path / "entered", tmp_path / "release"
    monkeypatch.setenv("MEMO33_TEST_ENTERED", str(entered))
    monkeypatch.setenv("MEMO33_TEST_RELEASE", str(release))
    hook = """
import os, time
from pathlib import Path
from memory_core import isolated_scope
from memory_core.adapters.ingest import AgentMemory
original = AgentMemory.remember
def blocked(self, data):
    Path(os.environ['MEMO33_TEST_ENTERED']).touch()
    deadline = time.monotonic() + 15
    while not Path(os.environ['MEMO33_TEST_RELEASE']).exists():
        assert time.monotonic() < deadline
        time.sleep(.03)
    return original(self, data)
AgentMemory.remember = blocked
raise SystemExit(isolated_scope.main())
"""
    proc, url = serve(root, destination, hook=hook)
    reply = []
    writer = threading.Thread(target=lambda: reply.append(remember(url, conversation, "drained fixture", "in-flight")))
    writer.start()
    try:
        wait_for(entered.exists)
        request = new_request(destination)
        pending = manager.delete(request, wait=0)
        assert pending["audit"]["status"] == "pending" and pending["receipt"] is None
        assert manager.delete(request, wait=0) == pending
        with pytest.raises(cleanup.ScopeCleanupError, match="area_already_requested"):
            manager.delete(new_request(destination), wait=0)
        area = root / "areas" / INDUSTRIAL
        with closing(sqlite3.connect(area / "agent.sqlite3")) as conn:
            assert conn.execute("SELECT count(*) FROM memory_pipeline_raw").fetchone()[0] == 0
        with pytest.raises(OSError):
            post(url, "/recent", {"conversation": conversation})
    finally:
        release.touch()
        writer.join(timeout=8)
    assert reply[0]["message_id"] == "in-flight"
    assert manager.delete(request)["receipt"]["deleted_files"]["originals"] > 0
    assert proc.wait(timeout=8) == 0 and not area.exists()
