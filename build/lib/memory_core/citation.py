"""Check a reply against the exact originals available to its sender."""
from __future__ import annotations

import re
import unicodedata

from .source_provenance import normalize, rank, readable, suppress_trace


CITATION_POLICY = {
    "version": 1,
    "endpoint": "/citation/check",
    "required_before_send": True,
    "on_unavailable": "block",
}
CHECK_UNAVAILABLE = "citation_check_unavailable"
_USER = re.compile(
    r"你(?:之前|以前|上次|昨天|前天|刚才|刚刚|刚)(?:跟我)?说(?:过)?|"
    r"(?:上次|之前|以前|昨天|前天)你说(?:过)?|我记得你说(?:过)?|你不是说过|"
    r"你(?:的)?(?:消息|原话|回复)里(?:写着|写了|提到)|你(?:说过|提到过)|"
    r"(?:另外|还有|而且)\s*你(?:答应|承诺|保证)(?:过)?")
_ASSISTANT = re.compile(
    r"我(?:之前|以前|上次|昨天|前天|刚才|刚刚|刚)(?:也)?说(?:过)?|"
    r"我(?:上一句|上句)(?:刚刚|刚才|刚)?说(?:过)?|"
    r"(?:上次|之前|刚才|刚刚)我说(?:过)?|我记得我说(?:过)?")
_INFERENCE = re.compile(r"说明|证明|意味着|表明|所以|因此|可见|也就是说|由此可知")
_LINKERS = r"而且|另外|还有|但是|不过|并且|同时|以及|接着|再说"
_ENDS = re.compile(r"[。！？!?\n]")
_CLOSERS = {"「": "」", "『": "』", "“": "”", '"': '"'}
_NOISE = (
    "你 我 他 她 它 您 你们 我们 他们 她们 它们 自己 这 那 这个 那个 这种 这样 那样 这些 那些 "
    "的 地 得 了 过 着 是 就是 有 没有 不 无 也 都 就 才 又 还 会 能 要 应该 必须 可以 已经 一直 真的 确实 "
    "把 被 在 从 到 向 给 为 和 与 或 及 而 对 将 让 用 个 条 件 次 一些 一个 "
    "说 提到 写着 写了 表示 认为 答应 承诺 保证 买 送 喜欢 不喜欢 喜爱 想 知道 看见 看到 "
    "今天 昨天 前天 今早 明天 上次 之前 刚才 刚刚 现在 当时 "
    "独立 认真 严格 单独 一起 重新 应当 当然 必然 正常 不断 永远"
).split()
_FUNCTIONS = re.compile("|".join(sorted(_NOISE, key=len, reverse=True)))
_WORDS = re.compile(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+")


def rejected(reason):
    return {
        "allowed": False, "checks": [], "stripped": None,
        "violations": [{"quote": "", "reason": reason}], "matches": [],
        "citing": False, "source_state": "absent",
    }


def _quoted_at(value, offset):
    start = offset + re.match(r"[\s:：]*", value[offset:]).end()
    close = _CLOSERS.get(value[start:start + 1])
    if close is None:
        return None
    last = value.find(close, start + 1)
    end = len(value) if last < 0 else last + 1
    return start, end, last >= 0, value[start + 1:last if last >= 0 else end]


def _support_pair(body, offset, quoted):
    if quoted:
        _, end, closed, quote = quoted
        rest = re.sub(r"^[\s，,；;。！？!?]*(?:(?:这|那)(?:种|样)?(?:话|内容|说法)|的话)?[\s，,；;。！？!?]*",
                      "", body[end - offset:])
        return dict(quote=quote, claim=rest, pure_hint=not rest, quote_closed=closed)
    pivot = _INFERENCE.search(body)
    quote = (re.sub(r"[\s，,；;]+$", "", body[:pivot.start()]) if pivot else body).lstrip(" \t\r\n:：").rstrip()
    return dict(quote=quote, claim=body[pivot.start():] if pivot else body, pure_hint=False, quote_closed=True)


def _fragments(text):
    markers = []
    protected_until = -1
    matches = sorted(
        [(m.start(), m, role) for pattern, role in ((_USER, "user"), (_ASSISTANT, "assistant"))
         for m in pattern.finditer(text)], key=lambda item: item[0])
    for start, match, role in matches:
        if start < protected_until:
            continue
        quoted = _quoted_at(text, match.end())
        if quoted:
            protected_until = quoted[1]
        markers.append((match, role, quoted))
    if not markers:
        return
    masked = list(text)
    for _, _, quoted in markers:
        if quoted:
            start, end, *_ = quoted
            masked[start:end] = _ENDS.sub(" ", text[start:end])
    for sentence in re.finditer(r"[^。！？!?\n]+[。！？!?\n]*", "".join(masked)):
        marked = [item for item in markers if sentence.start() <= item[0].start() < sentence.end()]
        if not marked:
            continue
        cuts = [sentence.start()]
        for (previous, _, _), (current, _, _) in zip(marked, marked[1:]):
            between = text[previous.end():current.start()]
            separator = max(between.rfind(c) for c in "，,；;")
            connector = separator >= 0 and re.fullmatch(rf"\s*(?:{_LINKERS}|但|还|也|又)?\s*", between[separator + 1:])
            linker = re.search(rf"(?:{_LINKERS})\s*$", between)
            cuts.append(previous.end() + separator + 1 if connector else
                        previous.end() + linker.start() if linker else current.start())
        cuts.append(sentence.end())
        for index, (marker, role, quoted) in enumerate(marked):
            start, end = cuts[index:index + 2]
            body = re.sub(r"[。！？!?\n]+$", "", text[marker.end():end])
            if quoted is None and index + 1 < len(marked):
                body = re.sub(r"[，,；;]\s*$", "", body)
            if role == "assistant" and marker.group().endswith("说") and re.match(r"\s*(?:错了|反了|漏了)", body):
                continue
            yield {
                "start": start, "end": end, "role": role, "text": text[start:end],
                "hint_only": quoted is None and marker.group().endswith("提到过") and not _INFERENCE.search(body),
                **_support_pair(body, marker.end(), quoted),
            }


def _claim_anchors(claim):
    # Word-like literal anchors only. This does not infer whether the claim follows.
    return list(dict.fromkeys(
        part for word in _WORDS.findall(claim)
        for part in (_FUNCTIONS.split(word) if "\u3400" <= word[0] <= "\u9fff" else (word,)) if part))


def _meta(source):
    return (source.get("meta_only") is True or source.get("support_target") == "utterance_only"
            or source.get("match_method") == "meta_quote" or source.get("evidence_kind") == "utterance_existence")


def _sources(snapshot):
    allowed = snapshot.get("allowed_raw_ids")
    records = snapshot.get("sources")
    if not isinstance(allowed, list) or not isinstance(records, list):
        raise ValueError("invalid_snapshot")
    if not all(isinstance(rid, str) and rid.strip() for rid in allowed):
        raise ValueError("invalid_snapshot")
    allowed = set(allowed)
    checkpoints = {}
    for group in ("allowed_from_injection", "allowed_from_transcript"):
        entries = snapshot.get(group, [])
        if not isinstance(entries, list):
            raise ValueError("invalid_snapshot")
        indexed = {}
        for entry in entries:
            if isinstance(entry, str):
                entry = {"raw_id": entry}
            if not isinstance(entry, dict) or not isinstance(entry.get("raw_id"), str):
                raise ValueError("invalid_snapshot")
            indexed.setdefault(entry["raw_id"], entry)
        checkpoints[group] = indexed
    scope = snapshot.get("scope")
    seen = []
    for source in records:
        if (not isinstance(source, dict) or not isinstance(source.get("raw_id"), str)
                or not isinstance(source.get("quote"), str)):
            raise ValueError("invalid_snapshot")
        if scope is not None and source.get("scope") is not None and source["scope"] != scope:
            continue
        if source["raw_id"] not in allowed:
            continue
        group = "allowed_from_transcript" if source.get("source_kind") == "transcript" else "allowed_from_injection"
        checkpoint = checkpoints[group].get(source["raw_id"])
        tags = normalize(source)
        if checkpoint is not None:
            limits = normalize({**checkpoint, "role": tags["send_role"]})
            if rank(limits) < rank(tags):
                tags = limits
        if tags["send_role"] not in ("user", "assistant") or rank(tags) == 0:
            continue
        seen.append({**source, **tags, "role": tags["send_role"],
                     "meta_only": _meta(source) or (checkpoint is not None and _meta(checkpoint))})
    restricted = [source for source in seen if source["meta_only"]]
    for source in seen:
        if any(source["role"] == item["role"] and (
                (source.get("archive_raw_id") or source["raw_id"]) == (item.get("archive_raw_id") or item["raw_id"])
                or (source.get("message_id") is not None and item.get("message_id") is not None
                    and str(source["message_id"]) == str(item["message_id"]))
                or source["quote"] == item["quote"]) for item in restricted):
            source.update(meta_only=True, match_method="meta_quote", support_target="utterance_only")
    return seen


def _state(sources):
    return {0: "absent", 1: "suspected", 2: "found"}[max(map(rank, sources), default=0)]


def _reply_state(checks):
    if checks and all(check["source_state"] == "found" for check in checks):
        return "found"
    return "suspected" if any(check["source_state"] == "suspected" for check in checks) else "absent"


def _overlap(quote):
    letters = 0
    for index, char in enumerate(quote):
        letters += char.isalnum()
        if letters == 6:
            return quote[:index + 1]
    return ""


def _check_fragment(fragment, sources):
    claims = []
    for claim in re.split(r"[，,；;]|而且|另外|并且|同时|还有", fragment["claim"]):
        pivot = _INFERENCE.search(claim)
        content = (claim[pivot.end():] if pivot else claim).strip()
        if content:
            claims.append(_claim_anchors(content))
    quote, role = fragment["quote"], fragment["role"]
    overlap = _overlap(quote)
    matches, limited = [], []
    matched = False
    meta_blocked = False
    hint_matched = False
    for source in sources:
        if source["role"] != role or not overlap or quote not in source["quote"]:
            continue
        matched = True
        disclaimer = re.sub(r"[\s，,；;。]", "", fragment["claim"]).rstrip("！") == "这句话本身是否成立另说"
        is_hint = fragment["pure_hint"] or fragment["hint_only"] or (source["meta_only"] and disclaimer)
        if source["meta_only"] and not is_hint:
            meta_blocked = True
            continue
        if (not source["meta_only"] and rank(source) == 1 and role != "assistant"
                and f"[{readable(source)}]" not in fragment["text"]
                and not re.search(r"转述|引用|来源不明|不确定|仅.*线索", fragment["text"])):
            limited.append(source)
            continue
        supports = fragment["quote_closed"] and (
            is_hint or bool(claims) and all(any(anchor in quote for anchor in group) for group in claims))
        if supports:
            hint_matched |= is_hint
            kind = source.get("source_kind") or "injection"
            matches.append({
                "raw_id": source["raw_id"], "overlap": overlap, "role": role, "source_kind": kind,
                "message_id": source.get("message_id"),
                "archive_raw_id": source.get("archive_raw_id") or (None if kind == "transcript" else source["raw_id"]),
                **normalize(source), "source_state": _state([source]),
                **({"meta_only": True, "match_method": "meta_quote"} if source["meta_only"] else {}),
            })
    if matches:
        reason = "supported"
    elif not fragment["quote_closed"]:
        reason = "quote_unclosed"
    elif meta_blocked:
        reason = "meta_only_cannot_support_claim"
    elif limited:
        reason = "source_requires_qualification"
    elif not matched:
        reason = "no_allowed_raw_match"
    elif not fragment["pure_hint"] and (not claims or any(not group for group in claims)):
        reason = "claim_anchor_missing"
    else:
        reason = "claim_object_mismatch"
    return {**fragment, "pure_hint": hint_matched, "anchors": claims, "allowed": bool(matches), "matches": matches,
            "source_state": _state(matches or limited), "reason": reason}


def check_reply(reply_text, injection_snapshot):
    """Pure sender-side verdict. A returned stripped version has been rechecked."""
    if not isinstance(reply_text, str) or len(reply_text) > 16000:
        return rejected("invalid_reply")
    if not isinstance(injection_snapshot, dict):
        return rejected("invalid_snapshot")
    try:
        with suppress_trace():
            sources = _sources(injection_snapshot)
            checks = [_check_fragment(fragment, sources) for fragment in _fragments(reply_text)]
            violations = [check for check in checks if not check["allowed"]]
            final = checks
            stripped = None
            if violations:
                clean = reply_text
                for check in reversed(violations):
                    before, after = clean[:check["start"]], clean[check["end"]:]
                    if before and after and not re.search(r"[。！？!?\n]\s*$", before):
                        before = re.sub(r"[，,；;\s]+$", "", before) + "。"
                    clean = before + after
                clean = clean.strip().strip("，,；;").strip()
                if any(char.isalnum() or unicodedata.category(char).startswith("S") for char in clean):
                    final = [_check_fragment(fragment, sources) for fragment in _fragments(clean)]
                    if all(check["allowed"] for check in final):
                        stripped = clean
                if stripped is None:
                    final = []
    except (TypeError, ValueError):
        return rejected("invalid_snapshot")
    matches = [match for check in final for match in check["matches"]]
    return {
        "allowed": not violations, "checks": checks, "stripped": stripped,
        "violations": [{"quote": check["quote"], "reason": check["reason"]} for check in violations],
        "matches": matches, "citing": bool(checks), "source_state": _reply_state(final if stripped else checks),
    }


def citation_request(data):
    if not isinstance(data, dict) or set(data) != {"reply", "snapshot"}:
        return rejected("invalid_citation_request")
    return check_reply(data["reply"], data["snapshot"])
