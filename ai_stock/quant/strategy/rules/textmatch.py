"""名称归一的唯一实现 (纯函数).

三个地方需要"这两个名字是不是同一件事":
- 新闻主题名 → 篮子名 (交叉验证矩阵)
- 篮子名 → 下游行业名 (权重回灌时找 driver)
- 概念名 → 板块名

分散写三份匹配逻辑, 必然在某个版本里出现"三处口径不一致"导致矩阵与回灌
互相矛盾. 所以只留这一份, 且它对**泛词**是零容忍的: 核心词少于 2 个字
直接不匹配, 避免 "AI" / "电力" 这类短词把任意主题挂到任意板块上.
"""

from __future__ import annotations

# 可比对的最大短名长度: 低于此长度的名字不参与"包含"匹配
MIN_CORE = 2

# 只剥行业后缀, 不剥业务词 —— 剥业务词会把 "减速器" 剥成 "减速"
_SUFFIXES = (
    "产业链", "概念", "板块", "设备", "材料", "制造", "加工", "服务",
    "行业", "产业", "指数", "主题",
)


def core(name: str) -> str:
    """剥掉通用后缀后的核心词. 每步保证剩余 ≥2 字, 不会剥空."""
    text = (name or "").strip()
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if text.endswith(suf) and len(text) - len(suf) >= MIN_CORE:
                text = text[: -len(suf)]
                changed = True
                break
    return text


def match(a: str, b: str) -> bool:
    """双向包含匹配 (原名 → 核心词降级).

    先比原名: 一方包含另一方即成立 (「CPO」⊂「CPO概念」).
    再比核心词: 只有当**核心词本身够长**时才用它, 否则退化为单字包含,
    一个 "车" 字就能把整车、汽车零部件、充电桩全串成一个板块.
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ca, cb = core(a), core(b)
    if len(ca) < MIN_CORE + 1 or len(cb) < MIN_CORE + 1:
        return False
    return ca in cb or cb in ca


def best(needle: str, candidates: list[str]) -> str:
    """在候选名里找最匹配的一个: 先精确, 再包含, 最后核心词.

    排序而非"取首个命中" —— 榜单顺序是涨跌幅降序, 取首个命中会让
    「电力设备」被「电力」抢走, 而这两者在资金上恰恰反向.
    """
    needle = (needle or "").strip()
    if not needle:
        return ""
    for name in candidates:
        if name == needle:
            return name
    # 包含关系可能命中多个, 取长度最接近的: 榜单顺序是涨跌幅降序, 取首个命中
    # 会让「电力设备」被「电力」抢走, 而这两者在资金上恰恰反向.
    hits = [n for n in candidates if needle in n or n in needle]
    if hits:
        return min(hits, key=lambda n: abs(len(n) - len(needle)))
    if len(core(needle)) >= MIN_CORE + 1:
        hits = [n for n in candidates if match(needle, n)]
        if hits:
            return min(hits, key=lambda n: abs(len(n) - len(needle)))
    return ""
