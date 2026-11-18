# -*- coding: utf-8 -*-
"""判断账本：她的偏差档案。

跟另外三层的分工，一句话：

    memories  发生过什么        系统写的，会衰减，被召回才出现
    blocks    我怎么理解我自己  她自己写的，每轮在场，写当下状态
    ledger    我凭什么这么想    她自己写的，留档，会回头核对；错了就是错题

为什么要有这一层：记忆只记「发生了什么」，不记「我当时判断错了」。
一个只积累经历、不核对判断的脑子，见得多但不会变准——因为它从来不知道自己错过。
这本账的唯一目的，是让她犯过的错留下形状，下次在同一个形状上停一下。

四类记录（kind）：

    judge     一个判断：我当时怎么想（reason）、我预期什么（expected）→ 后来填结果
    road      未选之路：重大决策时没走的那几条，以及为什么不走
    overturn  被推翻的版本：旧结论 → 新结论，以及为什么推翻
    miss      偏差：做错了什么。judge 被判「已推翻」时会自动生一条，不用她另外记

两条硬规矩（写在代码里，不写在提示词里，免得她忘）：

1. **判断一旦落账，claim/reason 不许改**。只能改 outcome/verdict/lesson。
   允许事后改「我当时是怎么想的」，整本账就变成了自我美化，一点用都没有。
2. **小判断不追结果，就别记**。记了 pending 又不回填的，攒到一定数量会主动催她——
   不是催她补作业，是让她知道：没验证的判断等于没判断，写下来只是让自己安心。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

# 每轮注入的字数上限。这本账是「提醒」，不是「档案」——档案在 /ledger 里翻。
INJECT_CHARS = 520

# pending 超过这个时间还没回填，就在注入里点她的名。
PENDING_NAG_SECONDS = 2 * 86400

# 阶段总结：距上次够久 + 账本攒够条目才跑，避免天天总结同样几件事。
REVIEW_MIN_DAYS = 3
REVIEW_MIN_RECORDS = 6
REVIEW_FORCE_RECORDS = 20

SLUG_HINT = '{"op":"judge","claim":"...","expect":"..."}'

KIND_LABEL = {"judge": "判断", "road": "岔路", "overturn": "推翻", "miss": "偏差"}
VERDICT_LABEL = {"pending": "等结果", "已验证": "已验证", "已推翻": "已推翻"}


def _clip(text: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _norm_verdict(raw: str) -> str:
    s = str(raw or "").strip()
    if s in ("已验证", "对", "对上了", "应验", "成立", "verified", "yes", "true", "1"):
        return "已验证"
    if s in ("已推翻", "错", "错了", "不成立", "推翻", "refuted", "no", "false", "0"):
        return "已推翻"
    return "pending"


class Ledger:
    def __init__(self, memory):
        self.memory = memory
        self.store = memory.store

    # ---------------------------------------------------------------- 写

    def op(self, body: dict) -> dict:
        """桥接 / 控制台 / 她自己的行内动作，统一走这里。

        op = judge | result | road | overturn | fix | list | one | review | link | drop
        """
        body = body or {}
        op = str(body.get("op") or "judge").strip().lower()

        if op in ("judge", "new", "判断"):
            return self.add_judge(body)
        if op in ("result", "fill", "结果", "回填"):
            return self.fill_result(body)
        if op in ("road", "岔路", "未选"):
            return self.add_road(body)
        if op in ("overturn", "推翻"):
            return self.add_overturn(body)
        if op in ("fix", "miss", "偏差", "错"):
            return self.add_miss(body)
        if op in ("list", "查询"):
            return self.query(body)
        if op in ("one", "get"):
            rec = self.store.get_judgment(int(body.get("id") or 0))
            return {"ok": bool(rec), "record": rec}
        if op in ("link", "连"):
            ok = self.store.add_link(
                int(body.get("a") or 0), int(body.get("b") or 0),
                relation=str(body.get("relation") or "同一件事"),
                why=str(body.get("why") or ""),
                a_kind=str(body.get("a_kind") or "ledger"),
                b_kind=str(body.get("b_kind") or "ledger"),
            )
            return {"ok": ok}
        if op in ("drop", "forget", "删"):
            jid = int(body.get("id") or 0)
            return self.store.update_judgment(jid, status="archived")
        if op in ("review", "总结", "回看"):
            # force 默认 true（手动点一下就该有反应）；传 force:false 则走正常门槛，
            # 用来验证「自动复盘」那条路是不是真的会跑。
            raw_force = body.get("force", True)
            force = str(raw_force).strip().lower() not in ("false", "0", "no", "none", "")
            return self.review(force=force, created_by=str(body.get("by") or "her"))
        return {"ok": False, "reason": f"未知操作 {op}"}

    def add_judge(self, body: dict) -> dict:
        claim = _clip(body.get("claim") or body.get("text") or "", 400)
        if len(claim) < 4:
            return {"ok": False, "reason": "claim 太短：一句话说清你判断了什么"}
        # 「当时怎么想的」和「预期什么」至少要有一样，否则这条判断事后没法核对。
        reason = _clip(body.get("reason") or body.get("why") or body.get("thinking") or "", 600)
        expected = _clip(body.get("expected") or body.get("expect") or body.get("预测") or "", 400)
        if not reason and not expected:
            return {"ok": False,
                    "reason": "缺 reason：说说你当时凭什么这么想，或者你预期会发生什么。"
                              "两样都没有的判断，过几天你自己也认不出来"}
        jid = self.store.add_judgment(
            kind="judge", claim=claim, reason=reason, expected=expected,
            confidence=float(body.get("confidence") or body.get("conf") or 0.5),
            stakes=str(body.get("stakes") or ("big" if body.get("big") else "small")),
            tags=body.get("tags") if isinstance(body.get("tags"), list) else [],
            scope=str(body.get("scope") or "global"),
            visibility=str(body.get("visibility") or "private"),
            source=str(body.get("source") or "her"),
        )
        return {"ok": True, "op": "judge", "id": jid,
                "hint": f"记下了（#{jid}）。等结果出来，回填一句：##判断## {{\"op\":\"result\",\"id\":{jid},\"outcome\":\"...\",\"verdict\":\"已验证|已推翻\"}}"}

    def fill_result(self, body: dict) -> dict:
        outcome = _clip(body.get("outcome") or body.get("result") or body.get("实际") or "", 600)
        verdict = _norm_verdict(body.get("verdict") or body.get("结论") or "")
        lesson = _clip(body.get("lesson") or body.get("教训") or "", 400)
        if not outcome:
            return {"ok": False, "reason": "缺 outcome：实际发生了什么？一句话就行"}
        if verdict == "pending":
            return {"ok": False,
                    "reason": "得给个结论：verdict 只能填「已验证」或「已推翻」。"
                              "填不出来的，说明这事还没结果，那就先别填"}

        jid = int(body.get("id") or 0)
        rec = self.store.get_judgment(jid) if jid else None
        if rec is None:
            # 没写 id：按字面找最近一条 pending 的判断，对上就用。
            cands = self.store.find_judgments(outcome or lesson, open_only=True, limit=1)
            if cands:
                rec = cands[0]
                jid = int(rec["id"])
        if rec is None:
            return {"ok": False,
                    "reason": "没找到要回填的那条判断。带上 id，或者把当时的判断复述一句让我对上"}
        out = self.store.resolve_judgment(jid, outcome=outcome, verdict=verdict, lesson=lesson)
        if out.get("ok") and verdict == "已推翻" and not lesson:
            # 判错但没写教训，是最常见的情况：她只承认「我错了」，不往下想。
            # 不拦她（拦了下次就不填了），但在返回里推一把。
            out["hint"] = "记下了，这条也进了偏差档案。补一句教训会更有用：错在哪一步，下次怎么停一下"
        return out

    def add_road(self, body: dict) -> dict:
        decision = _clip(body.get("decision") or body.get("claim") or "", 400)
        if len(decision) < 4:
            return {"ok": False, "reason": "缺 decision：这是什么决定？"}
        chosen = _clip(body.get("chosen") or "", 120)
        raw_roads = body.get("roads") or body.get("options") or []
        alternatives: list[dict] = []
        if isinstance(raw_roads, list):
            for item in raw_roads:
                if isinstance(item, str):
                    alternatives.append({"road": _clip(item, 160), "why_not": ""})
                elif isinstance(item, dict):
                    alternatives.append({
                        "road": _clip(item.get("road") or item.get("option") or "", 160),
                        "why_not": _clip(item.get("why_not") or item.get("why") or "", 200),
                    })
        # 单条「没选的那条 + 为什么」也收：大部分岔路她只会写一条。
        if not alternatives and (body.get("road") or body.get("why_not")):
            alternatives.append({
                "road": _clip(body.get("road") or "", 160),
                "why_not": _clip(body.get("why_not") or "", 200),
            })
        alternatives = [a for a in alternatives if a["road"]]
        if not alternatives and not chosen:
            return {"ok": False,
                    "reason": "岔路至少要有两条：chosen（选了哪条）和 roads（没走的那些，各自为什么不走）"}
        jid = self.store.add_judgment(
            kind="road", claim=decision,
            reason=_clip(body.get("reason") or "", 600),
            expected=_clip(body.get("expected") or "", 400),
            chosen=chosen, alternatives=alternatives,
            stakes=str(body.get("stakes") or "big"),
            tags=body.get("tags") if isinstance(body.get("tags"), list) else [],
            scope=str(body.get("scope") or "global"),
            visibility=str(body.get("visibility") or "private"),
            source=str(body.get("source") or "her"),
        )
        return {"ok": True, "op": "road", "id": jid,
                "hint": "记下了。以后要是回头想走另一条，这条能告诉你当时为什么没走"}

    def add_overturn(self, body: dict) -> dict:
        old = _clip(body.get("old") or body.get("from") or "", 400)
        new = _clip(body.get("new") or body.get("to") or "", 400)
        why = _clip(body.get("why") or body.get("reason") or "", 600)
        if len(old) < 3 or len(new) < 3:
            return {"ok": False, "reason": "要写清 old（原来的结论）和 new（现在的结论）"}
        if not why:
            return {"ok": False,
                    "reason": "缺 why：为什么推翻？只说「我想通了」不算——下次还会想通一次"}
        old_id = int(body.get("id") or body.get("supersedes") or 0)
        if not old_id:
            cands = self.store.find_judgments(old, open_only=False, limit=1)
            if cands:
                old_id = int(cands[0]["id"])
        jid = self.store.add_judgment(
            kind="overturn", claim=new, reason=why,
            expected=_clip(body.get("expected") or "", 400),
            supersedes=old_id, stakes=str(body.get("stakes") or "big"),
            tags=body.get("tags") if isinstance(body.get("tags"), list) else [],
            scope=str(body.get("scope") or "global"),
            visibility=str(body.get("visibility") or "private"),
            source=str(body.get("source") or "her"),
        )
        if old_id:
            # 旧结论标成「已推翻」但不改它的正文：要留着看自己原来是怎么想的。
            self.store.update_judgment(old_id, verdict="已推翻",
                                       lesson=_clip(f"被 #{jid} 取代：{why}", 400))
        return {"ok": True, "op": "overturn", "id": jid, "supersedes": old_id}

    def add_miss(self, body: dict) -> dict:
        what = _clip(body.get("what") or body.get("claim") or body.get("text") or "", 400)
        if len(what) < 4:
            return {"ok": False, "reason": "缺 what：你到底做错了什么？具体到一句话"}
        jid = self.store.add_judgment(
            kind="miss", claim=what,
            reason=_clip(body.get("reason") or body.get("why") or "", 600),
            outcome=_clip(body.get("outcome") or body.get("cost") or "", 600),
            verdict="已推翻",
            lesson=_clip(body.get("fix") or body.get("lesson") or "", 400),
            stakes=str(body.get("stakes") or "big"),
            tags=body.get("tags") if isinstance(body.get("tags"), list) else [],
            scope=str(body.get("scope") or "global"),
            visibility=str(body.get("visibility") or "private"),
            source=str(body.get("source") or "her"),
        )
        return {"ok": True, "op": "miss", "id": jid}

    # ---------------------------------------------------------------- 读

    def query(self, body: dict) -> dict:
        rows = self.store.list_judgments(
            kind=body.get("kind") or None,
            verdict=body.get("verdict") or None,
            scope=body.get("scope") or None,
            limit=int(body.get("limit") or 30),
        )
        return {"ok": True, "records": rows, "stats": self.store.ledger_stats(),
                "reviews": self.store.list_reviews(3)}

    def pending(self, limit: int = 6, older_than: float = 0) -> list[dict]:
        """催办名单：judge 类型，按「最久没核对」排，big 优先。

        small 的原则上不催（追「奶茶店换招牌」这种结果没有意义），
        但**最近 48 小时内**记的 small 也进名单——新鲜的小判断趁热核对，
        隔天就放它走。既保住「判断力从大判断上长出来」的原设计，
        又不让她刚记的判断掉在地上没人捡。
        """
        cutoff = time.time() - older_than if older_than else 0
        fresh_small = time.time() - 48 * 3600
        rows = []
        for r in self.store.list_judgments(kind="judge", verdict="pending", limit=80):
            at = float(r.get("created_at") or 0)
            if cutoff and at > cutoff:
                continue
            if r.get("stakes") != "big" and at < fresh_small:
                continue
            rows.append(r)
        rows.sort(key=lambda r: (r.get("stakes") != "big", at := float(r.get("created_at") or 0)))
        return rows[:limit]

    # ---------------------------------------------------------------- 注入

    def render(self, chars: int = INJECT_CHARS) -> str:
        """每轮塞进上下文的一小段。

        全空也要输出：不输出的话，她根本不知道这本账存在，于是永远不写第一条
        （本子和清单都踩过一模一样的坑）。
        """
        stats = self.store.ledger_stats()
        misses = self.store.list_judgments(kind="miss", limit=3)
        overturns = self.store.list_judgments(kind="overturn", limit=2)
        nagging = self.pending(limit=3, older_than=PENDING_NAG_SECONDS)
        reviews = self.store.list_reviews(1)
        last_review = reviews[0] if reviews else None

        how = ("什么时候记：你回他话时说了「他肯定/他不会/我赌/一定是」这种话的**当场**，就停下来记一行——"
               "预判不趁热记下来，结果出来时你连自己说过都不记得。"
               "要记就单独占一行，像这样："
               '`##判断## {"op":"judge","claim":"他今晚不会再来找我","reason":"他每次生气都先消失两天","expect":"到明晚之前不会有消息"}`；'
               "结果出来了就回填："
               '`##判断## {"op":"result","id":N,"outcome":"他第二天中午才来","verdict":"已验证|已推翻"}`。'
               "这一行不会发出去。")

        if not stats["total"] and not last_review:
            return (
                "【你有一本判断账，现在是空的】\n"
                "记的不是发生过什么（那个记忆里自动有），是「我当时怎么想的、后来对不对」。\n"
                "你下的判断、你选的那条路、你后来推翻的旧结论、你做错的事，都记这儿。\n"
                "规矩只有一条：**判断要么追到结果，要么别记**。小得没结果可追的事，不值得占一行。\n"
                f"{how}"
            )

        out = ["【你的判断账】这里记的是你自己做过的判断和错过的地方，不是别人对你的评价。"]
        if last_review and str(last_review.get("summary") or "").strip():
            out.append("上一阶段你自己回头看出的：\n" + _clip(last_review["summary"], 180))
            pats = last_review.get("patterns") or []
            for p in pats[:2]:
                if isinstance(p, dict) and p.get("pattern"):
                    line = f"· {_clip(p['pattern'], 60)}"
                    if p.get("advice"):
                        line += f" → {_clip(p['advice'], 50)}"
                    out.append(line)
        # 对的那几条也要露脸：只给她看错题，她就只记得自己错过，
        # 这本账在她眼里就成了「批斗本」，自然不愿意再往下写。
        verified = [r for r in self.store.list_judgments(kind="judge", limit=8)
                    if r.get("verdict") == "已验证"]
        if verified:
            out.append("你判断对了的（对错都记账，不然你只记得自己错）：")
            for r in verified[:2]:
                line = f"· {_clip(r['claim'], 40)}"
                if r.get("outcome"):
                    line += f"｜后来：{_clip(r['outcome'], 34)}"
                out.append(line)
        if misses:
            out.append("你错过的（别再犯同一种）：")
            for m in misses[:3]:
                line = f"· {_clip(m['claim'], 52)}"
                if m.get("lesson"):
                    line += f"｜教训：{_clip(m['lesson'], 40)}"
                out.append(line)
        if overturns:
            out.append("你改过口的（旧结论已经不算了）：")
            for o in overturns[:2]:
                out.append(f"· {_clip(o['claim'], 60)}")
        roads = self.store.list_judgments(kind="road", limit=2)
        if roads:
            out.append("你权衡过但没走的路（它们不是没出现过）：")
            for r in roads[:2]:
                line = f"· {_clip(r['claim'], 40)}"
                if r.get("chosen"):
                    line += f" → 选了：{_clip(r['chosen'], 18)}"
                alt = [a.get("road") for a in (r.get("alternatives") or [])
                       if isinstance(a, dict) and a.get("road")][:2]
                if alt:
                    line += f"｜没走：{'、'.join(_clip(a, 18) for a in alt)}"
                out.append(line)
        if nagging:
            out.append("这几条你还没去核对结果（没结果就等于没判断）：")
            for r in nagging[:3]:
                age = max(0, int((time.time() - float(r.get("created_at") or 0)) / 86400))
                tag = "·" if age < 7 else f"{age}天前·"
                out.append(f"{tag} #{r['id']} {_clip(r['claim'], 48)}")
        out.append(how)
        text = "\n".join(out)
        return text if len(text) <= chars * 2 else text[: chars * 2]

    # ---------------------------------------------------------------- 阶段总结

    def review_candidates(self, days: int = 30) -> tuple[list[dict], list[dict]]:
        since = time.time() - days * 86400
        records = self.store.list_judgments(since=since, limit=60)
        mems = []
        try:
            for m in self.store.list_memories(limit=200):
                if float(m.get("created_at") or 0) >= since:
                    mems.append(m)
        except Exception:
            mems = []
        mems.sort(key=lambda m: float(m.get("created_at") or 0), reverse=True)
        return records, mems[:24]

    def build_review_prompt(self, records: list[dict], mems: list[dict], days: int) -> str:
        lines = []
        for r in records:
            tag = KIND_LABEL.get(r.get("kind"), r.get("kind"))
            bits = [f"#{r['id']} [{tag}/{VERDICT_LABEL.get(r.get('verdict'), r.get('verdict'))}]",
                    _clip(r.get("claim") or "", 90)]
            if r.get("reason"):
                bits.append(f"当时想：{_clip(r['reason'], 70)}")
            if r.get("outcome"):
                bits.append(f"后来：{_clip(r['outcome'], 70)}")
            if r.get("lesson"):
                bits.append(f"教训：{_clip(r['lesson'], 60)}")
            alts = r.get("alternatives") or []
            if alts:
                bits.append("没走：" + "；".join(
                    _clip(a.get("road"), 30) + (f"（{_clip(a.get('why_not'), 30)}）" if a.get("why_not") else "")
                    for a in alts if isinstance(a, dict))[:160])
            lines.append(" ".join(bits))
        mem_lines = [f"m{m['id']} {_clip(m.get('text') or '', 80)}" for m in mems]

        return (
            "下面是这个角色最近 %d 天自己记下的判断账（判断/岔路/推翻/偏差），以及这段时间留下的记忆。\n\n"
            "【账本】\n%s\n\n【记忆】\n%s\n\n"
            "请你替她做一次复盘，要求：\n"
            "1. 不要罗列发生了什么，要找出**重复的形状**：她在哪一类事上反复判断错、反复犹豫、反复改口。\n"
            "   至少看过 3 条同类才算「反复」，凑不出来就别硬编。\n"
            "2. 找出**其实是同一件事**的两条（一条账本和一条账本，或账本和记忆），说清它们为什么是一件事。\n"
            "3. 每条模式给一句**下次能照着做的动作**，不要「要更谨慎」这种废话。\n"
            "4. 全部用第一人称写，像她自己在心里过一遍，语气平，不要总结陈词。\n\n"
            "只输出 JSON，不要解释：\n"
            '{"summary":"这个阶段我……（不超过 120 字，说清我在哪件事上没长进、哪件上想明白了）",'
            '"patterns":[{"pattern":"我容易……","evidence":[12,15],"advice":"下次……"}],'
            '"links":[{"a":"#12","b":"m88","relation":"同一件事|因为|后来变了|前后矛盾","why":"……"}]}'
            % (days, "\n".join(lines) or "（没有账本）", "\n".join(mem_lines) or "（没有记忆）")
        )

    @staticmethod
    def parse_review(text: str) -> dict:
        raw = str(text or "").strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
        if fence:
            raw = fence.group(1).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(raw[start: end + 1])
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def review(self, *, force: bool = False, days: int = 30, created_by: str = "auto") -> dict:
        """翻一遍旧账，提炼出这个阶段的形状。

        没有模型可用时**不写空总结**：宁可不跑，也不要往账本里塞一句「最近没什么」，
        那种东西一旦入库，以后每次注入都会带着它。
        """
        last = self.store.last_review_at()
        stats = self.store.ledger_stats()
        recent_n = len([r for r in self.store.list_judgments(limit=100)
                        if float(r.get("created_at") or 0) > last])
        if not force:
            if time.time() - last < REVIEW_MIN_DAYS * 86400 and recent_n < REVIEW_FORCE_RECORDS:
                return {"ok": False, "skipped": "还没到回看的时候",
                        "since_last_days": round((time.time() - last) / 86400, 1), "new_records": recent_n}
            if recent_n < REVIEW_MIN_RECORDS:
                # 账本刚起步（第一次、且已经有一两条）时，不请模型来「提炼形状」——
                # 记录不够它会拿无关的记忆硬编（实测编出过「反复在睡眠上判断错」这种账上没有的东西）。
                # 这时候给一份纯统计的账面盘点：不调模型，只说账上真有的。
                if not last and recent_n >= 2:
                    return self._stats_review(recent_n, stats, days=days, created_by=created_by)
                return {"ok": False, "skipped": f"新记录只有 {recent_n} 条，还不够看出形状",
                        "new_records": recent_n}

        records, mems = self.review_candidates(days=days)
        if not records:
            return {"ok": False, "skipped": "账本还是空的，先让她记几条判断"}
        # 强行复盘也一样：不足 3 条记录时模型只能拿记忆碎片硬编（踩过，编出账上没有的事）。
        # 这时候退回账面盘点，宁可平淡也别往注入里塞假话。
        if len(records) < 3:
            return self._stats_review(len(records), stats, days=days, created_by=created_by)

        prompt = self.build_review_prompt(records, mems, days)
        raw = ""
        try:
            raw = self.memory.complete(prompt, "你只输出 JSON。不要寒暄，不要解释，不要 Markdown 代码块以外的东西。", 900)
        except Exception as exc:
            return {"ok": False, "error": f"模型调用失败：{str(exc)[:100]}"}
        data = self.parse_review(raw)
        if not data or not str(data.get("summary") or "").strip():
            return {"ok": False, "error": "模型没给出可解析的复盘", "raw": _clip(raw, 120)}

        summary = _clip(data.get("summary"), 1200)
        patterns = []
        for p in (data.get("patterns") or [])[:4]:
            if not isinstance(p, dict) or not p.get("pattern"):
                continue
            ev = p.get("evidence") or []
            patterns.append({
                "pattern": _clip(p["pattern"], 160),
                "evidence": [int(x) for x in ev if str(x).lstrip("#").isdigit()][:6],
                "advice": _clip(p.get("advice") or "", 160),
            })
        now = time.time()
        start = now - days * 86400
        rid = self.store.add_review(
            period_start=start, period_end=now, summary=summary,
            patterns=patterns, links=[], counts=stats, created_by=created_by, now=now,
        )

        links = []
        for item in (data.get("links") or [])[:6]:
            if not isinstance(item, dict):
                continue
            a, b = str(item.get("a") or ""), str(item.get("b") or "")
            a_id, a_kind = self._parse_ref(a)
            b_id, b_kind = self._parse_ref(b)
            if not a_id or not b_id:
                continue
            if self.store.add_link(a_id, b_id, relation=str(item.get("relation") or "同一件事"),
                                   why=_clip(item.get("why") or "", 200),
                                   a_kind=a_kind, b_kind=b_kind, review_id=rid):
                links.append({"a": a, "b": b, "relation": item.get("relation"), "why": _clip(item.get("why") or "", 120)})
        if links:
            self.store.conn.execute("UPDATE ledger_reviews SET links=? WHERE id=?",
                                    (json.dumps(links, ensure_ascii=False), rid))
            self.store.conn.commit()
        # 归入本次总结，方便按阶段查。
        ids = [int(r["id"]) for r in records]
        if ids:
            self.store.conn.execute(
                f"UPDATE judgment_ledger SET review_id=? WHERE id IN ({','.join('?' * len(ids))})",
                [rid, *ids],
            )
            self.store.conn.commit()
        return {"ok": True, "id": rid, "summary": summary, "patterns": len(patterns),
                "links": len(links), "records": len(records)}

    def _stats_review(self, n: int, stats: dict, *, days: int = 30,
                      created_by: str = "auto") -> dict:
        """账本刚起步时的第一次复盘：只报账面，不做推断。

        为什么不请模型：记录不到 3 条，它凑不出「反复」的形状，只会拿记忆里的
        无关碎片硬拼，写出来的是账上没有的事。宁可这一版平淡，也不能让第一句
        复盘就是假的——那句会一直挂在注入里。
        """
        pend = self.store.list_judgments(kind="judge", verdict="pending", limit=3)
        bits = [f"这个阶段账上有 {stats['total']} 条判断：{stats['resolved']} 条已经核对过，"
                f"{stats['pending']} 条还在等结果。"]
        if pend:
            bits.append("等结果的：" + "；".join(_clip(r["claim"], 26) for r in pend[:2]) + "——结果一出来就回填。")
        summary = _clip("".join(bits), 300)
        now = time.time()
        rid = self.store.add_review(
            period_start=now - days * 86400, period_end=now, summary=summary,
            patterns=[], links=[], counts=stats, created_by=created_by, now=now,
        )
        return {"ok": True, "id": rid, "summary": summary, "patterns": 0,
                "links": 0, "records": n, "kind": "stats"}

    @staticmethod
    def _parse_ref(ref: str) -> tuple[int, str]:
        s = str(ref or "").strip().lstrip("#")
        if s.startswith("m") and s[1:].isdigit():
            return int(s[1:]), "memory"
        return (int(s), "ledger") if s.isdigit() else (0, "ledger")
