"""主题雷达 + 动态置信度维护 Agent (旁路注入增强, 队列: selection_control).

设计原则 (旁路注入 + 加权覆盖):
- ``ThemeRadarAgent``: 全市场微观异动扫描, 产出 ``radar_alerts`` 注入下游选股
  Context; 数据源挂 -> 空 alerts -> 选股退化为原版涨幅榜/事件映射 (行为零变化)。
- ``ConfidenceMaintainAgent``: 选股流程最后一步, 维护自选池 ``dyn_confidence``
  (衰减/增强/鱼尾标记/衰竭踢出); 全程不触碰原 ``confidence`` (0-10 LLM 分)。

两者均为独立 Agent, 不修改原有核心函数内部逻辑链条 (天然降级)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import db_ops
from ..config import (
    CONF_DECAY_AFTER_DAYS,
    CONF_DECAY_FACTOR,
    CONF_DEFAULT,
    CONF_KICK_THRESHOLD,
    CONF_REINFORCE_FACTOR,
    RADAR_STRONG_CLUSTER,
)
from ..market_utils import is_tail_phase
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ThemeRadarAgent(BaseAgent):
    """主题雷达: 扫描全市场"放量 + 温和上涨"的细分主题隐形冠军。

    产出 ``radar_alerts=[{theme, codes, strength}]`` 注入下游 IndustryScanAgent
    候选池 (旁路增强)。``strength`` 由簇规模归一 ``min(1, 家数/RADAR_STRONG_CLUSTER)``。
    数据源异常/空 -> ``radar_alerts:[]`` -> 选股退化为原版 (天然降级)。
    """

    agent_name = "theme_radar"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")

        try:
            clusters = self.data.scan_micro_anomalies() or {}
        except Exception as exc:
            logger.warning("theme_radar scan degraded to empty: %s", exc)
            clusters = {}

        radar_alerts: list[dict] = []
        for theme, codes in clusters.items():
            clean_codes = [str(c) for c in (codes or []) if c]
            if not theme or not clean_codes:
                continue
            strength = (
                min(1.0, len(clean_codes) / RADAR_STRONG_CLUSTER)
                if RADAR_STRONG_CLUSTER else 0.0
            )
            radar_alerts.append({
                "theme": theme,
                "codes": clean_codes,
                "strength": round(strength, 3),
            })
        # 强度降序: 强主题优先注入下游候选池
        radar_alerts.sort(key=lambda a: a["strength"], reverse=True)
        themes = [a["theme"] for a in radar_alerts]

        result = {
            "decision": "ok" if radar_alerts else "empty",
            "radar_alerts": radar_alerts,
            "themes": themes,
        }
        if radar_alerts:
            self.log(
                "radar_scan_done",
                reason=(
                    f"微观异动雷达命中 {len(radar_alerts)} 个主题: "
                    f"{[(a['theme'], len(a['codes']), a['strength']) for a in radar_alerts]}"
                ),
                task_id=task_id, flow_id=flow_id, detail=result,
            )
        else:
            self.log(
                "radar_scan_empty",
                reason="微观异动雷达无命中 (或数据源降级), 选股退化为原版涨幅榜/事件映射",
                task_id=task_id, flow_id=flow_id,
            )
        self.report(flow_type, flow_id, step, result)
        return result


class ConfidenceMaintainAgent(BaseAgent):
    """动态置信度维护官: 选股流程最后一步, 维护自选池 ``dyn_confidence`` (旁路)。

    遍历 active 自选池:
    - ``radar_theme`` 命中今日雷达主题 -> 增强 ``*CONF_REINFORCE_FACTOR`` 且刷新
      ``last_confirmed``;
    - 否则距 ``last_confirmed`` (缺则 ``add_time``) > ``CONF_DECAY_AFTER_DAYS`` 天
      -> 衰减 ``*CONF_DECAY_FACTOR``;
    - ``is_tail_phase`` 为真 -> ``stage_batch='TAIL'`` (供清理官/买入拦截消费);
    - clamp 到 [0,1]; ``dyn_confidence < CONF_KICK_THRESHOLD`` -> 移出 (置信度衰竭);
    - 其余经 ``update_optional_dynamic`` 落库。

    全程不触碰原 ``confidence`` (0-10 LLM 分) 与入池门槛语义。
    """

    agent_name = "confidence_maintain"

    def handle(self, task: dict) -> dict:
        flow_id, flow_type, step, context = self.extract_flow(task.get("payload", {}))
        task_id = task.get("task_id", "")

        radar = context.get("theme_radar") or {}
        today_themes = {t for t in (radar.get("themes") or []) if t}

        try:
            pool = db_ops.get_optional_pool("active")
        except Exception as exc:
            logger.warning("confidence_maintain read pool failed: %s", exc)
            pool = []

        now = datetime.now(timezone.utc)
        decayed = reinforced = tail_flagged = kicked = 0
        for entry in pool:
            symbol = entry.get("symbol", "")
            if not symbol:
                continue
            try:
                dyn = float(entry.get("dyn_confidence", CONF_DEFAULT) or CONF_DEFAULT)
            except (TypeError, ValueError):
                dyn = CONF_DEFAULT
            theme = entry.get("radar_theme", "") or ""

            # 增强优先 (今日主题再现即确认), 否则超期未验证才衰减
            hit_theme = self._theme_hit(theme, today_themes)
            new_dyn = dyn
            refreshed = False
            if hit_theme:
                new_dyn *= CONF_REINFORCE_FACTOR
                refreshed = True
                reinforced += 1
            elif self._is_stale(entry, now):
                new_dyn *= CONF_DECAY_FACTOR
                decayed += 1
            new_dyn = max(0.0, min(1.0, new_dyn))

            # 鱼尾标记 (数据不足/异常 -> is_tail_phase 返回 False, 保持原阶段)
            stage = entry.get("stage_batch", "BODY") or "BODY"
            if is_tail_phase(self.data, symbol):
                stage = "TAIL"
                tail_flagged += 1

            # 衰竭踢出: 低于门槛移出自选池 (记录 kicked_reason 与 remove_reason 区分)
            if new_dyn < CONF_KICK_THRESHOLD:
                try:
                    db_ops.update_optional_status(
                        symbol, "removed",
                        remove_reason="置信度衰竭",
                        kicked_reason="置信度衰竭",
                    )
                    kicked += 1
                    self.log(
                        "conf_kicked", symbol=symbol,
                        reason=(
                            f"dyn_confidence {new_dyn:.3f} < {CONF_KICK_THRESHOLD}, "
                            f"置信度衰竭移出自选池"
                        ),
                        task_id=task_id, flow_id=flow_id,
                    )
                except Exception as exc:
                    logger.warning("conf kick failed for %s: %s", symbol, exc)
                continue

            # 落库动态字段 (仅写变化项; last_confirmed 仅在增强命中时刷新)
            try:
                db_ops.update_optional_dynamic(
                    symbol,
                    dyn_confidence=new_dyn,
                    stage_batch=stage,
                    last_confirmed=now if refreshed else None,
                )
            except Exception as exc:
                logger.warning("conf dynamic update failed for %s: %s", symbol, exc)

        result = {
            "decision": "ok",
            "scanned": len(pool),
            "decayed": decayed,
            "reinforced": reinforced,
            "tail_flagged": tail_flagged,
            "kicked": kicked,
        }
        self.log(
            "conf_maintain_done",
            reason=(
                f"维护自选池 {len(pool)} 只: 增强 {reinforced}, 衰减 {decayed}, "
                f"鱼尾标记 {tail_flagged}, 衰竭踢出 {kicked}"
            ),
            task_id=task_id, flow_id=flow_id, detail=result,
        )
        self.report(flow_type, flow_id, step, result)
        return result

    @staticmethod
    def _theme_hit(theme: str, today_themes: set) -> bool:
        """radar_theme 是否命中今日雷达主题 (精确优先, 双向包含兜底)."""
        if not theme or not today_themes:
            return False
        if theme in today_themes:
            return True
        return any(theme in t or t in theme for t in today_themes if t)

    @staticmethod
    def _is_stale(entry: dict, now: datetime) -> bool:
        """距 last_confirmed (缺则 add_time) 是否超过 CONF_DECAY_AFTER_DAYS 天.

        时间戳缺失/不可解析 -> 保守返回 False (不衰减, 绝不误伤)。
        """
        raw = entry.get("last_confirmed") or entry.get("add_time")
        if not raw:
            return False
        try:
            last = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).days > CONF_DECAY_AFTER_DAYS
