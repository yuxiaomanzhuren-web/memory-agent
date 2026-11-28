"""Local control plane for the two registered areas of a disposable test session."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import threading
import time
from uuid import UUID, uuid4

from .memory_evidence import agent_conversation, scope_from

AREAS = ("industrial_synthetic", "production_snapshot_copy")
REGISTRY = "isolated-scope.sqlite3"
TARGET_FIELDS = {"instance_id", "scope", "area"}
SCOPE_FIELDS = {"bot_id", "persona", "conv_key"}
INDEX_PATHS = ("indexes", "agent.sqlite3", "agent.sqlite3-wal", "agent.sqlite3-shm", "agent.sqlite3-journal")
SCHEMA = """
CREATE TABLE instance (id TEXT PRIMARY KEY, scope TEXT NOT NULL, operator_hash TEXT NOT NULL,
                       boundaries TEXT NOT NULL);
CREATE TABLE areas (name TEXT PRIMARY KEY, directory_id TEXT NOT NULL, lease_id TEXT NOT NULL);
CREATE TABLE requests (id TEXT PRIMARY KEY, area TEXT NOT NULL UNIQUE, requested_at TEXT NOT NULL,
                       status TEXT NOT NULL, inventory TEXT, verified_at TEXT, error TEXT);
CREATE TABLE receipts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""


class ScopeCleanupError(ValueError):
    pass


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _valid_id(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _token_digest(token):
    if not (isinstance(token, str) and 32 <= len(token) <= 256 and token.isascii() and token.isprintable()
            and not any(character.isspace() for character in token)):
        raise ScopeCleanupError("unauthorized")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _root_path(path):
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise ScopeCleanupError("unsafe_storage_path")
    path = path.absolute()
    if os.name == "nt" and not re.fullmatch(r"[a-zA-Z]:", path.drive):
        raise ScopeCleanupError("unsafe_storage_path")
    for parent in reversed(path.parents):
        _inspect(parent, directory=True)
    return path


def _inspect(path, *, directory=None, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise ScopeCleanupError("storage_path_missing") from None
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    is_dir = stat.S_ISDIR(info.st_mode)
    is_file = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if reparse or not (is_dir or is_file) or (directory is not None and is_dir != directory):
        raise ScopeCleanupError("unsafe_storage_path")
    return info


def _identity(info):
    return _encoded([info.st_dev, info.st_ino])


def _mount_points():
    if sys.platform.startswith("linux"):
        entries = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="surrogateescape")
        return {Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
                for line in entries.splitlines()}
    return set()


def _inside(path, mounts, *, device, directory=None, missing=False):
    info = _inspect(path, directory=directory, missing=missing)
    if info is not None and (info.st_dev != device or path in mounts or path.is_mount()):
        raise ScopeCleanupError("unsafe_storage_path")
    return info


def _inventory(area, mounts, device):
    counts = dict(originals=0, sqlite=0, indexes=0, snapshots=0, logs=0, other=0)
    directories = [area]
    while directories:
        for path in directories.pop().iterdir():
            info = _inside(path, mounts, device=device)
            if stat.S_ISDIR(info.st_mode):
                directories.append(path)
                continue
            container = path.relative_to(area).parts[0]
            if container == "agent-raw":
                counts["originals"] += 1
            elif container in ("snapshots", "indexes", "logs"):
                counts[container] += 1
            elif re.search(r"\.(?:db|sqlite3?)(?:-(?:wal|shm|journal))?$", path.name):
                counts["sqlite"] += 1
            elif path.name == "source-provenance.jsonl" or path.suffix == ".log":
                counts["logs"] += 1
            else:
                counts["other"] += 1
    return counts


def initialize(path, operator_token):
    """Enroll a fresh root and generate the only scope this instance can ever target."""
    operator_hash = _token_digest(operator_token)
    root = _root_path(path)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        raise ScopeCleanupError("root_already_exists") from None
    control, data = root / "control", root / "areas"
    control.mkdir(mode=0o700)
    data.mkdir(mode=0o700)
    instance = str(uuid4())
    scope = {"bot_id": "agent:isolated-test", "persona": "isolated-test",
             "conv_key": "private:test-" + str(uuid4())}
    boundaries = {part.name: _identity(_inspect(part, directory=True)) for part in (control, data)}
    boundaries["root"] = _identity(_inspect(root, directory=True))
    with closing(sqlite3.connect(control / REGISTRY)) as db, db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO instance VALUES (?, ?, ?, ?)", (instance, _encoded(scope), operator_hash,
                                                               _encoded(boundaries)))
        for name in AREAS:
            area, lease = data / name, control / name
            area.mkdir(mode=0o700)
            lease.mkdir(mode=0o700)
            db.execute("INSERT INTO areas VALUES (?, ?, ?)", (name, _identity(_inspect(area, directory=True)),
                                                              _identity(_inspect(lease, directory=True))))
    return {"instance_id": instance, "scope": scope, "areas": list(AREAS)}


def is_managed_area(directory):
    return any(part.name.casefold() in AREAS and part.parent.name.casefold() == "areas"
               and (part.parent.parent / "control" / REGISTRY).is_file()
               for part in (directory, *directory.parents))


class ManagedInstance:
    def __init__(self, path, operator_token):
        operator_hash = _token_digest(operator_token)
        self.root = _root_path(path)
        self.control, self.data = self.root / "control", self.root / "areas"
        self.device = _inspect(self.root, directory=True).st_dev
        self.boundaries = None
        with self._database() as db:
            record = db.execute("SELECT * FROM instance").fetchone()
            if record is None or not hmac.compare_digest(operator_hash, record["operator_hash"]):
                raise ScopeCleanupError("unauthorized")
            self.instance_id = record["id"]
            self.scope = json.loads(record["scope"])
            self.boundaries = json.loads(record["boundaries"])
            conv_key = self.scope.get("conv_key", "")
            if (self.scope.get("bot_id") != "agent:isolated-test" or self.scope.get("persona") != "isolated-test"
                    or not isinstance(conv_key, str) or not conv_key.startswith("private:test-")
                    or not _valid_id(conv_key.removeprefix("private:test-"))):
                raise ScopeCleanupError("target_not_registered")
        self._check_boundaries()

    def _check_boundaries(self):
        _root_path(self.root)
        mounts = _mount_points()
        for key, path in (("root", self.root), ("control", self.control), ("areas", self.data)):
            info = _inside(path, mounts, device=self.device, directory=True)
            if self.boundaries is not None and _identity(info) != self.boundaries[key]:
                raise ScopeCleanupError("unsafe_storage_path")
        return mounts

    @contextmanager
    def _database(self):
        self._check_boundaries()
        path = self.control / REGISTRY
        _inspect(path, directory=False)
        for suffix in ("-journal", "-wal", "-shm"):
            _inspect(path.with_name(path.name + suffix), directory=False, missing=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db

    def _target(self, data, *, with_request=False):
        fields = TARGET_FIELDS | {"request_id"} if with_request else TARGET_FIELDS
        if not isinstance(data, dict) or data.keys() != fields:
            raise ScopeCleanupError("target_required")
        try:
            scope = data["scope"]
            if not isinstance(scope, dict) or scope.keys() != SCOPE_FIELDS or scope != scope_from(scope):
                raise ValueError
        except ValueError:
            raise ScopeCleanupError("invalid_scope") from None
        area = data["area"]
        if data["instance_id"] != self.instance_id or scope != self.scope or area not in AREAS:
            raise ScopeCleanupError("target_not_registered")
        with self._database() as db:
            if not db.execute("SELECT 1 FROM areas WHERE name=?", (area,)).fetchone():
                raise ScopeCleanupError("target_not_registered")
        return area

    def _request(self, data):
        area = self._target(data, with_request=True)
        request_id = data["request_id"]
        if not _valid_id(request_id):
            raise ScopeCleanupError("invalid_request_id")
        return request_id, area

    @contextmanager
    def _lease(self, name):
        from .adapters.ingest import AgentRequestError
        from .adapters.runtime import archive_lock

        mounts = self._check_boundaries()
        lease = self.control / name
        with self._database() as db:
            entry = db.execute("SELECT * FROM areas WHERE name=?", (name,)).fetchone()
        if entry is None or _identity(_inside(lease, mounts, device=self.device,
                                              directory=True)) != entry["lease_id"]:
            raise ScopeCleanupError("unsafe_storage_path")
        _inspect(lease / "agent.lock", directory=False, missing=True)
        try:
            with archive_lock(lease):
                yield entry
        except AgentRequestError as exc:
            if exc.code != "archive_in_use":
                raise
            raise ScopeCleanupError("area_in_use") from None

    def _area(self, entry, mounts, *, missing=False):
        area = self.data / entry["name"]
        info = _inside(area, mounts, device=self.device, directory=True, missing=missing)
        if info is not None and _identity(info) != entry["directory_id"]:
            raise ScopeCleanupError("unsafe_storage_path")
        return area, info is not None

    def _sealed(self, name):
        with self._database() as db:
            return db.execute("SELECT 1 FROM requests WHERE area=?", (name,)).fetchone() is not None

    @contextmanager
    def open_area(self, target):
        """Hold the managed lease until the caller closes all area files and stores."""
        name = self._target(target)
        if self._sealed(name):
            raise ScopeCleanupError("area_sealed")
        with self._lease(name) as entry:
            if self._sealed(name):
                raise ScopeCleanupError("area_sealed")
            mounts = self._check_boundaries()
            area, _ = self._area(entry, mounts)
            _inventory(area, mounts, self.device)
            yield area

    def _audit(self, row):
        complete = row["status"] == "complete"
        return {"request_id": row["id"],
                "target": {"instance_id": self.instance_id, "area": row["area"],
                           "scope_sha256": hashlib.sha256(_encoded(self.scope).encode("utf-8")).hexdigest()},
                "status": row["status"], "requested_at": row["requested_at"], "verified_at": row["verified_at"],
                "deleted_files": json.loads(row["inventory"]) if complete else None,
                "remaining": {"storage": 0, "index_entries": 0} if complete else None, "error": row["error"]}

    @staticmethod
    def _row(db, request_id, area):
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is not None and row["area"] != area:
            raise ScopeCleanupError("request_target_conflict")
        return row

    def status(self, request):
        request_id, area = self._request(request)
        with self._database() as db:
            db.execute("BEGIN")
            row = self._row(db, request_id, area)
            if row is None:
                raise ScopeCleanupError("request_not_found")
            stored = db.execute("SELECT payload FROM receipts WHERE id=?", (request_id,)).fetchone()
            return {"audit": self._audit(row), "receipt": json.loads(stored["payload"]) if stored else None}

    def delete(self, request, *, wait=10):
        request_id, name = self._request(request)
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, request_id, name)
            if row is None:
                if db.execute("SELECT 1 FROM requests WHERE area=?", (name,)).fetchone():
                    raise ScopeCleanupError("area_already_requested")
                db.execute("INSERT INTO requests (id, area, requested_at, status) VALUES (?, ?, ?, 'pending')",
                           (request_id, name, _now()))
        if row is not None and row["status"] == "complete":
            return self.status(request)
        deadline = time.monotonic() + wait
        while True:
            try:
                with self._lease(name) as entry:
                    self._erase(request_id, entry)
                break
            except ScopeCleanupError as exc:
                if str(exc) != "area_in_use":
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return self.status(request)

    def _erase(self, request_id, entry):
        with self._database() as db:
            row = self._row(db, request_id, entry["name"])
            if row["status"] == "complete":
                return
            db.execute("UPDATE requests SET status='pending', error=NULL WHERE id=?", (request_id,))
        try:
            mounts = self._check_boundaries()
            area, present = self._area(entry, mounts, missing=row["inventory"] is not None)
            if present:
                counts = _inventory(area, mounts, self.device)
                if row["inventory"] is None:
                    # Commit the inventory before the first unlink so crashes preserve the counts.
                    with self._database() as db:
                        db.execute("UPDATE requests SET inventory=? WHERE id=?", (_encoded(counts), request_id))
                self._area(entry, self._check_boundaries())
                shutil.rmtree(area)
            self._check_boundaries()
            area_info = _inspect(area, directory=True, missing=True)
            if any(_inspect(area / name, missing=True) is not None for name in INDEX_PATHS):
                raise ScopeCleanupError("index_residue")
            if area_info is not None:
                raise ScopeCleanupError("storage_residue")
        except (ScopeCleanupError, OSError) as exc:
            error = str(exc) if isinstance(exc, ScopeCleanupError) else "storage_io_error"
            with self._database() as db:
                db.execute("UPDATE requests SET status='retryable', error=? WHERE id=?", (error, request_id))
            return
        with self._database() as db:
            db.execute("UPDATE requests SET status='complete', verified_at=? WHERE id=?", (_now(), request_id))
            audit = self._audit(self._row(db, request_id, entry["name"]))
            receipt = {key: audit[key] for key in ("request_id", "target", "status", "verified_at",
                                                   "deleted_files", "remaining")}
            db.execute("INSERT INTO receipts VALUES (?, ?)", (request_id, _encoded(receipt)))


def serve(instance, target, port, ingress_token):
    from .adapters.http import IngestServer
    from .adapters.ingest import AgentMemory
    from .source_provenance import configure_trace

    _token_digest(ingress_token)
    name = instance._target(target)

    def accepting():
        try:
            return not instance._sealed(name)
        except (ScopeCleanupError, OSError, sqlite3.Error):
            return False

    class ManagedServer(IngestServer):
        def process_request(self, request, client_address):
            if accepting():
                super().process_request(request, client_address)
            else:
                self.shutdown_request(request)

    output = sys.stdout
    with instance.open_area(target) as area, (area / "service.log").open("a", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            configure_trace(area / "source-provenance.jsonl")
            memory = AgentMemory(area, agent_conversation(instance.scope))
            try:
                # handle_request keeps shutdown synchronous. Server close joins admitted handlers.
                with ManagedServer(port, ingress_token, memory) as server:
                    server.timeout = 0.1
                    address = {"url": f"http://127.0.0.1:{server.server_port}"}
                    print(_encoded(address), file=output, flush=True)
                    while accepting():
                        server.handle_request()
            finally:
                memory.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    commands.add_parser("delete").add_argument("--wait", type=int, choices=range(31), default=10)
    commands.add_parser("serve").add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    operator = os.environ.get("MEMORY_ISOLATED_OPERATOR_TOKEN", "")
    try:
        if args.command == "init":
            result = initialize(args.root, operator)
        else:
            manager = ManagedInstance(args.root, operator)
            try:
                request = json.load(sys.stdin)
            except ValueError:
                raise ScopeCleanupError("invalid_json") from None
            if args.command == "serve":
                if not 0 <= args.port <= 65535:
                    raise ScopeCleanupError("invalid_port")
                serve(manager, request, args.port, os.environ.get("MEMORY_AGENT_HTTP_TOKEN", ""))
                return 0
            result = manager.status(request) if args.command == "status" else manager.delete(request, wait=args.wait)
        print(_encoded(result))
        return 0
    except KeyboardInterrupt:
        return 130
    except (ScopeCleanupError, OSError, sqlite3.Error) as exc:
        print(_encoded({"error": str(exc) if isinstance(exc, ScopeCleanupError) else "control_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
