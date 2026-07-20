"""今日异动个股的自有解析(连板数 + AI 题材归因)。

为什么单独成模块 / 为什么要后台预生成:
- 连板数要拉日K、题材归因要拉公告+新闻再调 DeepSeek。几十只股 = 几十次外部请求,
  放在访客请求里既慢又必被行情源限流(东财对高频调用直接 502)。
- 故:后台按天只给「Top N 最强异动股」生成一次,写入 mover_insights 表,访客只读缓存。

内容全部基于公开行情/公告/新闻由本站自行生成,为自有原创,不依赖任何第三方付费内容。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 后台生成的并发护栏:同一 (market, date) 同时只跑一轮
_generating: set[str] = set()

# 每轮最多为多少只股生成(控制外部请求量与 token 成本)
TOP_N_DEFAULT = 12
# 每只股之间的间隔秒数(对行情源友好,避免被限流)
_SLEEP_BETWEEN = 1.2


def price_limit_pct(symbol: str, name: str = "") -> float:
    """该股涨跌停幅度:创业板/科创板 20%,北交所 30%,ST 5%,其余主板 10%。"""
    s = (symbol or "").strip()
    n = (name or "").upper()
    if "ST" in n:
        return 5.0
    if s.startswith(("300", "301", "688")):
        return 20.0
    if s.startswith(("8", "4")):
        return 30.0
    return 10.0


def compute_streak(symbol: str, market: str, name: str = "") -> tuple[int, int]:
    """从日K算 (连板数, 近20日涨停次数)。

    连板数 = 以最新一根日K为止的【连续】涨停数(定义明确、可人工核对)。
    刻意不做「N天M板」那种带缺口的宽窗口统计 —— 规则各家不一,算错还不如不显示。
    涨停判定用【涨停价】精确比对,不用涨跌幅容差(容差会把 9.7% 误判成涨停)。
    """
    try:
        from src.collectors.kline_collector import KlineCollector
        from src.models.market import MarketCode

        bars = KlineCollector(MarketCode(market)).get_klines(symbol, days=40)
    except Exception as e:
        logger.warning(f"[异动解析] {symbol} 取日K失败: {type(e).__name__}: {e}")
        return 0, 0

    if not bars or len(bars) < 2:
        return 0, 0

    lp = price_limit_pct(symbol, name)
    ups: list[bool] = []
    for i in range(1, len(bars)):
        prev = bars[i - 1].close
        cur = bars[i].close
        if not prev:
            ups.append(False)
            continue
        limit_price = round(float(prev) * (1 + lp / 100.0), 2)
        ups.append(float(cur) >= limit_price - 0.001)

    if not ups:
        return 0, 0

    streak = 0
    if ups[-1]:
        for u in reversed(ups):
            if not u:
                break
            streak += 1

    limit_ups_20d = sum(ups[-20:])
    return streak, limit_ups_20d


async def _fetch_context(symbol: str, name: str) -> str:
    """拉该股近期公告+新闻,拼成喂给 AI 的材料。拿不到就返回空串。"""
    try:
        from src.collectors.news_collector import NewsCollector

        # from_database:按「数据源」页配置的新闻源(雪球/东财资讯/东财公告)聚合。
        # since_hours 放宽到 72h —— 异动多由前几天的公告催化(如中报预增、控制权变更)。
        collector = NewsCollector.from_database()
        items = await collector.fetch_all(
            symbols=[symbol], since_hours=72, symbol_names={symbol: name}
        )
    except Exception as e:
        logger.warning(f"[异动解析] {symbol} 取新闻/公告失败: {type(e).__name__}: {e}")
        return ""

    lines: list[str] = []
    for it in (items or [])[:15]:
        title = getattr(it, "title", "") or ""
        content = getattr(it, "content", "") or ""
        pub = getattr(it, "publish_time", "") or ""
        if not title and not content:
            continue
        # 正文里才有「牛磺酸龙头」「净利3987万」这类关键细节,必须一起喂给 AI。
        body = content.strip().replace("\n", " ")[:280]
        block = f"[{pub}] {title}"
        if body:
            block += f"\n  正文:{body}"
        lines.append(block)
    return "\n".join(lines)


_PROMPT = """你是资深 A 股异动解析师。依据下面材料,详细说明这只股票今天异动的可能原因。要像专业异动复盘一样具体、有信息量。

股票:{name}({symbol})
今日表现:涨跌幅 {pct}%,量比 {vr},换手率 {tr}%{streak_note}

近期公告与新闻(含正文,细节都在正文里,务必仔细读):
{context}

输出格式(严格遵守):
第一行:只输出「题材标签」,用 + 连接 4-6 个关键词,尽量从正文里挖出行业地位/概念/催化,例如:中报预增+牛磺酸龙头(全球最大)+功能饮料+跨境电商。这一行不写别的。
之后:输出 3-5 条要点,每条一行,以「1、」「2、」…开头。每条尽量做到:
  - 带具体日期(如"7月14日公告")
  - 带具体主体(公司名/收购方/机构名)
  - 带具体数字(金额/股数/占比/增速/产能等,直接引用正文里的数字)
  - 说清来龙去脉,而不是一句话带过
把正文里出现的「行业龙头/全球最大/市占率/产能/主力资金流向」等硬信息都用上。

铁律:
- 只依据上面材料,数字和事实必须来自材料,绝不编造。材料确实不足时,题材标签写「暂无明确催化」,并在要点里如实说明公开信息有限。
- 只做事实陈述与归因,不得给出买入/卖出/加仓等操作建议,不得预测未来涨跌,不用"利好""值得关注"等诱导词。"""


async def generate_analysis(
    db, symbol: str, market: str, name: str, pct, vr, tr, streak: int
) -> tuple[str, str, str]:
    """生成题材归因 → (tags, text, status)。status: ok / failed。"""
    context = await _fetch_context(symbol, name)
    if not context:
        return "", "", "failed"

    try:
        from src.web.api.chat import _get_ai_client

        client = _get_ai_client(db)
    except Exception as e:
        logger.warning(f"[异动解析] 取 AI 客户端失败: {type(e).__name__}: {e}")
        return "", "", "failed"

    streak_note = f",已连续涨停 {streak} 天" if streak >= 2 else ""
    prompt = _PROMPT.format(
        name=name, symbol=symbol, pct=pct, vr=vr, tr=tr,
        streak_note=streak_note, context=context,
    )
    try:
        raw = await client.chat("你是严谨的 A 股异动解析助手,只依据材料陈述事实。", prompt)
    except Exception as e:
        logger.warning(f"[异动解析] {symbol} 调用 AI 失败: {type(e).__name__}: {e}")
        return "", "", "failed"

    text = (raw or "").strip()
    if not text:
        return "", "", "failed"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tags = lines[0] if lines else ""
    # AI 有时把 prompt 里的字段名也带出来(如"题材标签:电力+..."),或包了书名号,去掉。
    import re as _re
    tags = _re.sub(r"^(题材标签|标签|概念)\s*[:：]\s*", "", tags).strip()
    tags = tags.strip("「」【】").strip()
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    return tags[:200], body[:2500], "ok"


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def ensure_today_insights(items: list[dict], market: str, top_n: int = TOP_N_DEFAULT) -> None:
    """为 Top N 异动股生成当日 insight(已有则跳过)。供后台 fire-and-forget 调用。

    选股优先级:涨停 > 放量 > 绝对涨跌幅。只取涨的(异动解析主要看涨停/题材)。
    """
    guard = f"{market}:{_today_str()}"
    if guard in _generating:
        return
    _generating.add(guard)

    from src.web.database import SessionLocal
    from src.web.models import MoverInsight

    db = SessionLocal()
    try:
        today = _today_str()
        ups = [x for x in items if (x.get("change_pct") or 0) > 0]
        ups.sort(
            key=lambda x: (
                "涨停" in (x.get("tags") or []),
                "放量" in (x.get("tags") or []),
                abs(x.get("change_pct") or 0),
            ),
            reverse=True,
        )

        # ── 阶段1:连板快速计算 ───────────────────────────────────────────
        # 连板只对涨停股有意义,且计算只拉日K(腾讯源,便宜、不被东财限流)。
        # 所以先给所有涨停股快速算好连板存下来,让「N连板」徽章立刻出现在榜上,
        # 不必等阶段2 那个又慢又贵的 AI 题材归因(只覆盖 Top N)。
        limit_up_stocks = [x for x in ups if "涨停" in (x.get("tags") or [])][:35]
        for it in limit_up_stocks:
            symbol = it.get("symbol") or ""
            name = it.get("name") or symbol
            if not symbol:
                continue
            row = (
                db.query(MoverInsight)
                .filter(
                    MoverInsight.stock_symbol == symbol,
                    MoverInsight.stock_market == market,
                    MoverInsight.trade_date == today,
                )
                .first()
            )
            if row and (row.streak_count or 0) >= 1:
                continue  # 当天连板不变,已算过就跳过
            streak, ups20 = compute_streak(symbol, market, name)
            if streak < 1 and ups20 < 1:
                continue  # 没连板/涨停记录(或K线拉失败),不写脏数据
            if not row:
                row = MoverInsight(
                    stock_symbol=symbol, stock_market=market, trade_date=today
                )
                db.add(row)
            row.stock_name = name
            row.streak_count = streak
            row.limit_ups_20d = ups20
            db.commit()
            await asyncio.sleep(0.25)  # 对 K线源友好

        # ── 阶段2:AI 题材归因(贵,只覆盖 Top N 最强异动股)──────────────
        targets = ups[:top_n]

        for it in targets:
            symbol = it.get("symbol") or ""
            name = it.get("name") or symbol
            if not symbol:
                continue
            exists = (
                db.query(MoverInsight)
                .filter(
                    MoverInsight.stock_symbol == symbol,
                    MoverInsight.stock_market == market,
                    MoverInsight.trade_date == today,
                    MoverInsight.analysis_status == "ok",
                )
                .first()
            )
            if exists:
                continue

            streak, ups20 = compute_streak(symbol, market, name)
            tags, body, status = await generate_analysis(
                db, symbol, market, name,
                it.get("change_pct"), it.get("volume_ratio"), it.get("turnover_rate"),
                streak,
            )

            row = (
                db.query(MoverInsight)
                .filter(
                    MoverInsight.stock_symbol == symbol,
                    MoverInsight.stock_market == market,
                    MoverInsight.trade_date == today,
                )
                .first()
            )
            if not row:
                row = MoverInsight(
                    stock_symbol=symbol, stock_market=market, trade_date=today
                )
                db.add(row)
            row.stock_name = name
            row.streak_count = streak
            row.limit_ups_20d = ups20
            row.analysis_tags = tags
            row.analysis_text = body
            row.analysis_status = status
            db.commit()
            logger.info(
                f"[异动解析] {symbol} {name} 连板={streak} 20日板={ups20} 解析={status}"
            )
            # 对行情源/AI 友好:每只之间歇一下
            await asyncio.sleep(_SLEEP_BETWEEN)
    except Exception as e:
        logger.warning(f"[异动解析] 后台生成异常: {type(e).__name__}: {e}")
    finally:
        db.close()
        _generating.discard(guard)


def attach_cached_insights(db, items: list[dict], market: str) -> None:
    """把已生成的当日 insight 贴到 items 上(只读缓存,不触发生成)。"""
    from src.web.models import MoverInsight

    if not items:
        return
    try:
        rows = (
            db.query(MoverInsight)
            .filter(
                MoverInsight.stock_market == market,
                MoverInsight.trade_date == _today_str(),
            )
            .all()
        )
    except Exception:
        return
    by_sym = {r.stock_symbol: r for r in rows}
    for it in items:
        r = by_sym.get(it.get("symbol"))
        if not r:
            continue
        it["streak_count"] = r.streak_count or 0
        it["limit_ups_20d"] = r.limit_ups_20d or 0
        if r.analysis_status == "ok":
            it["analysis_tags"] = r.analysis_tags or ""
            it["analysis_text"] = r.analysis_text or ""
