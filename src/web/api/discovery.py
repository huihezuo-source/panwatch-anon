import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.config import Settings
from src.core.notifier import get_global_proxy
from src.collectors.discovery_collector import EastMoneyDiscoveryCollector
from src.web.database import get_db
from src.web.models import MarketScanSnapshot, Stock


router = APIRouter()

logger = logging.getLogger(__name__)


_cache: dict[str, tuple[float, object]] = {}


def _resolve_proxy() -> str:
    # Prefer UI-configured proxy, fallback to env settings.
    try:
        return (get_global_proxy() or "").strip() or (
            Settings().http_proxy or ""
        ).strip()
    except Exception:
        return ""


def _cache_get(key: str, ttl_s: int) -> object | None:
    now = time.time()
    hit = _cache.get(key)
    if not hit:
        return None
    ts, obj = hit
    if now - ts > ttl_s:
        return None
    return obj


def _cache_set(key: str, obj: object) -> None:
    _cache[key] = (time.time(), obj)


def _to_number(value) -> float | None:
    if value is None:
        return None
    try:
        n = float(value)
        if n != n:  # NaN
            return None
        return n
    except Exception:
        return None


def _pick_num(mapping: dict, keys: list[str]) -> float | None:
    for key in keys:
        if key in mapping:
            n = _to_number(mapping.get(key))
            if n is not None:
                return n
    return None


def _normalize_market(market: str) -> str:
    m = (market or "CN").strip().upper()
    return m if m in ("CN", "HK", "US") else "CN"


def _latest_snapshot_stocks(db: Session, market: str, limit: int = 120) -> list[dict]:
    mkt = _normalize_market(market)
    latest = (
        db.query(MarketScanSnapshot.snapshot_date)
        .filter(MarketScanSnapshot.stock_market == mkt)
        .order_by(MarketScanSnapshot.snapshot_date.desc())
        .first()
    )
    if not latest:
        return []
    rows = (
        db.query(MarketScanSnapshot)
        .filter(
            MarketScanSnapshot.stock_market == mkt,
            MarketScanSnapshot.snapshot_date == latest[0],
        )
        .order_by(MarketScanSnapshot.score_seed.desc(), MarketScanSnapshot.updated_at.desc())
        .limit(max(20, min(int(limit), 300)))
        .all()
    )
    out: list[dict] = []
    for row in rows:
        quote = row.quote if isinstance(row.quote, dict) else {}
        out.append(
            {
                "symbol": row.stock_symbol,
                "market": row.stock_market,
                "name": row.stock_name or row.stock_symbol,
                "price": _pick_num(quote, ["price", "current_price", "last", "close"]),
                "change_pct": _pick_num(quote, ["change_pct", "pct_change", "chg_pct"]),
                "turnover": _pick_num(quote, ["turnover", "amount", "turnover_value"]),
                "volume": _pick_num(quote, ["volume", "vol"]),
            }
        )
    return out


async def _hot_stocks_live_or_snapshot(
    *,
    collector: EastMoneyDiscoveryCollector,
    db: Session,
    market: str,
    mode: str,
    limit: int,
) -> list[dict]:
    mkt = _normalize_market(market)
    try:
        items = await collector.fetch_hot_stocks(market=mkt, mode=mode, limit=limit)
        data = [
            {
                "symbol": it.symbol,
                "market": it.market,
                "name": it.name,
                "price": it.price,
                "change_pct": it.change_pct,
                "turnover": it.turnover,
                "volume": it.volume,
            }
            for it in items
        ]
        if data:
            return data
    except Exception as e:
        logger.warning(f"discovery stocks live failed ({mkt}/{mode}): {type(e).__name__}: {e!r}")
    # Snapshot fallback: ensures UI is still usable when live source timeout/unavailable.
    return _latest_snapshot_stocks(db, mkt, limit=max(limit, 40))


def _watchlist_symbols(db: Session, market: str) -> set[str]:
    mkt = _normalize_market(market)
    rows = db.query(Stock.symbol).filter(Stock.market == mkt).all()
    return {str(x[0]).strip() for x in rows if x and x[0]}


def _avg(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _sum(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(sum(vals))


def _build_synthetic_boards(
    *,
    market: str,
    stocks: list[dict],
    watchlist: set[str],
    limit: int,
) -> list[dict]:
    mkt = _normalize_market(market)
    if not stocks:
        return []
    # Keep a stable high-quality universe for synthetic themes.
    universe = stocks[: max(30, min(len(stocks), 120))]
    gainers = sorted(universe, key=lambda x: _to_number(x.get("change_pct")) or -999.0, reverse=True)
    turnover = sorted(universe, key=lambda x: _to_number(x.get("turnover")) or 0.0, reverse=True)
    volatility = sorted(universe, key=lambda x: abs(_to_number(x.get("change_pct")) or 0.0), reverse=True)
    watch_related = [x for x in universe if str(x.get("symbol") or "") in watchlist]

    def build_bucket(code: str, name: str, items: list[dict]) -> dict | None:
        if not items:
            return None
        top = items[: min(12, len(items))]
        return {
            "code": f"{mkt}_{code}",
            "name": name,
            "change_pct": _avg([_to_number(x.get("change_pct")) for x in top]),
            "change_amount": None,
            "turnover": _sum([_to_number(x.get("turnover")) for x in top]),
        }

    market_name = {"CN": "A股", "HK": "港股", "US": "美股"}.get(mkt, mkt)
    buckets = [
        build_bucket("GAINERS", f"{market_name}涨幅领先", gainers),
        build_bucket("TURNOVER", f"{market_name}成交额领先", turnover),
        build_bucket("VOLATILITY", f"{market_name}波动活跃", volatility),
        build_bucket("WATCHLIST", f"{market_name}自选关联", watch_related),
    ]
    result = [x for x in buckets if x]
    return result[: max(1, min(int(limit), 20))]


def _stocks_by_synthetic_board(
    *,
    code: str,
    market: str,
    stocks: list[dict],
    watchlist: set[str],
    limit: int,
) -> list[dict]:
    mkt = _normalize_market(market)
    suffix = code.replace(f"{mkt}_", "", 1)
    universe = stocks[: max(30, min(len(stocks), 160))]
    if suffix == "GAINERS":
        ranked = sorted(universe, key=lambda x: _to_number(x.get("change_pct")) or -999.0, reverse=True)
    elif suffix == "TURNOVER":
        ranked = sorted(universe, key=lambda x: _to_number(x.get("turnover")) or 0.0, reverse=True)
    elif suffix == "VOLATILITY":
        ranked = sorted(universe, key=lambda x: abs(_to_number(x.get("change_pct")) or 0.0), reverse=True)
    elif suffix == "WATCHLIST":
        ranked = [x for x in universe if str(x.get("symbol") or "") in watchlist]
        ranked = sorted(ranked, key=lambda x: _to_number(x.get("turnover")) or 0.0, reverse=True)
    else:
        ranked = []
    return ranked[: max(1, min(int(limit), 100))]


@router.get("/stocks")
async def get_hot_stocks(
    market: str = "CN",
    mode: str = "turnover",
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """Hot stocks for discovery.

    mode: turnover | gainers
    """

    market = _normalize_market(market)
    mode = (mode or "turnover").lower()
    if mode not in ("turnover", "gainers"):
        raise HTTPException(400, f"不支持的 mode: {mode}")

    key = f"stocks:{market}:{mode}:{int(limit)}"
    cached = _cache_get(key, ttl_s=45)
    if cached is not None:
        return cached

    proxy = _resolve_proxy() or None
    collector = EastMoneyDiscoveryCollector(timeout_s=15.0, proxy=proxy, retries=1)
    data = await _hot_stocks_live_or_snapshot(
        collector=collector,
        db=db,
        market=market,
        mode=mode,
        limit=max(1, min(int(limit), 100)),
    )
    if not data:
        raise HTTPException(
            503, "热门股票数据源不可用（实时源与本地快照均不可用）"
        )
    _cache_set(key, data)
    return data


def _price_limit_pct(symbol: str, name: str) -> float:
    """该股当日涨跌停幅度:创业板/科创板 20%,ST 5%,其余主板 10%。用于准确判定涨停/跌停。"""
    s = (symbol or "").strip()
    n = (name or "").upper()
    if "ST" in n:
        return 5.0
    if s.startswith(("300", "301", "688")):
        return 20.0
    if s.startswith("8") or s.startswith("4"):  # 北交所
        return 30.0
    return 10.0


def _movers_tags(item: dict) -> list[str]:
    """按行情数据给异动打标(纯规则,不调 AI):涨跌停/大涨跌/放量/高换手。"""
    tags: list[str] = []
    pct = item.get("change_pct")
    vr = item.get("volume_ratio")
    tr = item.get("turnover_rate")
    limit = _price_limit_pct(item.get("symbol") or "", item.get("name") or "")
    if isinstance(pct, (int, float)):
        if pct >= limit - 0.3:
            tags.append("涨停")
        elif pct <= -(limit - 0.3):
            tags.append("跌停")
        elif pct >= 7:
            tags.append("大涨")
        elif pct <= -7:
            tags.append("大跌")
    if isinstance(vr, (int, float)) and vr >= 2:
        tags.append("放量")
    if isinstance(tr, (int, float)) and tr >= 10:
        tags.append("高换手")
    return tags


@router.get("/movers")
async def get_movers(
    market: str = "CN",
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """今日异动榜:涨幅榜 + 跌幅榜合并,按规则打标(涨停/放量/高换手等)。

    数据来自公开行情源(东方财富),异动判定为本站自有规则,不依赖任何第三方付费内容。
    归因解读由前端点进个股后的 AI 建议提供(已有缓存+限流)。
    """
    market = _normalize_market(market)
    limit = max(1, min(int(limit), 50))
    key = f"movers:{market}:{limit}"
    cached = _cache_get(key, ttl_s=45)
    if cached is not None:
        return cached

    proxy = _resolve_proxy() or None
    # 快速失败:要顺序拉两个榜,必须保证总耗时 < 前端 20s 超时。
    # 6s×2 = 最坏 12s;拉不到就走过期缓存兜底,不让前端卡到超时。
    collector = EastMoneyDiscoveryCollector(timeout_s=6.0, proxy=proxy, retries=0)

    async def _fetch(mode: str) -> list:
        try:
            return await collector.fetch_hot_stocks(market=market, mode=mode, limit=limit)
        except Exception as e:
            logger.warning(f"movers fetch {mode} failed: {type(e).__name__}: {e!r}")
            return []

    # 注意:东财 push2 不接受并发请求(同时打两个会 502),必须顺序拉。
    # 有 45s 缓存兜底,顺序多花 1-2s 可接受。
    gainers = await _fetch("gainers")
    losers = await _fetch("losers")
    if not gainers and not losers:
        # 东财会对高频调用限流(502/302)。此时若有过期缓存,宁可返回稍旧的异动数据,
        # 也不要把整页打成 503 —— 盘中异动晚几分钟仍有参考价值。
        stale = _cache.get(key)
        if stale:
            logger.warning("movers 实时源不可用,返回过期缓存兜底")
            data = dict(stale[1]) if isinstance(stale[1], dict) else stale[1]
            if isinstance(data, dict):
                data["stale"] = True
            return data
        raise HTTPException(503, "异动数据源暂不可用,请稍后再试")

    items: list[dict] = []
    seen: set[str] = set()
    for row, direction in [(r, "up") for r in gainers] + [(r, "down") for r in losers]:
        sym = (getattr(row, "symbol", "") or "").strip()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        it = {
            "symbol": sym,
            "market": market,
            "name": getattr(row, "name", "") or sym,
            "price": getattr(row, "price", None),
            "change_pct": getattr(row, "change_pct", None),
            "turnover": getattr(row, "turnover", None),
            "volume": getattr(row, "volume", None),
            "turnover_rate": getattr(row, "turnover_rate", None),
            "volume_ratio": getattr(row, "volume_ratio", None),
            "direction": direction,
        }
        it["tags"] = _movers_tags(it)
        items.append(it)

    # 按异动强度排序:绝对涨跌幅优先
    items.sort(key=lambda x: abs(x.get("change_pct") or 0), reverse=True)
    result = {
        "market": market,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
    }
    _cache_set(key, result)
    return result


@router.get("/boards")
async def get_hot_boards(
    market: str = "CN",
    mode: str = "gainers",
    limit: int = 12,
    db: Session = Depends(get_db),
):
    """Hot boards (industry) for discovery.

    mode: gainers | turnover
    """

    market = _normalize_market(market)
    mode = (mode or "gainers").lower()
    if mode not in ("gainers", "turnover", "hot"):
        raise HTTPException(400, f"不支持的 mode: {mode}")

    key = f"boards:{market}:{mode}:{int(limit)}"
    cached = _cache_get(key, ttl_s=60)
    if cached is not None:
        return cached

    proxy = _resolve_proxy() or None
    collector = EastMoneyDiscoveryCollector(timeout_s=15.0, proxy=proxy, retries=1)
    data: list[dict] = []
    # CN: prefer real industry boards; HK/US: synthetic themed buckets from market hot pool.
    if market == "CN":
        try:
            items = await collector.fetch_hot_boards(market=market, mode=mode, limit=limit)
            data = [
                {
                    "code": it.code,
                    "name": it.name,
                    "change_pct": it.change_pct,
                    "change_amount": it.change_amount,
                    "turnover": it.turnover,
                }
                for it in items
            ]
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError) as e:
            logger.warning(f"discovery boards connect timeout: {e!r}")
        except Exception as e:
            logger.warning(f"discovery boards failed: {type(e).__name__}: {e!r}")

    if not data:
        stocks = await _hot_stocks_live_or_snapshot(
            collector=collector,
            db=db,
            market=market,
            mode="turnover" if mode == "turnover" else "gainers",
            limit=max(50, int(limit) * 10),
        )
        watchlist = _watchlist_symbols(db, market)
        data = _build_synthetic_boards(
            market=market,
            stocks=stocks,
            watchlist=watchlist,
            limit=limit,
        )
    if not data:
        raise HTTPException(503, "热门板块/主题数据源不可用")
    _cache_set(key, data)
    return data


@router.get("/boards/{board_code}/stocks")
async def get_board_stocks(
    board_code: str,
    mode: str = "gainers",
    limit: int = 20,
    market: str = "CN",
    db: Session = Depends(get_db),
):
    """Top stocks in a board."""

    code = (board_code or "").strip()
    if not code:
        raise HTTPException(400, "缺少板块代码")

    mkt = _normalize_market(market)
    mode = (mode or "gainers").lower()
    if mode not in ("gainers", "turnover", "hot"):
        raise HTTPException(400, f"不支持的 mode: {mode}")

    key = f"board_stocks:{mkt}:{code}:{mode}:{int(limit)}"
    cached = _cache_get(key, ttl_s=60)
    if cached is not None:
        return cached

    if code.startswith(("CN_", "HK_", "US_")):
        proxy = _resolve_proxy() or None
        collector = EastMoneyDiscoveryCollector(timeout_s=15.0, proxy=proxy, retries=1)
        market_from_code = code.split("_", 1)[0]
        stocks = await _hot_stocks_live_or_snapshot(
            collector=collector,
            db=db,
            market=market_from_code,
            mode="turnover" if mode == "turnover" else "gainers",
            limit=max(80, int(limit) * 8),
        )
        watchlist = _watchlist_symbols(db, market_from_code)
        data = _stocks_by_synthetic_board(
            code=code,
            market=market_from_code,
            stocks=stocks,
            watchlist=watchlist,
            limit=limit,
        )
        _cache_set(key, data)
        return data

    proxy = _resolve_proxy() or None
    collector = EastMoneyDiscoveryCollector(timeout_s=15.0, proxy=proxy, retries=1)
    try:
        items = await collector.fetch_board_stocks(
            board_code=code, mode=mode, limit=limit
        )
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError) as e:
        logger.warning(f"discovery board_stocks connect timeout: {e!r}")
        raise HTTPException(
            503, "板块成分股数据源连接超时（可能需要配置代理 http_proxy）"
        )
    except Exception as e:
        logger.warning(f"discovery board_stocks failed: {type(e).__name__}: {e!r}")
        raise HTTPException(503, "板块成分股数据源不可用")
    data = [
        {
            "symbol": it.symbol,
            "market": "CN",
            "name": it.name,
            "price": it.price,
            "change_pct": it.change_pct,
            "turnover": it.turnover,
            "volume": it.volume,
        }
        for it in items
    ]
    _cache_set(key, data)
    return data
