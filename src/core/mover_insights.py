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
    for it in (items or [])[:12]:
        title = getattr(it, "title", "") or ""
        pub = getattr(it, "publish_time", "") or ""
        src = getattr(it, "source", "") or ""
        if title:
            lines.append(f"- [{pub}] {title}({src})")
    return "\n".join(lines)


_PROMPT = """你是 A 股异动解析助手。依据下面给出的材料,客观说明这只股票今天出现异动的可能原因。

股票:{name}({symbol})
今日表现:涨跌幅 {pct}%,量比 {vr},换手率 {tr}%{streak_note}

近期公告与新闻:
{context}

输出要求:
1. 第一行只输出「题材标签」,用 + 连接 3-5 个关键词,例如:中报预增+功能饮料+跨境电商。这一行不要写别的字。
2. 之后输出 2-3 条要点,每条一行,以「1、」「2、」「3、」开头,引用材料里的具体日期和内容。
3. 只依据上面给出的材料,不得编造。材料不足以解释异动时,题材标签写「暂无明确催化」,要点里如实说明公开信息有限。
4. 只做事实陈述与归因,不得给出买入/卖出等操作建议,不得预测未来涨跌。"""


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
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    return tags[:120], body[:1200], "ok"


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
        ups = [x for x in items if (x.get("change_pct") or 0) > 0]
        ups.sort(
            key=lambda x: (
                "涨停" in (x.get("tags") or []),
                "放量" in (x.get("tags") or []),
                abs(x.get("change_pct") or 0),
            ),
            reverse=True,
        )
        targets = ups[:top_n]
        today = _today_str()

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
