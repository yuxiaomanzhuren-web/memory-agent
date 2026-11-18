# -*- coding: utf-8 -*-
"""L0 原文库：按轮 append 到按月分片的 JSONL，零加工、永不丢。

和 memories 表的分工（这是这一层存在的全部理由）：

- **这里**存「他说了什么、我回了一句什么」的原话。不总结、不提炼、不覆盖、不删除。
- **memories** 存从原话里提炼出来的事实。会衰减、会归档、会被后来的记忆挤下去。

以前这两件事挤在同一张表里，结果是召回被几百字的原文全文霸占——
长文里几乎任何查询词都有二元组命中，短事实永远比不过它。
分层之后：`memories` 只放提炼稿，想翻原话就来这里。

检索是两级的：先查 memories（快、短、经过整理），需要「那天到底说了什么」时再回 L0。
"""

from __future__ import annotations

import json
import hashlib
from .source_provenance import classify_source, normalize, FIELDS
import math
import os
import glob
import random
import threading
import time

# 检索时最多往回翻多少个月的分片（当月之前）
MAX_MONTHS_BACK = 2

# 出现在超过这个比例消息里的二元组当作虚词组合，不参与打分
# （「那个」「了吗」「说的」这类词每段对话都有，拿它算相关度等于没算）
DF_CUTOFF = 0.20

# 虚词字：由它们组成的二元组一律不参与打分。
# 「上次说的那个报告」→ 只留 上次、报告；「那个」「说的」不参与。
# 注意不要收录「上/下/中/里/好/大/小/多/少/看/来/去」这类常做实词的字。
STOP_CHARS = set(
    "的了着过吗呢吧啊呀哦嘛嗯是我你他她它这那个说会要不得把被给和与都也就还"
    "从到对很太更最但又再才让如果因为所以什怎谁些该"
)


def _content_grams(text: str) -> list[str]:
    """只保留两个字都是实词的二元组——用来判断「这句话在讲什么事」。"""
    t = "".join(ch for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    out = []
    for i in range(len(t) - 1):
        g = t[i:i + 2]
        if g[0] in STOP_CHARS or g[1] in STOP_CHARS:
            continue
        out.append(g)
    return out


# 疑问短语：问的是「哪件事」，不是这些词本身。
# 「我什么时候该睡觉」剥完只剩「睡觉」，才不会去匹配一堆「你想什么时候找我」。
QUESTION_PHRASES = (
    "什么时候", "什么时间", "怎么样", "怎么办", "怎么", "为什么", "为啥",
    "是不是", "有没有", "多少", "多久", "哪里", "哪个", "哪些", "几点", "什么",
)


def _strip_question(text: str) -> str:
    t = str(text or "")
    for p in sorted(QUESTION_PHRASES, key=len, reverse=True):
        t = t.replace(p, " ")
    return t


def _month_key(ts: float) -> str:
    return time.strftime("%Y-%m", time.localtime(float(ts)))


def timestamp_seconds(ts: float | None) -> float:
    value = time.time() if ts is None else float(ts)
    if value > 1e11:
        value /= 1000
    if not math.isfinite(value):
        raise ValueError('ts must be finite epoch seconds or milliseconds')
    return value


def valid_speaker(role) -> bool:
    from .memory_evidence import valid_agent_id
    return isinstance(role, str) and (
        role in ('user', 'assistant') or role.startswith('other:') and valid_agent_id(role[6:]))


def _prev_month(month: str, back: int = 1) -> str:
    year, mon = (int(x) for x in month.split("-"))
    mon -= back
    while mon <= 0:
        mon += 12
        year -= 1
    return f"{year:04d}-{mon:02d}"


def _bigrams(text: str) -> list[str]:
    """和 store.bigrams 保持一致：字符二元组，不依赖分词。"""
    t = "".join(ch for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    if len(t) <= 1:
        return [t] if t else []
    return [t[i:i + 2] for i in range(len(t) - 1)]


class RawLog:
    """按月分片的 append-only 原文库。"""

    def __init__(self, root: str):
        self.root = str(root)
        self._lock = threading.Lock()
        self.provenance_lookup = None
        os.makedirs(self.root, exist_ok=True)

    # ---------------------------------------------------------------- 写入

    def _path(self, ts: float) -> str:
        return os.path.join(self.root, _month_key(ts) + ".jsonl")

    def _new_id(self, ts: float) -> str:
        return "%d-%04d" % (int(ts * 1000), random.randint(0, 9999))

    def append(
        self,
        session: str,
        role: str,
        text: str,
        ts: float | None = None,
        extra: dict | None = None,
        *,
        strict: bool = False,
    ) -> str | None:
        """写一条原话。返回 id；空文本直接忽略（不占位，免得留下空行）。"""
        body = str(text or "")
        if not body.strip():
            return None
        if strict and not valid_speaker(role):
            raise ValueError('invalid_speaker')
        ts = timestamp_seconds(ts)
        rec = {
            "id": self._new_id(ts) if not strict else self._event_id(ts),
            "ts": round(ts, 3),
            "s": str(session or "private"),
            "r": role if strict else ("assistant" if str(role).startswith("a") else "user"),
            "t": body[:8000],
        }
        if extra:
            for k, v in dict(extra).items():
                if k not in rec:
                    rec[k] = v
        rec.update(normalize(rec,stage='raw_ingress') if any(k in rec for k in ('stance','support_scope','classifier_version')) else classify_source(rec))
        message_id = rec.get('message_id')
        if message_id is not None:
            message_id = str(message_id).strip()
            if not message_id:
                raise ValueError('message_id must not be empty')
            rec['message_id'] = message_id
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            if message_id is not None:
                existing = self._find_message(message_id, rec['s'], None if strict else rec['r'], strict=strict)
                if existing:
                    if strict and any(existing.get(key) != rec.get(key) for key in ('r', 't', 'ts', 'ingest_source_kind')):
                        raise ValueError('message_id_conflict')
                    return str(existing.get('id'))
            with open(self._path(ts), "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        return rec["id"]

    @staticmethod
    def _event_id(ts: float) -> str:
        from uuid import uuid4
        return f"{int(ts * 1000)}-{uuid4().hex}"

    def _find_message(self, message_id, session=None, role=None, *, strict=False):
        # ponytail: linear archive scan; add an index if archive size slows ingress.
        wanted = str(message_id or '').strip()
        if not wanted:
            return None
        for path in glob.glob(os.path.join(self.root, '*.jsonl')):
            try:
                with open(path, 'r', encoding='utf-8') as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        if (isinstance(row, dict) and str(row.get('message_id', '')).strip() == wanted
                                and (session is None or row.get('s') == session)
                                and (role is None or row.get('r') == role)):
                            return row
            except OSError:
                if strict:
                    raise
        return None

    def find_message_id(self, message_id: str, session: str | None = None, role: str | None = None) -> str | None:
        """Return the existing raw id for an upstream message id, if any."""
        row = self._find_message(message_id, session, role)
        return str(row.get('id')) if row else None

    def append_turn(
        self,
        session: str,
        user: str = "",
        reply: str = "",
        ts: float | None = None,
    ) -> list[str]:
        """一整轮一次调用：他说的和她回的差几毫秒，但都落在同一分钟里。"""
        ts = timestamp_seconds(ts)
        ids = []
        for role, text in (("user", user), ("assistant", reply)):
            got = self.append(session, role, text, ts=ts)
            if got:
                ids.append(got)
        return ids

    # ---------------------------------------------------------------- 读取

    def _iter_month(self, month: str, since: float = 0.0):
        path = os.path.join(self.root, month + ".jsonl")
        if not os.path.exists(path):
            return
        try:
            with open(path, "rb") as f:
                for blob in f:
                    digest = hashlib.sha256(blob).hexdigest()
                    line = blob.decode('utf-8')
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if float(rec.get("ts") or 0) < since:
                        continue
                    rec["_raw_sha256"] = digest
                    yield rec
        except OSError:
            return

    def source_tags(self, record):
        return self.provenance_lookup(record) if self.provenance_lookup else normalize(record, stage="raw_read")

    def source_tags_for(self, ids, session):
        wanted=set(ids);out=[]
        for path in sorted(glob.glob(os.path.join(self.root, '*.jsonl'))):
            for rec in self._iter_month(os.path.basename(path)[:-6]):
                if rec.get('id') in wanted and rec.get('s')==session:
                    out.append(dict(raw_id=rec['id'],message_id=rec.get('message_id'),**self.source_tags(rec)))
                    wanted.remove(rec['id'])
            if not wanted:break
        return out

    def search(
        self,
        query: str,
        limit: int = 10,
        days: int = 45,
        session: str = "",
    ) -> list[dict]:
        """按 idf 加权命中率排序，长度做惩罚。

        L0 是「翻原话」用的，宁可慢一点、准一点，不要被长段聊天记录带偏。

        关键点：查询里的「那个」「了吗」「说的」在几乎每段聊天里都出现，
        如果按原始命中率算，「上次说的那个报告」会命中一堆「昨晚说的那个口子」。
        所以先统计每个二元组在这个时间窗里出现过多少条消息（df），
        只让出现率低于 DF_CUTOFF 的词参与打分——它们的 idf 才代表「这句话在讲什么」。
        """
        q = str(query or "").strip()
        if not q:
            return []
        grams = _bigrams(q)
        if not grams:
            return []
        since = time.time() - max(1, int(days)) * 86400
        month = _month_key(time.time())
        months = [month] + [_prev_month(month, i) for i in range(1, MAX_MONTHS_BACK + 1)]

        # 先把候选收进内存，才能算 df（时间窗内就几千条，几十毫秒）
        cands: list[tuple[str, dict]] = []
        seen: set[str] = set()
        for mon in months:
            for rec in self._iter_month(mon, since=since):
                if session and rec.get("s") != session:
                    continue
                text = str(rec.get("t") or "")
                if not text:
                    continue
                rid = str(rec.get("id") or "")
                if rid and rid in seen:
                    continue
                seen.add(rid)
                cands.append((text.lower(), rec))
        if not cands:
            return []

        n = len(cands)
        strong_cand = _content_grams(_strip_question(q))
        need = set(grams) | set(strong_cand)
        df = {g: 0 for g in need}
        for low, _ in cands:
            for g in need:
                if g in low:
                    df[g] += 1

        # 优先用实词组合打分
        strong = [g for g in strong_cand if df.get(g, 0) > 0]
        scored: list[tuple[float, dict]] = []

        if strong:
            core = [g for g in strong if df[g] <= n * DF_CUTOFF] or strong
            w = {g: math.log((n + 1.0) / (df[g] + 0.5)) for g in core}
            den = sum(w.values()) or 1.0
            for low, rec in cands:
                got = [g for g in core if g in low]
                if not got:
                    continue
                cover = sum(w[g] for g in got) / den
                # 命中多个词加分，长度做温和惩罚：同一句话，短的更可能是「答案」
                score = cover * (1.0 + 0.15 * len(got)) / (1.0 + max(0, len(low) - 80) / 600.0)
                scored.append((score, rec))
        elif strong_cand:
            # 实词组合在库里一次也没出现过（比如问「我叫什么」，库里只有「叫我大肥鱼」）。
            # 这时退到实词的单字，但要求这些字**全部**出现——宁可查不到，也别拿
            # 「你还记得我为你改过什么吗」去冒充「你还记得我叫什么吗」。
            chars = sorted({c for g in strong_cand for c in g if c not in STOP_CHARS})
            for low, rec in cands:
                if not all(c in low for c in chars):
                    continue
                score = 1.0 / (1.0 + max(0, len(low) - 40) / 300.0)
                scored.append((score, rec))
        else:
            # 整句都是虚词/代词（「在吗」这种）：短查询本来就是靠字面命中，按原样匹配。
            # 长句（「你还记得我叫什么吗」）用虚词定位没有意义——那样只会把
            # 「你还记得我为你改过什么吗」当成答案，宁可不给。
            if len(grams) > 6:
                return []
            core = [g for g in grams if df.get(g, 0) > 0]
            if not core:
                return []
            for low, rec in cands:
                got = [g for g in core if g in low]
                if not got:
                    continue
                score = (len(got) / len(core)) / (1.0 + max(0, len(low) - 80) / 600.0)
                scored.append((score, rec))

        scored.sort(key=lambda kv: (kv[0], float(kv[1].get("ts") or 0)), reverse=True)
        out = []
        for score, rec in scored[: int(limit)]:
            out.append({
                "id": rec.get("id"),
                "ts": rec.get("ts"),
                "session": rec.get("s"),
                "role": rec.get("r"),
                "text": rec.get("t"),
                "score": round(float(score), 4),
                **self.source_tags(rec),
            })
        return out

    def recent(self, limit: int = 20, session: str = "") -> list[dict]:
        month = _month_key(time.time())
        months = [month] + [_prev_month(month, 1)]
        rows: list[dict] = []
        for mon in months:
            for rec in self._iter_month(mon):
                if session and rec.get("s") != session:
                    continue
                rows.append(rec)
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return [
            {"id": r.get("id"), "ts": r.get("ts"), "session": r.get("s"),
             "role": r.get("r"), "text": r.get("t"), **self.source_tags(r)}
            for r in rows[: int(limit)]
        ]

    def stats(self) -> dict:
        files = []
        total = 0
        size = 0
        try:
            names = sorted(n for n in os.listdir(self.root) if n.endswith(".jsonl"))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(self.root, name)
            try:
                n = 0
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            n += 1
                total += n
                size += os.path.getsize(path)
                files.append({"month": name[:-6], "lines": n})
            except OSError:
                continue
        return {"root": self.root, "months": files, "lines": total, "bytes": size}
