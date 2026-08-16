"""History endpoints: saved analysis reports + markdown / PDF export."""

from __future__ import annotations

import re
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.trading import bootstrap as bootstrap_engine
from app.dependencies import get_current_user
from app.models import AnalysisTask, User


def _web() -> SimpleNamespace:
    """Lazy import of the original project's history/export helpers."""
    bootstrap_engine()
    from web import history as web_history
    from web import pdf_export as web_pdf_export
    from web import stock_display as web_stock_display

    return SimpleNamespace(
        history=web_history,
        pdf=web_pdf_export,
        display=web_stock_display,
    )


router = APIRouter(prefix="/history", tags=["history"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER_RE = re.compile(r"^\d{6}$")


def _content_disposition(label: str, ticker: str, trade_date: str, ext: str) -> str:
    """RFC 5987: ASCII fallback + UTF-8 filename* — labels carry Chinese stock names,
    which crash latin-1 header encoding when put into Content-Disposition directly."""
    ascii_name = f"AI-Stock_{ticker.upper()}_{trade_date}.{ext}"
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", label).strip("_") or ticker.upper()
    utf8_name = quote(f"AI-Stock_{safe}_{trade_date}.{ext}")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"


def _user_task_keys(db: Session, user_id: int) -> set[tuple[str, str]]:
    """(ticker, trade_date) pairs the user has ever run an analysis for.

    Report files are shared on disk (keyed by ticker+date only), so user
    isolation is enforced against the per-user ``analysis_tasks`` rows.
    """
    rows = (
        db.query(AnalysisTask.ticker, AnalysisTask.trade_date)
        .filter(AnalysisTask.user_id == user_id)
        .all()
    )
    return {(ticker.upper(), trade_date) for ticker, trade_date in rows}


def _find_history_path(db: Session, current_user: User, ticker: str, trade_date: str) -> str:
    if not _TICKER_RE.match(ticker) or not _DATE_RE.match(trade_date):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="参数格式错误（需6位代码与 YYYY-MM-DD）"
        )
    # Per-user isolation: only the user who ran this analysis may read its
    # report. 404 (not 403) so other users' records are not discoverable.
    if (ticker.upper(), trade_date) not in _user_task_keys(db, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 {ticker} 在 {trade_date} 的分析记录",
        )
    for entry in _web().history.get_history():
        if entry["ticker"] == ticker.upper() and entry["date"] == trade_date:
            return entry["path"]
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"未找到 {ticker} 在 {trade_date} 的分析记录",
    )


def _load_normalized(db: Session, current_user: User, ticker: str, trade_date: str) -> dict:
    web = _web()
    state = web.history.load_analysis(
        _find_history_path(db, current_user, ticker, trade_date)
    )
    # Normalize "code name" mentions the same way the live report does (#55).
    web.display.normalize_report_state_mentions(state, ticker)
    return state


@router.get("", summary="分析历史记录列表（仅当前用户的记录）")
def list_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    owned = _user_task_keys(db, current_user.id)
    return [
        entry
        for entry in _web().history.get_history()
        if (entry["ticker"], entry["date"]) in owned
    ]


@router.get("/{ticker}/{trade_date}", summary="加载某次分析的完整报告 JSON（仅限本人）")
def get_report(
    ticker: str,
    trade_date: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    web = _web()
    state = _load_normalized(db, current_user, ticker, trade_date)
    return {
        "ticker": ticker.upper(),
        "stock_label": web.display.stock_display_label(ticker, state),
        "trade_date": trade_date,
        "signal": web.history.extract_signal(state),
        "final_state": state,
    }


@router.get("/{ticker}/{trade_date}/markdown", summary="导出 Markdown 报告（仅限本人）")
def export_markdown(
    ticker: str,
    trade_date: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    web = _web()
    state = web.history.load_analysis(
        _find_history_path(db, current_user, ticker, trade_date)
    )
    signal = web.history.extract_signal(state)
    label = web.display.stock_display_label(ticker, state)
    md = web.pdf.generate_markdown(state, ticker, trade_date, signal)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(label, ticker, trade_date, "md")},
    )


@router.get("/{ticker}/{trade_date}/pdf", summary="导出 PDF 报告（仅限本人）")
def export_pdf(
    ticker: str,
    trade_date: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    web = _web()
    state = _load_normalized(db, current_user, ticker, trade_date)
    signal = web.history.extract_signal(state)
    label = web.display.stock_display_label(ticker, state)
    try:
        pdf = web.pdf.generate_pdf(state, ticker, trade_date, signal)
    except Exception as exc:  # noqa: BLE001 — PDF/font failures must not 500-crash
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"PDF 生成失败，请改用 Markdown 导出。原因：{exc}",
        ) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(label, ticker, trade_date, "pdf")},
    )
