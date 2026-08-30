from langchain_core.tools import tool
from typing import Annotated
from ai_stock.dataflows.interface import route_to_vendor


def fetch_fundamentals(ticker: str, curr_date: str = None):
    """纯函数内核: 返回供应商原始结果 (基本面文本), 供程序化调用."""
    return route_to_vendor("get_fundamentals", ticker, curr_date)


def fetch_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """纯函数内核: 返回供应商原始结果 (资产负债表文本), 供程序化调用."""
    return route_to_vendor("get_balance_sheet", ticker, freq, curr_date)


def fetch_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """纯函数内核: 返回供应商原始结果 (现金流量表文本), 供程序化调用."""
    return route_to_vendor("get_cashflow", ticker, freq, curr_date)


def fetch_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """纯函数内核: 返回供应商原始结果 (利润表文本), 供程序化调用."""
    return route_to_vendor("get_income_statement", ticker, freq, curr_date)


@tool
def get_fundamentals(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """
    Retrieve comprehensive fundamental data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing comprehensive fundamental data
    """
    return fetch_fundamentals(ticker, curr_date)


@tool
def get_balance_sheet(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve balance sheet data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing balance sheet data
    """
    return fetch_balance_sheet(ticker, freq, curr_date)


@tool
def get_cashflow(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve cash flow statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing cash flow statement data
    """
    return fetch_cashflow(ticker, freq, curr_date)


@tool
def get_income_statement(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve income statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing income statement data
    """
    return fetch_income_statement(ticker, freq, curr_date)
