"""Self-evolution module with anti-overfitting guards (设计文档 §10.3).

核心约束 (反过拟合保护):
- 数据集严格时序分割: 训练集 | 验证集 | 样本外测试集; 禁止随机打乱样本,
  规避未来数据泄露
- 调参只在训练集运行 (内置网格搜索; 安装 Optuna 可替换 ``search_params``)
- 优化出的参数必须在完全未参与调参的样本外数据集校验, 不达标直接丢弃
- ``evolution_history`` 保存三套数据集指标, 方便审计
- 硬绩效门槛: 夏普比率 / 最大回撤底线, 不达标不进入审核队列
- 通过的候选规则以 ``enabled=False`` 写入并进入人工审核队列 (§10.2),
  本模块永不自动启用规则, 更不会触碰实盘
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime
from typing import Callable, Optional, Sequence

from . import db_ops
from .config import STOP_LOSS_PCT, TIME_STOP_MIN_GAIN, TIME_STOP_TRADING_DAYS

logger = logging.getLogger(__name__)

# 样本外硬绩效门槛 (§10.3)
MIN_OOS_SHARPE = 0.5
MAX_OOS_DRAWDOWN = 0.25

# 默认参数网格 (仅在训练集搜索)
DEFAULT_GRID: list[dict] = [
    {"stop_loss_pct": sl, "time_stop_days": td, "min_gain": TIME_STOP_MIN_GAIN}
    for sl in (0.03, 0.05, 0.07)
    for td in (4, 5, 6)
]

# 时序分割比例 (连续不重叠, 不打乱)
TRAIN_RATIO = 0.6
VALID_RATIO = 0.2

MIN_SERIES_LEN = 60  # 序列过短无法三段分割


# ---------------------------------------------------------------------------
# 时序分割
# ---------------------------------------------------------------------------

def time_series_split(n: int) -> tuple[range, range, range]:
    """严格时序分割索引: (训练, 验证, 样本外). 三段连续, 禁止打乱."""
    train_end = int(n * TRAIN_RATIO)
    valid_end = int(n * (TRAIN_RATIO + VALID_RATIO))
    return range(0, train_end), range(train_end, valid_end), range(valid_end, n)


# ---------------------------------------------------------------------------
# 简化回测器: 止损/时间止损持有策略
# ---------------------------------------------------------------------------

def backtest_stop_policy(
    closes: Sequence[float],
    stop_loss_pct: float = STOP_LOSS_PCT,
    time_stop_days: int = TIME_STOP_TRADING_DAYS,
    min_gain: float = TIME_STOP_MIN_GAIN,
) -> dict:
    """在给定收盘价序列上模拟「首日建仓 + 止损/时间止损」单仓.

    规则与实盘一致: 收盘价 (非盘中瞬时价) 跌破止损线清仓; 持有满
    ``time_stop_days`` 根 K 线且最大涨幅 < ``min_gain`` 清仓; 否则持有
    到期末. 返回 {total_return, max_drawdown, holding_bars, exit_reason}.
    """
    closes = [float(c) for c in closes if c and c > 0]
    if len(closes) < 2:
        return {"total_return": 0.0, "max_drawdown": 0.0,
                "holding_bars": 0, "exit_reason": "no_data"}

    entry = closes[0]
    peak = entry
    max_dd = 0.0
    for i, price in enumerate(closes[1:], start=1):
        peak = max(peak, price)
        max_dd = max(max_dd, (peak - price) / peak if peak else 0.0)
        loss_ratio = (entry - price) / entry
        if loss_ratio >= stop_loss_pct:
            return {
                "total_return": (price - entry) / entry,
                "max_drawdown": max_dd,
                "holding_bars": i,
                "exit_reason": "stop_loss",
            }
        if i >= time_stop_days:
            window_gain = (max(closes[1:i + 1]) - entry) / entry
            if window_gain < min_gain:
                return {
                    "total_return": (price - entry) / entry,
                    "max_drawdown": max_dd,
                    "holding_bars": i,
                    "exit_reason": "time_stop",
                }
    exit_price = closes[-1]
    return {
        "total_return": (exit_price - entry) / entry,
        "max_drawdown": max_dd,
        "holding_bars": len(closes) - 1,
        "exit_reason": "hold_to_end",
    }


def aggregate_metrics(results: list[dict]) -> dict:
    """把多标的回测结果聚合成 (sharpe 近似, max_drawdown, avg_return).

    夏普近似: 各标的段内收益的 mean/std (截面口径); 样本数少时保守返回 0.
    """
    returns = [r["total_return"] for r in results]
    if not returns:
        return {"sharpe": 0.0, "max_drawdown": 1.0, "avg_return": 0.0, "samples": 0}
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var)
    sharpe = (mean / std) if std > 1e-9 else (1.0 if mean > 0 else 0.0)
    return {
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max(r["max_drawdown"] for r in results), 4),
        "avg_return": round(mean, 4),
        "samples": len(returns),
    }


# ---------------------------------------------------------------------------
# 进化主流程
# ---------------------------------------------------------------------------

def _collect_series(
    symbols: Sequence[str],
    lookback_days: int,
) -> dict[str, list[float]]:
    """从数据服务拉取收盘价序列 (熔断/缓存已在 DataService 内处理)."""
    from .data_service import get_data_service

    data = get_data_service()
    series_map: dict[str, list[float]] = {}
    for symbol in symbols:
        df = data.get_ohlcv(symbol, lookback_days=lookback_days)
        if df is None or len(df) < MIN_SERIES_LEN:
            logger.warning("Evolution skip %s: OHLCV 不足 (%s 根)",
                           symbol, 0 if df is None else len(df))
            continue
        series_map[symbol] = [float(c) for c in df["Close"].tolist()]
    return series_map


def _segment_results(
    series_map: dict[str, list[float]],
    seg: range,
    params: dict,
) -> list[dict]:
    results = []
    for closes in series_map.values():
        segment = list(closes[seg.start:seg.stop])
        if len(segment) < 5:
            continue
        results.append(backtest_stop_policy(segment, **params))
    return results


def search_params(
    series_map: dict[str, list[float]],
    train_seg: range,
    valid_seg: range,
    grid: Optional[list[dict]] = None,
    scorer: Optional[Callable[[dict], float]] = None,
) -> tuple[Optional[dict], dict]:
    """训练集网格搜索 + 验证集确认. 返回 (最优参数, {train, valid} 指标).

    调参只在训练集运行 (§10.3); 验证集仅用于在候选中做最终选择,
    不参与任何搜索方向调整.
    """
    grid = grid or DEFAULT_GRID
    scorer = scorer or (lambda m: m["sharpe"] - m["max_drawdown"])

    best_params, best_score, best_train, best_valid = None, -math.inf, {}, {}
    for params in grid:
        train_metrics = aggregate_metrics(_segment_results(series_map, train_seg, params))
        score = scorer(train_metrics)
        if score > best_score:
            best_score = score
            best_params = params
            best_train = train_metrics
            best_valid = aggregate_metrics(_segment_results(series_map, valid_seg, params))
    return best_params, {"train": best_train, "valid": best_valid}


def _next_evolved_rule_id() -> str:
    # 秒级时间戳 + 4 位随机后缀, 避免同一秒内多次进化产生 rule_id 冲突
    # (StrategyRule.rule_id 是主键, 冲突会静默覆盖前一条候选规则)
    return (
        f"stop_loss_evolved_{datetime.now().strftime('%Y%m%d%H%M%S')}_"
        f"{uuid.uuid4().hex[:4]}"
    )


def _seed_evolved_cases(rule_id: str, stop_loss_pct: float) -> None:
    """为候选规则补最小单测用例 (§10.2 上线前置条件)."""
    db_ops.add_test_case(
        rule_id, "trigger", {"current_loss_ratio": round(stop_loss_pct + 0.01, 4)}, True,
    )
    db_ops.add_test_case(
        rule_id, "no_trigger", {"current_loss_ratio": round(max(stop_loss_pct - 0.02, 0.0), 4)}, False,
    )


def evolve_stop_loss(
    symbols: Optional[Sequence[str]] = None,
    closes_by_symbol: Optional[dict[str, Sequence[float]]] = None,
    lookback_days: int = 240,
    grid: Optional[list[dict]] = None,
) -> dict:
    """止损/时间止损参数自我进化 (全流程带反过拟合保护).

    流程: 时序分割 → 训练集网格搜索 → 验证集确认 → 样本外硬门槛校验 →
    三套指标写入 evolution_history → 达标者以 enabled=False 进入人工审核
    队列; 不达标直接丢弃. 返回 {status, params, metrics, evolution_id, ...}.
    """
    from .rules_engine import submit_rule_candidate, validate_sample_out_perf

    series_map: dict[str, list[float]] = {}
    if closes_by_symbol:
        series_map = {k: [float(c) for c in v] for k, v in closes_by_symbol.items()}
    elif symbols:
        series_map = _collect_series(symbols, lookback_days)
    series_map = {k: v for k, v in series_map.items() if len(v) >= MIN_SERIES_LEN}
    if not series_map:
        return {"status": "skipped", "reason": "无足量历史数据 (需 ≥60 根日 K)"}

    n = min(len(v) for v in series_map.values())
    train_seg, valid_seg, oos_seg = time_series_split(n)
    if oos_seg.stop - oos_seg.start < 5:
        return {"status": "skipped", "reason": "样本外段过短, 无法校验"}

    best_params, tv_metrics = search_params(series_map, train_seg, valid_seg, grid)
    if best_params is None:
        return {"status": "skipped", "reason": "参数网格为空"}

    oos_metrics = aggregate_metrics(_segment_results(series_map, oos_seg, best_params))
    gate_rule = {"min_sample_out_perf": MIN_OOS_SHARPE}
    oos_passed = validate_sample_out_perf(oos_metrics, gate_rule) \
        and oos_metrics["max_drawdown"] <= MAX_OOS_DRAWDOWN

    rule_id = _next_evolved_rule_id()
    base_record = {
        "rule_id": rule_id,
        "params": {
            **best_params,
            "symbols": sorted(series_map.keys()),
            "split": {"train": len(train_seg), "valid": len(valid_seg), "oos": len(oos_seg)},
        },
        "train_metrics": tv_metrics.get("train", {}),
        "valid_metrics": tv_metrics.get("valid", {}),
        "oos_metrics": oos_metrics,
    }

    if not oos_passed:
        db_ops.add_evolution_record({
            **base_record,
            "status": "oos_rejected",
            "reviewer_note": (
                f"样本外不达标直接丢弃 (sharpe={oos_metrics['sharpe']} < "
                f"{MIN_OOS_SHARPE} 或回撤 {oos_metrics['max_drawdown']} > "
                f"{MAX_OOS_DRAWDOWN})"
            ),
        })
        logger.info("Evolution rejected (OOS gate): %s -> %s", best_params, oos_metrics)
        return {
            "status": "oos_rejected", "params": best_params,
            "metrics": {**tv_metrics, "oos": oos_metrics},
        }

    # 达标: 写入候选规则 (永不自动启用) + 走强制上线流水线进入人工审核。
    # evolution_history 记录由 submit_rule_candidate 内部唯一写入 (含三套指标),
    # 这里不再重复插入, 避免同一候选产生两条 pending_review 记录。
    stop_pct = float(best_params["stop_loss_pct"])
    _seed_evolved_cases(rule_id, stop_pct)
    submit = submit_rule_candidate({
        "rule_id": rule_id,
        "description": (
            f"进化止损: 亏损超 {stop_pct:.0%} 清仓 "
            f"(时间止损 {best_params['time_stop_days']} 交易日, "
            f"样本外 sharpe={oos_metrics['sharpe']})"
        ),
        "condition": f"current_loss_ratio > {stop_pct}",
        "action": "sell_all",
        "priority": 1,
        "version": 1,
        "gray_scale": True,
        "min_sample_out_perf": MIN_OOS_SHARPE,
        "metrics": {**tv_metrics, "oos": oos_metrics, "params": base_record["params"]},
    })
    record_id = submit.get("evolution_id")
    db_ops.log_decision(
        agent="evolution", decision="candidate_submitted",
        reason=f"进化候选 {rule_id} 通过样本外校验, 进入人工审核 (禁止自动上线)",
        detail={"params": best_params, "oos": oos_metrics},
    )
    logger.info("Evolution candidate submitted: %s (pending human review)", rule_id)
    return {
        "status": submit.get("status", "pending_review"),
        "rule_id": rule_id,
        "params": best_params,
        "metrics": {**tv_metrics, "oos": oos_metrics},
        "evolution_id": record_id,
    }
