"""事件-行业映射知识库 (选股优化点 2: 降低 LLM 行业映射幻觉).

本地关键词 → 利好行业映射表, 用于宏观事件解析的预检:
- ``match_industries(text)``: 关键词子串匹配, 返回命中的行业名列表 (保序去重)
- 内置规则覆盖常见主题; 支持通过 ``QUANT_EVENT_KB_FILE`` 环境变量指向
  额外 JSON 文件扩展 (格式: [{"keywords": [...], "industries": [...]}]),
  便于跟进最新政策/题材而无需改代码.

设计取舍:
- 只做粗筛与交叉验证, 不做最终决策 — LLM 仍负责事件提取与影响力排序
- 知识库无命中时不拦截 LLM 结果 (兜底), 有命中时以「交集优先」收敛
  LLM 输出, 压缩凭空捏造的行业关联
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

# 扩展知识库文件路径 (可选): [{"keywords": [...], "industries": [...]}]
_KB_FILE_ENV = "QUANT_EVENT_KB_FILE"

# 内置映射规则: 关键词 → 直接利好行业 (行业名与东财板块名尽量对齐)
_BUILTIN_RULES: list[dict] = [
    {"keywords": ["算力", "大模型", "人工智能", "AI", "AIGC", "ChatGPT", "智能体"],
     "industries": ["半导体", "通信设备", "计算机设备", "软件开发"]},
    {"keywords": ["光模块", "CPO", "数据中心", "东数西算", "液冷"],
     "industries": ["通信设备", "光学光电子"]},
    {"keywords": ["存储", "DRAM", "NAND", "HBM"],
     "industries": ["半导体", "电子元件"]},
    {"keywords": ["固态电池", "锂电池", "动力电池", "储能", "续航"],
     "industries": ["电池", "能源金属"]},
    {"keywords": ["新能源车", "电动车", "充电桩", "智能驾驶", "无人驾驶", "智驾"],
     "industries": ["汽车整车", "汽车零部件"]},
    {"keywords": ["光伏", "组件", "硅料", "逆变器"],
     "industries": ["光伏设备", "电源设备"]},
    {"keywords": ["风电", "海上风电"],
     "industries": ["风电设备"]},
    {"keywords": ["碳中和", "绿电", "电力市场", "电价"],
     "industries": ["电力行业", "环保行业"]},
    {"keywords": ["核电", "核聚变"],
     "industries": ["核能核电"]},
    {"keywords": ["低空经济", "eVTOL", "飞行汽车", "无人机"],
     "industries": ["航空航天", "通用设备"]},
    {"keywords": ["机器人", "人形机器人", "减速器", "灵巧手"],
     "industries": ["机器人概念", "自动化设备"]},
    {"keywords": ["军工", "国防", "导弹", "军贸"],
     "industries": ["军工行业", "航天航空"]},
    {"keywords": ["卫星", "商业航天", "火箭", "星链"],
     "industries": ["航天航空", "通信设备"]},
    {"keywords": ["创新药", "医保", "集采", "GLP-1", "减肥药"],
     "industries": ["生物制品", "化学制药", "医疗器械"]},
    {"keywords": ["中药", "中医药"],
     "industries": ["中药行业"]},
    {"keywords": ["白酒", "消费", "促消费", "以旧换新"],
     "industries": ["酿酒行业", "商业百货", "家电行业"]},
    {"keywords": ["房地产", "楼市", "城中村", "保障房"],
     "industries": ["房地产开发", "房地产服务"]},
    {"keywords": ["基建", "设备更新", "工程机械"],
     "industries": ["工程机械", "工程建设"]},
    {"keywords": ["黄金", "贵金属", "避险"],
     "industries": ["贵金属"]},
    {"keywords": ["铜", "有色", "稀土", "锂矿"],
     "industries": ["有色金属", "能源金属", "小金属"]},
    {"keywords": ["石油", "油价", "OPEC", "俄乌"],
     "industries": ["石油行业", "油气开采"]},
    {"keywords": ["粮食", "种业", "转基因", "小麦"],
     "industries": ["农牧饲渔", "农药兽药"]},
    {"keywords": ["券商", "资本市场", "并购重组", "牛市"],
     "industries": ["证券行业"]},
    {"keywords": ["数字经济", "数据要素", "信创"],
     "industries": ["软件开发", "互联网服务"]},
    {"keywords": ["半导体设备", "光刻机", "国产替代", "晶圆"],
     "industries": ["半导体"]},
]


@lru_cache(maxsize=1)
def _load_rules() -> tuple[dict, ...]:
    """内置规则 + 可选外部 JSON 扩展 (文件损坏仅告警不阻断)."""
    rules = [dict(r) for r in _BUILTIN_RULES]
    path = os.getenv(_KB_FILE_ENV, "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                extra = json.load(f)
            for item in extra:
                keywords = [str(k) for k in (item.get("keywords") or []) if k]
                industries = [str(i) for i in (item.get("industries") or []) if i]
                if keywords and industries:
                    rules.append({"keywords": keywords, "industries": industries})
            logger.info("Event-industry KB extended with %d rules from %s",
                        len(extra), path)
        except Exception as exc:
            logger.warning("Event-industry KB file %s load failed: %s", path, exc)
    return tuple(rules)


def match_industries(text: str) -> list[str]:
    """关键词子串匹配 → 命中行业列表 (保序去重); 空文本返回空."""
    if not text:
        return []
    out: list[str] = []
    for rule in _load_rules():
        if any(kw in text for kw in rule["keywords"]):
            for ind in rule["industries"]:
                if ind not in out:
                    out.append(ind)
    return out


def reconcile(llm_industries: list[str], kb_industries: list[str]) -> list[str]:
    """LLM 映射与知识库预检结果交叉收敛 (幻觉抑制):

    - 知识库无命中 → 原样保留 LLM 结果 (兜底, 不拦截)
    - 有命中 → 优先取双向模糊匹配的交集; 交集为空时用知识库结果替换
    """
    if not kb_industries:
        return list(llm_industries)
    matched: list[str] = []
    for llm_tag in llm_industries:
        if any(_fuzzy_match(llm_tag, kb) for kb in kb_industries):
            matched.append(llm_tag)
    if matched:
        return matched
    return list(kb_industries)


def industry_matches_any(name: str, candidates: list[str]) -> bool:
    """行业名 *name* 是否与 *candidates* 中任一项模糊匹配 (跨粒度容错).

    用于「行业榜细分板块名 ↔ 知识库粗粒度行业名」的关联: 例如榜单行业
    「锂电池」/「电池化学品」可命中新闻标签「电池」, 「半导体材料」可命中
    「半导体」。空名或空候选返回 False。
    """
    if not name:
        return False
    return any(_fuzzy_match(name, c) for c in (candidates or []))


def _fuzzy_match(a: str, b: str) -> bool:
    """双向包含, 或核心词 (去通用后缀, ≥2字) 双向包含."""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    suffix = "车机器材业链备件务网体化概念"
    core_a = a.rstrip(suffix)
    core_b = b.rstrip(suffix)
    if len(core_a) >= 2 and len(core_b) >= 2:
        return core_a in b or core_b in a
    return False
