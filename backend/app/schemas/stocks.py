"""Stock data Pydantic schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StockSearchResponse(BaseModel):
    raw: str
    code: str
    name: str | None = None
    label: str


class KlineItem(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class KlineResponse(BaseModel):
    code: str
    name: str | None = None
    end_date: str
    items: list[KlineItem] = Field(default_factory=list)


class QuoteResponse(BaseModel):
    code: str
    name: str | None = None
    price: float | None = None
    change_pct: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    mcap_yi: float | None = None
    turnover_pct: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
