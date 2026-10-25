"""三榜联动 API (变化视图 / 证据链 / 成绩单)。

三个榜原本各自刷新、各自排序、互不引用。本模块只提供"把它们连起来看"的
读接口:

- ``/diff``      每个榜"今天变了什么"（新进 / 上升 / 下降 / 掉榜）
- ``/chain``     给定主题，一次返回 新闻 → 行业 → 龙头 → 池内标的
- ``/scorecard`` 每个榜"上次这么说，兑现了吗"
- ``/cluster``   新闻榜"一行一件事"的多源展开

全部只读。写入口仍在各自的生产链路里（选股流程 / 新闻榜落库），这里不落库、
不改阈值 —— 成绩单只用来**看**，不用来**调参**。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_user
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/board", tags=["board"])

_READY = False


def _ensure_ready() -> None:
    """幂等建表: 池快照表靠 create_all 建, 上线动作只有重启。"""
    global _READY
    if _READY:
        return
    from app.services.quant_service import prepare_quant_env

    prepare_quant_env()
    from ai_stock.quant import db

    db.init_quant_db()
    _READY = True


# ---------------------------------------------------------------------------
# 变化视图
# ---------------------------------------------------------------------------


@router.get("/industry/diff")
def industry_diff(date: Optional[str] = None, _: User = Depends(get_current_user)) -> dict:
    """行业榜变化: Δrank / 今日新进 / 连榜天数。数据来自现表, 零新增依赖。"""
    _ensure_ready()
    from app.services.pipeline_service import get_pipeline_service

    return get_pipeline_service().get_industry_diff(date or "")


@router.get("/impact/diff")
def impact_diff(date: Optional[str] = None, _: User = Depends(get_current_user)) -> dict:
    """新闻榜变化: 与上一份快照对比(一天有 3 份, 所以不是"与昨天比")。"""
    _ensure_ready()
    from app.services.pipeline_service import get_pipeline_service

    return get_pipeline_service().get_impact_diff(date or "")


@router.get("/recommendation/diff")
def recommendation_diff(
    date: Optional[str] = None, _: User = Depends(get_current_user),
) -> dict:
    """热股榜变化: 靠 ``quant_pool_snapshot`` 日快照才能算(此前历史不可复现)。"""
    _ensure_ready()
    from app.services.pipeline_service import get_pipeline_service

    return get_pipeline_service().get_hot_stocks_diff(date or "")


# ---------------------------------------------------------------------------
# 证据链
# ---------------------------------------------------------------------------


@router.get("/chain")
def theme_chain(
    theme: str = Query(..., min_length=1, description="主题/行业名"),
    date: Optional[str] = None,
    _: User = Depends(get_current_user),
) -> dict:
    """一个主题在三榜上的全部证据: 新闻 → 行业 → 龙头 → 池内标的。

    三段用同一套名称匹配（``strategy.rules.textmatch``），所以三个页面上的
    "相关新闻"从此是同一批；某一段确实没有数据时返回空列表 + ``notes``，
    不编造。
    """
    _ensure_ready()
    from app.services.board_chain import build_chain

    return build_chain(theme, date_str=date or "")


@router.get("/cluster")
def cluster_members(
    snapshot_id: int,
    cluster_id: str,
    _: User = Depends(get_current_user),
) -> dict:
    """同事件多源成员: 这条新闻被哪几家媒体报道。

    新闻榜每个事件只占一个榜位，其余转载存在 ``news_cluster_member``；
    这张表上线前落库的快照没有明细，返回空列表是正常的。
    """
    _ensure_ready()
    from app.services.pipeline_service import get_pipeline_service

    members = get_pipeline_service().get_cluster_members(snapshot_id, cluster_id)
    return {"snapshot_id": snapshot_id, "cluster_id": cluster_id, "members": members}


# ---------------------------------------------------------------------------
# 成绩单
# ---------------------------------------------------------------------------


@router.get("/scorecard")
def scorecard(
    days: int = Query(30, ge=1, le=365),
    include_returns: int = Query(0, ge=0, le=1),
    _: User = Depends(get_current_user),
) -> dict:
    """三榜成绩单（只读）。

    三条纪律：分母只算已判定样本、样本 < 30 不出胜率、不回写任何阈值。
    ``include_returns=1`` 会额外拉 K 线算热股榜 5 日收益（较慢，默认关闭）。
    """
    _ensure_ready()
    from app.services.board_scorecard import build_scorecard

    return build_scorecard(days=days, include_returns=bool(include_returns))
