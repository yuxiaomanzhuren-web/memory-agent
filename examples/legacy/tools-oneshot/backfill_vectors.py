# Backfill embeddings for memories that were stored before an embed model was configured.
# One-off maintenance tool: run with the memory service stopped, then let the bat restart it.

import json
import os
import sqlite3
import time
import urllib.request

import numpy as np

DB = os.environ.get("MEMORY_DB", r"$MEMORY_ROOT\data\brain.db")
URL = os.environ.get("MEMORY_EMBED_URL", "")
KEY = os.environ.get("MEMORY_EMBED_KEY", "")
MODEL = os.environ.get("MEMORY_EMBED_MODEL", "embedding-3")
BATCH = 8


def embed(texts):
    body = json.dumps({"model": MODEL, "input": texts}).encode()
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    return [np.asarray(item["embedding"], dtype=np.float32) for item in data["data"]]


def main():
    if not URL:
        raise SystemExit("MEMORY_EMBED_URL not set")
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT m.id, m.text FROM memories m
           LEFT JOIN vectors v ON v.memory_id = m.id
           WHERE v.memory_id IS NULL AND m.status = 'active'
           ORDER BY m.id"""
    ).fetchall()
    print(f"need vectors: {len(rows)}")
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        texts = [str(r["text"])[:2000] for r in chunk]
        vecs = embed(texts)
        for row, vec in zip(chunk, vecs):
            conn.execute(
                "INSERT OR REPLACE INTO vectors(memory_id, dim, vec) VALUES (?,?,?)",
                (int(row["id"]), int(vec.shape[0]), vec.tobytes()),
            )
            done += 1
        conn.commit()
        time.sleep(0.2)
    total = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    print(f"done {done}, vectors total {total}")
    conn.close()


if __name__ == "__main__":
    main()
