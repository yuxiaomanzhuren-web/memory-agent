"""小词表：只收「亲自遇到的漏检词」，一条一句，带场景，别做大。

规矩（2026-11-10 定的）：
- 只收确实搜不到、换一个词就搜得到的词组；
- 一条映射最多 2 个变体，扩查询时默认只用一个；
- 不加通用近义词（「换手机号」不是「换号」，别混）。

怎么加：在 TABLE 里加一行 `"原词": ["变体"]`，写清这一行是替哪种问法兜的。
"""

TABLE: dict[str, list[str]] = {
    # 换号这件事，群里有人叫「切号」「换号」「换账号」
    "记忆串": ["记忆混乱", "串味"],
    "记忆混乱": ["记忆串"],
    # 桥接那边叫「会话」，他嘴里常说「对话」
    "开新会话": ["开新对话"],
    "开新对话": ["开新会话"],
}


def variants(query: str, limit: int = 1) -> list[str]:
    """给一句问法找替身。最多 limit 个，找不到就返回空。"""
    q = (query or "").strip()
    if not q:
        return []
    out: list[str] = []
    for key, alts in TABLE.items():
        if key in q:
            for alt in alts:
                v = q.replace(key, alt)
                if v != q and v not in out:
                    out.append(v)
                if len(out) >= limit:
                    return out
        if len(out) >= limit:
            break
    return out
