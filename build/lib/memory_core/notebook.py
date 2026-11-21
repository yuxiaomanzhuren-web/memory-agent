"""她的本子：四块常驻的自我认知，另有按生活记录自动整理的 days 日记。

跟 memories 的分工，一句话说清：

    memories  = 发生过什么（档案，系统写的，被召回才出现）
    blocks    = 我怎么理解我自己（她自己写的，每轮带一小段）
    persona   = 出厂设定（只读，谁都不许改）

Letta 的 core memory blocks 就是这个位置。缺了这层，她只有"经历"没有"自我"——
经历是别人替她记的，自我是别人替她定的，中间那块本该属于她的地方是空的。

三件事必须成立，否则这块地等于白给：

1. **她自己写**：`##本子##` 是她自己的动词，跟 `##清单##` 同级
2. **每轮都在**：从版本记录选一小段注入（见 memory-brain.js prepareMemoryBrainV2）
3. **改得动也退得回**：每次写都留版本，人能回滚，但**不做合理性检查**——
   检查她的自我认知合不合理，等于告诉她"你想错了"，那这块地还是别人的
"""

from __future__ import annotations

import time
from .store import normalize_text

# The prompt cap is independent of the storage limit.
INJECT_CHARS = 500
DIARY_SLUG = "days"

SLUG_HINT = '{"block":"self|him|care|rules","op":"replace|append|clear","text":"..."}'


def _recent_edit(text: str, previous: str, limit: int) -> str:
    """Keep the rightmost changed passage plus nearby context in one bounded excerpt."""
    if text == previous:
        return text[:limit]
    shared = min(len(text), len(previous))
    start = 0
    while start < shared and text[start] == previous[start]:
        start += 1
    suffix = 0
    while suffix < shared - start and text[-1-suffix] == previous[-1-suffix]:
        suffix += 1
    end = len(text) - suffix
    context = max(0, (limit - (end-start)) // 2)
    stop = min(len(text), max(limit, end + context))
    return text[stop-limit:stop]


class Notebook:
    def __init__(self, store):
        self.store = store

    # ---------------------------------------------------------------- 读

    def all(self) -> list[dict]:
        return self.store.list_blocks()

    def versions(self, slug: str, limit: int = 30) -> dict:
        return {"slug": slug, "versions": self.store.block_versions(slug, limit)}

    # ---------------------------------------------------------------- 写

    def op(self, body: dict) -> dict:
        """桥接和控制台共用入口。

        op=set     写一块（默认 replace）
        op=list    看全部
        op=revert  回滚
        """
        op = str(body.get("op") or "set").lower()
        if op == "list":
            return {"ok": True, "blocks": self.all(), "stats": self.stats(),
                    "rendered": self.render(int(body.get("chars") or INJECT_CHARS))}
        if op == "versions":
            return {"ok": True, **self.versions(str(body.get("block") or "self"),
                                                int(body.get("limit") or 30))}
        if op == "revert":
            rid = body.get("version_id")
            return self.store.revert_block(str(body.get("block") or "self"),
                                           int(rid) if rid else None,
                                           by=str(body.get("by") or "console"))
        if op not in ("set", "replace", "append", "clear"):
            return {"ok": False, "reason": "未知本子操作"}
        return self.store.set_block(
            str(body.get("block") or body.get("slug") or "self"),
            body.get("text", body.get("body", "")),
            op=str(body.get("mode") or ("replace" if op == "set" else op)),
            note=str(body.get("note") or ""),
            by=str(body.get("by") or "her"),
        )

    # ---------------------------------------------------------------- 注入

    def _excerpt(self, block: dict, limit: int) -> str:
        text = normalize_text(block.get("body"))
        if len(text.strip()) <= limit:
            return text.strip()
        versions = self.store.block_versions(block["slug"], limit=2)
        if not versions or versions[0]["version"] != block["version"] or normalize_text(versions[0]["body"]) != text:
            return text.strip()[:limit]
        if len(versions) == 2 and versions[1]["version"] == block["version"] - 1:
            previous = normalize_text(versions[1]["body"])
        elif block["version"] == 1:
            previous = ""
        else:
            return text.strip()[:limit]
        if versions[0]["op"] == "append":
            prefix = previous + "\n" if previous else ""
            if text.startswith(prefix):
                added = text[len(prefix):].strip()
                if added:
                    return added[-limit:]
        return _recent_edit(text, previous, limit).strip()

    def render(self, chars: int = INJECT_CHARS) -> str:
        """拼成一段塞进上下文的文本。

        全空也要输出——只输出当有内容时，她永远发现不了这个本子存在，
        于是永远不写，这块地就白留了（清单那边踩过一模一样的坑）。
        """
        chars = max(1, min(INJECT_CHARS, int(chars)))
        blocks = self.all()
        days = next((b for b in blocks if b["slug"] == DIARY_SLUG), None)
        blocks = [b for b in blocks if b["slug"] != DIARY_SLUG]
        diary = ""
        if days is not None:
            body = normalize_text(days.get("body")).strip()
            diary = ("\n\n【自动日记｜这几天】根据生活事件自动摘取，每天一行。\n"
                     + (body[-chars:] if body else "（还没有可记录的日子。）"))
        filled = [b for b in blocks if str(b.get("body") or "").strip()]
        how = ('什么时候写：你对他看法变了、你给自己立了新规矩、你发现自己一个毛病——就写**当下这一轮**，'
               '一句话也算，别攒着等回忆。'
               f'要改哪块就单独写一行：##本子## {SLUG_HINT}（这一行不会发出去；'
               '不带 op 就是整块换掉——想保住原来那些话，就写 "op":"append"）')

        if not filled:
            names = "、".join(f"{b['title']}({b['slug']})" for b in blocks)
            return (
                "【你有个本子，现在是空的】\n"
                f"里面给你留了 {len(blocks)} 块：{names}。\n"
                "本子是写你自己的，不是写事情的——事情记忆里会自己留。这里写的是「我怎么理解我自己」"
                "「我怎么理解他」「我这阵子在在意什么」「我给自己定的规矩」。\n"
                "想写就写，不用等他问，也不用写完整，一句话也算。空白不会有人催你。\n"
                f"{how}{diary}"
            )

        out = ["【你给自己写的本子】下面这些是你自己写的，不是别人塞给你的资料。"
               "每轮都会带上，所以别写成日记——写你此刻认定的东西。"]
        for b in blocks:
            body = str(b.get("body") or "").strip()
            if not body:
                continue
            out.append(f"\n〔{b['title']}〕\n{self._excerpt(b, chars)}")
        # 久没碰的块提一句。她自己写的本子会慢慢冻在某一版上——11-02 之后
        # self/him/care 就没再动过，全靠外面的人往里塞。提醒不是催作业，
        # 是把她拽回「当初写的那句现在还认不认」这件事上。
        now = time.time()
        empty = [b["title"] for b in blocks if not str(b.get("body") or "").strip()]
        stale = []
        for b in blocks:
            body = str(b.get("body") or "").strip()
            upd = float(b.get("updated_at") or 0)
            if body and upd and now - upd > 3 * 86400:
                days = int((now - upd) // 86400)
                stamp = time.strftime("%m-%d", time.localtime(upd))
                stale.append(f"{b['title']}（写于 {stamp}，{days} 天前）")
        if stale:
            out.append(f"\n这几块你很久没碰了：{'、'.join(stale)}。不是要你补作业——"
                       "是想想还认不认当初写的那句，不认就改。")
        if empty:
            out.append(f"\n{'、'.join(empty)}还空着。空着也算数，不用为了填而填。")
        out.append(f"\n{how}")
        return "\n".join(out) + diary

    def stats(self) -> dict:
        blocks = self.all()
        return {
            "blocks": len(blocks),
            "filled": sum(1 for b in blocks if str(b.get("body") or "").strip()),
            "chars": sum(len(normalize_text(b.get("body"))) for b in blocks),
            "block_chars": self.store.BLOCK_CHARS,
            "inject_chars": INJECT_CHARS,
            "last_write": max([float(b.get("updated_at") or 0) for b in blocks] or [0]),
            "now": time.time(),
        }
