"""归因链组装与保存（spec P1a-3：大盘-板块-事件 链树的 agent 侧产物）。"""
import re
from urllib.parse import urlparse

import structlog

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation
from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()

# 溯源未确认驱动原因时的 trace_summary 回退文案（区别于"溯源完成"占位——溯源
# insufficient 或无法从 stages 提取 trigger 结论时，如实说明原因未确认）。
_FALLBACK_TRACE_SUMMARY = "溯源未确认驱动原因"

# 「否定句摘要」标记词（2026-09-18 迭代 4）：溯源阶段如实说明"没找到原因"的句式。
# 这类句子的存在前提是**事件层也为空**——`children[].events` 非空却写着"未检索到触发事件"
# 就是结论与证据相反（生产实证：同一板块卡片上两句话并存）。
#
# 刻意只用**无歧义的否定词**：肯定归因句里的"不足/没有/缺少/缺乏"（如"供给不足推动多晶硅
# 价格上涨"）一旦入表就会被误判成否定句、把真有归因的摘要错误让位给事件标题，故不收。
_NEGATIVE_SUMMARY_MARKERS = (
    "未检索到", "未找到", "未发现", "未确认", "未明确", "未识别", "未匹配",
    "没有检索到", "没有找到", "没有发现",
    "无法确认", "无法判断", "不能确认", "暂无", "尚未",
)

# 弱依据日（无主链：板块提取走候选链/快照兜底）链根摘要回退文案：报告中
# attribution_summary 空缺时用中性表述，不编造主因（Task 9.1）。
_WEAK_ATTRIBUTION_SUMMARY = "证据不足，未确认主因"

# 大盘涨跌幅旧候选键：生产快照已不产出（真实形状是 a_share.indexes），
# 仅保留读取以兼容历史报告/旧 fixture。
_LEGACY_INDEX_PCT_KEYS = (
    "index_change_pct",
    "index_pct",
    "benchmark_change_pct",
    "sh_change_pct",
)

# 上证指数识别：code 取 000001（裸码）/ 000001.SH / SH000001 三种写法
_SHANGHAI_INDEX_CODES = frozenset({"000001", "000001.SH", "SH000001"})


def _numeric_pct(value: object) -> float | None:
    """仅接受真实数值（bool/字符串等视为缺失，不伪造 0）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _index_items(indexes: object) -> list[dict[str, object]]:
    """兼容 a_share.indexes 两种形状，返回指数项列表。

    真实快照形状：normalize_a_share（market_trace_snapshot.py）把 Node 的 list
    归一化为 dict（key=SH000001 → 指数项）；list 形状为归一化前的原始载荷，
    两种都支持，避免下游按形状踩空。
    """
    if isinstance(indexes, dict):
        return [item for item in indexes.values() if isinstance(item, dict)]
    if isinstance(indexes, list):
        return [item for item in indexes if isinstance(item, dict)]
    return []


def _is_shanghai_index(item: dict[str, object]) -> bool:
    name = item.get("name")
    if isinstance(name, str) and "上证" in name:
        return True
    for key in ("ts_code", "code"):
        code = item.get(key)
        if isinstance(code, str) and code.upper() in _SHANGHAI_INDEX_CODES:
            return True
    return False


def index_pct_from_snapshot(snapshot: dict[str, object]) -> float | None:
    """从市场溯源快照解析大盘（上证）涨跌幅，缺失返回 None。

    真实快照形状为 ``a_share["indexes"]``：优先取上证指数项，找不到则取首项；
    值非数值视为缺失，再回退旧候选键（见 _LEGACY_INDEX_PCT_KEYS）。全部缺失
    返回 None——保持"未知"，不伪造 0（0 会让 relation 误判为 market_follow）。
    归因链（本模块）与板块溯源父链引用（event_consumers）共用，避免两处漂移。
    """
    if not isinstance(snapshot, dict):
        return None
    a_share = snapshot.get("a_share")
    if not isinstance(a_share, dict):
        return None
    items = _index_items(a_share.get("indexes"))
    chosen = next((item for item in items if _is_shanghai_index(item)), None)
    if chosen is None and items:
        chosen = items[0]
    if chosen is not None:
        for key in ("change_pct", "pct_chg"):  # pct_chg：Node 原始字段（归一化前）
            pct = _numeric_pct(chosen.get(key))
            if pct is not None:
                return pct
    for legacy_key in _LEGACY_INDEX_PCT_KEYS:
        pct = _numeric_pct(a_share.get(legacy_key))
        if pct is not None:
            return pct
    return None


def _is_negative_summary(text: str) -> bool:
    """摘要是否为「未找到原因」的否定句（`_NEGATIVE_SUMMARY_MARKERS`，纯子串判定）。"""
    return any(marker in text for marker in _NEGATIVE_SUMMARY_MARKERS)


def _first_event_headline(events: object) -> str:
    """事件层首条可用 headline（空/空白/非 dict/非字符串一律跳过），取不到返回 ``""``。"""
    if not isinstance(events, list):
        return ""
    for node in events:
        headline = node.get("headline") if isinstance(node, dict) else None
        if isinstance(headline, str) and headline.strip():
            return headline.strip()
    return ""


def _trace_summary_from_report(trace_result: dict[str, object]) -> str:
    """`_trace_summary` 的报告侧取源（1→4 优先级），不含事件层裁决。"""
    if not isinstance(trace_result, dict):
        return _FALLBACK_TRACE_SUMMARY
    summary = trace_result.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    stages = trace_result.get("stages")
    if not isinstance(stages, list):
        return _FALLBACK_TRACE_SUMMARY
    trigger = next(
        (s for s in stages if isinstance(s, dict) and s.get("kind") == "trigger"),
        None,
    )
    if not isinstance(trigger, dict):
        return _FALLBACK_TRACE_SUMMARY
    headline = trigger.get("headline")
    if isinstance(headline, str) and headline.strip():
        return headline.strip()
    claims = trigger.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, str) and claim.strip():
                return claim.strip()
    return _FALLBACK_TRACE_SUMMARY


def _trace_summary(trace_result: dict[str, object], *, events: object = ()) -> str:
    """链 children[].trace_summary：优先取该板块 sector_trace 报告的非空摘要。

    取源优先级（**报告有内容就不得被兜底覆盖**）：

    1. 报告顶层 `summary`（写入侧若提供非空字符串，直接采用）；
    2. trigger 阶段 headline（事件主因句；**不看 `attribution_status`**）；
    3. trigger 阶段首个非空 claim（报告无标题时的同源兜底）；
    4. `_FALLBACK_TRACE_SUMMARY`（报告确实无内容时才出现的中性兜底）。

    为什么去掉"attribution_status == insufficient → 直接兜底"：生产实证
    （2026-09-17 玉米）同一板块在 `GET /api/agent/sector-insight/:date` 的
    `trace.summary`（app-api `extractTraceSummary` 取 trigger headline，从不看该字段）
    是有内容的归因句，链却是"溯源未确认驱动原因"——前端两页（市场洞见按链摘要判
    "有无归因"、板块四环按 sector-insight 主因）口径必须一致，故摘要只按"有没有内容"
    决定是否兜底。

    刻意**不**回退 phenomenon/首 stage 的 headline：那是现象描述（"今日大幅波动"），
    拿它当驱动原因正是本次要修的问题；无 trigger 即无归因，如实走中性兜底。

    **迭代 4（2026-09-18）：结论不得与证据相反**——`events` 非空（事件层确有驱动事件）
    且上面取到的摘要是否定句（`_is_negative_summary`）时，摘要让位给事件首条 headline。
    依据：`trace_summary` 语义是"该板块的驱动原因"，而 `events` 是它的支撑证据；链上已经
    有事件节点却写着"未检索到触发事件"，同一个板块卡片上两句话自相矛盾（生产实证）。
    事件首条 headline 也取不到时（节点无标题）让不了位，如实保留否定句。
    """
    summary = _trace_summary_from_report(trace_result)
    if _is_negative_summary(summary):
        headline = _first_event_headline(events)
        if headline:
            return headline
    return summary


def _pct_from(snapshot: dict[str, object]) -> float | None:
    sector = snapshot.get("sector") if isinstance(snapshot, dict) else None
    if not isinstance(sector, dict):
        return None
    v = sector.get("pct_change")
    if v is None:
        # 兼容 wind-leaders 快照行（无 pct_change，只有 today_change 字段）
        v = sector.get("today_change")
    return float(v) if isinstance(v, int | float) else None


# --- R14：链板块标识增强（ts_code + 归一化权威名），消除前端按名匹配不上角色徽 ---

# 归一化口径**逐字对齐** app-api `ThsBoardService.normName` 与 app-frontend
# `utils/sectorInsight.normalizeSectorName`（去空白/括号 → 剥「（A股）/概念/板块/行业/产业链」
# 后缀 → 小写）。为什么必须同口径：前端用归一化名把链 child 桥到 THS 权威候选名，两侧
# 任一处口径漂移即回到"有链但角色徽不显示"（R14）。先删空白/括号再剥后缀的顺序也与前端一致
# （故「（A股）」两条分支在前端同样不可达，保留是为逐字对齐、便于比对）。
_SECTOR_STD_SPACE_RE = re.compile(r"[\s（）()]")
_SECTOR_STD_SUFFIX_RE = re.compile(r"（A股）|\(A股\)|概念$|板块$|行业$|产业链$")


def normalize_sector_std(name: object) -> str:
    """板块名归一化（R14）：非字符串/空归一化结果返回空串（调用方据此省略键）。"""
    if not isinstance(name, str):
        return ""
    return _SECTOR_STD_SUFFIX_RE.sub(
        "", _SECTOR_STD_SPACE_RE.sub("", name)
    ).lower()


def _sector_meta(sector: str, sector_row: object) -> dict[str, str]:
    """child 的板块标识增强字段（R14）：`ts_code` + `sector_std`（归一化权威名）。

    取不到即**省略键**（与仓库"无匹配省略键"惯例一致，不写 null）：
    - `ts_code`：仅取快照行（`extract_primary_sectors` 命中行，含 app-api SectorFact 的
      `ts_code`）的非空字符串，缺失/非字符串一律省略（不编造）；
    - `sector_std`：优先快照行 `name`（THS 权威榜名），行缺失/name 不可用时回退复盘原始
      `sector`（归一化仍是有效的桥接键，只是权威性较弱）；归一化结果为空则省略。
    """
    row = sector_row if isinstance(sector_row, dict) else {}
    meta: dict[str, str] = {}
    ts_code = row.get("ts_code")
    if isinstance(ts_code, str) and ts_code.strip():
        meta["ts_code"] = ts_code.strip()
    row_name = row.get("name")
    source_name = (
        row_name if isinstance(row_name, str) and row_name.strip() else sector
    )
    std = normalize_sector_std(source_name)
    if std:
        meta["sector_std"] = std
    return meta


# --- 链事件层（spec §3.2-4：中台优先 → 检索补漏，去重 + 上限，禁编造） ---

# 每板块事件节点上限（取最相关；超出丢弃并留痕）
MAX_CHAIN_EVENTS_PER_SECTOR = 3

_EVENT_SOURCE_WAREHOUSE = "warehouse"
_EVENT_SOURCE_SEARCH = "search"

# 板块定向检索来源的 kind 前缀（sector_trace_snapshot._normalize_source 产出
# kind=f"sector_event:{query}"）：链事件层的检索补漏**只消费定向检索产物**，
# 不另起检索（溯源快照已强制跑过，见 sector_trace_snapshot.build_sector_snapshot）。
_SECTOR_SOURCE_PREFIX = "sector_event:"

# 板块名常见后缀（"券商板块" 同时按 "券商" 匹配）；不建别名表——无权威别名源，
# 猜测性别名会引入误召回。
_SECTOR_NAME_SUFFIXES = ("板块", "概念", "行业", "指数")

# --- 事件准入：只收「驱动原因」，拒收「行情综述/现象」（2026-09-18 组长口径） ---
# 生产实证（2026-09-17 CRO 概念）：检索补漏把「A股收評|滬指跌0.41% 三大指數收跌農業
# 板塊逆勢大漲」「今天A股，三大指数集体下跌 - 时间线- 搜狐」这类**行情综述**塞进事件
# 层——它们只复述"发生了什么"（现象），回答不了"为什么动"（原因）。口径：宁可漏判
# （少放），也不拿综述当原因；全部被拒即 events=[]（如实交空，不得回退成综述兜底）。
#
# 2026-09-18 修订（今日生产实证漏网两条，均 source=search 进链）：
# 「注册制次新股大涨八个点，A股市场全线拉升，沪指站上五日均线 - 网易」「全线上涨！
# A股这一板块，涨幅第一！ - 21财经」——旧规则只看 marker/百分号/两市/时段，"八个点"
# "全线上涨""涨幅第一""站上五日均线"这类**无百分号**写法全漏。故判据由「命中现象即拒」
# 改为「命中现象 **且** 原因词未命中 → 拒」：现象外壳但讲清原因（政策落地/价格上涨/
# 订单放量…）必须放行——它们是原因不是现象，误拒成本高于误留。
#
# 为什么做在**准入**而不是只写进 prompt：生成侧（LLM prompt）与判定侧（本护栏）双保险，
# 不依赖单次 LLM 输出的稳定性。
#
# 2026-09-18 修订 3（页面噪声，同一原因词豁免口径）：生产链 children[].events 漏网
# 「国家大基金持股 - 行情中心- 同花顺」（URL http://q.10jqka.com.cn/gn/detail/code/…）。
# 它是**行情页/UI 页面标题**——既无现象词也无原因词，按"判不出即放行"进了事件层，但页面
# 标题回答不了"为什么动"。两道网：标题级 `_PAGE_NOISE_TOKENS`（reason=page_noise）、
# URL 级 `is_page_noise_url`（reason=page_noise_url）。两网豁免口径与现象判据一致：headline
# 命中任一原因词即放行（「同花顺：某公司公告中标5亿元订单」必须留下——站点名本身不是噪声词，
# 2026-09-18 收窄）。
_SUMMARY_TITLE_MARKERS = (
    # 复盘/综述体裁词（简繁同列，英文综述标题同列）——体裁即综述，不看原因词
    "收评", "收盤", "收盘", "午评", "早评", "复盘", "盘点", "盘面",
    "三大指数", "三大指數", "涨跌家数", "漲跌家數", "时间线", "時間線",
    "资金流向", "資金流向", "涨停潮", "漲停潮", "异动", "異動",
    "closing bell", "market wrap", "market recap", "daily recap",
)
# 页面噪声词（准入第三判据）：行情页/数据中心/股吧/盘口等 **UI 页面标题**用语——页面标题
# 只说明"这是一页行情/资料"，不承载"为什么动"（2026-09-18 生产实证）。与现象判据**相互
# 独立**（现象看涨跌描述，页面噪声看页面形态），但共用"原因词未命中才拒"的豁免口径。
#
# 注意「公告列表」在现有 `_CAUSE_TOKENS` 含「公告」时**不可达**（必然被豁免），保留为对齐
# 建议口径；调参时可删。「资金流向表」同时命中 marker（reason=summary_marker）。
_PAGE_NOISE_TOKENS = (
    # 行情页/行情模块用语
    "行情中心", "行情页", "行情頁", "行情查询", "行情查詢", "行情报价", "行情報價",
    "行情走势", "行情走勢", "个股行情", "個股行情", "概念行情", "板块行情", "板塊行情",
    # 站内栏目/数据中心
    "资金流向表", "資金流向表", "数据中心", "資料中心", "资讯中心", "資訊中心",
    "研报中心", "研報中心", "公告列表",
    # 股吧/F10/盘口（页面而非报道）
    "f10", "股吧", "盘口", "盤口",
    # 栏目/首页形态（2026-09-18 收窄后的覆盖回归补偿）："股票频道- 东方财富网"这类**站点栏目名**
    # 不是事件标题——站点名已移出词表，改由"栏目形态词"识别，既拦住栏目名又不误拒
    # "同花顺：国家大基金三期成立"（含站点名但无栏目形态）。
    "频道", "頻道", "首页", "首頁", "栏目", "欄目", "导航", "導航",
    # 英文页面标题
    "quote page", "market center", "stock quote",
)
# 站点名（同花顺/东方财富）**刻意不入表**：它们只是**来源品牌**，不是页面形态——真原因标题
# 若只带站点名而不含原因词（「同花顺：国家大基金三期成立」）会被误判成页面噪声。生产实证那条
# 「国家大基金持股 - 行情中心- 同花顺」靠「行情中心」即可命中，删站点名不丢覆盖（2026-09-18 收窄）。
#
# 页面级 URL 特征（网 2）：**行情/数据站点域** 下的 **页面模块子域**（首段标签），或路径含页面
# 段——这类页面的正文是表格/讨论区，不承载原因。
#
# 2026-09-18 收窄：旧实现是「主机**任意子串**匹配」（`guba`/`f10`/`quote`），
# `f10.example.com`、`quotes.example.com`、`guba.example.com` 这类**非行情站**域名会被误判成
# 页面噪声 → 误拒其转载的真驱动。现改为**站点域后缀 + 页面模块首段标签**双条件；站点首页
# （`www.eastmoney.com`）与站内**报道页**（`finance.eastmoney.com/news/…`）不再命中。
_PAGE_NOISE_URL_SITE_DOMAINS = ("10jqka.com.cn", "eastmoney.com")
_PAGE_NOISE_URL_PAGE_LABELS = ("q", "data", "stockpage", "quote", "f10", "guba")
_PAGE_NOISE_URL_PATH_TOKENS = ("/detail/code/", "/quote/", "/f10/", "/guba/")
# 站点/栏目首页路径（2026-09-18 收窄后的覆盖回归补偿）：已知行情/数据站点域（含子域）下
# path 为空/`/`/`index.*` → 是"某频道的首页"，不是任何报道。生产实证：
# `https://stock.eastmoney.com/` → 标题 `股票频道- 东方财富网` 漏进事件层。
_PAGE_NOISE_URL_ROOT_PATHS = ("", "/", "/index.html", "/index.htm", "/index.shtml", "/index.php")
# 市场级词元（大盘/指数/两市/A 股整体）——与涨跌动作/涨跌幅式描述同现即行情复述
_MARKET_WIDE_TOKENS = (
    "沪指", "滬指", "上证指数", "上證指數", "深证成指", "深證成指", "创业板指",
    "創業板指", "科创50", "科創50", "沪深300", "滬深300", "大盘", "大盤", "两市",
    "兩市", "a股", "s&p 500", "nasdaq", "dow jones", "shanghai composite",
)
# 市场级涨跌动作词（无百分号写法："沪指站上五日均线""大盘走弱""创业板指跌破2000点"）
_MARKET_ACTION_TOKENS = (
    "站上", "失守", "跌破", "拉升", "上涨", "上漲", "下跌", "走强", "走強",
    "走弱", "回落", "低开", "低開", "高开", "高開",
)
# 板块级涨幅语（无数字、无百分号的行情复述："全线上涨""涨幅第一""领涨两市"）
_RALLY_PHRASES = (
    "全线上涨", "全線上漲", "全线拉升", "全線拉升", "集体上涨", "集體上漲",
    "集体拉升", "集體拉升", "普涨", "普漲", "涨幅第一", "漲幅第一",
    "涨幅居前", "漲幅居前", "涨幅榜", "漲幅榜", "领涨两市", "領漲兩市",
)
# "大涨八个点"/"涨了3个点"/"跌超2个点"：中文/阿拉伯数字＋"个点"（无百分号也能读出涨幅）
_POINT_MOVE_RE = re.compile(
    r"[涨漲跌](?:了|超|近|逾)?\s*[0-9零一二三四五六七八九十百两半]+\s*个点"
)
# 新高语：**限与板块/指数同现**才算行情复述（"板块创阶段新高"）；个股新高不在本链口径
_NEW_HIGH_PHRASES = (
    "创阶段新高", "創階段新高", "创年内新高", "創年內新高", "创新高", "創新高",
    "创出新高", "刷新新高",
)
_NEW_HIGH_QUALIFIERS = (
    "板块", "板塊", "指数", "指數", "大盘", "大盤", "概念", "行业", "行業",
    "两市", "兩市", "etf",
)
# 涨跌幅式描述："跌0.41%"/"涨超2%"/"下跌1.2%"
_PCT_RECAP_RE = re.compile(r"[涨漲跌][幅超逾]?\s*\d+(?:\.\d+)?\s*%")
# 成交额/家数综述词（"两市"＋其一即成交额/涨跌家数综述）
_TURNOVER_RECAP_TOKENS = ("成交", "亿元", "億元", "家数", "家數")
# 时段词＋涨跌动词 = 盘中盘面复述（"午后跌幅扩大"/"早盘跳水"）
_SESSION_TOKENS = ("午后", "午後", "早盘", "早盤", "盘中", "盤中", "尾盘", "尾盤", "开盘", "開盤")
_DIRECTION_TOKENS = ("涨", "漲", "跌", "跳水", "拉升")

# 原因词表（准入第二判据）：命中现象形态 **且** 这些词一个不命中才拒。"价格"/"供给"
# 为 2026-09-18 按需增补——"多晶硅价格上涨"是价格驱动原因，不加会被当纯现象误拒。
# 刻意与 `_DRIVING_KEYWORDS`（只用于检索候选排序）分开：准入与排序口径不同，合并会
# 牵动排序行为（准入只求"能读出原因"，排序还要看权重）。
_CAUSE_TOKENS = (
    # 政策/监管/部委
    "政策", "监管", "監管", "部委", "国常会", "國常會", "发改委", "發改委",
    "工信部", "证监会", "證監會", "央行", "国务院", "國務院", "牌照",
    # 公司/交易披露
    "公告", "披露", "预案", "預案", "中标", "中標", "订单", "訂單", "签约",
    "簽約", "招标", "招標", "获批", "獲批", "并购", "並購", "重组", "重組",
    "收购", "收購", "增持", "回购", "回購",
    # 供需/价格/产能
    "涨价", "漲價", "提价", "提價", "降价", "降價", "价格", "價格", "减产",
    "減產", "扩产", "擴產", "投产", "投產", "产能", "產能", "供需", "需求",
    "供给", "供給", "库存", "庫存",
    # 外贸/政策工具
    "出口", "进口", "進口", "关税", "關稅", "补贴", "補貼", "试点", "試點",
    "细则", "細則", "方案", "规划", "規劃", "标准", "標準",
    # 业绩/落地（弱原因词：宁可放行）
    "业绩", "業績", "财报", "財報", "落地",
)

# 驱动类正面特征（政策/监管/供需/价格/公司公告/行业事件/资金制度）：只用于检索候选
# **排序加权**，不做准入（真原因千变万化，白名单式准入门槛会漏掉真驱动——误拒成本高于
# 误留：留下来的仍要过"是不是综述"的准入判定）。
_DRIVING_KEYWORDS = (
    "政策", "监管", "部委", "国务院", "发改委", "工信部", "财政部",
    "证监会", "央行", "公告", "披露", "预案", "中标", "订单", "合同", "业绩",
    "盈利", "涨价", "提价", "降价", "供需", "供给", "需求", "库存", "减产",
    "扩产", "投产", "并购", "重组", "增持", "回购", "分红", "立案", "调查",
    "处罚", "禁令", "限制", "出口管制", "关税", "补贴", "试点", "标准", "新规",
    "条例", "法案", "会议", "预期",
)


def _has_phenomenon(low: str) -> bool:
    """现象形态识别（纯词表/正则，判不出即 False）——覆盖形态：

    - 市场级主语（沪指/上证指数/创业板指/沪深300/大盘/两市/A股…）＋涨跌动作词或涨跌幅；
    - 两市＋成交额/家数/涨跌综述（"两市成交额跌破万亿"）；
    - 时段词（午后/早盘/盘中/尾盘/开盘）＋涨跌幅或涨跌动词；
    - 板块级涨幅语（全线上涨/集体拉升/普涨/涨幅第一/领涨两市…）；
    - "大涨八个点"/"涨了3个点"（`_POINT_MOVE_RE`）；
    - 板块/指数创新高（`_NEW_HIGH_PHRASES` × `_NEW_HIGH_QUALIFIERS`）。
    """
    has_pct = bool(_PCT_RECAP_RE.search(low))
    market = any(token in low for token in _MARKET_WIDE_TOKENS)
    action = any(token in low for token in _MARKET_ACTION_TOKENS)
    if market and (has_pct or action):
        return True
    if ("两市" in low or "兩市" in low) and (
        has_pct or action or any(token in low for token in _TURNOVER_RECAP_TOKENS)
    ):
        return True
    if any(token in low for token in _SESSION_TOKENS) and (
        has_pct or action or any(token in low for token in _DIRECTION_TOKENS)
    ):
        return True
    if any(phrase in low for phrase in _RALLY_PHRASES):
        return True
    if _POINT_MOVE_RE.search(low):
        return True
    return any(phrase in low for phrase in _NEW_HIGH_PHRASES) and any(
        qualifier in low for qualifier in _NEW_HIGH_QUALIFIERS
    )


def _has_cause_token(low: str) -> bool:
    """原因词命中判定（准入共用）：现象判据与页面噪声判据的豁免口径必须**一致**——
    含任一原因词即放行（"同花顺：某公司公告中标5亿元订单"必须留下）。
    """
    return any(token in low for token in _CAUSE_TOKENS)


def _is_page_noise_host(host: str) -> bool:
    """站点域下的**页面模块子域**判定：主机以 ``q.``/``data.``/``stockpage.``/``quote.``/
    ``f10.``/``guba.`` 开头且落在已知行情/数据站点域（`_PAGE_NOISE_URL_SITE_DOMAINS`）。

    刻意**不做任意子串匹配**（2026-09-18 收窄）：`f10.example.com`、`quotes.example.com`
    这类非行情站域名不得被判页面噪声；站点首页（`www.eastmoney.com`）与站内报道页
    （`finance.eastmoney.com/news/…`）同样不判。
    """
    for domain in _PAGE_NOISE_URL_SITE_DOMAINS:
        if not host.endswith("." + domain):
            continue
        subdomain = host[: -(len(domain) + 1)]
        return subdomain.split(".")[0] in _PAGE_NOISE_URL_PAGE_LABELS
    return False


def _is_site_root(host: str, path: str) -> bool:
    """已知行情/数据站点域（含子域）的**站点/栏目首页**判定（`_PAGE_NOISE_URL_ROOT_PATHS`）。

    首页路径本身不含任何报道内容，无论标题多干净都不承载原因（页面模块子域之外的第二道形态判据）。
    """
    if path not in _PAGE_NOISE_URL_ROOT_PATHS:
        return False
    return any(
        host == domain or host.endswith("." + domain)
        for domain in _PAGE_NOISE_URL_SITE_DOMAINS
    )


def is_page_noise_url(url: object) -> bool:
    """URL 级页面噪声判定：True = 该 URL 指向行情页/股吧/F10 等**页面**而非事件报道。

    只看 URL 形态（确定性纯函数，无网络/无 LLM）：主机是已知行情/数据站点域下的页面模块
    子域（`q.10jqka.com.cn`/`quote.eastmoney.com`/`f10.eastmoney.com`/`guba.eastmoney.com`/
    `data.10jqka.com.cn`/`stockpage.10jqka.com.cn`…），或是该站点域（含任意子域）的
    **站点/栏目首页**（path 空/`/`/`index.*`），或路径含 ``/detail/code/``/``/quote/``/
    ``/f10/``/``/guba/``。非字符串/空 → False（判不出即放行）。

    **刻意只收 url 一个参数**：原因词豁免由调用方判定——把 URL 逻辑塞进
    `event_summary_reason` 的 ``headline`` 签名会破坏既有调用方。
    """
    if not isinstance(url, str):
        return False
    raw = url.strip().lower()
    if not raw:
        return False
    parsed = urlparse(raw)
    # 无 scheme 的裸域（"q.10jqka.com.cn/gn/detail/code/30"）会被 urlparse 当路径，
    # 故主机回退取首段，保证裸域同样能判出；顺手剥掉 userinfo 与端口。
    netloc = parsed.netloc or parsed.path.split("/", 1)[0]
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    if _is_page_noise_host(host) or _is_site_root(host, parsed.path):
        return True
    return any(token in parsed.path for token in _PAGE_NOISE_URL_PATH_TOKENS)


def event_summary_reason(headline: object) -> str:
    """行情综述/页面噪声判定：返回拒收原因码（``""`` = 放行，即原因事件或判不出形态）。

    三条独立判据（确定性纯函数，无 LLM/无网络）：

    1. ``summary_marker``：**综述体裁词**（收评/收盤/复盘/盘面/三大指数/涨跌家数/时间线/
       资金流向/涨停潮/异动，简繁与英文综述标题同列）——体裁本身就是综述，**不因**含
       原因词而放行（"今日收评：某政策落地"仍是收评，不可能是一条原因事件）；
    2. ``phenomenon_without_cause``：命中现象形态（`_has_phenomenon`）**且**原因词表
       （`_CAUSE_TOKENS`：政策/监管/公告/中标/订单/涨跌价/产能/供需/关税/补贴/并购/
       业绩/落地…）一个不命中；
    3. ``page_noise``：命中页面噪声词（`_PAGE_NOISE_TOKENS`：行情中心/行情页/行情查询/
       行情报价/行情走势/数据中心/资讯中心/研报中心/F10/股吧/盘口/**频道/首页/栏目/导航**，
       简繁同列）**且**原因词一个不命中。**站点名（同花顺/东方财富）刻意不在词表内**——
       来源品牌不是页面形态（2026-09-18 收窄，防误拒「同花顺：国家大基金三期成立」）；
       站点**栏目名**改由栏目形态词拦（「股票频道- 东方财富网」，2026-09-18 覆盖回归补偿）。

    判据 2 是 2026-09-18 修订的核心：由「命中现象即拒」改为「现象 且 无原因才拒」——
    "某政策落地带动光伏板块大涨""多晶硅价格上涨 供需缺口扩大"是原因不是现象，必须放行；
    "注册制次新股大涨八个点…沪指站上五日均线""全线上涨！…涨幅第一"是纯现象，拒收。

    判据 3 与判据 2 **相互独立**（现象看涨跌描述，页面噪声看页面形态）：判定顺序为
    marker → 现象 → 页面噪声，同时命中时原因码取现象（既有口径不变）；两者共用
    "原因词未命中才拒"的豁免。

    匹配统一对 ``lower()`` 后文本做。判不出来一律放行（宁可漏判：宁可少放，也不拿综述
    当原因；真原因写法的多样性远高于现象/页面标题）。
    """
    if not isinstance(headline, str):
        return "not_text"
    text = headline.strip()
    if not text:
        return "empty"
    low = text.lower()
    if any(marker in low for marker in _SUMMARY_TITLE_MARKERS):
        return "summary_marker"
    has_phenomenon = _has_phenomenon(low)
    has_page_noise = any(token in low for token in _PAGE_NOISE_TOKENS)
    if not (has_phenomenon or has_page_noise):
        return ""
    if _has_cause_token(low):
        return ""
    return "phenomenon_without_cause" if has_phenomenon else "page_noise"


def is_driving_event(headline: object) -> bool:
    """事件准入（单点判定）：True = 该 headline 可作为"驱动原因"进链事件节点。

    只挡"确定是行情综述/现象"的形态（见 `event_summary_reason`）；判不出即放行。
    仅作用于**检索补漏**（source=search）；中台存量事件（source=warehouse）路径不变。
    """
    return event_summary_reason(headline) == ""


def _normalize_match_text(value: object) -> str:
    """匹配/去重归一化：仅保留字母数字与汉字（去空格、标点、大小写差异）。"""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _sector_tokens(sector: str) -> list[str]:
    """板块匹配词：板块名 + 剥常见后缀后的核心名（长度 ≥2 才用）。"""
    name = (sector or "").strip()
    if not name:
        return []
    tokens = [name]
    for suffix in _SECTOR_NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= 2:
            tokens.append(name[: -len(suffix)])
    return tokens


def _mentions(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens if token)


def _warehouse_match_weight(tokens: list[str], event: dict[str, object]) -> int:
    """中台事件与板块的相关度权重（确定性：实体 > 关键词 > 标题 > 摘要；0 = 不相关）。"""
    industry = str(event.get("industry") or "")
    if industry and _mentions(industry, tokens):
        return 3
    raw_keywords = event.get("involved_keywords")
    keywords = (
        [str(k) for k in raw_keywords if isinstance(k, str)]
        if isinstance(raw_keywords, list)
        else []
    )
    for token in tokens:
        if any(len(k) >= 2 and (token in k or k in token) for k in keywords):
            return 3
    if _mentions(str(event.get("title") or ""), tokens):
        return 2
    if _mentions(str(event.get("summary") or ""), tokens):
        return 1
    return 0


def _node(
    *,
    event_id: str | None,
    ref: str,
    headline: str,
    source: str,
    content_hash: str = "",
) -> dict[str, object]:
    """事件节点（内部形状：公开四字段 + 去重键；_dedup 后投影为公开契约）。"""
    return {
        "event_id": event_id,
        "ref": ref,
        "headline": headline,
        "source": source,
        "_title_key": _normalize_match_text(headline),
        "_content_hash": content_hash,
        "_url_key": ref if ref.startswith(("http://", "https://")) else "",
    }


def _warehouse_candidates(
    sector: str, warehouse_events: list[dict[str, object]]
) -> list[dict[str, object]]:
    """中台存量命中（spec §3.2-4 ①）：按板块别名/关键词/实体匹配，权重→影响力→原序。"""
    tokens = _sector_tokens(sector)
    if not tokens:
        return []
    scored: list[tuple[int, int, int, dict[str, object]]] = []
    for index, event in enumerate(warehouse_events):
        if not isinstance(event, dict):
            continue
        weight = _warehouse_match_weight(tokens, event)
        if weight <= 0:
            continue
        title = str(event.get("title") or "").strip()
        # 契约：source=warehouse 的 event_id 必须非空；无标题无法作为事件摘要 →
        # 二者任一缺失即不产节点（宁缺不造，不用 url 冒充 id）
        event_id = str(event.get("event_id") or event.get("app_event_id") or "").strip()
        if not title or not event_id:
            continue
        url = str(event.get("url") or "").strip()
        impact = event.get("impact_score")
        impact_score = impact if isinstance(impact, int) else 0
        scored.append(
            (
                -weight,
                -impact_score,
                index,
                _node(
                    event_id=event_id,
                    ref=url or f"event:{event_id}",
                    headline=title,
                    source=_EVENT_SOURCE_WAREHOUSE,
                    content_hash=str(event.get("content_hash") or ""),
                ),
            )
        )
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored]


def _trigger_evidence_urls(trace_result: dict[str, object]) -> set[str]:
    """溯源 trigger 阶段引用的来源 URL（最贴近根因的判断来自既有溯源产物，非新增 LLM 判定）。"""
    stages = trace_result.get("stages") if isinstance(trace_result, dict) else None
    if not isinstance(stages, list):
        return set()
    urls: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("kind") != "trigger":
            continue
        evidence = stage.get("evidence")
        if not isinstance(evidence, list):
            continue
        for ref in evidence:
            url = str(ref.get("url") or "").strip() if isinstance(ref, dict) else ""
            if url:
                urls.add(url)
    return urls


def _search_candidates(
    sector: str,
    trace_result: dict[str, object],
    snapshot: dict[str, object],
) -> tuple[list[dict[str, object]], int]:
    """检索补漏（spec §3.2-4 ②，溯源板块一律强制执行）。

    消费溯源快照的定向检索来源（sources[].kind="sector_event:<query>"，由
    sector_trace_snapshot._run_directed_searches 真实产出）。三道门槛：

    1. **事件准入**（`event_summary_reason`）：行情综述/现象/页面标题一律拒收——综述回答不了
       "为什么动"，拿它填充会让用户误以为已归因（2026-09-17/18 生产实证）。2026-09-18
       起判据为"命中现象形态**且**原因词未命中"（`_has_phenomenon` × `_CAUSE_TOKENS`）：
       现象外衣但讲清原因（政策落地/价格上涨/订单放量…）放行，纯现象（全线上涨/涨幅第一/
       沪指站上五日均线…）拒收；页面噪声词（行情中心/数据中心/股吧…）同理，reason=page_noise。
       被拒逐条留痕 `chain_event_rejected_not_driving` 并返回被拒条数（汇总进
       `chain_sector_events`）；
    2. **URL 准入门槛**（`is_page_noise_url`，2026-09-18）：headline 干净但 URL 是行情页/
       股吧/F10 页（reason=page_noise_url）→ 拒收；headline 含原因词则**不因 URL 被拒**，
       留痕与计数口径同 1；
    3. **相关性门槛**：标题/正文命中板块词，或 URL 被 trigger 阶段引用；
       门槛不过 → 不产节点（不编造事件）。

    排序（全等分时保原序）：trigger 证据引用 → 标题命中 → 正文命中 → 含驱动类关键词
    （政策/公告/供需/价格…，`_DRIVING_KEYWORDS`）→ 原序。返回 (节点列表, 被拒条数)。
    """
    sources = snapshot.get("sources") if isinstance(snapshot, dict) else None
    if not isinstance(sources, list):
        return [], 0
    tokens = _sector_tokens(sector)
    evidence_urls = _trigger_evidence_urls(trace_result)
    scored: list[tuple[int, int, int, int, int, dict[str, object]]] = []
    rejected = 0
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if not kind.startswith(_SECTOR_SOURCE_PREFIX):
            continue
        query = kind[len(_SECTOR_SOURCE_PREFIX) :]
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "")
        headline = title or content[:60].strip()
        if not headline:
            continue
        reason = event_summary_reason(headline)
        if reason:
            rejected += 1
            logger.info(
                "chain_event_rejected_not_driving",
                sector=sector,
                reason=reason,
                headline=headline[:60],  # 截断：综述标题可能很长，日志只留可辨识前缀
            )
            continue
        url = str(item.get("url") or "").strip()
        # 网 2（URL 级，2026-09-18）：headline 干净但 URL 指向行情页/股吧/F10 页 → 页面噪声。
        # 豁免口径与标题级一致：headline 命中任一原因词即放行（行情站也可能转载真原因）。
        if is_page_noise_url(url) and not _has_cause_token(headline.lower()):
            rejected += 1
            logger.info(
                "chain_event_rejected_not_driving",
                sector=sector,
                reason="page_noise_url",
                headline=headline[:60],  # 截断口径与标题级一致
            )
            continue
        in_title = _mentions(title, tokens)
        in_content = _mentions(content, tokens)
        by_evidence = bool(url) and url in evidence_urls
        if not (by_evidence or in_title or in_content):
            continue
        low_headline = headline.lower()
        driving = 0 if any(k in low_headline for k in _DRIVING_KEYWORDS) else 1
        scored.append(
            (
                0 if by_evidence else 1,
                0 if in_title else 1,
                0 if in_content else 1,
                driving,
                index,
                _node(
                    event_id=None,  # 检索来源无中台权威 id（不冒充）
                    ref=url or f"search:{query}|{headline}",
                    headline=headline,
                    source=_EVENT_SOURCE_SEARCH,
                ),
            )
        )
    scored.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    return [item[5] for item in scored], rejected


def _is_same_event(left: dict[str, object], right: dict[str, object]) -> bool:
    """同一现实事件判定（确定性：content_hash → URL → 标题归一化/互相包含）。"""
    left_hash, right_hash = str(left["_content_hash"]), str(right["_content_hash"])
    if left_hash and right_hash and left_hash == right_hash:
        return True
    if left["_url_key"] and left["_url_key"] == right["_url_key"]:
        return True
    left_title, right_title = str(left["_title_key"]), str(right["_title_key"])
    if not left_title or not right_title:
        return False
    if left_title == right_title:
        return True
    shorter, longer = sorted((left_title, right_title), key=len)
    return len(shorter) >= 8 and shorter in longer


def _child_events(
    sector: str,
    trace_result: dict[str, object],
    snapshot: dict[str, object],
    warehouse_events: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """板块事件节点集合：中台优先 → 检索补漏 → 去重 → 上限（返回节点 + 留痕计数）。

    `rejected_not_driving` = 检索补漏里被"非驱动原因（行情综述/现象）"准入挡下的条数
    （留痕/调参用；中台事件不参与该筛选）。
    """
    candidates = _warehouse_candidates(sector, warehouse_events)
    search_candidates, rejected_not_driving = _search_candidates(
        sector, trace_result, snapshot
    )
    candidates.extend(search_candidates)
    kept: list[dict[str, object]] = []
    dropped = 0
    for candidate in candidates:
        if any(_is_same_event(candidate, existing) for existing in kept):
            dropped += 1
            continue
        kept.append(candidate)
    capped = len(kept) - MAX_CHAIN_EVENTS_PER_SECTOR
    kept = kept[:MAX_CHAIN_EVENTS_PER_SECTOR]
    events = [
        {
            "event_id": item["event_id"],
            "ref": item["ref"],
            "headline": item["headline"],
            "source": item["source"],
        }
        for item in kept
    ]
    stats = {
        "warehouse": sum(1 for e in events if e["source"] == _EVENT_SOURCE_WAREHOUSE),
        "search": sum(1 for e in events if e["source"] == _EVENT_SOURCE_SEARCH),
        "deduped": dropped,
        "capped": max(capped, 0),
        "rejected_not_driving": rejected_not_driving,
    }
    return events, stats


async def load_chain_warehouse_events(report_date: str) -> list[dict[str, object]]:
    """读当日中台存量事件（事件抓取中台 report_type=event_scrape）供链事件层匹配。

    复用 event_store.load_event_scrape（内部已吞异常返回 []，空事件库是常态）：
    链事件层不因中台不可用而失败——events 退化为检索补漏或空数组。
    """
    from aistock_agent.services.event_store import load_event_scrape  # noqa: PLC0415

    events = await load_event_scrape(report_date)
    return [dict(event) for event in events if isinstance(event, dict)]


def _attribution_parent(result: object) -> dict[str, object]:
    """板块溯源结果携带的报告 attribution_parent（sector_trace.py 写入的报告字段）。

    run_sector_trace 把要落库的 content["attribution_parent"] 原样带入结果：板块
    溯源报告与链路同键 report_date（多板块同日互相覆盖），回读无法区分板块，故由
    写入侧携带、链组装消费（Task 2.2 修"只写不读"）。
    """
    parent = getattr(result, "attribution_parent", None)
    return parent if isinstance(parent, dict) else {}


def _sector_extraction(result: object) -> dict[str, object]:
    """板块溯源结果携带的提取来源/弱标记（SectorTraceRunResult.extraction）。

    SectorTraceConsumer 消费 extract_primary_sectors 的 SectorHit 后写入
    ``{"source": "primary_claim"|"candidate_claim"|"snapshot", "weak": bool}``；
    缺该属性（旧调用方/回放）→ 空 dict，视同主链命中（不误标弱）。
    """
    value = getattr(result, "extraction", None)
    return value if isinstance(value, dict) else {}


def _reconcile_index_pct(
    report_date: str, snapshot_index_pct: float | None, sector_results: list[object]
) -> float | None:
    """用报告 attribution_parent.index_pct 校验/补全大盘涨跌幅（以报告为准并告警）。

    - 报告缺该字段 → 沿用现有组装逻辑（快照口径，向后兼容）；
    - 快照缺失、报告有 → 补全（info）；
    - 两者不一致 → warning chain_parent_mismatch 并采用报告值（不静默）；
    - 多板块报告之间不一致 → warning（以首个为准，便于排查父链引用漂移）。
    """
    authoritative: float | None = None
    for result in sector_results:
        parent = _attribution_parent(result)
        value = _numeric_pct(parent.get("index_pct")) if parent else None
        if value is None:
            continue
        if authoritative is None:
            authoritative = value
            continue
        if value != authoritative:
            logger.warning(
                "chain_parent_mismatch",
                report_date=report_date,
                field="index_pct",
                sector=str(getattr(result, "sector", "") or ""),
                report_index_pct=value,
                first_report_index_pct=authoritative,
            )
    if authoritative is None:
        return snapshot_index_pct
    if snapshot_index_pct is None:
        logger.info(
            "chain_parent_index_filled",
            report_date=report_date,
            report_index_pct=authoritative,
        )
    elif snapshot_index_pct != authoritative:
        logger.warning(
            "chain_parent_mismatch",
            report_date=report_date,
            field="index_pct",
            source="snapshot_vs_report",
            snapshot_index_pct=snapshot_index_pct,
            report_index_pct=authoritative,
        )
    return authoritative


def assemble_attribution_chain(
    report_date: str,
    review_payload: dict[str, object],
    sector_results: list[object],
    warehouse_events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """组装 大盘(market) → 主驱动板块(self_driven/follow) 归因链。

    `warehouse_events` 为当日中台存量事件（`load_chain_warehouse_events` 产物，供
    children[].events 的"中台优先"匹配）；缺省 None = 不做中台匹配，只走检索补漏。

    Task 9.1：sector_results 携带的 extraction（板块提取来源/弱标记，见
    SectorTraceRunResult.extraction）为弱依据时 → children[] 写 extraction 标弱、
    root 写 evidence_weak（+报告 attribution_status，摘要空缺用中性表述，不编造主因）；
    主链命中路径不写这些键（正常链不被弱标记污染）。

    R14：children[] 加性写 `ts_code`/`sector_std`（快照权威行，取不到省略键，见
    `_sector_meta`）——供前端把链板块桥到 THS 权威候选（消除命名漂移导致的角色徽丢失）；
    `sector` 保持复盘原始名不变（app-api 校验要求非空字符串，向后兼容）。
    """
    report = review_payload.get("report")
    content = report.get("content") if isinstance(report, dict) else None
    content = content if isinstance(content, dict) else None
    mt = content.get("market_trace") if isinstance(content, dict) else None
    mt = mt if isinstance(mt, dict) else None
    snapshot = mt.get("snapshot") if isinstance(mt, dict) else None
    trace = mt.get("trace") if isinstance(mt, dict) else None

    # 大盘涨跌幅来自快照 a_share.indexes（旧四个候选键在生产快照并不存在，
    # 曾导致 root.index_pct 恒 None → children relation 恒 unknown）
    snapshot_index_pct = (
        index_pct_from_snapshot(snapshot) if isinstance(snapshot, dict) else None
    )
    # Task 2.2：消费板块溯源报告写入的 attribution_parent（原先只写不读）——
    # 校验/补全大盘涨跌幅，不一致以报告为准并 warning
    index_pct = _reconcile_index_pct(report_date, snapshot_index_pct, sector_results)

    summary = str(trace.get("attribution_summary") or "") if isinstance(trace, dict) else ""

    children: list[dict[str, object]] = []
    for res in sector_results:
        sector = str(getattr(res, "sector", "") or "")
        trace_result = getattr(res, "trace_result", {}) or {}
        snapshot_dict = getattr(res, "snapshot", {}) or {}
        trace_result_dict = trace_result if isinstance(trace_result, dict) else {}
        pct = _pct_from(snapshot_dict) if isinstance(snapshot_dict, dict) else None
        # 链事件层（spec §3.2-4）：中台优先 → 检索补漏 → 去重 → 上限；无命中为空数组
        events, event_stats = _child_events(
            sector,
            trace_result_dict,
            snapshot_dict if isinstance(snapshot_dict, dict) else {},
            warehouse_events or [],
        )
        if (
            events
            or event_stats["deduped"]
            or event_stats["capped"]
            or event_stats["rejected_not_driving"]
        ):
            # 判定留痕（spec §3.2-4 去重/上限口径调参用；含"综述被拒"计数）
            logger.info(
                "chain_sector_events",
                report_date=report_date,
                sector=sector,
                **event_stats,
            )
        extraction = _sector_extraction(res)
        # 迭代 4：摘要与事件层一致性裁决——否定句摘要 + 有事件 → 摘要让位（见 `_trace_summary`）
        report_summary = _trace_summary_from_report(trace_result_dict)
        trace_summary = _trace_summary(trace_result_dict, events=events)
        if trace_summary != report_summary:
            logger.info(
                "chain_trace_summary_overridden_by_events",
                report_date=report_date,
                sector=sector,
                from_summary=report_summary,
                to_headline=trace_summary,
            )
        child: dict[str, object] = {
            "sector": sector,
            # R14：板块标识增强（ts_code/sector_std，取自溯源命中的快照权威行）——
            # sector 保持复盘原始名（app-api 校验要求非空字符串，向后兼容）
            **_sector_meta(sector, getattr(res, "sector_row", None)),
            "relation": judge_sector_driver_relation(pct, index_pct),
            "pct": pct,
            "trace_summary": trace_summary,
            "events": events,
        }
        # Task 9.1：兜底命中（无主链 → 候选链/快照）标弱依据，供展示层提示证据不足；
        # 主链命中不写该键（正常路径不被弱标记污染）
        if extraction.get("weak"):
            child["extraction"] = {
                "source": str(extraction.get("source") or ""),
                "weak": True,
            }
        children.append(child)

    # 整体走 T2/T3（无主链）→ 链根如实标注证据弱；摘要空缺用中性表述，不编造主因
    evidence_weak = any(_sector_extraction(res).get("weak") for res in sector_results)
    root: dict[str, object] = {
        "type": "market",
        "date": report_date,
        "summary": summary,
        "index_pct": index_pct,
    }
    if evidence_weak:
        status = str(trace.get("attribution_status") or "") if isinstance(trace, dict) else ""
        if status:
            root["attribution_status"] = status
        root["evidence_weak"] = True
        if not summary:
            root["summary"] = _WEAK_ATTRIBUTION_SUMMARY

    return {
        "date": report_date,
        "root": root,
        "children": children,
    }


class AttributionChainStore:
    def __init__(self) -> None:
        self.node_api = node_api

    async def save(self, report_date: str, chain: dict[str, object]) -> None:
        # 路径必须带 /api 前缀：app-api 把 attributionChainRouter 挂在 /api 下
        # （index.ts:165），绝对路径为 POST /api/internal/attribution-chain；不带 /api 会命中
        # /internal 那个 router（index.ts:631）而恒 404，且 post 吞错返回 None → 静默不落库。
        # error_out（2026-09-18）：把 data_client 捕获的**真实失败原因**（业务码/HTTP 状态）
        # 并入本条 warning —— R17 排障代价正来自"真实原因只在那几条独立日志里，需交叉 grep"。
        cause: dict[str, object] = {}
        result = await self.node_api.post(
            "/api/internal/attribution-chain",
            {"date": report_date, "chain": chain},
            error_out=cause,
        )
        if result is None:
            # data_client.post 失败/业务码异常吞错返回 None → 告警而非误报 saved
            logger.warning(
                "attribution_chain.save_failed",
                report_date=report_date,
                stage=str(cause.get("stage") or "unknown"),
                error=str(cause.get("detail") or "node_api.post 返回 None（原因未捕获）"),
            )
            return
        logger.info(
            "attribution_chain.saved",
            report_date=report_date,
            children=len(chain.get("children", [])),
        )
