"""Local, allowlisted raw archive export into a registered isolation area."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from uuid import uuid4

from .memory_evidence import RETIRED_RAW, _decode_line, _timestamp, raw_session, scope_from, scope_key
from .scope_auth import ScopeError, authorize


AREA = "production_snapshot_copy"
COPY_NAME = AREA + ".jsonl"
SCOPE_FIELDS = {"bot_id", "persona", "conv_key"}
REQUEST_FIELDS = {"source_scope", "test_scope", "area", "raw_ids"}


class SnapshotError(ValueError):
    pass


def _full_scope(data):
    if not isinstance(data, dict) or data.keys() != SCOPE_FIELDS:
        raise SnapshotError("invalid_scope")
    try:
        return scope_from(data)
    except ValueError:
        raise SnapshotError("invalid_scope") from None


def _request(data):
    if not isinstance(data, dict) or data.keys() != REQUEST_FIELDS:
        raise SnapshotError("invalid_request")
    ids = data["raw_ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 1000
            or any(not isinstance(rid, str) or not rid.strip() for rid in ids)
            or len(set(ids)) != len(ids)):
        raise SnapshotError("invalid_record_list")
    return {**data, "source_scope": _full_scope(data["source_scope"]),
            "test_scope": _full_scope(data["test_scope"])}


def _inspect(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not directory and info.st_nlink != 1):
        raise SnapshotError("unsafe_path")
    return info


def _local_path(value, *, directory=False):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SnapshotError("unsafe_path")
    if os.name == "nt":
        from ctypes import windll

        # DRIVE_FIXED excludes UNC shares, mapped network drives and removable media.
        if not re.fullmatch(r"[A-Za-z]:", path.drive) or windll.kernel32.GetDriveTypeW(path.anchor) != 3:
            raise SnapshotError("unsafe_path")
    for parent in reversed(path.parents):
        _inspect(parent, directory=True)
    _inspect(path, directory=directory)
    if path.resolve(strict=True) != path:
        raise SnapshotError("unsafe_path")
    return path


def _registered_area(request, registry):
    if type(registry["version"]) is not int or registry["version"] != 1:
        raise SnapshotError("invalid_registration")
    if request["area"] != AREA or AREA not in registry["areas"]:
        raise SnapshotError("unregistered_area")
    registration = registry["areas"][AREA]
    if (_full_scope(registration["test_scope"]) != request["test_scope"]
            or request["source_scope"] == request["test_scope"]):
        raise SnapshotError("target_scope_forbidden")
    if _request(registry["approval"]) != request:
        raise SnapshotError("batch_not_approved")
    root = _local_path(registry["isolation_root"], directory=True)
    relative = Path(registration["directory"])
    if relative.anchor or not relative.parts or ".." in relative.parts or ":" in str(relative):
        raise SnapshotError("unsafe_path")
    return root, _local_path(root / relative, directory=True)


def _index_rows(index, request):
    # A read-only SQLite connection can still create WAL sidecars on a writable
    # directory. Reject a WAL index without its registered source-side files.
    with index.open("rb", buffering=0) as stream:
        header = stream.read(20)
    if not header.startswith(b"SQLite format 3\0") or len(header) != 20:
        raise SnapshotError("index_unavailable")
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = index.with_name(index.name + suffix)
        try:
            _inspect(sidecar)
        except FileNotFoundError:
            if header[18:20] == b"\x02\x02" and suffix != "-journal":
                raise SnapshotError("index_unavailable") from None
    key = scope_key(request["source_scope"])
    session = raw_session(request["source_scope"])
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        rows = []
        for rid in request["raw_ids"]:
            row = conn.execute(
                """SELECT file, offset, size, sha256, ts, role, raw_id, source_tags
                   FROM memory_pipeline_raw AS raw
                   WHERE raw_id = ? AND scope = ? AND session = ? AND processed != ?
                   AND NOT EXISTS (SELECT 1 FROM memory_pipeline_conflicts WHERE raw_id = raw.raw_id)""",
                (rid, key, session, RETIRED_RAW),
            ).fetchone()
            if row is None:
                raise SnapshotError("record_unavailable")
            rows.append(row)
        return rows


def _read_record(root, row, session):
    name, offset, size = row["file"], row["offset"], row["size"]
    if (not isinstance(name, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}\.jsonl", name)
            or type(offset) is not int or type(size) is not int or offset < 0 or size < 1):
        raise SnapshotError("record_invalid")
    path = root / name
    info = _inspect(path)
    with path.open("rb", buffering=0) as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())) or offset + size > info.st_size:
            raise SnapshotError("record_invalid")
        stream.seek(offset)
        blob = stream.read(size)
    if len(blob) != size or blob.count(b"\n") != 1 or hashlib.sha256(blob).hexdigest() != row["sha256"]:
        raise SnapshotError("record_invalid")
    try:
        record = _decode_line(blob)
        tags = json.loads(row["source_tags"])
        if (record["id"] != row["raw_id"] or record["s"] != session or record["r"] != row["role"]
                or _timestamp(record["ts"]) != _timestamp(row["ts"]) or not isinstance(tags, dict)):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise SnapshotError("record_invalid") from None
    return blob, tags


@contextmanager
def _staging_file(area):
    if os.name == "nt":
        # On the last close, Windows deletes the staging name. After link(),
        # the verified final name survives, even if the process is terminated.
        pending = area / (".snapshot-" + uuid4().hex)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY | os.O_TEMPORARY
        with os.fdopen(os.open(pending, flags, 0o600), "wb", buffering=0) as stream:
            yield stream, lambda: os.link(pending, area / COPY_NAME)
        return
    if not hasattr(os, "O_TMPFILE"):
        raise SnapshotError("atomic_publish_unavailable")
    directory = os.open(area, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open(".", os.O_TMPFILE | os.O_RDWR, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb", buffering=0) as stream:
            def publish():
                os.link(f"/proc/self/fd/{fd}", COPY_NAME, dst_dir_fd=directory, follow_symlinks=True)
                os.fsync(directory)

            yield stream, publish
    finally:
        os.close(directory)


def _write(stream, blob):
    if stream.write(blob) != len(blob):
        raise SnapshotError("partial_write")


def _publish(area, archive, rows, request):
    target = area / COPY_NAME
    session = raw_session(request["source_scope"])
    binding = {key: request[key] for key in ("source_scope", "test_scope", "area")}
    digest = hashlib.sha256()
    source_tags = []
    byte_count = 0
    with _staging_file(area) as (stream, publish):
        for row in rows:
            blob, tags = _read_record(archive, row, session)
            _write(stream, blob)
            digest.update(blob)
            byte_count += len(blob)
            source_tags.append(tags)
        manifest = (json.dumps(
            dict(version=1, **binding, payload_sha256=digest.hexdigest(), source_tags=source_tags),
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ) + "\n").encode("ascii")
        _write(stream, manifest)
        digest.update(manifest)
        byte_count += len(manifest)
        receipt = dict(status="complete", record_count=len(rows), byte_count=byte_count, sha256=digest.hexdigest())
        os.fsync(stream.fileno())
        _local_path(area, directory=True)
        try:
            publish()
        except FileExistsError:
            pass
        else:
            return receipt
    info = _inspect(target)
    with target.open("rb") as stream:
        if (not os.path.samestat(info, os.fstat(stream.fileno()))
                or hashlib.file_digest(stream, "sha256").digest() != digest.digest()):
            raise SnapshotError("copy_conflict")
    return receipt


def export(data, registry, credential):
    """Process one request using trusted operator registration. Return a redacted receipt."""
    request = _request(data)
    _local_path(os.environ["MEMORY_SCOPE_CREDENTIALS_FILE"])
    authorize({"X-Memory-Scope-Credential": credential}, request["source_scope"])
    isolation, area = _registered_area(request, registry)
    index = _local_path(registry["source_index"])
    archive = _local_path(registry["archive_root"], directory=True)
    if any(path.is_relative_to(isolation) or isolation.is_relative_to(path) for path in (index.parent, archive)):
        raise SnapshotError("source_target_overlap")
    try:
        _inspect(area / COPY_NAME)
    except FileNotFoundError:
        pass
    rows = _index_rows(index, request)
    return _publish(area, archive, rows, request)


def main():
    parser = argparse.ArgumentParser(description="Export one preapproved archive batch. Read request JSON on stdin.")
    _, extra = parser.parse_known_args()
    try:
        if extra:
            raise SnapshotError("invalid_request")
        registry = json.loads(_local_path(os.environ["MEMORY_SNAPSHOT_REGISTRY_FILE"]).read_text("utf-8-sig"))
        limit = 1024 * 1024
        body = sys.stdin.buffer.read(limit + 1)
        if len(body) > limit:
            raise SnapshotError("invalid_request")
        data = json.loads(body.decode("utf-8-sig"))
        result = export(data, registry, os.environ["MEMORY_SNAPSHOT_SCOPE_CREDENTIAL"])
    except (SnapshotError, ScopeError) as exc:
        result = dict(status="rejected", reason=str(exc), record_count=0)
    except (OSError, KeyError, ValueError, TypeError, sqlite3.Error, OverflowError):
        result = dict(status="rejected", reason="snapshot_unavailable", record_count=0)
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return int(result["status"] != "complete")


if __name__ == "__main__":
    sys.exit(main())
