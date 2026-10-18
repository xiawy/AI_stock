"""种子数据静态体检：泛词不准进关键词表，也不准跨主题共用。

线上事故（2026-10-18）：「AI 短剧出海」被配到「风电（出海）」，起因就是风电
关键词表里有裸词"出海"，而"出海"同样出现在医药（创新药出海）里 —— 一个词
属于两个主题，说明它根本不是赛道特征。这组用例把这类种子写法挡在提交前。
"""

from __future__ import annotations

from ai_stock.quant.theme_seed import DEFAULT_THEMES, GENERIC_TOKENS


def _all_tokens(theme: dict) -> list[str]:
    return list(theme.get("keywords", [])) + list(theme.get("aliases", []))


def test_no_generic_token_in_seed():
    offenders = [
        (t["name"], tok)
        for t in DEFAULT_THEMES
        for tok in _all_tokens(t)
        if tok.lower() in GENERIC_TOKENS
    ]
    assert offenders == [], f"泛词不该出现在关键词/别名里：{offenders}"


def test_tokens_are_not_shared_between_themes():
    seen: dict[str, str] = {}
    dup: list[tuple[str, str, str]] = []
    for t in DEFAULT_THEMES:
        for tok in _all_tokens(t):
            key = tok.lower()
            owner = seen.get(key)
            if owner and owner != t["name"]:
                dup.append((tok, owner, t["name"]))
            seen[key] = t["name"]
    assert dup == [], f"同一个词被两个主题共用（必然误配）：{dup}"


def test_each_theme_has_enough_keywords():
    for t in DEFAULT_THEMES:
        assert len(t.get("keywords", [])) >= 3, t["name"]


def test_theme_names_are_unique():
    names = [t["name"] for t in DEFAULT_THEMES]
    assert len(names) == len(set(names))
