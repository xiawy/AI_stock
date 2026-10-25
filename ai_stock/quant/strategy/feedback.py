"""记分回灌: driver 胜率 → 行业优先级乘子 (P2).

这是整套策略台里唯一会碰到交易链路的一段, 所以边界写得比别处更死:

1. **只做乘子, 不做闸门**. 乘子最多把行业优先级放大到 1.2 倍或压到 0.8 倍;
   闸门(硬门槛)一旦否决, 就会直接掐死交易 —— 项目已经为"分析结论变硬门槛"
   付过全天零交易的代价.
2. **周频, 不是日频**. n=1 的胜率只能是 0% 或 100%, 按日调权等于把追涨杀跌
   自动化, 还会污染自己的样本(调权 → 改选股 → 改样本分布 → 下期失真).
3. **样本不足 / 数据质量差 → 不出系数**. 宁可这周不调, 也不能拿一次接口封禁
   去下调整周权重.
4. **开关默认关**. 系数照算照存(可观察), 但只有
   ``strategy_reproduce_coef_enable=1`` 时 selection 才消费它.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Iterable, Mapping, Sequence

from .. import db_ops
from ..config import (
    STRATEGY_COEF_MAX,
    STRATEGY_COEF_MIN,
    STRATEGY_COEF_MIN_SAMPLES,
    STRATEGY_MAX_UNJUDGED_RATE as MAX_UNJUDGED_RATE,
)

logger = logging.getLogger(__name__)

CONFIG_KEY = "strategy_driver_coef"
ENABLE_KEY = "strategy_reproduce_coef_enable"

# 回灌查找 basket→driver 的回看天数: 与观察期一致, 太久以前的驱动关系已失效
DRIVER_LOOKBACK_DAYS = 10


# ---------------------------------------------------------------------------
# 纯函数: 胜率 → 系数
# ---------------------------------------------------------------------------


def compute_coefs(
    driver_quality: Mapping[str, Mapping[str, float]],
    *,
    min_samples: int = STRATEGY_COEF_MIN_SAMPLES,
    lo: float = STRATEGY_COEF_MIN,
    hi: float = STRATEGY_COEF_MAX,
) -> tuple[dict[str, float], dict[str, str]]:
    """(系数, 未出系数的原因). 两个返回值都要展示 —— 只给系数会让人以为全都在调.

    映射是线性的: 胜率 0% → lo, 50% → 中值, 100% → hi.
    不做"相对整体胜率"的相对化: 需要同时估计整体基线, 样本少时基线本身
    抖得比单组更厉害, 反而引入更多噪音.
    """
    coefs: dict[str, float] = {}
    skipped: dict[str, str] = {}
    for driver, q in sorted(driver_quality.items()):
        judged = int(q.get("judged", 0) or 0)
        rate = q.get("unjudged_rate")
        if judged < min_samples:
            skipped[driver] = f"样本 {judged} < {min_samples}"
            continue
        if rate is not None and float(rate) > MAX_UNJUDGED_RATE:
            skipped[driver] = f"无法判定率 {float(rate):.0%} > {MAX_UNJUDGED_RATE:.0%}"
            continue
        win = q.get("win_rate")
        if win is None:
            skipped[driver] = "无已判定样本"
            continue
        coefs[driver] = round(lo + (hi - lo) * float(win), 3)
    return coefs, skipped


# ---------------------------------------------------------------------------
# 读写配置
# ---------------------------------------------------------------------------


def read_payload() -> dict:
    """读已存系数 (含 updated_at / skipped), 前端要能看到"为什么没调"."""
    raw = db_ops.get_config_value(CONFIG_KEY, "") or ""
    if not raw:
        return {"coefs": {}, "skipped": {}, "updated_at": "", "as_of": ""}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("strategy_driver_coef 不是合法 JSON, 忽略")
        return {"coefs": {}, "skipped": {}, "updated_at": "", "as_of": ""}
    if not isinstance(data, dict):
        return {"coefs": {}, "skipped": {}, "updated_at": "", "as_of": ""}
    data.setdefault("coefs", {})
    data.setdefault("skipped", {})
    data.setdefault("updated_at", "")
    data.setdefault("as_of", "")
    return data


def save_coefs(
    coefs: Mapping[str, float], skipped: Mapping[str, str], as_of: str,
) -> dict:
    payload = {
        "coefs": dict(coefs),
        "skipped": dict(skipped),
        "as_of": as_of,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    db_ops.set_config_value(
        CONFIG_KEY, json.dumps(payload, ensure_ascii=False),
        "策略台 driver 权重系数(JSON, 由周任务回算)",
    )
    return payload


def enabled() -> bool:
    return db_ops.get_config_value(ENABLE_KEY, "0") == "1"


# ---------------------------------------------------------------------------
# 落到选股: 行业名 → 篮子 → driver → 系数
# ---------------------------------------------------------------------------


def multipliers(
    industry_names: Sequence[str], trade_date: str, *, repo=None,
) -> dict[str, float]:
    """行业名 → 优先级乘子. 返回空字典表示"本期不调权"(开关关/无系数/未匹配).

    匹配不上就不调 —— 默认 1.0 不可用"猜一个 driver"代替, 猜错方向的乘子
    比不调更糟: 它会把错误结论直接写进行业排序, 且事后无法归因.
    """
    if not enabled():
        return {}
    payload = read_payload()
    coefs: dict[str, float] = payload.get("coefs") or {}
    if not coefs:
        return {}

    from .repo import StrategyRepo

    repo = repo or StrategyRepo()
    driver_of_basket = repo.basket_drivers(trade_date, lookback_days=DRIVER_LOOKBACK_DAYS)
    if not driver_of_basket:
        return {}

    from .rules import textmatch

    baskets = list(driver_of_basket)
    out: dict[str, float] = {}
    for name in industry_names:
        basket = textmatch.best(name, baskets)
        if not basket:
            continue
        driver = driver_of_basket.get(basket, "")
        coef = coefs.get(driver)
        if coef and coef != 1.0:
            out[name] = float(coef)
    return out


def apply(
    industries: Iterable[dict], trade_date: str,
) -> tuple[int, dict[str, float]]:
    """就地把系数乘到 priority 上 (返回 调整条数, 使用的系数表).

    只改 ``priority`` 与附加展示字段 ``strategy_coef``: 不动 stage / 门槛 /
    confidence, 那三者会牵动买入链路, 不属于本模块职责.
    """
    items = list(industries)
    if not items:
        return 0, {}
    table = multipliers([i.get("name", "") for i in items], trade_date)
    if not table:
        return 0, {}
    changed = 0
    for item in items:
        coef = table.get(item.get("name", ""))
        if not coef:
            continue
        item["priority"] = round(float(item.get("priority", 0) or 0) * coef, 2)
        item["strategy_coef"] = coef
        changed += 1
    return changed, table
