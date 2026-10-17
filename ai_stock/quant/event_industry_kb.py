"""事件-行业映射知识库 (选股优化点 2: 降低 LLM 行业映射幻觉).

本地关键词 → 利好行业映射表, 用于宏观事件解析的预检:
- ``match_industries(text, board_names=None)``: 静态 KB 关键词子串匹配, 叠加
  "新闻文本直接命中实时板块名"的动态层, 返回命中行业名列表 (保序去重)
- 内置规则覆盖常见主题; 支持通过 ``QUANT_EVENT_KB_FILE`` 环境变量指向
  额外 JSON 文件扩展 (格式: [{"keywords": [...], "industries": [...]}]),
  便于跟进最新政策/题材而无需改代码.
- 新行业兜底: 板块全集本身实时抓取 (非写死); 传入 ``board_names`` 后, 东财新
  上线的题材板只要被新闻提及即自动命中, 无需为其新增 KB 关键词 (动态覆盖层)。

设计取舍:
- 只做粗筛与交叉验证, 不做最终决策 — LLM 仍负责事件提取与影响力排序
- 知识库无命中时不拦截 LLM 结果 (兜底), 有命中时以「交集优先」收敛
  LLM 输出, 压缩凭空捏造的行业关联
- industries 输出对齐东财真实板块名 (行业板 + 概念板), 并按主题精度分层:
  泛 AI 应用/大模型 → 人工智能/算力概念/AIGC概念 等概念板; 芯片/晶圆/设备 →
  半导体 等板。避免粗标签「半导体」被子串匹配炸开到「半导体设备/半导体材料」
  等无关细分板 (泛 AI/数字消费新闻不应驱动半导体设备)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from functools import lru_cache

logger = logging.getLogger(__name__)

# 扩展知识库文件路径 (可选): [{"keywords": [...], "industries": [...]}]
_KB_FILE_ENV = "QUANT_EVENT_KB_FILE"

# --- 动态板块名匹配 (新行业兜底: 不依赖 KB 关键词, 直接命中实时板块名) --------
# 参与"新闻文本直接命中板块名"的最小板块名长度: 过滤"电子/通信/军工"等 2 字
# 歧义名 (易被普通词误命中); 新题材板名几乎都 ≥3 字 (谷子经济/脑机接口/算力概念)。
_DYNAMIC_BOARD_MIN_LEN = 3

# 排除模式: 板块名含以下任一子串即视为"非行业"技术/指数/资金/风格/交易事件
# 标签板, 不参与动态命中 (这些词高频出现在行情快讯, 参与匹配会引入噪声)。刻意
# 不含"国资/央企/龙头/参股"等宽泛词, 避免误杀"国资云概念/央国企改革"等真实题材板。
_DYNAMIC_BOARD_EXCLUDES: tuple[str, ...] = (
    # 交易状态 / 技术形态
    "昨日", "涨停", "跌停", "连板", "首板", "炸板", "触板", "多板", "打板",
    "新高", "历史", "近期", "振幅", "换手", "超跌", "反转股", "摘帽",
    # 指数 / 资金 / 互联互通
    "指数", "股通", "融资融券", "融券", "重仓", "标的", "MSCI", "富时",
    "罗素", "道琼斯", "标普", "GDR", "做市",
    # 业绩 / 股本 / 交易事件
    "预增", "预减", "预盈", "预亏", "扭亏", "业绩", "次新", "新股", "转债",
    "回购", "增持", "减持", "解禁", "质押", "送转", "分红", "配股", "举牌",
    "股权激励", "破净", "破发", "ST", "退市", "风险警示",
    # 市场分类 / 风格标签
    "北交所", "科创板", "创业板", "注册制", "微盘", "小盘", "大盘", "白马",
    "蓝筹", "风格", "题材股", "金股", "红利股", "价值股", "成长股", "独角兽",
    "行业龙头", "宁组合", "股权集中", "股权分散", "B股", "AH股", "AB股",
)

# 内置映射规则: 关键词 → 直接利好行业 (行业名与东财板块名尽量对齐)
_BUILTIN_RULES: list[dict] = [
    # AI 应用/大模型/智能体 → 对齐东财 AI 概念板块; 不再粗映射到「半导体」,
    # 避免泛 AI 新闻经子串匹配污染「半导体设备/半导体材料」细分行业板
    {"keywords": ["人工智能", "AI", "智能体", "AIGC", "ChatGPT", "大模型",
                  "多模态", "生成式"],
     "industries": ["人工智能", "AI应用", "AIGC概念", "算力概念"]},
    # 算力/数据中心等 AI 基础设施 → 算力概念板 + 通信设备 (硬件侧, 仍不碰半导体设备)
    {"keywords": ["算力", "智算", "超算", "数据中心", "东数西算", "液冷"],
     "industries": ["算力概念", "数据中心", "东数西算", "通信设备"]},
    # 光模块/CPO/光通信 → 精确概念板块
    {"keywords": ["光模块", "CPO", "光通信", "硅光"],
     "industries": ["CPO概念", "光学光电子", "通信设备"]},
    # 存储芯片 → 精确概念板 (用「半导体概念」控制粒度, 不炸开到半导体设备)
    {"keywords": ["存储", "DRAM", "NAND", "HBM"],
     "industries": ["存储芯片", "半导体概念", "元件"]},
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
    {"keywords": ["数字经济", "数据要素", "信创", "国产软件"],
     "industries": ["信创", "国产软件", "软件开发", "数据要素", "数字经济"]},
    # 芯片/半导体硬件 (真·半导体新闻) → 半导体行业板 + 精确细分板; 保留粗标签
    # 「半导体」供跨粒度匹配 (芯片/晶圆/设备新闻确与半导体设备/材料相关, 与泛 AI 区分)
    {"keywords": ["芯片", "半导体", "晶圆", "光刻机", "光刻胶", "集成电路",
                  "封测", "刻蚀", "先进封装", "国产替代", "华为昇腾", "昇腾"],
     "industries": ["半导体", "国产芯片", "半导体概念", "集成电路制造", "先进封装"]},
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


def _is_dynamic_matchable(name: str) -> bool:
    """板块名是否可用于"新闻文本直接命中"的动态匹配:
    长度 ≥ ``_DYNAMIC_BOARD_MIN_LEN`` 且不含 ``_DYNAMIC_BOARD_EXCLUDES`` 噪声模式."""
    if not name or len(name) < _DYNAMIC_BOARD_MIN_LEN:
        return False
    return not any(x in name for x in _DYNAMIC_BOARD_EXCLUDES)


def match_board_names(text: str, board_names: Iterable[str]) -> list[str]:
    """在 *text* 中直接查找出现的实时板块名 (动态关键词覆盖层, 无需维护 KB).

    新题材板块一经东财上线, 只要新闻正文提及其板块名即自动命中, 解决内置 KB
    关键词写死、跟不上新行业的问题。仅保留可匹配板块名 (见
    ``_is_dynamic_matchable``), 保序去重; *board_names* 为空时返回空列表。
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in board_names or ():
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        if _is_dynamic_matchable(name) and name in text:
            seen.add(name)
            out.append(name)
    return out


def match_industries(
    text: str, board_names: Iterable[str] | None = None,
) -> list[str]:
    """命中行业列表 (保序去重); 空文本返回空. 两层来源并集:

    - 静态 KB (``_BUILTIN_RULES`` + 可选外部 JSON): 关键词 → 行业, 精度层;
    - 动态板块名 (``board_names`` 提供时): 新闻文本直接命中的实时板块名,
      覆盖层 — 东财新上线题材板无需改 KB 即可被关联 (新行业兜底)。
    """
    if not text:
        return []
    out: list[str] = []
    for rule in _load_rules():
        if any(kw in text for kw in rule["keywords"]):
            for ind in rule["industries"]:
                if ind not in out:
                    out.append(ind)
    if board_names:
        for name in match_board_names(text, board_names):
            if name not in out:
                out.append(name)
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
