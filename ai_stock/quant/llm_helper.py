"""LLM helper for quant agents.

复用 ``ai_stock.llm_clients.factory`` 创建 quick/deep 双 LLM;
``structured_invoke`` 提供 with_structured_output → 自由文本 + JSON 提取
的两段式调用 (与 pipeline/llm_judge.py 相同模式).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class QuantLLM:
    """quick/deep 双 LLM 持有者."""

    def __init__(self, llm_quick: Any = None, llm_deep: Any = None):
        self.quick = llm_quick
        self.deep = llm_deep

    @property
    def available(self) -> bool:
        return self.quick is not None or self.deep is not None

    def pick(self, deep: bool = False) -> Any:
        return self.deep if (deep and self.deep is not None) else self.quick


def create_quant_llm(config: dict) -> QuantLLM:
    """按全局 DEFAULT_CONFIG 创建 quant 用的双 LLM."""
    from ai_stock.llm_clients.factory import create_llm_client

    provider = config.get("llm_provider", "openai")
    backend_url = config.get("backend_url")
    max_tokens = config.get("max_tokens")

    def _build(model_name: str):
        if not model_name:
            return None
        client = create_llm_client(
            provider, model_name, base_url=backend_url, max_tokens=max_tokens,
        )
        return client.get_llm()

    try:
        quick = _build(config.get("quick_think_llm"))
    except Exception as exc:
        logger.warning("quant quick LLM init failed: %s", exc)
        quick = None
    try:
        deep = _build(config.get("deep_think_llm"))
    except Exception as exc:
        logger.warning("quant deep LLM init failed: %s", exc)
        deep = None
    return QuantLLM(llm_quick=quick, llm_deep=deep)


def structured_invoke(
    llm: Any,
    schema: Type[T],
    prompt: str,
    default: Optional[T] = None,
    deep: bool = False,
) -> T:
    """结构化调用: with_structured_output 优先, 失败降级 invoke + JSON 提取."""
    if llm is None:
        if default is not None:
            return default
        raise RuntimeError("LLM 不可用")
    try:
        structured = llm.with_structured_output(schema)
        result = structured.invoke(prompt)
        if isinstance(result, schema):
            return result
        if isinstance(result, dict):
            return schema.model_validate(result)
    except Exception as exc:
        logger.debug("structured_output failed (%s); 自由文本降级", exc)

    try:
        response = llm.invoke(prompt)
        content = (
            response.content if hasattr(response, "content") else str(response)
        )
        parsed = _extract_json(content)
        if parsed is not None:
            return schema.model_validate(parsed)
    except Exception as exc:
        logger.warning("LLM invoke/parse failed: %s", exc)

    if default is not None:
        return default
    raise RuntimeError("LLM 结构化输出解析失败")


def _extract_json(text: str) -> Optional[dict]:
    """从自由文本中提取第一个 JSON 对象 (容忍 markdown 代码块)."""
    if not text:
        return None
    cleaned = text.replace("```json", "```")
    match = re.search(r"```(.*?)```", cleaned, re.DOTALL)
    candidates = [match.group(1)] if match else []
    # 直接找 {...} 块
    brace = re.search(r"\{[\s\S]*\}", cleaned)
    if brace:
        candidates.append(brace.group())
    for candidate in candidates:
        try:
            data = json.loads(candidate.strip())
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            continue
    return None
