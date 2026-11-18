# -*- coding: utf-8 -*-
"""记忆大脑的存储层：SQLite 单文件，不依赖任何外部服务。

表结构见 DESIGN.md 第 2 节。这里只做「存/取/改」，不做打分和策略，
方便单独测试，也方便以后换存储（Postgres/向量库）而不动引擎。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import numpy as np

log = logging.getLogger("memory.store")

SCHEMA_VERSION = 1


def normalize_text(value) -> str:
    """Match browser textarea newlines while preserving every other code point."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


class TextLimitError(ValueError):
    def __init__(self, field: str, limit: int, chars: int):
        self.field, self.limit, self.chars = field, limit, chars
        label = {"block": "每块本子", "loop_title": "清单标题", "loop_detail": "清单详情"}[field]
        super().__init__(f"{label}最多 {limit} 字，当前 {chars} 字。")

    def result(self) -> dict:
        return {"ok": False, "error_code": "text_too_long", "field": self.field,
                "limit": self.limit, "chars": self.chars, "reason": str(self), "error": str(self)}


def _bounded_text(value, field: str, limit: int) -> str:
    text = normalize_text(value)
    if len(text) > limit:
        raise TextLimitError(field, limit, len(text))
    return text

# 一条事务开着超过这么久还没提交，就当它「卡住了」：滚掉并告警。
# 背景：服务是 ThreadingHTTPServer + 单连接（check_same_thread=False），
# 任何一条写语句抛异常后没人回滚，事务就一直挂着写锁，
# 其他写请求全部 "database is locked" —— 2026-11-03 那次本子/判断帐停更
# 21 小时就是这么来的。
_TXN_STALE_SECS = float(os.environ.get("MEMORY_TXN_STALE_SECS", "90"))
_TXN_WATCHDOG = os.environ.get("MEMORY_TXN_WATCHDOG", "1") not in ("0", "false", "no")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'episode',
    text TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    valence REAL NOT NULL DEFAULT 0.0,
    arousal REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    last_access REAL NOT NULL DEFAULT 0,
    access_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    visibility TEXT NOT NULL DEFAULT 'public',
    archived_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    tokens,
    memory_id UNINDEXED,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS vectors (
    memory_id INTEGER PRIMARY KEY,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'thing',
    mention_count INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    profile TEXT NOT NULL DEFAULT '',
    profile_at REAL NOT NULL DEFAULT 0,
    skip_profile INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name, type)
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_me_entity ON memory_entities(entity_id);

CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    a_id INTEGER NOT NULL,
    b_id INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS working (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'todo',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_working_session ON working(session_id, done);

CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_role ON raw_messages(role, id);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL,
    dst_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    memory_id INTEGER,
    created_at REAL NOT NULL,
    UNIQUE(src_id, dst_id, relation)
);

-- 未完成的事：她自己的待办本。承诺、约定、欠账、没聊完的话。
-- 跟 working 表不同：working 是「这一轮对话的临时状态」，几天就过期；
-- open_loops 是跨天跨周还生效的，到期没做会一直亮着，直到她（或他）划掉。
CREATE TABLE IF NOT EXISTS open_loops (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scope            TEXT NOT NULL DEFAULT 'global',
    kind             TEXT NOT NULL DEFAULT 'unfinished',
    title            TEXT NOT NULL,
    detail           TEXT,
    due_at           REAL,
    surface_after    REAL,
    weight           REAL NOT NULL DEFAULT 0.5,
    status           TEXT NOT NULL DEFAULT 'open',
    last_surfaced_at REAL,
    surface_count    INTEGER NOT NULL DEFAULT 0,
    source_memory_id INTEGER,
    created_at       REAL NOT NULL,
    closed_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_loops_open ON open_loops(status, due_at);

-- 唤醒决策流水：每次「要不要开口」都留一行，包含各信号拆解。
-- 没有这张表，主动性就不可验证——你只能相信她，不能检查她。
CREATE TABLE IF NOT EXISTS wake_decisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    conv_key  TEXT NOT NULL,
    act       INTEGER NOT NULL,
    score     REAL,
    threshold REAL,
    reason    TEXT,
    signals   TEXT
);
CREATE INDEX IF NOT EXISTS idx_wake_ts ON wake_decisions(ts DESC);

-- 可调参数：控制台改的阈值/权重落这里，服务每轮现读。
-- 单独一张表而不是塞进 life_state：life_state 是「她此刻的生理数值」，
-- 这张是「调参面板」，混在一起会让状态机误把配置当数值去衰减。
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- 她的本子：四块常驻的自我认知，每轮对话整块注入。
-- 跟 memories 的区别不是「重要程度」，而是寿命和作用：memories 是发生过什么（档案，
-- 系统写的、被召回才出现）；blocks 是「我怎么理解我自己」（她自己写的、永远在场）。
-- 人设（persona）是出厂设定，只读；本子归她自己，随便改。
CREATE TABLE IF NOT EXISTS memory_blocks (
    slug       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    ord        INTEGER NOT NULL DEFAULT 0,
    version    INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL DEFAULT 'her'
);

-- 每次写本子都留一版。她改歪了能回滚；也能看见她这一年是怎么变的。
-- 不存 diff 存全文：每块上限 700 字，全文比 diff 便宜也好回滚。
CREATE TABLE IF NOT EXISTS block_versions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug    TEXT NOT NULL,
    body    TEXT NOT NULL,
    version INTEGER NOT NULL,
    op      TEXT NOT NULL DEFAULT 'replace',
    note    TEXT,
    by      TEXT NOT NULL DEFAULT 'her',
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blockver ON block_versions(slug, ts DESC);
"""

# 判断账本（偏差档案）。单独一段，因为它跟上面所有表的关系不一样：
#
#   memories  记「发生过什么」——系统写的，会衰减，被召回才出现
#   blocks    记「我怎么理解我自己」——她自己写的，永远在场，但只写当下状态
#   ledger    记「我凭什么这么想，后来对不对」——她自己写的，结构性留档，会回头核对
#
# 为什么要单独一张表而不是塞进 memories：
# 一条判断的寿命不是一个时间点，而是一段过程——先有「我当时怎么想、我预期什么」，
# 隔几天才有「实际发生了什么、我错没错」，中间可能空了半个月。memories 是单点快照，
# 装不下这个来回；而一旦判断有了「后来被证伪」这个状态，它就不再是记忆，是教训。
#
# 四类记录共用一张表（kind 区分），因为它们回答的是同一个问题的四个侧面：
#   我下的判断对不对（judge） / 我为什么没走另一条（road） / 我什么时候改了口（overturn）
#   / 我做错了什么（miss）
# 分开建四张表会让「这个阶段我到底哪里判断力没长」这种跨类型的问题查不出来。
_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS judgment_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL DEFAULT 'judge',   -- judge | road | overturn | miss
    claim        TEXT NOT NULL,                   -- 判断/决策/结论/做错的事，一句话
    reason       TEXT NOT NULL DEFAULT '',        -- 当时是怎么想的（依据），不是事后解释
    expected     TEXT NOT NULL DEFAULT '',        -- 预期会发生什么（可观测的那种）
    outcome      TEXT NOT NULL DEFAULT '',        -- 实际发生了什么
    verdict      TEXT NOT NULL DEFAULT 'pending', -- pending | 已验证 | 已推翻
    lesson       TEXT NOT NULL DEFAULT '',        -- 教训：下次具体怎么做
    alternatives TEXT NOT NULL DEFAULT '[]',      -- 未选之路：[{road, why_not}]
    chosen       TEXT NOT NULL DEFAULT '',        -- 实际选了哪条（road 用）
    supersedes   INTEGER NOT NULL DEFAULT 0,      -- 推翻了哪一条的记录 id（overturn 用）
    confidence   REAL NOT NULL DEFAULT 0.5,       -- 下判断时多有把握
    stakes       TEXT NOT NULL DEFAULT 'small',   -- small | big：小判断可以不追结果
    tags         TEXT NOT NULL DEFAULT '[]',
    scope        TEXT NOT NULL DEFAULT 'global',  -- global | private:<id> | group:<id>
    visibility   TEXT NOT NULL DEFAULT 'private',
    status       TEXT NOT NULL DEFAULT 'active',  -- active | archived
    review_id    INTEGER NOT NULL DEFAULT 0,      -- 归入哪次阶段总结
    source       TEXT NOT NULL DEFAULT 'her',     -- her | review | console
    created_at   REAL NOT NULL,
    resolved_at  REAL NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_kind ON judgment_ledger(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_pending ON judgment_ledger(verdict, created_at DESC);

-- 阶段总结。每跑一次「翻旧账」，留一份快照：这个阶段我反复栽在哪、哪些判断其实是一件事、
-- 有哪些当时没看懂后来才连上的线。
-- 不进 memories：总结是「关于记忆的记忆」，混进记忆流会在语义检索里反复命中自己。
CREATE TABLE IF NOT EXISTS ledger_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start REAL NOT NULL,
    period_end   REAL NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    patterns     TEXT NOT NULL DEFAULT '[]',  -- [{pattern, evidence:[id], advice}]
    links        TEXT NOT NULL DEFAULT '[]',  -- [{a, b, relation, why}]
    counts       TEXT NOT NULL DEFAULT '{}',
    created_by   TEXT NOT NULL DEFAULT 'auto', -- auto | her | console
    created_at   REAL NOT NULL
);

-- 记忆之间的连线。她自己在回看时把「这两件事其实是同一件事」连起来，存这里。
-- 不写 relations 表：那张是「实体之间的关系」（谁是谁的谁），连错了会污染人物画像；
-- 这张只表示「这两条记忆在我脑子里是通的」，连错了删掉即可，代价小得多。
CREATE TABLE IF NOT EXISTS memory_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    a_id       INTEGER NOT NULL,      -- 记忆 id 或账本 id
    b_id       INTEGER NOT NULL,
    a_kind     TEXT NOT NULL DEFAULT 'memory',  -- memory | ledger
    b_kind     TEXT NOT NULL DEFAULT 'memory',
    relation   TEXT NOT NULL DEFAULT '同一件事', -- 同一件事 / 因为 / 后来变了 / 前后矛盾
    why        TEXT NOT NULL DEFAULT '',
    review_id  INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(a_id, b_id, a_kind, b_kind, relation)
);
CREATE INDEX IF NOT EXISTS idx_links_a ON memory_links(a_id, a_kind);
CREATE INDEX IF NOT EXISTS idx_links_b ON memory_links(b_id, b_kind);
"""


def _bigram_sim(a: str, b: str) -> float:
    ga, gb = set(bigrams(a)), set(bigrams(b))
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def bigrams(text: str) -> list[str]:
    """中文友好的检索词：字符二元组，不依赖分词。"""
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or ""))
    if len(t) <= 1:
        return [t] if t else []
    return [t[i: i + 2] for i in range(len(t) - 1)]


def _token_blob(text: str) -> str:
    return " ".join(bigrams(text))


class _GuardedConn:
    """给 sqlite3 连接套一层护栏（不改任何调用方代码）：

    1. **串行化**：ThreadingHTTPServer 是多线程的，但只有一条连接；
       以前靠 GIL 碰运气，现在用 RLock 把每次数据库调用排队。
    2. **出错必回滚**：任何 sqlite3 异常都先 rollback 再往外抛，
       不再留下「开着写事务、锁着库、谁也不提交」的僵尸事务。
    3. **记录事务开着的时长**：交给看门狗判断是否卡死。
    4. **commit 遇忙退避重试**：commit 是幂等操作，重试是安全的。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        self._txn_at = 0.0

    # 属性（row_factory / isolation_level 等）直接透传给真连接
    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value):
        if name in ("_conn", "_lock", "_txn_at"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    # ---------------------------------------------------------------- 内部

    @property
    def raw(self) -> sqlite3.Connection:
        return self._conn

    def _mark(self) -> None:
        """记录事务第一次打开的时刻；事务结束就清零。"""
        try:
            if self._conn.in_transaction:
                if not self._txn_at:
                    self._txn_at = time.time()
            else:
                self._txn_at = 0.0
        except Exception:  # 连接已关等边缘情况，不影响主流程
            pass

    def _rollback_quietly(self) -> bool:
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
                return True
        except Exception:
            log.warning("回滚失败（连接可能已损坏）", exc_info=True)
        finally:
            self._txn_at = 0.0
        return False

    def _run(self, name: str, *args, **kwargs):
        with self._lock:
            fn = getattr(self._conn, name)
            try:
                out = fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                rolled = self._rollback_quietly()
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    log.warning(
                        "数据库忙/被锁：%s 失败（已回滚=%s）：%s", name, rolled, exc,
                    )
                raise
            except sqlite3.Error:
                self._rollback_quietly()
                raise
            self._mark()
            return out

    # ---------------------------------------------------------------- 对外

    def execute(self, *args, **kwargs):
        return self._run("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._run("executemany", *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._run("executescript", *args, **kwargs)

    def commit(self) -> None:
        last: Exception | None = None
        for attempt in range(1, 5):
            with self._lock:
                try:
                    self._conn.commit()
                    self._txn_at = 0.0
                    return
                except sqlite3.OperationalError as exc:
                    last = exc
                    busy = "locked" in str(exc).lower() or "busy" in str(exc).lower()
                    if not busy:
                        self._rollback_quietly()
                        raise
            # commit 幂等，遇忙可以等一会儿再来（不占着锁睡）
            time.sleep(0.2 * attempt)
        self._rollback_quietly()
        log.error("commit 连续 %s 次遇忙放弃，已回滚：%s", 4, last)
        raise last if last else sqlite3.OperationalError("database is locked")

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()
            self._txn_at = 0.0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- 看门狗

    def txn_age(self) -> float:
        return (time.time() - self._txn_at) if self._txn_at else 0.0

    def reap_stale_txn(self, threshold: float = _TXN_STALE_SECS) -> bool:
        age = self.txn_age()
        if age and age >= threshold:
            with self._lock:
                if not self._conn.in_transaction:
                    self._txn_at = 0.0
                    return False
                log.error(
                    "发现僵死事务（已开着 %.0fs ≥ %.0fs），强制回滚，释放写锁",
                    age, threshold,
                )
                self._conn.rollback()
                self._txn_at = 0.0
                return True
        return False


def _start_txn_watchdog(conn: _GuardedConn, interval: float = 15.0) -> threading.Thread:
    def _loop() -> None:
        while True:
            try:
                time.sleep(interval)
                conn.reap_stale_txn()
            except Exception:  # 看门狗自己不能把服务搞挂
                log.warning("事务看门狗异常", exc_info=True)

    t = threading.Thread(target=_loop, name="memory-txn-watchdog", daemon=True)
    t.start()
    return t


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        _raw = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            # 遇到锁别立刻失败，交给 SQLite 自己退避重试
            timeout=15.0,
        )
        _raw.row_factory = sqlite3.Row
        _raw.execute("PRAGMA journal_mode=WAL")
        _raw.execute("PRAGMA synchronous=NORMAL")
        _raw.execute("PRAGMA busy_timeout=15000")
        # WAL 别无限长：攒到 1000 页就做一次 checkpoint
        _raw.execute("PRAGMA wal_autocheckpoint=1000")
        self.conn = _GuardedConn(_raw, self._lock)
        # A forward-only cross-store purge must finish before any worker can replay old sources.
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_delete_log'").fetchone():
            pending=self.conn.execute("""SELECT memory_id FROM memory_delete_log d WHERE mode='purge_pending'
                AND id=(SELECT MAX(id) FROM memory_delete_log x WHERE x.memory_id=d.memory_id) LIMIT 1""").fetchone()
            if pending:
                self.conn.close()
                raise RuntimeError('unfinished offline purge for memory #'+str(pending[0])+'; resume maintenance before starting memory service')
        self.conn.executescript(_SCHEMA)
        # 判断账本单独一段脚本：它跟上面那批表没有外键耦合，也要能在老库上增量建。
        self.conn.executescript(_LEDGER_SCHEMA)
        from .operation_ledger import ensure_schema
        ensure_schema(self.conn)
        self._migrate_columns()
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()
        # 僵死事务看门狗：开着写事务超过 _TXN_STALE_SECS 就强制回滚并告警
        self._watchdog: threading.Thread | None = None
        if _TXN_WATCHDOG:
            self._watchdog = _start_txn_watchdog(self.conn)

    def _migrate_columns(self) -> None:
        cols = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(memories)")}
        if "archived_at" not in cols:
            self.conn.execute("ALTER TABLE memories ADD COLUMN archived_at REAL NOT NULL DEFAULT 0")
        if "visibility" not in cols:
            self.conn.execute(
                "ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'"
            )
            # 旧数据保守处理：事件按私聊算，事实/反思/技能保持公开
            self.conn.execute("UPDATE memories SET visibility='private' WHERE kind='episode'")
        self.conn.commit()
        self._migrate_entity_profile()

    def _migrate_entity_profile(self) -> None:
        cols = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(entities)")}
        if "profile" not in cols:
            self.conn.execute("ALTER TABLE entities ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
        if "profile_at" not in cols:
            self.conn.execute("ALTER TABLE entities ADD COLUMN profile_at REAL NOT NULL DEFAULT 0")
        if "skip_profile" not in cols:
            self.conn.execute(
                "ALTER TABLE entities ADD COLUMN skip_profile INTEGER NOT NULL DEFAULT 0")
        self.conn.commit()

    # ---------------------------------------------------------------- 记忆

    def insert_memory(
        self,
        *,
        text: str,
        kind: str = "episode",
        importance: float = 0.5,
        valence: float = 0.0,
        arousal: float = 0.0,
        source: str = "",
        meta: dict | None = None,
        vector: np.ndarray | None = None,
        created_at: float | None = None,
        visibility: str = "public",
    ) -> int:
        now = created_at or time.time()
        cur = self.conn.execute(
            """INSERT INTO memories
               (kind, text, importance, valence, arousal, created_at, last_access, source, meta, visibility)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                kind,
                text,
                float(importance),
                float(valence),
                float(arousal),
                now,
                0.0,
                source,
                json.dumps(meta or {}, ensure_ascii=False),
                visibility if visibility in ("public", "private", "group") else "public",
            ),
        )
        memory_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO memories_fts(tokens, memory_id) VALUES (?, ?)",
            (_token_blob(text), memory_id),
        )
        if vector is not None:
            self.set_vector(memory_id, vector)
        self.conn.commit()
        return memory_id

    def set_vector(self, memory_id: int, vec: np.ndarray) -> None:
        v = np.asarray(vec, dtype=np.float32).reshape(-1)
        self.conn.execute(
            "INSERT OR REPLACE INTO vectors(memory_id, dim, vec) VALUES (?,?,?)",
            (int(memory_id), int(v.shape[0]), v.tobytes()),
        )
        self.conn.commit()

    def count_memories(
        self,
        *,
        source: str | None = None,
        since: float | None = None,
        status: str = "active",
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM memories WHERE status=?"
        args: list[Any] = [status]
        if source:
            sql += " AND source=?"
            args.append(source)
        if since:
            sql += " AND created_at>=?"
            args.append(float(since))
        row = self.conn.execute(sql, args).fetchone()
        return int(row["n"] if row else 0)

    def get_memory(self, memory_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
        return self._row_to_memory(row) if row else None

    def list_memories(
        self,
        *,
        status: str = "active",
        kind: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM memories WHERE status=?"
        args: list[Any] = [status]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        return [self._row_to_memory(r) for r in self.conn.execute(sql, args).fetchall()]

    def ids_by_source_scope(self, scope: str) -> set[int] | None:
        """属于哪条线：source 形如 qq:<机器人号>|<会话键>。

        返回 None 表示「这条线一条都没有」和「过滤不适用」要分开——所以没命中时返回空集，
        调用方拿到空集就等于「这条线现在没记忆」，而不是「不过滤」。
        scope 传 'qq:' 开头就按整串匹配，否则允许只给 <号>|<会话>。
        """
        s = (scope or "").strip()
        if not s:
            return None
        if s.startswith("qq:"):
            like = s + "%"
            like2 = s + "%"
        else:
            like = "qq:" + s + "%"
            like2 = "%" + s
        rows = self.conn.execute(
            "select id from memories where source like ? or source like ?", (like, like2)).fetchall()
        return {int(r[0] if not isinstance(r, dict) else r["id"]) for r in rows}

    def pinned_memories(self) -> list[dict]:
        """所有被钉住且还活着的记忆。

        为什么需要单独一个查询：钉住的意义是「问到这个话题就必须答得出」，
        可它原先只是打分时的一个加成——前提是这条记忆先被关键词或向量召进候选集。
        向量服务一挂（2026-11-06 实测智谱接口整段超时），「我叫什么」就进不了候选，
        pin 分再高也轮不到它。所以要把钉住的先捞出来，按问法直接塞进候选。
        """
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE status='active' AND meta LIKE '%\"pin\"%'"
        ).fetchall()
        out = []
        for r in rows:
            m = self._row_to_memory(r)
            meta = m.get("meta") or {}
            if isinstance(meta, dict) and meta.get("pin"):
                out.append(m)
        return out

    def raw_between(
        self, start: float, end: float, limit: int = 8, session_like: str | None = None
    ) -> list[tuple[float, str, str]]:
        sql = """SELECT created_at, sender, text FROM raw_messages
                 WHERE role='user' AND created_at >= ? AND created_at < ?"""
        args: list[Any] = [float(start), float(end)]
        if session_like:
            sql += " AND session_id LIKE ?"
            args.append(session_like)
        sql += " ORDER BY created_at ASC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [(float(r[0]), str(r[1]), str(r[2])) for r in rows]

    def memories_in_range(
        self, start: float, end: float, visibility: set[str], limit: int = 5,
        kinds: tuple[str, ...] | None = None,
    ) -> list[dict]:
        marks = ",".join("?" * len(visibility))
        sql = f"""SELECT * FROM memories
                WHERE status='active' AND created_at >= ? AND created_at < ?
                  AND visibility IN ({marks})"""
        args: list[Any] = [float(start), float(end), *visibility]
        if kinds:
            sql += " AND kind IN (" + ",".join("?" * len(kinds)) + ")"
            args.extend(kinds)
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        args.append(int(limit))
        return [self._row_to_memory(r) for r in self.conn.execute(sql, args).fetchall()]

    def delete_memory(self, memory_id: int, mode="soft", *, actor="legacy", origin="store", automatic=False, **kwargs) -> bool:
        from .memory_delete import delete
        return delete(self,memory_id,mode,actor=actor,origin=origin,automatic=automatic,**kwargs)

    def delete_soft(self, memory_id: int, **kwargs) -> bool:
        return self.delete_memory(memory_id,"soft",**kwargs)

    def delete_hard(self, memory_id: int, **kwargs) -> bool:
        return self.delete_memory(memory_id,"hard",**kwargs)

    def delete_purge(self, memory_id: int, **kwargs) -> bool:
        # Cross-store cleanup cannot run safely while the HTTP service is active.
        return self.delete_memory(memory_id,"purge",**kwargs)

    def restore_soft(self, memory_id: int, *, actor="legacy", **kwargs) -> bool:
        from .memory_delete import restore_soft
        return restore_soft(self,memory_id,actor=actor,**kwargs)

    @contextmanager
    def _change_tx(self):
        """Keep receipts atomic without committing a caller's transaction."""
        with self._lock:
            # The guarded connection rolls back the whole transaction on SQL errors.
            conn = self.conn.raw
            nested = conn.in_transaction
            marker = "memory_change_" + uuid.uuid4().hex
            conn.execute("SAVEPOINT " + marker if nested else "BEGIN IMMEDIATE")
            try:
                yield conn
                if nested:
                    conn.execute("RELEASE SAVEPOINT " + marker)
                else:
                    conn.commit()
            except BaseException:
                if nested:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK TO SAVEPOINT " + marker)
                        conn.execute("RELEASE SAVEPOINT " + marker)
                else:
                    conn.rollback()
                raise

    def update_memory(self, memory_id: int, **fields: Any) -> None:
        allowed = {"text", "kind", "importance", "valence", "arousal", "status", "last_access", "access_count", "meta"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "meta":
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return
        from .operation_ledger import begin_change, finish_change
        mid = int(memory_id)
        with self._change_tx() as conn:
            row = conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
            ticket = None
            if fields.get("status") == "archived" and row is not None and row["status"] == "active":
                now = time.time()
                ticket = begin_change(conn, mid, "archive", "update_memory 显式状态更新：active → archived",
                                      "store.update_memory", now=now, reason_code="explicit_status_update")
                sets.append("archived_at=?")
                args.append(now)
            conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", [*args, mid])
            if "text" in fields:
                conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (mid,))
                conn.execute("INSERT INTO memories_fts(tokens, memory_id) VALUES (?,?)",
                             (_token_blob(str(fields["text"])), mid))
            if ticket is not None:
                finish_change(conn, ticket, reversible=True, details={"origin": "update_memory"})

    def find_active_by_text(self, text: str) -> int | None:
        """完全同文本的活跃记忆（写入查重的快路径）。"""
        rows = self.conn.execute(
            "SELECT id FROM memories WHERE status='active' AND text=? ORDER BY id DESC",
            ((text or "").strip(),),
        ).fetchall()
        from .memory_temporal import filter_memories
        rows = filter_memories(self.conn,[dict(r) for r in rows])
        return int(rows[0]['id']) if rows else None

    def touch(self, memory_ids: Iterable[int], now: float | None = None) -> None:
        now = now or time.time()
        self.conn.executemany(
            "UPDATE memories SET last_access=?, access_count=access_count+1 WHERE id=?",
            [(now, int(mid)) for mid in memory_ids],
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 检索

    def fts_search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        grams = bigrams(query)
        if not grams:
            return []
        match = " OR ".join(f'"{g}"' for g in grams)
        try:
            rows = self.conn.execute(
                """SELECT memory_id, bm25(memories_fts) AS score
                   FROM memories_fts
                   WHERE memories_fts MATCH ?
                   ORDER BY score
                   LIMIT ?""",
                (match, int(limit)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(r["memory_id"]), float(r["score"])) for r in rows]

    def all_vectors(self) -> tuple[list[int], np.ndarray | None]:
        rows = self.conn.execute(
            """SELECT v.memory_id, v.vec FROM vectors v
               JOIN memories m ON m.id=v.memory_id
               WHERE m.status='active'"""
        ).fetchall()
        if not rows:
            return [], None
        ids = [int(r["memory_id"]) for r in rows]
        dim = len(np.frombuffer(rows[0]["vec"], dtype=np.float32))
        mat = np.zeros((len(rows), dim), dtype=np.float32)
        for i, r in enumerate(rows):
            mat[i] = np.frombuffer(r["vec"], dtype=np.float32)
        return ids, mat

    # ---------------------------------------------------------------- 实体与关系

    def upsert_entity(
        self,
        name: str,
        type: str = "thing",
        *,
        now: float | None = None,
        aliases: list[str] | None = None,
    ) -> int:
        now = now or time.time()
        row = self.conn.execute(
            "SELECT id, mention_count FROM entities WHERE name=? AND type=?", (name, type)
        ).fetchone()
        if row:
            self.conn.execute(
                "UPDATE entities SET mention_count=mention_count+1, last_seen=? WHERE id=?",
                (now, int(row["id"])),
            )
            self.conn.commit()
            return int(row["id"])
        cur = self.conn.execute(
            """INSERT INTO entities(name, type, mention_count, first_seen, last_seen, aliases)
               VALUES (?,?,?,?,?,?)""",
            (name, type, 1, now, now, json.dumps(aliases or [], ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def link_entity(self, memory_id: int, entity_id: int, weight: float = 1.0) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_entities(memory_id, entity_id, weight) VALUES (?,?,?)",
            (int(memory_id), int(entity_id), float(weight)),
        )
        self.conn.commit()

    def memory_entities(self, memory_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT entity_id FROM memory_entities WHERE memory_id=?", (int(memory_id),)
        ).fetchall()
        return [int(r["entity_id"]) for r in rows]

    def entities_in_text(self, text: str, limit: int = 6) -> list[int]:
        """query 里直接出现的实体（名字长度 >=2，避免单字误命中）。"""
        text = str(text or "")
        if not text:
            return []
        rows = self.conn.execute(
            "SELECT id, name FROM entities WHERE length(name) >= 2 ORDER BY mention_count DESC"
        ).fetchall()
        hits = []
        for r in rows:
            if str(r["name"]) in text:
                hits.append(int(r["id"]))
                if len(hits) >= limit:
                    break
        return hits

    def memory_ids_of_entities(self, entity_ids: Iterable[int], limit: int = 200) -> set[int]:
        ids = [int(e) for e in entity_ids]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT memory_id FROM memory_entities WHERE entity_id IN ({marks}) LIMIT ?",
            (*ids, int(limit)),
        ).fetchall()
        return {int(r["memory_id"]) for r in rows}

    def entities_of(self, memory_ids: Iterable[int]) -> set[int]:
        ids = [int(x) for x in memory_ids]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT entity_id FROM memory_entities WHERE memory_id IN ({marks})",
            ids,
        ).fetchall()
        return {int(r["entity_id"]) for r in rows}

    def memories_by_entities(self, entity_ids: Iterable[int], limit: int = 50) -> list[int]:
        ids = [int(x) for x in entity_ids]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""SELECT memory_id, SUM(weight) AS w
                FROM memory_entities
                WHERE entity_id IN ({marks})
                GROUP BY memory_id
                ORDER BY w DESC
                LIMIT ?""",
            ids + [int(limit)],
        ).fetchall()
        return [int(r["memory_id"]) for r in rows]

    def neighbor_memories(self, memory_ids: Iterable[int], limit: int = 30) -> list[int]:
        """1 跳：共享实体的其它记忆。"""
        ents = self.entities_of(memory_ids)
        if not ents:
            return []
        exclude = {int(x) for x in memory_ids}
        return [m for m in self.memories_by_entities(ents, limit=limit) if m not in exclude]

    def find_entity_by_name(self, name: str) -> dict | None:
        name = str(name or "").strip()
        if not name:
            return None
        row = self.conn.execute(
            "SELECT * FROM entities WHERE name=? ORDER BY mention_count DESC LIMIT 1", (name,)
        ).fetchone()
        if row:
            return dict(row)
        row = self.conn.execute(
            "SELECT * FROM entities WHERE name LIKE ? ORDER BY mention_count DESC LIMIT 1",
            (f"%{name}%",),
        ).fetchone()
        if row:
            return dict(row)
        # 退一步：忽略 emoji/标点后比对（带 emoji 的昵称 → 去掉 emoji 后的名字）
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "", name).lower()
        if key:
            for r in self.conn.execute("SELECT * FROM entities"):
                if re.sub(r"[^\w\u4e00-\u9fff]+", "", str(r["name"])).lower() == key:
                    return dict(r)
        return None

    def profiled_entities(self, *, min_profile_len: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT e.id, e.name, e.type, e.profile,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e
               WHERE LENGTH(e.profile) >= ?
               ORDER BY degree DESC""",
            (int(min_profile_len),),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_profile(self, entity_id: int, text: str, now: float | None = None) -> None:
        self.conn.execute(
            "UPDATE entities SET profile=?, profile_at=? WHERE id=?",
            (str(text)[:600], now or time.time(), int(entity_id)),
        )
        self.conn.commit()

    def get_entity(self, entity_id: int) -> dict | None:
        row = self.conn.execute(
            """SELECT e.*,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e WHERE e.id=?""",
            (int(entity_id),),
        ).fetchone()
        return dict(row) if row else None

    def entity_relations(self, entity_id: int, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT r.relation, e.name AS other, e.type AS other_type,
                      CASE WHEN r.src_id=? THEN 'out' ELSE 'in' END AS direction
               FROM relations r
               JOIN entities e ON e.id = CASE WHEN r.src_id=? THEN r.dst_id ELSE r.src_id END
               WHERE r.src_id=? OR r.dst_id=?
               ORDER BY r.weight DESC LIMIT ?""",
            (int(entity_id), int(entity_id), int(entity_id), int(entity_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def entities_needing_profile(
        self,
        *,
        min_degree: int = 2,
        older_than: float = 7 * 86400,
        limit: int = 10,
        now: float | None = None,
    ) -> list[dict]:
        now = now or time.time()
        # 两个坑：
        # 1) profile_at 默认 0 但早期行是 NULL，`profile_at < ?` 遇到 NULL 恒为假——
        #    从没写过画像的实体反而永远排不进来，一直空着。用 COALESCE 兜住。
        # 2) 抽象词（称呼、权限、回忆、晚饭…）度数够高，会被写成「角色对X的印象」，
        #    但她根本拿这些当不了人看。清过画像的那批在 skip_profile 里标了 1。
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(entities)")}
        skip = "AND COALESCE(e.skip_profile,0)=0" if "skip_profile" in cols else ""
        rows = self.conn.execute(
            f"""SELECT e.id, e.name, e.type, e.mention_count, e.profile_at,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e
               WHERE e.type IN ('person', 'topic')
                 AND (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) >= ?
                 AND COALESCE(e.profile_at, 0) < ?
                 {skip}
               ORDER BY degree DESC, e.last_seen DESC
               LIMIT ?""",
            (int(min_degree), float(now - older_than), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def absorb_entity(self, old_id: int, keep_id: int) -> int:
        """把 old 实体并进 keep：迁移记忆关联和关系边，然后删掉 old。"""
        old_id, keep_id = int(old_id), int(keep_id)
        if old_id == keep_id:
            return 0
        rows = self.conn.execute(
            "SELECT memory_id, weight FROM memory_entities WHERE entity_id=?", (old_id,)
        ).fetchall()
        moved = 0
        for r in rows:
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_entities(memory_id, entity_id, weight) VALUES (?,?,?)",
                (int(r["memory_id"]), keep_id, float(r["weight"])),
            )
            moved += 1
        self.conn.execute("DELETE FROM memory_entities WHERE entity_id=?", (old_id,))
        self.conn.execute("UPDATE OR REPLACE relations SET src_id=? WHERE src_id=?", (keep_id, old_id))
        self.conn.execute("UPDATE OR REPLACE relations SET dst_id=? WHERE dst_id=?", (keep_id, old_id))
        lost = self.conn.execute(
            "SELECT mention_count FROM entities WHERE id=?", (old_id,)
        ).fetchone()
        if lost:
            self.conn.execute(
                "UPDATE entities SET mention_count = mention_count + ? WHERE id=?",
                (int(lost["mention_count"]), keep_id),
            )
        self.conn.execute("DELETE FROM entities WHERE id=?", (old_id,))
        self.conn.commit()
        return moved

    def rename_entity(self, entity_id: int, name: str) -> None:
        try:
            self.conn.execute("UPDATE entities SET name=? WHERE id=?", (name, int(entity_id)))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def absorb_memory(self, drop_id: int, keep_id: int, similarity: float = 0.0) -> None:
        """把 drop 并进 keep：实体关联、访问次数、重要性取大，然后删掉 drop。"""
        drop_id, keep_id = int(drop_id), int(keep_id)
        keep = self.get_memory(keep_id)
        drop = self.get_memory(drop_id)
        if not keep or not drop:
            return
        for eid in self.memory_entities(drop_id):
            row = self.conn.execute(
                "SELECT weight FROM memory_entities WHERE memory_id=? AND entity_id=?",
                (drop_id, eid),
            ).fetchone()
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_entities(memory_id, entity_id, weight) VALUES (?,?,?)",
                (keep_id, eid, float(row["weight"]) if row else 1.0),
            )
        self.conn.execute(
            """UPDATE memories SET
                 importance = MAX(importance, ?),
                 access_count = access_count + ?,
                 last_access = MAX(last_access, ?)
               WHERE id=?""",
            (
                float(drop["importance"]),
                int(drop["access_count"]),
                float(drop["last_access"]),
                keep_id,
            ),
        )
        self.conn.execute("UPDATE relations SET memory_id=? WHERE memory_id=?", (keep_id, drop_id))
        self.conn.commit()
        self.delete_memory(drop_id,"hard",actor="automatic",origin="merge",automatic=True)

    def link_relation(self, src: int, dst: int, relation: str, memory_id: int | None = None, weight: float = 1.0) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO relations(src_id, dst_id, relation, weight, memory_id, created_at)
               VALUES (?,?,?,?,?,?)""",
            (int(src), int(dst), relation, float(weight), memory_id, time.time()),
        )
        self.conn.commit()

    def cooccurrence_edges(self, limit: int = 800) -> list[dict[str, Any]]:
        """共现边：两条记忆里同时出现的两个实体，连一条虚线。"""
        rows = self.conn.execute(
            """SELECT a.entity_id AS src, b.entity_id AS dst, COUNT(*) AS w
               FROM memory_entities a
               JOIN memory_entities b
                 ON a.memory_id = b.memory_id AND a.entity_id < b.entity_id
               GROUP BY a.entity_id, b.entity_id
               ORDER BY w DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
        return [{"src": int(r["src"]), "dst": int(r["dst"]), "weight": int(r["w"])} for r in rows]

    def entity_names(self) -> dict[int, str]:
        rows = self.conn.execute("SELECT id, name FROM entities").fetchall()
        return {int(r["id"]): str(r["name"]) for r in rows}

    def memories_of_entity(self, entity_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """SELECT m.* FROM memories m
               JOIN memory_entities me ON me.memory_id = m.id
               WHERE me.entity_id = ? AND m.status = 'active'
               ORDER BY m.created_at DESC LIMIT ?""",
            (int(entity_id), int(limit)),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def graph(self, limit: int = 500) -> dict[str, list[dict]]:
        nodes = self.conn.execute(
            """SELECT e.id, e.name, e.type, e.mention_count,
                      (SELECT COUNT(*) FROM memory_entities me WHERE me.entity_id=e.id) AS degree
               FROM entities e ORDER BY degree DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        edges = self.conn.execute(
            """SELECT src_id, dst_id, relation, weight FROM relations
               ORDER BY weight DESC LIMIT ?""",
            (int(limit * 3),),
        ).fetchall()
        return {
            "nodes": [dict(r) for r in nodes],
            "edges": [dict(r) for r in edges],
        }

    # ---------------------------------------------------------------- 维护

    def decay(
        self,
        rate: float = 0.01,
        now: float | None = None,
        *,
        access_boost: float = 0.5,
        access_window_days: float = 30.0,
        access_max_count: float = 10.0,
        protect_names: list[str] | None = None,
        core_floor: float = 0.45,
        emotion_factor: float = 0.4,
    ) -> int:
        """每日衰减：频率感知 + 情绪保护 + 核心人物地板。"""
        now = now or time.time()
        window = now - max(1.0, access_window_days) * 86400
        protect = {str(n).strip() for n in (protect_names or []) if str(n).strip()}
        core_ids: set[int] = set()
        if protect:
            for row in self.conn.execute(
                """SELECT DISTINCT me.memory_id FROM memory_entities me
                   JOIN entities e ON e.id = me.entity_id
                   WHERE e.name IN (%s)""" % ",".join("?" * len(protect)),
                list(protect),
            ):
                core_ids.add(int(row[0]))
        rows = self.conn.execute(
            "SELECT id, importance, access_count, last_access, valence, arousal FROM memories WHERE status='active'"
        ).fetchall()
        updates = []
        for r in rows:
            factor = 1.0
            if r["last_access"] and float(r["last_access"]) >= window:
                factor = 1.0 - access_boost * min(
                    1.0, float(r["access_count"]) / max(1.0, access_max_count)
                )
            rate_i = float(rate)
            val = abs(float(r["valence"] or 0))
            if val > 1:
                val /= 100.0
            aro = float(r["arousal"] or 0)
            if aro > 1:
                aro /= 100.0
            # 情绪强的记忆，忘得更慢
            if val >= 0.5 or aro >= 0.5:
                rate_i *= float(emotion_factor)
            new_imp = max(0.01, float(r["importance"]) * (1.0 - rate_i * factor))
            # 核心人物相关：不低于地板
            if int(r["id"]) in core_ids:
                new_imp = max(new_imp, float(core_floor))
            updates.append((round(new_imp, 4), int(r["id"])))
        if updates:
            self.conn.executemany("UPDATE memories SET importance=? WHERE id=?", updates)
            self.conn.commit()
        return len(updates)

    def archive(
        self,
        importance_below: float,
        older_than_days: float,
        now: float | None = None,
        *,
        protect_days: float = 30.0,
        drop_vectors: bool = True,
        actor_kind: str = "unknown",
    ) -> int:
        """归档：最近 protect_days 内被访问过的不动；归档后可以丢掉向量省空间。"""
        now = now or time.time()
        cutoff = now - float(older_than_days) * 86400
        protect = now - float(protect_days) * 86400
        with self._change_tx() as conn:
            rows = conn.execute(
                """SELECT id FROM memories
                   WHERE status='active' AND importance < ? AND created_at < ?
                     AND (last_access = 0 OR last_access < ?)""",
                (float(importance_below), cutoff, protect),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            reason = (f"低重要性旧记忆归档：importance < {float(importance_below)}；created_at < {cutoff}；"
                      f"last_access = 0 或 last_access < {protect}（保护最近 {float(protect_days)} 天访问）")
            self._archive_rows(conn, ids, now, reason, "old_low_importance", drop_vectors=drop_vectors, actor_kind=actor_kind)
            return len(ids)

    def _archive_rows(self, conn, ids, now, reason, reason_code, *, drop_vectors, actor_kind="unknown"):
        from .operation_ledger import begin_change, finish_change
        for mid in ids:
            ticket = begin_change(conn, mid, "archive", reason, "store." + reason_code,
                                  actor=actor_kind, now=now, reason_code=reason_code)
            changed = conn.execute("UPDATE memories SET status='archived', archived_at=? WHERE id=? AND status='active'",
                                   (now, mid))
            if changed.rowcount != 1:
                raise RuntimeError("archive target is no longer active")
            if drop_vectors:
                conn.execute("DELETE FROM vectors WHERE memory_id=?", (mid,))
            finish_change(conn, ticket, reversible=True, details={"vectors_dropped": bool(drop_vectors)})

    def trim_kind(self, kind: str, max_count: int, now: float | None = None, *, actor_kind="unknown") -> int:
        """某一类记忆超过上限时，把最不重要、最旧的归档。"""
        now = now or time.time()
        with self._change_tx() as conn:
            rows = conn.execute(
                """SELECT id FROM memories WHERE status='active' AND kind=?
                   ORDER BY importance ASC, created_at ASC""",
                (kind,),
            ).fetchall()
            if len(rows) <= int(max_count):
                return 0
            victims = [int(r["id"]) for r in rows[: len(rows) - int(max_count)]]
            reason = (f"类型容量裁剪：kind={kind}，active 数量 {len(rows)} 超过上限 {int(max_count)}；"
                      "按 importance ASC、created_at ASC 选择超额条目")
            self._archive_rows(conn, victims, now, reason, "kind_capacity", drop_vectors=True, actor_kind=actor_kind)
            return len(victims)

    def purge_archived(self, keep_days: float = 180.0, now: float | None = None) -> int:
        """归档超过 keep_days 后做 hard 清理；保留原话，不等于隐私 purge。"""
        now = now or time.time()
        cutoff = now - float(keep_days) * 86400
        rows = self.conn.execute(
            "SELECT id FROM memories WHERE status='archived' AND archived_at > 0 AND archived_at < ?",
            (cutoff,),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        return sum(self.delete_memory(mid,"hard",actor="automatic",origin="archive_cleanup",automatic=True,
                                      reason=f"归档超过 {float(keep_days)} 天，archived_at < {cutoff}，自动硬清理") for mid in ids)

    def drop_orphan_entities(self) -> int:
        """删掉没有任何记忆引用的实体（合并/删除后留下的空节点）。"""
        cur = self.conn.execute(
            """DELETE FROM entities
               WHERE id NOT IN (SELECT DISTINCT entity_id FROM memory_entities)"""
        )
        self.conn.commit()
        return cur.rowcount

    def memories_between(self, start: float, end: float, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM memories
               WHERE status='active' AND created_at >= ? AND created_at < ?
               ORDER BY importance DESC, created_at DESC LIMIT ?""",
            (float(start), float(end), int(limit)),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    # ---------------------------------------------------------------- 每日计数

    def bump_daily(self, key: str, n: float = 1.0, day: str | None = None) -> None:
        day = day or time.strftime("%Y-%m-%d")
        self.conn.execute(
            """INSERT INTO daily_stats(day, key, value) VALUES (?,?,?)
               ON CONFLICT(day, key) DO UPDATE SET value = value + excluded.value""",
            (day, key, float(n)),
        )
        self.conn.commit()

    def daily_stats(self, day: str | None = None) -> dict[str, float]:
        day = day or time.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT key, value FROM daily_stats WHERE day=?", (day,)
        ).fetchall()
        return {str(r["key"]): float(r["value"]) for r in rows}

    # ---------------------------------------------------------------- 冲突

    def add_conflict(self, a_id: int, b_id: int, reason: str, now: float | None = None) -> bool:
        row = self.conn.execute(
            "SELECT id FROM conflicts WHERE ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?)) AND resolved=0",
            (a_id, b_id, b_id, a_id),
        ).fetchone()
        if row:
            return False
        self.conn.execute(
            "INSERT INTO conflicts(a_id, b_id, reason, created_at) VALUES (?,?,?,?)",
            (a_id, b_id, reason, now or time.time()),
        )
        self.conn.commit()
        return True

    def list_conflicts(self, resolved: bool = False, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM conflicts WHERE resolved=? ORDER BY id DESC LIMIT ?",
            (1 if resolved else 0, int(limit)),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["a"] = self.get_memory(int(r["a_id"]))
            item["b"] = self.get_memory(int(r["b_id"]))
            out.append(item)
        return out

    def resolve_conflict(self, conflict_id: int, keep_id: int | None = None) -> None:
        self.conn.execute("UPDATE conflicts SET resolved=1 WHERE id=?", (int(conflict_id),))
        self.conn.commit()

    def set_meta(self, memory_id: int, meta: dict) -> None:
        self.conn.execute(
            "UPDATE memories SET meta=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), int(memory_id)),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 未完成清单

    LOOP_TITLE_CHARS = 200
    LOOP_DETAIL_CHARS = 5000

    def add_loop(
        self,
        title: str,
        kind: str = "unfinished",
        detail: str = "",
        due_at: float | None = None,
        surface_after: float | None = None,
        weight: float = 0.5,
        scope: str = "global",
        source_memory_id: int | None = None,
        now: float | None = None,
    ) -> int | None:
        """记一件没做完的事。

        去重策略跟 working 不一样：working 可以随便覆盖，open_loops 里同一件事
        重复记是多出来的噪音（她会反复提同一句），所以同名且还 open 的直接复用。
        """
        title = _bounded_text(title, "loop_title", self.LOOP_TITLE_CHARS)
        detail = _bounded_text(detail, "loop_detail", self.LOOP_DETAIL_CHARS)
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 2:
            return None
        now = now or time.time()
        for row in self.list_loops(status="open", limit=200):
            if _bigram_sim(title, row["title"]) >= 0.72:
                return int(row["id"])
        cur = self.conn.execute(
            """INSERT INTO open_loops
                 (scope, kind, title, detail, due_at, surface_after, weight,
                  status, source_memory_id, created_at)
               VALUES (?,?,?,?,?,?,?,'open',?,?)""",
            (
                scope, kind, title, detail or None,
                float(due_at) if due_at else None,
                float(surface_after) if surface_after else None,
                max(0.0, min(1.0, float(weight))),
                int(source_memory_id) if source_memory_id else None,
                now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_loops(
        self,
        status: str = "open",
        limit: int = 20,
        scope: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM open_loops WHERE 1=1"
        args: list[Any] = []
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if scope:
            sql += " AND (scope='global' OR scope=?)"
            args.append(scope)
        sql += " ORDER BY (due_at IS NULL), due_at ASC, weight DESC, id ASC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def update_loop(self, loop_id: int, **fields) -> bool:
        allowed = {"title", "detail", "kind", "weight", "due_at", "surface_after", "status"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "weight":
                v = max(0.0, min(1.0, float(v)))
            elif k == "title":
                v = _bounded_text(v, "loop_title", self.LOOP_TITLE_CHARS)
            elif k == "detail":
                v = _bounded_text(v, "loop_detail", self.LOOP_DETAIL_CHARS)
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return False
        if "status" in fields and str(fields.get("status")) in ("done", "dropped"):
            sets.append("closed_at=?")
            args.append(time.time())
        args.append(int(loop_id))
        cur = self.conn.execute(f"UPDATE open_loops SET {', '.join(sets)} WHERE id=?", args)
        self.conn.commit()
        return cur.rowcount > 0

    def close_loop(self, loop_id: int, status: str = "done") -> bool:
        return self.update_loop(loop_id, status=str(status or "done"))

    def touch_loop(self, loop_id: int, now: float | None = None) -> None:
        """她被这条清单条目叫醒一次，记一笔。surface_count 会参与冷却计算。"""
        self.conn.execute(
            "UPDATE open_loops SET last_surfaced_at=?, surface_count=surface_count+1 WHERE id=?",
            (now or time.time(), int(loop_id)),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 她的本子

    BLOCKS = (
        ("self", "我是谁", 10),
        ("him", "关于他", 20),
        ("care", "我正在意的事", 30),
        ("rules", "我给自己定的规矩", 40),
        ("days", "这几天", 50),
    )
    BLOCK_CHARS = 50_000

    def ensure_blocks(self, *, commit: bool = True) -> None:
        """本子各页建出来就是空的——空着比塞一段别人写的介绍更诚实。
        她什么时候想写，什么时候写；一直空着也说明那件事她还没想清楚。"""
        now = time.time()
        for slug, title, ordv in self.BLOCKS:
            self.conn.execute(
                "INSERT OR IGNORE INTO memory_blocks(slug, title, body, ord, version, updated_at) VALUES (?,?,'',?,0,?)",
                (slug, title, ordv, now),
            )
        if commit:
            self.conn.commit()

    def list_blocks(self) -> list[dict]:
        self.ensure_blocks()
        rows = self.conn.execute("SELECT * FROM memory_blocks ORDER BY ord ASC").fetchall()
        return [dict(r) for r in rows]

    def get_block(self, slug: str, *, commit: bool = True) -> dict | None:
        self.ensure_blocks(commit=commit)
        row = self.conn.execute("SELECT * FROM memory_blocks WHERE slug=?", (str(slug),)).fetchone()
        return dict(row) if row else None

    def set_block(
        self,
        slug: str,
        body: str,
        op: str = "replace",
        note: str = "",
        by: str = "her",
        now: float | None = None,
        *,
        commit: bool = True,
    ) -> dict:
        """Validate the assembled body before changing it or recording a version."""
        op = str(op or "replace").lower()
        if op not in ("replace", "append", "clear"):
            return {"ok": False, "reason": "未知本子写入方式"}
        text = normalize_text(body)
        # commit=False participates in the daily recap's transaction.
        guard = self._change_tx() if commit else self._lock
        try:
            with guard:
                current = self.get_block(slug, commit=False)
                if current is None:
                    return {"ok": False, "reason": "没有这块本子"}
                old = normalize_text(current["body"])
                if op == "clear":
                    text = ""
                elif op == "append" and old and text:
                    text = old + "\n" + text
                elif op == "append" and not text:
                    text = old
                text = _bounded_text(text, "block", self.BLOCK_CHARS)
                now = now or time.time()
                version = int(current["version"]) + 1
                self.conn.execute(
                    "INSERT INTO block_versions(slug,body,version,op,note,by,ts) VALUES (?,?,?,?,?,?,?)",
                    (slug, text, version, op, str(note or "")[:200] or None, by, now),
                )
                self.conn.execute(
                    "UPDATE memory_blocks SET body=?,version=?,updated_at=?,updated_by=? WHERE slug=?",
                    (text, version, now, by, slug),
                )
        except TextLimitError as error:
            return error.result()
        return {"ok": True, "slug": slug, "version": version, "chars": len(text),
                "truncated": False, "body": text}

    def block_versions(self, slug: str, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM block_versions WHERE slug=? ORDER BY id DESC LIMIT ?",
            (str(slug), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def revert_block(self, slug: str, version_id: int | None = None, by: str = "console") -> dict:
        """回滚。不给 version_id 就退一版（版本表里比当前更早的那条）。"""
        cur = self.get_block(slug)
        if cur is None:
            return {"ok": False, "reason": f"没有这块本子：{slug}"}
        if version_id:
            row = self.conn.execute("SELECT * FROM block_versions WHERE id=? AND slug=?",
                                    (int(version_id), str(slug))).fetchone()
        else:
            rows = self.block_versions(slug, limit=2)
            row = rows[1] if len(rows) >= 2 else None
        if row is None:
            return {"ok": False, "reason": "没有可回滚的版本"}
        out = self.set_block(slug, row["body"], op="replace", note=f"回滚到 v{row['version']}", by=by)
        out["reverted_to"] = int(row["version"])
        return out

    # ---------------------------------------------------------------- 判断账本
    #
    # 读的时候一律把 JSON 字段解开，写的时候一律关起来：外面只跟 list/dict 打交道，
    # 不要每处调用都记得 json.loads。

    LEDGER_KINDS = ("judge", "road", "overturn", "miss")
    LEDGER_VERDICTS = ("pending", "已验证", "已推翻")
    _LEDGER_FIELDS = (
        "kind", "claim", "reason", "expected", "outcome", "verdict", "lesson",
        "alternatives", "chosen", "supersedes", "confidence", "stakes", "tags",
        "scope", "visibility", "status", "review_id", "source",
    )
    _LEDGER_JSON = ("alternatives", "tags")

    def _ledger_row(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for key in self._LEDGER_JSON:
            raw = d.get(key)
            try:
                d[key] = json.loads(raw) if isinstance(raw, str) and raw else []
            except json.JSONDecodeError:
                d[key] = []
        return d

    def add_judgment(
        self,
        *,
        kind: str = "judge",
        claim: str,
        reason: str = "",
        expected: str = "",
        outcome: str = "",
        verdict: str = "pending",
        lesson: str = "",
        alternatives: list | None = None,
        chosen: str = "",
        supersedes: int = 0,
        confidence: float = 0.5,
        stakes: str = "small",
        tags: list | None = None,
        scope: str = "global",
        visibility: str = "private",
        source: str = "her",
        now: float | None = None,
    ) -> int:
        now = now or time.time()
        kind = kind if kind in self.LEDGER_KINDS else "judge"
        verdict = verdict if verdict in self.LEDGER_VERDICTS else "pending"
        cur = self.conn.execute(
            """INSERT INTO judgment_ledger
               (kind, claim, reason, expected, outcome, verdict, lesson, alternatives, chosen,
                supersedes, confidence, stakes, tags, scope, visibility, source,
                created_at, resolved_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kind, str(claim).strip()[:400], str(reason or "")[:600],
                str(expected or "")[:400], str(outcome or "")[:600], verdict,
                str(lesson or "")[:400],
                json.dumps(alternatives or [], ensure_ascii=False),
                str(chosen or "")[:120], int(supersedes or 0),
                max(0.0, min(1.0, float(confidence))), str(stakes or "small"),
                json.dumps(tags or [], ensure_ascii=False), str(scope or "global"),
                str(visibility or "private"), str(source or "her"),
                now, now if verdict != "pending" else 0.0, now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_judgment(self, jid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM judgment_ledger WHERE id=? AND source <> 'auto:gate1'", (int(jid),)).fetchone()
        return self._ledger_row(row)

    def list_judgments(
        self,
        *,
        kind: str | None = None,
        verdict: str | None = None,
        status: str = "active",
        scope: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM judgment_ledger WHERE source <> 'auto:gate1'"
        args: list[Any] = []
        if status:
            sql += " AND status=?"
            args.append(str(status))
        if kind:
            sql += " AND kind=?"
            args.append(str(kind))
        if verdict:
            sql += " AND verdict=?"
            args.append(str(verdict))
        if scope:
            sql += " AND scope=?"
            args.append(str(scope))
        if since:
            sql += " AND created_at>=?"
            args.append(float(since))
        if until:
            sql += " AND created_at<?"
            args.append(float(until))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        return [self._ledger_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def update_judgment(self, jid: int, **fields: Any) -> dict:
        jid = int(jid)
        row = self.get_judgment(jid)
        if row is None:
            return {"ok": False, "reason": f"没有这条记录：{jid}"}
        sets, args = [], []
        for key, val in fields.items():
            if key not in self._LEDGER_FIELDS:
                continue
            if key in self._LEDGER_JSON:
                val = json.dumps(val or [], ensure_ascii=False)
            elif key in ("supersedes", "review_id"):
                val = int(val or 0)
            elif key == "confidence":
                val = max(0.0, min(1.0, float(val)))
            elif key == "verdict":
                val = val if val in self.LEDGER_VERDICTS else "pending"
            else:
                val = str(val or "")
            sets.append(f"{key}=?")
            args.append(val)
        if not sets:
            return {"ok": False, "reason": "没有可改的字段"}
        args.extend([time.time(), jid])
        self.conn.execute(
            f"UPDATE judgment_ledger SET {', '.join(sets)}, updated_at=? WHERE id=?", args
        )
        self.conn.commit()
        return {"ok": True, "record": self.get_judgment(jid)}

    def resolve_judgment(
        self,
        jid: int,
        *,
        outcome: str,
        verdict: str,
        lesson: str = "",
        now: float | None = None,
    ) -> dict:
        """回填结果。这是整本账唯一真正长本事的地方。

        只改状态不改 claim——判断可以错，但记录不许跟着改。改了就看不出自己错过，
        而这本账的全部价值就在于「我当时真的是这么想的」。
        """
        now = now or time.time()
        row = self.get_judgment(jid)
        if row is None:
            return {"ok": False, "reason": f"没有这条记录：{jid}"}
        if verdict not in ("已验证", "已推翻"):
            return {"ok": False, "reason": "verdict 只能是「已验证」或「已推翻」"}
        self.conn.execute(
            "UPDATE judgment_ledger SET outcome=?, verdict=?, lesson=?, resolved_at=?, updated_at=? WHERE id=?",
            (str(outcome or "")[:600], verdict, str(lesson or "")[:400], now, now, int(jid)),
        )
        # 被推翻的判断自动进偏差档案：她不必记得「错了还要另外记一笔」。
        if verdict == "已推翻":
            same = self.conn.execute(
                "SELECT id FROM judgment_ledger WHERE kind='miss' AND source=? AND claim=?",
                (f"auto:{int(jid)}", str(row["claim"])[:400]),
            ).fetchone()
            if not same:
                self.conn.execute(
                    """INSERT INTO judgment_ledger
                       (kind, claim, reason, expected, outcome, verdict, lesson, alternatives, chosen,
                        supersedes, confidence, stakes, tags, scope, visibility, source,
                        created_at, resolved_at, updated_at)
                       VALUES ('miss',?,?,?,?, '已推翻', ?, '[]', '', ?, ?, ?, ?, ?, ?, 'auto', ?, ?, ?)""",
                    (
                        str(row["claim"])[:400], str(row.get("reason") or "")[:600],
                        str(row.get("expected") or "")[:400], str(outcome or "")[:600],
                        str(lesson or row.get("lesson") or "")[:400], int(jid),
                        float(row.get("confidence") or 0.5), str(row.get("stakes") or "small"),
                        json.dumps(row.get("tags") or [], ensure_ascii=False),
                        str(row.get("scope") or "global"), str(row.get("visibility") or "private"),
                        now, now, now,
                    ),
                )
        self.conn.commit()
        return {"ok": True, "record": self.get_judgment(jid)}

    def find_judgments(self, text: str, *, open_only: bool = True, limit: int = 5) -> list[dict]:
        """按字面重叠找相近的判断。用于她回填时没写 id 的情况——「上次那个判断」也能对上。

        不上向量：账本条目少（几十上百条），字面二元组重叠够用，还省一次模型调用。
        """
        key = bigrams(text)
        if not key:
            return []
        pool = self.list_judgments(verdict="pending" if open_only else None, limit=300)
        scored = []
        for row in pool:
            hay = f"{row.get('claim') or ''}{row.get('reason') or ''}{row.get('expected') or ''}"
            overlap = len(set(key) & set(bigrams(hay)))
            if overlap:
                scored.append((overlap / max(1, len(set(key))), row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    def ledger_stats(self, since: float | None = None) -> dict:
        since = since or 0.0
        rows = self.conn.execute(
            """SELECT kind, verdict, count(*) n FROM judgment_ledger
               WHERE status='active' AND source <> 'auto:gate1' AND created_at>=? GROUP BY kind, verdict""",
            (since,),
        ).fetchall()
        out = {"total": 0, "by_kind": {}, "by_verdict": {}, "pending": 0, "resolved": 0}
        for r in rows:
            n = int(r["n"])
            out["total"] += n
            out["by_kind"][r["kind"]] = out["by_kind"].get(r["kind"], 0) + n
            out["by_verdict"][r["verdict"]] = out["by_verdict"].get(r["verdict"], 0) + n
        out["pending"] = int(out["by_verdict"].get("pending", 0))
        out["resolved"] = out["total"] - out["pending"]
        refuted = int(out["by_verdict"].get("已推翻", 0))
        out["hit_rate"] = round((out["resolved"] - refuted) / out["resolved"], 3) if out["resolved"] else None
        return out

    # ---------------------------------------------------------------- 阶段总结 / 连线

    def add_review(
        self,
        *,
        period_start: float,
        period_end: float,
        summary: str = "",
        patterns: list | None = None,
        links: list | None = None,
        counts: dict | None = None,
        created_by: str = "auto",
        now: float | None = None,
    ) -> int:
        now = now or time.time()
        cur = self.conn.execute(
            """INSERT INTO ledger_reviews
               (period_start, period_end, summary, patterns, links, counts, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                float(period_start), float(period_end), str(summary or "")[:2000],
                json.dumps(patterns or [], ensure_ascii=False),
                json.dumps(links or [], ensure_ascii=False),
                json.dumps(counts or {}, ensure_ascii=False),
                str(created_by or "auto"), now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_reviews(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ledger_reviews ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for key in ("patterns", "links", "counts"):
                try:
                    d[key] = json.loads(d.get(key) or ("[]" if key != "counts" else "{}"))
                except json.JSONDecodeError:
                    d[key] = [] if key != "counts" else {}
            out.append(d)
        return out

    def last_review_at(self) -> float:
        row = self.conn.execute("SELECT max(created_at) t FROM ledger_reviews").fetchone()
        return float(row["t"] or 0) if row else 0.0

    def add_link(
        self,
        a_id: int,
        b_id: int,
        *,
        relation: str = "同一件事",
        why: str = "",
        a_kind: str = "memory",
        b_kind: str = "memory",
        review_id: int = 0,
        now: float | None = None,
    ) -> bool:
        if int(a_id) == int(b_id) and a_kind == b_kind:
            return False
        from .memory_temporal import ARC
        if relation in ('纠正了','后来变了') or ARC.search(str(why or '')):
            # Arc claims need scoped raw evidence; use MemoryPipeline.link.
            return False
        # 无向边存两次太贵，也不便查询：统一按小 id 在前存一遍。
        if (int(b_id), b_kind) < (int(a_id), a_kind):
            a_id, b_id, a_kind, b_kind = b_id, a_id, b_kind, a_kind
        try:
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_pipeline_link_meta'").fetchone():
                removed = self.conn.execute(
                    "SELECT 1 FROM memory_links l JOIN memory_pipeline_link_meta p ON p.link_id=l.id "
                    "WHERE l.a_id=? AND l.b_id=? AND l.a_kind=? AND l.b_kind=? AND l.relation=? AND p.state='removed'",
                    (int(a_id), int(b_id), a_kind, b_kind, str(relation)[:40])).fetchone()
                if removed:
                    return False
            self.conn.execute(
                """INSERT OR IGNORE INTO memory_links
                   (a_id, b_id, a_kind, b_kind, relation, why, review_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (int(a_id), int(b_id), str(a_kind), str(b_kind), str(relation)[:40],
                 str(why or "")[:300], int(review_id or 0), now or time.time()),
            )
            self.conn.commit()
            return True
        except Exception:
            return False

    def list_links(self, *, memory_id: int | None = None, review_id: int | None = None,
                   limit: int = 50, include_history: bool = False, link_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM memory_links WHERE 1=1"
        args: list[Any] = []
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_pipeline_link_meta'").fetchone():
            sql += " AND NOT EXISTS (SELECT 1 FROM memory_pipeline_link_meta p WHERE p.link_id=memory_links.id AND p.state IN ('removed','orphaned'))"
        if memory_id:
            sql += " AND (a_id=? OR b_id=?)"
            args.extend([int(memory_id), int(memory_id)])
        if review_id:
            sql += " AND review_id=?"
            args.append(int(review_id))
        from .memory_temporal import current_link_sql
        suffix, stamps = current_link_sql(self.conn, 'memory_links') if not include_history and link_id is None else ('',[])
        sql += suffix
        args.extend(stamps)
        if link_id is not None:
            sql += ' AND id=?'
            args.append(int(link_id))
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def log_wake(
        self,
        conv_key: str,
        act: bool,
        score: float,
        threshold: float,
        reason: str,
        signals: dict | None = None,
        now: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO wake_decisions(ts, conv_key, act, score, threshold, reason, signals) VALUES (?,?,?,?,?,?,?)",
            (
                now or time.time(), str(conv_key), 1 if act else 0,
                float(score), float(threshold), str(reason or "")[:300],
                json.dumps(signals or {}, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_wakes(self, limit: int = 50, conv_key: str | None = None) -> list[dict]:
        sql = "SELECT * FROM wake_decisions"
        args: list[Any] = []
        if conv_key:
            sql += " WHERE conv_key=?"
            args.append(conv_key)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for r in self.conn.execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["signals"] = json.loads(d.get("signals") or "{}")
            except json.JSONDecodeError:
                d["signals"] = {}
            out.append(d)
        return out

    # ---------------------------------------------------------------- 工作记忆

    def add_working(
        self,
        session_id: str,
        text: str,
        kind: str = "todo",
        now: float | None = None,
        ttl_days: float = 3.0,
        keep: int = 5,
    ) -> int | None:
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:60]
        if len(text) < 2:
            return None
        now = now or time.time()
        for row in self.list_working(session_id, limit=20):
            if _bigram_sim(text, row["text"]) >= 0.5:
                return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO working(session_id, text, kind, created_at, expires_at) VALUES (?,?,?,?,?)",
            (session_id, text, kind, now, now + float(ttl_days) * 86400),
        )
        self.conn.execute(
            """DELETE FROM working WHERE session_id=? AND done=0 AND id NOT IN (
                   SELECT id FROM working WHERE session_id=? AND done=0
                   ORDER BY id DESC LIMIT ?)""",
            (session_id, session_id, int(keep)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_working(self, session_id: str, include_done: bool = False, limit: int = 10) -> list[dict]:
        sql = "SELECT * FROM working WHERE session_id=?"
        if not include_done:
            sql += " AND done=0"
        sql += " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, (session_id, int(limit))).fetchall()]

    def close_working(self, session_id: str, text: str, now: float | None = None) -> int:
        closed = 0
        for row in self.list_working(session_id, limit=20):
            if _bigram_sim(text, row["text"]) >= 0.5:
                self.conn.execute("UPDATE working SET done=1 WHERE id=?", (int(row["id"]),))
                closed += 1
        if closed:
            self.conn.commit()
        return closed

    def prune_working(self, now: float | None = None) -> int:
        now = now or time.time()
        cur = self.conn.execute("DELETE FROM working WHERE expires_at < ? OR done=1", (now,))
        self.conn.commit()
        return cur.rowcount

    # ---------------------------------------------------------------- 原始消息（语感分析用）

    def add_raw(
        self,
        session_id: str,
        role: str,
        text: str,
        sender: str = "",
        now: float | None = None,
        keep: int = 2000,
    ) -> None:
        text = str(text or "").strip()
        if not text:
            return
        self.conn.execute(
            "INSERT INTO raw_messages(session_id, role, sender, text, created_at) VALUES (?,?,?,?,?)",
            (str(session_id)[:80], role, str(sender)[:32], text[:400], now or time.time()),
        )
        self.conn.execute(
            "DELETE FROM raw_messages WHERE id NOT IN (SELECT id FROM raw_messages ORDER BY id DESC LIMIT ?)",
            (int(keep),),
        )
        self.conn.commit()

    def recent_raw(self, role: str, limit: int = 300) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM raw_messages WHERE role=? ORDER BY id DESC LIMIT ?",
            (role, int(limit)),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------------------------------------------------------------- 调参面板
    # 控制台写这里，服务每轮现读，改完立刻生效（不用重启 memory-core）。
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default

    def set_setting(self, key: str, value: Any, now: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now or time.time()),
        )
        self.conn.commit()

    def all_settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for row in self.conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall():
            try:
                out[str(row[0])] = json.loads(row[1])
            except Exception:
                out[str(row[0])] = row[1]
        return out

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM memories WHERE status='active') AS active,
                 (SELECT COUNT(*) FROM memories WHERE status='archived') AS archived,
                 (SELECT COUNT(*) FROM entities) AS entities,
                 (SELECT COUNT(*) FROM relations) AS relations,
                 (SELECT COUNT(*) FROM vectors) AS vectors,
                 (SELECT MIN(created_at) FROM memories) AS oldest"""
        ).fetchone()
        kinds = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM memories WHERE status='active' GROUP BY kind"
        ).fetchall()
        return {
            "active": int(row["active"]),
            "archived": int(row["archived"]),
            "entities": int(row["entities"]),
            "relations": int(row["relations"]),
            "vectors": int(row["vectors"]),
            "oldest": float(row["oldest"] or 0),
            "kinds": {r["kind"]: int(r["n"]) for r in kinds},
        }

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        return d
