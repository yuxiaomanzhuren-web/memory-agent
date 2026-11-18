# -*- coding: utf-8 -*-
"""意图引擎：决定「她现在该不该主动开口」，并且必须说得出为什么。

背景：原来桥接是用一个概率常数决定要不要找人的——
    idle > 60s && random() < 0.9 × 状态系数
摇中了才开始翻记忆找话题，所以第一句常常是「在吗」。记忆对「要不要说」这件事
完全没有因果力。这个模块把那个骰子换成一份可读的账：

    score = loop + mood + unspoken + noise − cooldown − night − flood
    act   = score >= 0.55

关键设计（都是刻意的，别随手改）：
1. 未完成清单（open_loops）取**最大值**，不累加。一堆小事不该叠成必说。
2. 无聊单独不足以打扰人：boredom 满格也只值 0.18，凑不到阈值。
3. 随机只做噪声（0~0.08），永远不足以把分数从线下推到线上。
4. 每次决策都写 wake_decisions，含各信号拆解——不可验证的主动性不叫可信。
"""

from __future__ import annotations

import random
import re
import time
from typing import Any
from .store import TextLimitError

THRESHOLD = 0.55          # 说 / 不说 的分界
NOISE_MAX = 0.08          # 随机只能在这个范围内晃，不能决定结果
NIGHT_HOURS = (0, 7)      # 她睡觉的时段


def _fmt_due(due_at: float | None, now: float) -> str:
    if not due_at:
        return ""
    diff = due_at - now
    if diff <= -86400:
        return f"已经过了 {-diff / 86400:.0f} 天"
    if diff <= -3600:
        return f"已经过了 {-diff / 3600:.0f} 小时"
    if diff <= 0:
        return f"已经过了 {-diff / 60:.0f} 分钟"
    if diff < 3600:
        return f"约 {diff / 60:.0f} 分钟后到期"
    if diff < 86400:
        return f"约 {diff / 3600:.0f} 小时后到期"
    return time.strftime("%m月%d日", time.localtime(due_at))


class IntentService:
    """无状态（除了 store），每次调用现算。方便测试，也方便以后换公式。"""

    def __init__(self, store, life_state=None):
        self.store = store
        self._life_state = life_state or (lambda: {})

    # ------------------------------------------------------------ 清单读写

    def loops(self, op: str, data: dict | None = None) -> dict:
        """她的待办本读写。op = add / update / close / drop / list。"""
        try:
            return self._loop_op((op or "list").lower(), data or {})
        except TextLimitError as error:
            return error.result()

    def _loop_op(self, op: str, data: dict) -> dict:
        if op == "list":
            status = str(data.get("status") or "open")
            return {"loops": self.store.list_loops(status=status, limit=int(data.get("limit") or 20))}

        if op == "add":
            now = time.time()
            title = str(data.get("title") or "")
            due_at = data.get("due_at")
            if isinstance(due_at, str) and due_at:
                # 允许「今天 18:00」「明天」这类口语时间由调用方先换算；这里只接数字或 ISO
                try:
                    import datetime
                    due_at = datetime.datetime.fromisoformat(due_at).timestamp()
                except Exception:
                    due_at = None
            lid = self.store.add_loop(
                title=title,
                kind=str(data.get("kind") or "unfinished"),
                detail=str(data.get("detail") or ""),
                due_at=float(due_at) if due_at else None,
                surface_after=float(data["surface_after"]) if data.get("surface_after") else None,
                weight=float(data.get("weight") or 0.5),
                scope=str(data.get("scope") or "global"),
                source_memory_id=int(data["source_memory_id"]) if data.get("source_memory_id") else None,
                now=now,
            )
            return {"ok": True, "id": lid} if lid else {"ok": False, "error": "title 太短"}

        lid = int(data.get("id") or 0)
        if not lid:
            return {"ok": False, "error": "缺 id"}
        if op in ("close", "done"):
            return {"ok": self.store.close_loop(lid, "done")}
        if op == "drop":
            return {"ok": self.store.close_loop(lid, "dropped")}
        if op == "update":
            fields = {k: data[k] for k in ("title", "detail", "kind", "weight", "due_at", "surface_after", "status") if k in data}
            return {"ok": self.store.update_loop(lid, **fields)}
        return {"ok": False, "error": f"未知操作 {op}"}

    def render(self, limit: int = 6) -> str:
        """把清单渲染成给她看的一段话，塞进上下文。空清单返回空串。"""
        rows = self.store.list_loops(status="open", limit=limit)
        if not rows:
            return ""
        now = time.time()
        lines = []
        for i, r in enumerate(rows, 1):
            due = _fmt_due(r.get("due_at"), now)
            tag = f"（{due}）" if due else ""
            head = {"promise": "答应过", "reminder": "要提醒", "debt": "欠着",
                    "intention": "想告诉他"}.get(r.get("kind"), "")
            prefix = f"[{head}] " if head else ""
            lines.append(f"{i}. {prefix}{r['title'][:120]}{tag}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 决策

    def _tuning(self) -> dict:
        """控制台可调的三个旋钮。读不到就用模块常量，绝不让调参坏了主流程。"""
        try:
            return self.store.get_setting("intent.tuning", {}) or {}
        except Exception:
            return {}

    def decide(
        self,
        conv_key: str = "default",
        idle_seconds: float = 0.0,
        last_proactive_at: float = 0.0,
        proactive_today: int = 0,
        now: float | None = None,
        record: bool = True,
    ) -> dict:
        now = now or time.time()
        hour = int(time.localtime(now).tm_hour)
        tune = self._tuning()
        try:
            threshold = float(tune.get("threshold", THRESHOLD))
        except Exception:
            threshold = THRESHOLD
        try:
            night_penalty = float(tune.get("night_penalty", 0.50))
        except Exception:
            night_penalty = 0.50
        try:
            noise_max = float(tune.get("noise_max", NOISE_MAX))
        except Exception:
            noise_max = NOISE_MAX
        night_hours = tuple(tune.get("night_hours") or NIGHT_HOURS)
        is_night = night_hours[0] <= hour < night_hours[1]

        loops = self.store.list_loops(status="open", limit=50)
        loop_score, loop_row, loop_note = self._loop_signal(loops, now)

        # 心情：只做加分项，且封顶很低——无聊不能单独构成打扰人的理由
        mood = 0.0
        boredom = curiosity = 0.0
        try:
            st = self._life_state() or {}
            boredom = float(st.get("boredom") or 0)
            curiosity = float(st.get("curiosity") or 0)
            mood = round(min(0.18, boredom * 0.24), 3)
        except Exception:
            pass

        # 多久没联系了。这是「没有具体事由」时唯一站得住的理由
        unspoken = 0.0
        hours = float(idle_seconds or 0) / 3600
        if hours >= 24:
            unspoken = 0.44
        elif hours >= 12:
            unspoken = 0.34
        elif hours >= 6:
            unspoken = 0.22
        elif hours >= 3:
            unspoken = 0.10

        # 刚聊完就别急着再开口。这条是原骰子里的「刚说完 ×0.7」，漏了会让她
        # 在你回完一句之后立刻又冒出来——比不说话更伤可信度。
        recent_talk = 0.0
        if idle_seconds is not None:
            if idle_seconds < 600:
                recent_talk = 0.50
            elif idle_seconds < 1800:
                recent_talk = 0.25

        noise = round(random.uniform(0, noise_max), 3)

        # 冷却：刚说过就别连着说
        cooldown = 0.0
        if last_proactive_at:
            gap_min = (now - float(last_proactive_at)) / 60
            if gap_min < 45:
                cooldown = 0.55
            elif gap_min < 120:
                cooldown = 0.25

        # 深夜压低声量，但「有硬到期的事」可以减免——她答应了半夜喊他那种
        night = 0.0
        if is_night:
            hard_due = any(r.get("due_at") and float(r["due_at"]) <= now for r in loops)
            night = 0.15 if hard_due else night_penalty

        flood = 0.40 if int(proactive_today or 0) >= 6 else 0.0

        score = loop_score + mood + unspoken + noise - recent_talk - cooldown - night - flood
        score = round(score, 3)
        act = score >= threshold

        reason = self._reason(act, loop_row, loop_note, loop_score, unspoken, mood,
                              cooldown, night, flood, recent_talk)
        hint = ""
        if act and loop_row and loop_score >= max(0.30, unspoken):
            due = _fmt_due(loop_row.get("due_at"), now)
            hint = f"你这次是因为这件事醒的：{loop_row['title']}" + (f"（{due}）" if due else "")
            hint += "。开口第一句就提它，别用「在吗」铺垫。"
        elif act:
            hint = f"你们已经 {hours:.0f} 小时没说话了。开口说件具体的，别只是打招呼。"

        signals = {
            "loop": round(loop_score, 3), "loop_id": loop_row.get("id") if loop_row else None,
            "mood": mood, "boredom": round(boredom, 3), "curiosity": round(curiosity, 3),
            "unspoken": unspoken, "noise": noise, "recent_talk": round(recent_talk, 3),
            "cooldown": round(cooldown, 3), "night": night, "flood": flood,
            "hour": hour, "idle_min": round((idle_seconds or 0) / 60, 1),
        }
        out = {
            "act": act, "score": score, "threshold": threshold,
            "reason": reason, "hint": hint, "signals": signals,
            "loop": loop_row, "night": is_night,
        }

        if record:
            try:
                wid = self.store.log_wake(conv_key, act, score, threshold, reason, signals, now=now)
                out["decision_id"] = wid
            except Exception:
                pass
            # 只有真落账才记「提过一次」。否则测试/预演会白白消耗她的耐心。
            if act and loop_row:
                try:
                    self.store.touch_loop(int(loop_row["id"]), now=now)
                except Exception:
                    pass
        return out

    # ------------------------------------------------------------ 内部

    def _loop_signal(self, loops: list[dict], now: float) -> tuple[float, dict | None, str]:
        """清单的贡献。取最大值——一堆小事不该叠加成「必说」。"""
        best_s, best_row, best_note = 0.0, None, ""
        for r in loops:
            if r.get("status") != "open":
                continue
            if r.get("surface_after") and now < float(r["surface_after"]):
                continue          # 刚约定完就说「我记着」太急
            s = 0.20 + 0.45 * float(r.get("weight") or 0.5)
            note = ""
            due = r.get("due_at")
            if due:
                due = float(due)
                if now >= due:
                    over_h = (now - due) / 3600
                    s += 0.25 + min(0.15, over_h / 24 * 0.15)
                    note = "到期了"
                else:
                    ahead_h = (due - now) / 3600
                    if ahead_h > 2:
                        s -= 0.35      # 还没到，别提前催
                    else:
                        s += 0.05
                        note = "快到了"
            last = r.get("last_surfaced_at")
            if last:
                gap_h = (now - float(last)) / 3600
                if gap_h < 3:
                    s -= 0.45          # 3 小时内提过，别再提
                    note = note or "刚提过"
                elif gap_h < 12:
                    s -= 0.15
            cnt = int(r.get("surface_count") or 0)
            s -= min(0.18, 0.06 * cnt)  # 提多了还没做，说明他不想做，逐步让她自己放弃
            if s > best_s:
                best_s, best_row, best_note = s, r, note
        return max(0.0, round(best_s, 3)), best_row, best_note

    @staticmethod
    def _reason(act, loop_row, note, loop_score, unspoken, mood, cooldown, night, flood, recent_talk=0.0) -> str:
        if act:
            # 谁的分高就说谁的原因。清单上挂着没事由的条目时，真正起作用的是
            # 「很久没联系」，别把功劳记到一条明天才到期的事情上。
            if loop_row and loop_score >= max(0.30, unspoken):
                core = f"清单上「{loop_row['title']}」"
                if loop_row.get("due_at"):
                    core += f"（{_fmt_due(loop_row['due_at'], time.time())}）"
                elif note:
                    core += f"（{note}）"
                return "说了：" + core
            if unspoken >= 0.22:
                return "说了：隔了很久没联系，主动找他"
            return "说了：心情和时机都合适"
        # 没说：给出主要压制项，方便回头调参
        blockers = []
        if recent_talk:
            blockers.append("刚聊完")
        if cooldown:
            blockers.append("刚主动说过")
        if night:
            blockers.append("深夜")
        if flood:
            blockers.append("今天说太多了")
        if not loop_row:
            blockers.append("清单上没有到期的事")
        if blockers:
            return "没说：" + "、".join(blockers)
        return "没说：分数不够"
