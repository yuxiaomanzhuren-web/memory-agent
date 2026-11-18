# -*- coding: utf-8 -*-
"""离线整理：实体归一、重复记忆合并、冲突检测。不调模型。"""
from __future__ import annotations

from . import conflict, consolidate


class MaintenanceService:
    def __init__(self, memory):
        self.memory = memory

    def run(self, similarity: float = 0.92, alias_map: dict | None = None) -> dict:
        out = consolidate.sleep(self.memory.store, alias_map=alias_map, similarity=similarity)
        out["backfill"] = self.backfill_entities()
        facts = []
        try:
            rows = self.memory.store.conn.execute(
                """SELECT m.id, m.text, GROUP_CONCAT(me.entity_id) AS ents
                   FROM memories m LEFT JOIN memory_entities me ON me.memory_id=m.id
                   WHERE m.status='active' AND m.kind='fact'
                   GROUP BY m.id LIMIT 2000"""
            ).fetchall()
            for r in rows:
                ids = {int(x) for x in str(r["ents"] or "").split(",") if x.strip()}
                if ids:
                    facts.append({"id": int(r["id"]), "text": str(r["text"] or ""), "entity_ids": ids})
        except Exception:
            facts = []
        added = 0
        for a, b, reason in conflict.find(facts):
            if self.memory.store.add_conflict(a, b, reason):
                added += 1
        out["conflicts"] = added
        return out

    def backfill_entities(self, limit: int = 2000) -> dict:
        """给没有实体的活跃记忆补挂实体（规则法，不调模型）。

        记忆文本里出现已有实体名（≥2 字）就 link 上。幂等：已有链接的跳过。
        补上之后星图的共现边、实体度数会自己长出来。
        """
        store = self.memory.store
        try:
            rows = store.conn.execute(
                """SELECT m.id, m.text FROM memories m
                   WHERE m.status='active'
                     AND NOT EXISTS (SELECT 1 FROM memory_entities me WHERE me.memory_id=m.id)
                   ORDER BY m.created_at DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        except Exception:
            return {"memories": 0, "links": 0}
        links = 0
        touched = 0
        for r in rows:
            mid = int(r["id"])
            try:
                hits = store.entities_in_text(str(r["text"] or ""), limit=6)
            except Exception:
                continue
            if not hits:
                continue
            for eid in hits:
                try:
                    store.link_entity(mid, int(eid))
                    links += 1
                except Exception:
                    pass
            touched += 1
        return {"memories": touched, "links": links}

    def conflicts(self, limit: int = 50) -> list[dict]:
        out = []
        for row in self.memory.store.list_conflicts(limit=limit):
            a = self.memory.store.get_memory(int(row["a_id"]))
            b = self.memory.store.get_memory(int(row["b_id"]))
            out.append({
                "id": int(row["id"]), "reason": row.get("reason") or "",
                "a": {"id": int(row["a_id"]), "text": (a or {}).get("text", "")},
                "b": {"id": int(row["b_id"]), "text": (b or {}).get("text", "")},
            })
        return out

    def resolve(self, conflict_id: int, keep: str = "a") -> bool:
        rows = [c for c in self.memory.store.list_conflicts(limit=100) if int(c["id"]) == int(conflict_id)]
        if not rows:
            return False
        row = rows[0]
        keep_id = int(row["a_id"] if keep == "a" else row["b_id"])
        drop_id = int(row["b_id"] if keep == "a" else row["a_id"])
        from .memory_temporal import retained_memory
        if retained_memory(self.memory.store.conn,keep_id) or retained_memory(self.memory.store.conn,drop_id):
            return False
        self.memory.store.resolve_conflict(int(conflict_id), keep_id)
        # 被丢掉的那条如果钉着（名字、群号这种身份事实），把钉转移给留下的那条，
        # 否则「判一次冲突就把她的名字判没了」。
        self._carry_pin(drop_id, keep_id)
        try:
            self.memory.store.delete_memory(drop_id,"hard",actor="automatic",origin="conflict_resolution",automatic=True)
        except Exception:
            pass
        return True

    def _carry_pin(self, drop_id: int, keep_id: int) -> None:
        import json as _json

        def _meta(mid: int) -> dict:
            m = self.memory.store.get_memory(int(mid)) or {}
            raw = m.get("meta") or {}
            if isinstance(raw, str):
                try:
                    raw = _json.loads(raw or "{}")
                except Exception:
                    raw = {}
            return dict(raw) if isinstance(raw, dict) else {}

        src, dst = _meta(drop_id), _meta(keep_id)
        if not src.get("pin") or dst.get("pin"):
            return
        dst["pin"] = True
        if src.get("subject"):
            dst["subject"] = src["subject"]
        if src.get("kind"):
            dst["kind"] = src["kind"]
        try:
            self.memory.store.set_meta(int(keep_id), dst)
        except Exception:
            pass
