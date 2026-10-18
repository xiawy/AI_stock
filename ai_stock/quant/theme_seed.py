"""L3 细分赛道种子数据（首批 23 个）.

设计依据见 ``docs/THEME_TAXONOMY_DESIGN.md``。这里只声明"应该是什么"，
是否真的成立由 ``theme_registry.validate_theme()`` 用真实行情跑 6 道校验裁决 ——
不达标的会被标成 ``rejected`` 并写明原因，不做人工豁免。

2026-10-18 二次校准（首轮 24 个体检出 4 个不达标后的重划）:
- 储能（大储）      → 储能（PCS/系统集成）   剔除阳光电源（单票 65%）
- 军工（红外）      → 军工（光电）           扩到光电成像链（原 6 只）
- 机器人（丝杠）+ 机器人（减速器） → 机器人（关节模组）  父子粒级合并
- 家电（白电）      → 家电（清洁电器）       白电三巨头 76% 退为 L1；清洁电器
  仍不达标（相关 0.22 / 离散 1.78），作为 rejected 留档
净结果 24 → 23 个种子，体检 22 active / 1 rejected。

字段：
- name        对外唯一口径（模板：赛道（限定词））
- origin      split 拆分 / compose 拼装 / emergent 数据源没有的题材
- thesis      一句话驱动逻辑（回答"为什么这些票是一组"）
- keywords    新闻匹配关键词（≥3 才合格）
- aliases     同义写法（海风→风电（出海））
- members     [(代码, 角色)]，角色 leader/core/peripheral
"""

from __future__ import annotations

DEFAULT_THEMES: list[dict] = [
    {
        "name": "风电（出海）",
        "parent_industry": "电力设备",
        "origin": "compose",
        "thesis": "海外海风订单 + 国内海风开工，受益塔筒/海缆/桩基等出海环节，避开整机价格战",
        "keywords": ["海上风电", "海风", "出海", "塔筒", "海缆", "桩基", "海外订单", "风电招标"],
        "aliases": ["海风", "海上风电", "风电出海", "风电出口"],
        "members": [
            ("002487", "leader"), ("603606", "leader"), ("002531", "core"),
            ("300129", "core"), ("300443", "core"), ("603218", "core"),
            ("600522", "core"), ("605305", "core"), ("300772", "peripheral"),
            ("002202", "peripheral"), ("601615", "peripheral"),
        ],
    },
    {
        "name": "储能（PCS/系统集成）",
        "parent_industry": "电力设备",
        "origin": "split",
        "thesis": (
            "电网侧/电源侧独立储能招标放量，聚焦变流器（PCS）与系统集成环节；"
            "剔除光储一体的阳光电源——其流通市值占原主题 65%，会把主题退化成单票"
        ),
        "keywords": [
            "储能变流器", "PCS", "独立储能", "电网侧储能", "大储",
            "储能招标", "共享储能", "容量电价",
        ],
        "aliases": ["大储", "独立储能", "电网侧储能", "共享储能", "储能PCS"],
        "members": [
            ("688248", "leader"), ("601126", "leader"), ("688676", "core"),
            ("002335", "core"), ("002518", "core"), ("300827", "core"),
            ("300693", "core"), ("002169", "peripheral"), ("300376", "peripheral"),
            ("002121", "peripheral"), ("002733", "peripheral"),
        ],
    },
    {
        "name": "光伏（BC电池）",
        "parent_industry": "电力设备",
        "origin": "split",
        "thesis": "背接触电池技术路线扩产，受益主链龙头与配套银浆/激光设备",
        "keywords": ["BC电池", "背接触", "HPBC", "BC组件", "TOPCon替代"],
        "aliases": ["BC", "BC电池", "背接触", "HPBC"],
        "members": [
            ("601012", "leader"), ("600732", "leader"), ("300776", "core"),
            ("688516", "core"), ("688599", "core"), ("002459", "core"),
            ("300118", "peripheral"), ("688503", "peripheral"), ("300842", "peripheral"),
        ],
    },
    {
        "name": "电网（特高压）",
        "parent_industry": "电力设备",
        "origin": "compose",
        "thesis": "特高压直流线路核准与开工节奏，受益换流阀/开关/GIS 与主设备",
        "keywords": ["特高压", "直流输电", "换流阀", "线路核准", "电网投资", "GIS"],
        "aliases": ["特高压", "直流输电", "换流阀", "电网投资"],
        "members": [
            ("600406", "leader"), ("000400", "leader"), ("600312", "core"),
            ("601179", "core"), ("002028", "core"), ("601126", "core"),
            ("600089", "core"), ("002879", "peripheral"), ("300062", "peripheral"),
        ],
    },
    {
        "name": "半导体（先进封装）",
        "parent_industry": "电子",
        "origin": "split",
        "thesis": "先进制程受限后以封装提升算力密度，CoWoS/HBM 扩产直接受益封测与设备",
        "keywords": ["先进封装", "CoWoS", "HBM", "2.5D", "3D封装", "封测", "Chiplet"],
        "aliases": ["先进封装", "封测", "CoWoS", "Chiplet", "HBM封装"],
        "members": [
            ("600584", "leader"), ("002156", "leader"), ("002185", "core"),
            ("688362", "core"), ("603005", "core"), ("000021", "core"),
            ("002436", "core"), ("603283", "peripheral"), ("688072", "peripheral"),
        ],
    },
    {
        "name": "半导体（存储涨价）",
        "parent_industry": "电子",
        "origin": "compose",
        "thesis": "存储原厂减产/涨价周期，受益利基存储设计与模组，弹性大于设备",
        "keywords": ["存储涨价", "DRAM", "NAND", "内存涨价", "存储模组", "利基存储", "涨价函"],
        "aliases": ["存储涨价", "内存涨价", "存储芯片", "DRAM", "NAND"],
        "members": [
            ("603986", "leader"), ("301308", "leader"), ("300223", "core"),
            ("688110", "core"), ("688766", "core"), ("688525", "core"),
            ("001309", "core"), ("300475", "peripheral"), ("688008", "peripheral"),
        ],
    },
    {
        "name": "半导体（设备零部件）",
        "parent_industry": "电子",
        "origin": "split",
        "thesis": "晶圆厂扩产 + 零部件国产化，受益刻蚀/薄膜/清洗/测试设备与核心零部件",
        "keywords": ["半导体设备", "刻蚀", "薄膜沉积", "清洗设备", "零部件国产化", "晶圆厂扩产"],
        "aliases": ["半导体设备", "设备零部件", "刻蚀机", "薄膜沉积", "国产设备"],
        "members": [
            ("002371", "leader"), ("688012", "leader"), ("688072", "core"),
            ("688120", "core"), ("300604", "core"), ("688037", "core"),
            ("300316", "core"), ("688233", "peripheral"), ("688135", "peripheral"),
        ],
    },
    {
        "name": "算力（租赁/IDC）",
        "parent_industry": "通信",
        "origin": "emergent",
        "thesis": "智算中心建设与算力租赁上架率提升，受益 IDC 与服务器运营方",
        "keywords": ["算力租赁", "智算中心", "IDC", "上架率", "算力集群", "服务器采购"],
        "aliases": ["算力租赁", "智算中心", "IDC", "算力"],
        "members": [
            ("300442", "leader"), ("300383", "core"), ("300738", "core"),
            ("603881", "core"), ("600602", "core"), ("000938", "core"),
            ("000977", "peripheral"), ("603019", "peripheral"),
        ],
    },
    {
        "name": "通信（光模块800G）",
        "parent_industry": "通信",
        "origin": "split",
        "thesis": "海外 AI 集群互联升级到 800G/1.6T，受益光模块与光器件",
        "keywords": ["800G", "1.6T", "光模块", "CPO", "光器件", "硅光"],
        "aliases": ["光模块", "800G", "CPO", "硅光", "光器件"],
        "members": [
            ("300308", "leader"), ("300502", "leader"), ("300394", "core"),
            ("002281", "core"), ("688498", "core"), ("300913", "core"),
            ("601869", "peripheral"), ("300570", "peripheral"),
        ],
    },
    {
        "name": "军工（光电）",
        "parent_industry": "国防军工",
        "origin": "split",
        "thesis": (
            "军用光电/红外热成像与成像器件国产化；原「军工（红外）」纯红外只有 6 只、"
            "睿创单票 52%，扩到光电成像链（探测器+光学+CMOS）才构成可分散篮子"
        ),
        "keywords": [
            "红外", "热成像", "光电", "图像传感器", "CMOS",
            "军贸", "制冷型探测器", "非制冷", "光电吊舱",
        ],
        "aliases": ["红外", "热成像", "军用光电", "红外探测器", "光电吊舱"],
        "members": [
            ("688002", "leader"), ("002414", "leader"), ("688213", "core"),
            ("002214", "core"), ("600184", "core"), ("300516", "core"),
            ("002338", "core"), ("300790", "peripheral"), ("600071", "peripheral"),
            ("688272", "peripheral"), ("688511", "peripheral"),
        ],
    },
    {
        "name": "军工（航发零部件）",
        "parent_industry": "国防军工",
        "origin": "compose",
        "thesis": "两机专项与发动机备件消耗，受益控制系统、叶片、高温合金与钛材（不含整机厂）",
        "keywords": ["航空发动机", "两机专项", "叶片", "高温合金", "发动机备件", "航发"],
        "aliases": ["航发", "航空发动机", "两机专项", "发动机叶片"],
        "members": [
            ("000738", "leader"), ("300855", "core"), ("300034", "core"),
            ("603308", "core"), ("605123", "core"), ("600391", "core"),
            ("600456", "peripheral"), ("002985", "peripheral"), ("688122", "peripheral"),
        ],
    },
    {
        "name": "军工（导弹链）",
        "parent_industry": "国防军工",
        "origin": "split",
        "thesis": "导弹消耗属性强，受益总装、制导电子与结构件",
        "keywords": ["导弹", "精确制导", "总装", "弹药", "制导电子"],
        "aliases": ["导弹", "精确制导", "弹药", "制导"],
        "members": [
            ("300395", "leader"), ("002025", "leader"), ("300593", "core"),
            ("688776", "core"), ("300696", "core"), ("002389", "core"),
            ("688239", "core"), ("600372", "peripheral"),
        ],
    },
    {
        "name": "机器人（关节模组）",
        "parent_industry": "机械设备",
        "origin": "compose",
        "thesis": (
            "人形机器人旋转+直线关节的零部件合集（谐波/RV 减速器、行星滚柱丝杠、"
            "无框电机、轴承），驱动同源于量产预期；原「丝杠」「减速器」是两个高度"
            "重叠的父子粒级，合并后才达到 8 只以上下限"
        ),
        "keywords": [
            "人形机器人", "关节模组", "谐波减速器", "行星滚柱丝杠",
            "无框电机", "空心杯电机", "线性关节", "灵巧手", "丝杠",
        ],
        "aliases": [
            "丝杠", "减速器", "关节模组", "谐波减速器", "滚柱丝杠",
            "人形机器人零部件",
        ],
        "members": [
            ("688017", "leader"), ("002472", "leader"), ("603728", "core"),
            ("300953", "core"), ("603667", "core"), ("002249", "core"),
            ("300718", "core"), ("002896", "core"), ("000837", "core"),
            ("688697", "core"), ("300580", "peripheral"), ("688577", "peripheral"),
            ("300809", "peripheral"), ("002520", "peripheral"), ("603178", "peripheral"),
        ],
    },
    {
        "name": "汽车（智驾域控）",
        "parent_industry": "汽车",
        "origin": "compose",
        "thesis": "城市 NOA 落地带动域控制器、线控制动与感知硬件放量",
        "keywords": ["域控制器", "城市NOA", "智能驾驶", "线控制动", "智驾方案"],
        "aliases": ["智驾", "域控制器", "城市NOA", "智能驾驶"],
        "members": [
            ("002920", "leader"), ("688326", "leader"), ("603786", "core"),
            ("603596", "core"), ("002906", "core"), ("600699", "core"),
            ("601799", "peripheral"), ("002965", "peripheral"),
        ],
    },
    {
        "name": "汽车（一体化压铸）",
        "parent_industry": "汽车",
        "origin": "split",
        "thesis": "大型压铸机替代传统冲焊，受益压铸厂、免热处理合金与模具",
        "keywords": ["一体化压铸", "免热处理合金", "压铸机", "大型压铸", "车身一体化"],
        "aliases": ["一体化压铸", "压铸", "免热处理合金"],
        "members": [
            ("603348", "leader"), ("002101", "leader"), ("300428", "core"),
            ("600933", "core"), ("603305", "core"), ("603982", "core"),
            ("002997", "peripheral"), ("603917", "peripheral"),
        ],
    },
    {
        "name": "医药（GLP-1）",
        "parent_industry": "医药生物",
        "origin": "emergent",
        "thesis": "减重适应症放量，受益多肽原料药、CDMO 与国内获批品种",
        "keywords": ["GLP-1", "司美格鲁肽", "减重", "多肽", "替尔泊肽", "减肥药"],
        "aliases": ["GLP-1", "司美格鲁肽", "减重", "减肥药", "多肽"],
        "members": [
            ("000963", "leader"), ("688166", "core"), ("688117", "core"),
            ("300199", "core"), ("300255", "core"), ("002332", "core"),
            ("002317", "peripheral"), ("002038", "peripheral"),
        ],
    },
    {
        "name": "医药（创新药出海）",
        "parent_industry": "医药生物",
        "origin": "compose",
        "thesis": "License-out 与海外临床进展驱动估值重估，与国内医保谈判逻辑分离",
        "keywords": ["License-out", "出海", "FDA", "海外授权", "BD交易", "临床三期"],
        "aliases": ["创新药出海", "License-out", "BD交易", "海外授权"],
        "members": [
            ("600276", "leader"), ("688235", "leader"), ("002422", "core"),
            ("688180", "core"), ("688266", "core"), ("688278", "core"),
            ("300759", "peripheral"), ("603259", "peripheral"),
        ],
    },
    {
        "name": "有色（钕铁硼磁材）",
        "parent_industry": "有色金属",
        "origin": "split",
        "thesis": "钕铁硼需求（机器人/风电/新能源车）与稀土价格共振，聚焦磁材加工而非上游采矿",
        "keywords": ["稀土配额", "钕铁硼", "稀土涨价", "磁材", "出口管制"],
        "aliases": ["稀土", "钕铁硼", "磁材", "稀土永磁"],
        "members": [
            ("300748", "leader"), ("300224", "core"), ("600366", "core"),
            ("000970", "core"), ("002056", "core"), ("688077", "core"),
            ("000795", "peripheral"), ("300127", "peripheral"),
        ],
    },
    {
        "name": "化工（氟化工）",
        "parent_industry": "基础化工",
        "origin": "split",
        "thesis": "三代制冷剂配额锁定供给，萤石资源与含氟新材料同链受益",
        "keywords": ["制冷剂", "三代制冷剂", "配额", "R32", "R134a", "涨价"],
        "aliases": ["制冷剂", "氟制冷剂", "R32", "配额制", "萤石"],
        "members": [
            ("600160", "leader"), ("603379", "leader"), ("605020", "core"),
            ("002326", "core"), ("002407", "core"), ("600378", "core"),
            ("603505", "peripheral"), ("002915", "peripheral"),
        ],
    },
    {
        "name": "家电（清洁电器）",
        "parent_industry": "家用电器",
        "origin": "split",
        "thesis": (
            "扫地机/洗地机/吸尘器整机 + 代工供应链，驱动是海外渠道扩张与新品周期，"
            "与白电的以旧换新、厨电的竣工逻辑完全不同；"
            "剔除美的/海尔/格力三大白电（合计 76%）与厨电，需求侧家电补贴落 L1 行业、不进主题榜"
        ),
        "keywords": [
            "扫地机", "清洁电器", "洗地机", "吸尘器", "家电出口",
            "跨境电商", "海外收入", "代工",
        ],
        "aliases": ["清洁电器", "扫地机", "洗地机", "吸尘器", "家电出海"],
        "members": [
            ("603486", "leader"), ("688169", "leader"), ("689009", "core"),
            ("603355", "core"), ("002705", "core"), ("603219", "core"),
            ("603657", "peripheral"), ("605555", "peripheral"),
        ],
    },
    {
        "name": "农业（种业转基因）",
        "parent_industry": "农林牧渔",
        "origin": "split",
        "thesis": "转基因品种审定与商业化推广，受益性状公司与传统种业龙头",
        "keywords": ["转基因", "品种审定", "种业振兴", "生物育种", "商业化推广"],
        "aliases": ["转基因", "种业", "生物育种", "品种审定"],
        "members": [
            ("002385", "leader"), ("000998", "leader"), ("002041", "core"),
            ("600371", "core"), ("000713", "core"), ("300189", "core"),
            ("601952", "peripheral"), ("600598", "peripheral"),
        ],
    },
    {
        "name": "建筑（水利投资）",
        "parent_industry": "建筑装饰",
        "origin": "compose",
        "thesis": "水利专项债与万亿国债投向，受益水利施工与地方基建龙头",
        "keywords": ["水利", "万亿国债", "重大项目", "水利投资", "防洪工程"],
        "aliases": ["水利", "水利投资", "万亿国债", "防洪"],
        "members": [
            ("601669", "leader"), ("002060", "leader"), ("600502", "core"),
            ("601186", "core"), ("601668", "core"), ("600170", "core"),
            ("601800", "peripheral"), ("002307", "peripheral"),
        ],
    },
    {
        "name": "传媒（AI应用）",
        "parent_industry": "传媒",
        "origin": "emergent",
        "thesis": "AI 应用商业化落地（内容生产/营销/游戏），弹性看付费与 DAU 数据",
        "keywords": ["AI应用", "AI Agent", "多模态", "AIGC", "AI营销", "AI游戏"],
        "aliases": ["AI应用", "AIGC", "AI Agent", "多模态"],
        "members": [
            ("300418", "leader"), ("300459", "core"), ("300364", "core"),
            ("300785", "core"), ("002292", "core"), ("300251", "core"),
            ("600633", "peripheral"), ("002517", "peripheral"),
        ],
    },
]
