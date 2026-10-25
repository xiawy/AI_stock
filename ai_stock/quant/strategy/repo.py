"""持久化 —— 唯一写入口.

三条铁律 (违反即视为 bug, 且有测试守着):
1. 单事务: 报告/篮子/高标/影子/regime/断言一次提交, 不留半份报告给 18:00 的选股读.
2. 报告可重跑覆盖, 断言不可覆盖: 断言重复计数会让胜率静默失真.
3. intraday 断言直接拒绝入库: 盘中结论未定局, 进统计就是污染.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date, timedelta
from typing import Any, Sequence

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db import session_scope
from ..db_models import (
    StrategyEvidence,
    StrategyFlowShadow,
    StrategyLeaderTrace,
    StrategyMacroRegime,
    StrategyPrediction,
    StrategyReport,
    StrategyStructureBasket,
)
from .contracts import (
    BasketVerdict,
    Claim,
    Evidence,
    Judgement,
    LeaderTrace,
    Regime,
    ShadowSeries,
)


class StrategyRepo:
    """全部读写在 SQLite 上同步完成, 不做 ORM 关系, 不用延迟加载."""

    # ---- 写入 --------------------------------------------------------------

    def save_run(
        self,
        *,
        trade_date: str,
        snapshot_type: str,
        effective_from: str,
        evidences: Sequence[Evidence],
        verdicts: Sequence[BasketVerdict],
        traces: Sequence[LeaderTrace],
        shadows: Sequence[ShadowSeries],
        regime: Regime,
        claims: Sequence[Claim],
        sections: dict,
        degraded: Sequence[str],
        prompt_version: int,
    ) -> dict:
        """一次事务写完当日全部产出."""
        stats = {"baskets": 0, "leaders": 0, "shadows": 0, "claims": 0}

        with closing(session_scope()) as s:
            try:
                self._save_evidence(s, trade_date, evidences)
                stats["baskets"] = self._save_baskets(s, trade_date, verdicts)
                stats["leaders"] = self._save_leaders(s, trade_date, traces)
                stats["shadows"] = self._save_shadows(s, trade_date, shadows)
                self._save_regime(s, trade_date, regime)
                self._save_report(
                    s, trade_date, snapshot_type, effective_from,
                    sections, degraded, prompt_version,
                )
                stats["claims"] = self._insert_claims(s, claims)
                s.commit()
            except Exception:
                s.rollback()
                raise
        return stats

    def _save_evidence(self, s, trade_date: str, rows: Sequence[Evidence]) -> None:
        if not rows:
            return
        s.execute(
            sqlite_insert(StrategyEvidence).values([
                {
                    "trade_date": trade_date,
                    "kind": e.kind,
                    "source": e.source,
                    "ok": e.ok,
                    "empty_reason": e.empty_reason[:200],
                    "fetched_at": e.fetched_at,
                    "payload_json": json.dumps(e.payload, ensure_ascii=False)[:4000],
                }
                for e in rows
            ])
        )

    def _save_baskets(self, s, trade_date: str, rows: Sequence[BasketVerdict]) -> int:
        if not rows:
            return 0
        stmt = sqlite_insert(StrategyStructureBasket).values([
            {
                "trade_date": trade_date,
                "parent_board": v.basket.parent_board,
                "basket_name": v.basket.name,
                "members_json": json.dumps(v.basket.members, ensure_ascii=False),
                "change_json": json.dumps(v.basket.change_pct, ensure_ascii=False),
                "synth_change_pct": v.synth_change_pct,
                "valid_n": v.valid_n,
                "evidence_dist_json": json.dumps(v.evidence_dist, ensure_ascii=False),
                "driver_consistency": v.driver_consistency,
                "rating_cap": v.rating_cap,
                "verdict": v.verdict,
                "reason": v.reason[:300],
            }
            for v in rows
        ])
        s.execute(stmt.on_conflict_do_update(
            index_elements=["trade_date", "basket_name"],
            set_={
                "synth_change_pct": stmt.excluded.synth_change_pct,
                "valid_n": stmt.excluded.valid_n,
                "rating_cap": stmt.excluded.rating_cap,
                "verdict": stmt.excluded.verdict,
                "reason": stmt.excluded.reason,
                "members_json": stmt.excluded.members_json,
                "change_json": stmt.excluded.change_json,
                "evidence_dist_json": stmt.excluded.evidence_dist_json,
                "driver_consistency": stmt.excluded.driver_consistency,
            },
        ))
        return len(rows)

    def _save_leaders(self, s, trade_date: str, rows: Sequence[LeaderTrace]) -> int:
        if not rows:
            return 0
        stmt = sqlite_insert(StrategyLeaderTrace).values([
            {
                "trade_date": trade_date,
                "symbol": t.symbol,
                "name": t.name,
                "boards": t.boards,
                "board_type": t.board_type,
                "official_board": t.official_board,
                "real_driver": t.real_driver,
                "attribution": t.attribution,
                "fund_verdict": t.fund_verdict,
                "extrapolable": t.extrapolable,
                "exclude_flag": t.exclude_from_basket,
                "reason_tags_json": json.dumps(t.reason_tags, ensure_ascii=False),
            }
            for t in rows
        ])
        s.execute(stmt.on_conflict_do_update(
            index_elements=["trade_date", "symbol"],
            set_={
                "boards": stmt.excluded.boards,
                "board_type": stmt.excluded.board_type,
                "real_driver": stmt.excluded.real_driver,
                "attribution": stmt.excluded.attribution,
                "fund_verdict": stmt.excluded.fund_verdict,
                "extrapolable": stmt.excluded.extrapolable,
                "exclude_flag": stmt.excluded.exclude_flag,
            },
        ))
        return len(rows)

    def _save_shadows(self, s, trade_date: str, rows: Sequence[ShadowSeries]) -> int:
        if not rows:
            return 0
        stmt = sqlite_insert(StrategyFlowShadow).values([
            {
                "trade_date": trade_date,
                "basket_name": x.basket_name,
                "members_json": json.dumps(x.members, ensure_ascii=False),
                "d2": x.d2 or 0.0, "d1": x.d1 or 0.0, "d0": x.d0 or 0.0,
                "pattern": x.pattern, "confidence": x.confidence,
            }
            for x in rows
        ])
        s.execute(stmt.on_conflict_do_update(
            index_elements=["trade_date", "basket_name"],
            set_={
                "d2": stmt.excluded.d2, "d1": stmt.excluded.d1, "d0": stmt.excluded.d0,
                "pattern": stmt.excluded.pattern, "confidence": stmt.excluded.confidence,
            },
        ))
        return len(rows)

    def _save_regime(self, s, trade_date: str, regime: Regime) -> None:
        stmt = sqlite_insert(StrategyMacroRegime).values([{
            "trade_date": trade_date,
            "regime": regime.code,
            "growth_cap": regime.growth_cap or "",
            "note": regime.note[:300],
            "vars_json": regime.vars.model_dump_json(),
            "triggers_json": json.dumps(regime.triggers, ensure_ascii=False),
            "missing_json": json.dumps(regime.missing, ensure_ascii=False),
        }])
        s.execute(stmt.on_conflict_do_update(
            index_elements=["trade_date"],
            set_={
                "regime": stmt.excluded.regime,
                "growth_cap": stmt.excluded.growth_cap,
                "note": stmt.excluded.note,
                "vars_json": stmt.excluded.vars_json,
                "triggers_json": stmt.excluded.triggers_json,
                "missing_json": stmt.excluded.missing_json,
            },
        ))

    def _save_report(
        self,
        s,
        trade_date: str,
        snapshot_type: str,
        effective_from: str,
        sections: dict,
        degraded: Sequence[str],
        prompt_version: int,
    ) -> None:
        stmt = sqlite_insert(StrategyReport).values([{
            "trade_date": trade_date,
            "snapshot_type": snapshot_type,
            "effective_from": effective_from,
            "sections_json": json.dumps(sections, ensure_ascii=False),
            "degraded_json": json.dumps(list(degraded), ensure_ascii=False),
            "prompt_version": prompt_version,
        }])
        s.execute(stmt.on_conflict_do_update(
            index_elements=["trade_date", "snapshot_type"],
            set_={
                "effective_from": stmt.excluded.effective_from,
                "sections_json": stmt.excluded.sections_json,
                "degraded_json": stmt.excluded.degraded_json,
                "prompt_version": stmt.excluded.prompt_version,
            },
        ))

    def _insert_claims(self, s, claims: Sequence[Claim]) -> int:
        """断言入库. 只接受 close 快照; 重复 claim_id 忽略 (不覆盖)."""
        rows = []
        for c in claims:
            if c.snapshot_type != "close":
                raise ValueError(
                    f"intraday claim {c.claim_id} 不得入账: 盘中结论未定局 (PRD §7.6.3)"
                )
            rows.append({
                "claim_id": c.claim_id,
                "period": c.period,
                "type": c.type,
                "target": c.target,
                "driver": c.driver,
                "claim_text": c.text,
                "direction": c.direction,
                "horizon": c.horizon,
                "confidence": c.confidence,
                "falsify": c.falsify,
                "magnitude": c.magnitude,
                "regime": c.regime,
                "crowding": c.crowding,
                "verify_date": c.verify_date.isoformat(),
                "snapshot_type": c.snapshot_type,
                "verify_state": "pending",
            })
        if not rows:
            return 0
        stmt = sqlite_insert(StrategyPrediction).values(rows)
        s.execute(stmt.on_conflict_do_nothing(index_elements=["claim_id"]))
        return len(rows)

    # ---- 读取 --------------------------------------------------------------

    def load_report(self, trade_date: str, snapshot_type: str = "close") -> dict | None:
        with closing(session_scope()) as s:
            row = (
                s.query(StrategyReport)
                .filter(
                    StrategyReport.trade_date == trade_date,
                    StrategyReport.snapshot_type == snapshot_type,
                )
                .one_or_none()
            )
            if row is None:
                return None
            return {
                "trade_date": row.trade_date,
                "snapshot_type": row.snapshot_type,
                "effective_from": row.effective_from,
                "sections": json.loads(row.sections_json or "{}"),
                "degraded": json.loads(row.degraded_json or "[]"),
                "prompt_version": row.prompt_version,
            }

    def due_claims(self, asof: str) -> list[dict]:
        with closing(session_scope()) as s:
            rows = (
                s.query(StrategyPrediction)
                .filter(
                    StrategyPrediction.verify_state == "pending",
                    StrategyPrediction.verify_date <= asof,
                    StrategyPrediction.snapshot_type == "close",
                )
                .all()
            )
            return [self._claim_dict(r) for r in rows]

    def apply_judgement(self, claim_id: str, j: Judgement) -> bool:
        with closing(session_scope()) as s:
            row = (
                s.query(StrategyPrediction)
                .filter(StrategyPrediction.claim_id == claim_id)
                .one_or_none()
            )
            if row is None:
                return False
            row.verify_state = j.state
            row.outcome = j.outcome
            row.move = float(j.move or 0.0)
            row.unjudged_reason = j.reason[:120]
            s.commit()
            return True

    def basket_members(self, trade_date: str, basket_name: str) -> list[str]:
        """取当日篮子的成员 —— 回填时必须用**当时**的成员, 不能用今天的."""
        with closing(session_scope()) as s:
            row = (
                s.query(StrategyStructureBasket)
                .filter(
                    StrategyStructureBasket.trade_date == trade_date,
                    StrategyStructureBasket.basket_name == basket_name,
                )
                .one_or_none()
            )
            if row is None:
                return []
            return json.loads(row.members_json or "[]")

    def basket_drivers(self, trade_date: str, lookback_days: int = 10) -> dict[str, str]:
        """篮子名 → 驱动源. 来源是断言表 (target=篮子名, driver=当日主驱动).

        为什么不在 basket 表加 driver 列: 量化表没有 Alembic, 给已存在的表
        加列必须手工 DDL, 而"重启即生效"是这个项目一贯的上线动作。断言表
        本来就要存 driver, 复用它既不加列也不产生第二份真相.
        """
        try:
            since = (
                date.fromisoformat(trade_date[:10]) - timedelta(days=lookback_days)
            ).isoformat()
        except ValueError:
            since = trade_date[:10]
        with closing(session_scope()) as s:
            rows = (
                s.query(StrategyPrediction.target, StrategyPrediction.driver)
                .filter(
                    StrategyPrediction.type == "MAINLINE",
                    StrategyPrediction.period >= since,
                )
                .all()
            )
        out: dict[str, str] = {}
        for target, driver in rows:
            if target and driver:
                out.setdefault(str(target), str(driver))
        return out

    def score(self) -> dict:
        """分组胜率. 分母只算 judged —— unjudged 是数据故障, 不是判断错误."""
        with closing(session_scope()) as s:
            rows = s.query(StrategyPrediction).all()
        judged = [r for r in rows if r.verify_state == "judged"]
        unjudged = [r for r in rows if r.verify_state == "unjudged"]

        def _win(group: Sequence[Any]) -> dict:
            n = len(group)
            hit = sum(1 for r in group if r.outcome == "兑现")
            partial = sum(1 for r in group if r.outcome == "部分")
            return {"n": n, "hit": hit, "partial": partial,
                    "win_rate": round(hit / n, 3) if n else None}

        by_driver: dict[str, dict] = {}
        for r in judged:
            by_driver.setdefault(r.driver, []).append(r)

        return {
            "total": len(rows),
            "judged": len(judged),
            "unjudged": len(unjudged),
            "unjudged_rate": round(len(unjudged) / max(len(rows), 1), 3),
            "overall": _win(judged),
            "by_driver": {k: _win(v) for k, v in by_driver.items()},
            "by_type": {
                t: _win([r for r in judged if r.type == t])
                for t in {r.type for r in judged}
            },
            # 回灌用的 driver 质量表: 含 unjudged, 因为"数据缺失率"必须参与
            # 出不出系数的判断 (见 feedback.compute_coefs)
            "driver_quality": _driver_quality(rows),
        }

    @staticmethod
    def _claim_dict(r: StrategyPrediction) -> dict:
        return {
            "claim_id": r.claim_id,
            "period": r.period,
            "target": r.target,
            "type": r.type,
            "direction": r.direction,
            "verify_date": r.verify_date,
            "driver": r.driver,
        }


def _driver_quality(rows: Sequence[StrategyPrediction]) -> dict[str, dict]:
    """按 driver 汇总: 只算 judged 的胜率, 但 unjudged 单独列出并计入分母.

    unjudged 率高代表"数据拿不到", 若把它混进胜率分母, 一次数据源故障就能
    把整组胜率稀释掉进而错调权重 —— 这正是要显式分开的原因.
    """
    buckets: dict[str, list[StrategyPrediction]] = {}
    for row in rows:
        buckets.setdefault(row.driver or "情绪", []).append(row)

    out: dict[str, dict] = {}
    for driver, items in buckets.items():
        judged = [r for r in items if r.verify_state == "judged"]
        unjudged = [r for r in items if r.verify_state == "unjudged"]
        hit = sum(1 for r in judged if r.outcome == "兑现")
        out[driver] = {
            "n": len(items),
            "judged": len(judged),
            "unjudged": len(unjudged),
            "unjudged_rate": round(len(unjudged) / max(len(items), 1), 3),
            "hit": hit,
            "win_rate": round(hit / len(judged), 3) if judged else None,
        }
    return out
