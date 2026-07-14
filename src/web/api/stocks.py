import asyncio
import logging
import threading
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import (
    Stock,
    StockAgent,
    AgentConfig,
    Position,
    PriceAlertRule,
    PriceAlertHit,
)
from src.web.stock_list import search_stocks, refresh_stock_list
from src.collectors.akshare_collector import _tencent_symbol, _fetch_tencent_quotes
from src.models.market import MarketCode, MARKETS
from src.core.agent_catalog import AGENT_KIND_WORKFLOW, infer_agent_kind

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================================
# 深度分析(TradingAgents)防滥用:每 IP 限流 + 全局限流。
# 匿名公开版下 trigger 端点无登录保护,单个访客可换着股票刷 → 烧站长 DeepSeek 钱。
# 缓存/去重命中不计数(免费复用),只对"真正启新任务"的路径限流;登录站长不受限。
# 单 uvicorn worker,进程内内存计数即可(重启清零,可接受)。上限可按需调大。
# ============================================================================
from collections import deque

_TA_RATE_PER_IP_MAX = 6        # 每个 IP 每窗口最多启多少次新分析
_TA_RATE_PER_IP_WINDOW = 3600  # 每 IP 窗口(秒)= 1 小时
_TA_RATE_GLOBAL_MAX = 60       # 全站所有访客合计每窗口上限(挡分布式刷)
_TA_RATE_GLOBAL_WINDOW = 3600
_ta_ip_buckets: dict[str, deque] = {}
_ta_global_bucket: deque = deque()
_ta_rate_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """取真实访客 IP。生产在 Nginx 反代后,真实 IP 在 X-Forwarded-For 首段。"""
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"


def _is_owner_request(request: Request) -> bool:
    """请求是否带有效登录 token(站长)。站长不受匿名限流约束。"""
    try:
        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return False
        from src.web.api.auth import verify_token
        return bool(verify_token(auth.split(" ", 1)[1].strip()))
    except Exception:
        return False


def _ta_rate_check_and_record(ip: str) -> tuple[bool, str]:
    """滑动窗口限流:未超限则记一次并返回 (True, "")，超限返回 (False, 提示)。"""
    import time as _t
    now = _t.time()
    with _ta_rate_lock:
        # 全局窗口
        while _ta_global_bucket and now - _ta_global_bucket[0] > _TA_RATE_GLOBAL_WINDOW:
            _ta_global_bucket.popleft()
        if len(_ta_global_bucket) >= _TA_RATE_GLOBAL_MAX:
            return False, "当前深度分析请求较多,请稍后再试"
        # 每 IP 窗口
        dq = _ta_ip_buckets.setdefault(ip, deque())
        while dq and now - dq[0] > _TA_RATE_PER_IP_WINDOW:
            dq.popleft()
        if len(dq) >= _TA_RATE_PER_IP_MAX:
            wait_min = int((_TA_RATE_PER_IP_WINDOW - (now - dq[0])) / 60) + 1
            return False, f"深度分析请求过于频繁(每小时上限 {_TA_RATE_PER_IP_MAX} 次),请约 {wait_min} 分钟后再试"
        # 记录本次
        dq.append(now)
        _ta_global_bucket.append(now)
        # 顺手清理长期不活跃 IP,避免字典无限膨胀
        if len(_ta_ip_buckets) > 5000:
            for k in [k for k, v in _ta_ip_buckets.items() if not v]:
                _ta_ip_buckets.pop(k, None)
    return True, ""


# ============================================================================
# AI建议(intraday_monitor)防滥用:比深度分析便宜但公开站访客每开一只股票就生成一次,
# 也走 DeepSeek。两道防护:① 同股票近 N 分钟已有建议 → 复用不重生成(所有访客共享);
# ② 每 IP + 全局滑动窗口限流(登录站长不限)。
# ============================================================================
_INTRADAY_CACHE_MINUTES = 15    # 同股票 N 分钟内已有 intraday 建议 → 复用,不重新生成
_INTRADAY_PER_IP_MAX = 20       # 每 IP 每小时最多触发多少次生成
_INTRADAY_PER_IP_WINDOW = 3600
_INTRADAY_GLOBAL_MAX = 120      # 全站每小时合计上限(预算闸角色,挡分布式刷)
_INTRADAY_GLOBAL_WINDOW = 3600
_intraday_ip_buckets: dict[str, deque] = {}
_intraday_global_bucket: deque = deque()
_intraday_rate_lock = threading.Lock()


def _intraday_rate_check_and_record(ip: str) -> tuple[bool, str]:
    """intraday 生成的滑动窗口限流:未超记一次返回 (True,"")，超了返回 (False, 提示)。"""
    import time as _t
    now = _t.time()
    with _intraday_rate_lock:
        while _intraday_global_bucket and now - _intraday_global_bucket[0] > _INTRADAY_GLOBAL_WINDOW:
            _intraday_global_bucket.popleft()
        if len(_intraday_global_bucket) >= _INTRADAY_GLOBAL_MAX:
            return False, "当前 AI 建议请求较多,请稍后再试"
        dq = _intraday_ip_buckets.setdefault(ip, deque())
        while dq and now - dq[0] > _INTRADAY_PER_IP_WINDOW:
            dq.popleft()
        if len(dq) >= _INTRADAY_PER_IP_MAX:
            return False, "AI 建议请求过于频繁,请稍后再试"
        dq.append(now)
        _intraday_global_bucket.append(now)
        if len(_intraday_ip_buckets) > 5000:
            for k in [k for k, v in _intraday_ip_buckets.items() if not v]:
                _intraday_ip_buckets.pop(k, None)
    return True, ""


def _recent_intraday_suggestion_exists(db, symbol: str, market: str, minutes: int) -> bool:
    """该股票近 minutes 分钟内是否已有 intraday_monitor 建议(用于缓存复用,免重复调 DeepSeek)。"""
    try:
        from datetime import datetime, timedelta
        from src.web.models import StockSuggestion
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        row = (
            db.query(StockSuggestion.id)
            .filter(
                StockSuggestion.agent_name == "intraday_monitor",
                StockSuggestion.stock_symbol == symbol,
                StockSuggestion.stock_market == market,
                StockSuggestion.created_at >= cutoff,
            )
            .first()
        )
        return row is not None
    except Exception:
        return False


class StockCreate(BaseModel):
    symbol: str
    name: str
    market: str = "CN"


class StockUpdate(BaseModel):
    name: str | None = None


class StockAgentInfo(BaseModel):
    agent_name: str
    schedule: str = ""
    ai_model_id: int | None = None
    notify_channel_ids: list[int] = []


class StockResponse(BaseModel):
    id: int
    symbol: str
    name: str
    market: str
    sort_order: int
    agents: list[StockAgentInfo] = []

    class Config:
        from_attributes = True


class StockAgentItem(BaseModel):
    agent_name: str
    schedule: str = ""
    ai_model_id: int | None = None
    notify_channel_ids: list[int] = []


class StockAgentUpdate(BaseModel):
    agents: list[StockAgentItem]


class StockReorderItem(BaseModel):
    id: int
    sort_order: int


class StockReorderRequest(BaseModel):
    items: list[StockReorderItem]


def _stock_to_response(stock: Stock) -> dict:
    return {
        "id": stock.id,
        "symbol": stock.symbol,
        "name": stock.name,
        "market": stock.market,
        "sort_order": stock.sort_order or 0,
        "agents": [
            {
                "agent_name": sa.agent_name,
                "schedule": sa.schedule or "",
                "ai_model_id": sa.ai_model_id,
                "notify_channel_ids": sa.notify_channel_ids or [],
            }
            for sa in stock.agents
            if infer_agent_kind(sa.agent_name) == AGENT_KIND_WORKFLOW
        ],
    }


@router.get("/markets/status")
def get_market_status():
    """获取各市场的交易状态"""
    from datetime import datetime

    result = []
    for market_code, market_def in MARKETS.items():
        try:
            now = datetime.now(market_def.get_tz())
            is_trading = market_def.is_trading_time()

            # 获取交易时段描述
            sessions_desc = []
            for session in market_def.sessions:
                sessions_desc.append(f"{session.start.strftime('%H:%M')}-{session.end.strftime('%H:%M')}")

            # 判断状态
            weekday = now.weekday()
            current_time = now.time()

            if weekday >= 5:
                status = "closed"
                status_text = "休市（周末）"
            elif is_trading:
                status = "trading"
                status_text = "交易中"
            else:
                # 判断是盘前还是盘后
                first_session = market_def.sessions[0]
                last_session = market_def.sessions[-1]
                if current_time < first_session.start:
                    status = "pre_market"
                    status_text = "盘前"
                elif current_time > last_session.end:
                    status = "after_hours"
                    status_text = "已收盘"
                else:
                    status = "break"
                    status_text = "午间休市"

            result.append({
                "code": market_code.value,
                "name": market_def.name,
                "status": status,
                "status_text": status_text,
                "is_trading": is_trading,
                "sessions": sessions_desc,
                "local_time": now.strftime("%H:%M"),
                "timezone": market_def.timezone,
            })
        except Exception as e:
            # 单个市场获取失败不影响其他市场
            logger.error(f"获取 {market_code.value} 市场状态失败: {e}")
            result.append({
                "code": market_code.value,
                "name": market_def.name,
                "status": "unknown",
                "status_text": "未知",
                "is_trading": False,
                "sessions": [],
                "local_time": "--:--",
                "timezone": market_def.timezone,
                "error": str(e),
            })

    return result


@router.get("/search")
def search(q: str = Query("", min_length=1), market: str = Query("")):
    """模糊搜索股票(代码/名称)"""
    return search_stocks(q, market)


@router.post("/refresh-list")
def refresh_list():
    """刷新股票列表缓存"""
    stocks = refresh_stock_list()
    return {"count": len(stocks)}


@router.get("", response_model=list[StockResponse])
def list_stocks(db: Session = Depends(get_db)):
    stocks = db.query(Stock).order_by(Stock.sort_order.asc(), Stock.id.asc()).all()
    return [_stock_to_response(s) for s in stocks]


@router.get("/quotes")
def get_quotes(db: Session = Depends(get_db)):
    """获取所有自选股的实时行情"""
    stocks = db.query(Stock).all()
    if not stocks:
        return {}

    # 按市场分组
    market_stocks: dict[str, list[Stock]] = {}
    for s in stocks:
        market_stocks.setdefault(s.market, []).append(s)

    quotes = {}
    for market, stock_list in market_stocks.items():
        try:
            market_code = MarketCode(market)
        except ValueError:
            continue

        symbols = [_tencent_symbol(s.symbol, market_code) for s in stock_list]
        try:
            items = _fetch_tencent_quotes(symbols)
            for item in items:
                quotes[item["symbol"]] = {
                    "current_price": item["current_price"],
                    "change_pct": item["change_pct"],
                    "change_amount": item["change_amount"],
                    "prev_close": item["prev_close"],
                }
        except Exception as e:
            logger.error(f"获取 {market} 行情失败: {e}")

    return quotes


@router.post("", response_model=StockResponse)
def create_stock(stock: StockCreate, db: Session = Depends(get_db)):
    existing = db.query(Stock).filter(
        Stock.symbol == stock.symbol, Stock.market == stock.market
    ).first()
    if existing:
        raise HTTPException(400, f"股票 {stock.symbol} 已存在")

    max_order = db.query(func.max(Stock.sort_order)).scalar() or 0
    db_stock = Stock(**stock.model_dump(), sort_order=int(max_order) + 1)
    db.add(db_stock)
    db.commit()
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.put("/reorder")
def reorder_stocks(body: StockReorderRequest, db: Session = Depends(get_db)):
    if not body.items:
        return {"updated": 0}
    ids = [int(x.id) for x in body.items]
    rows = db.query(Stock).filter(Stock.id.in_(ids)).all()
    row_map = {r.id: r for r in rows}
    updated = 0
    for item in body.items:
        row = row_map.get(int(item.id))
        if not row:
            continue
        row.sort_order = int(item.sort_order)
        updated += 1
    db.commit()
    return {"updated": updated}


@router.put("/{stock_id}", response_model=StockResponse)
def update_stock(stock_id: int, stock: StockUpdate, db: Session = Depends(get_db)):
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    for key, value in stock.model_dump(exclude_unset=True).items():
        setattr(db_stock, key, value)

    db.commit()
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.delete("/{stock_id}")
def delete_stock(stock_id: int, db: Session = Depends(get_db)):
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    # 删除股票前，要求先清理持仓，避免误删资产数据。
    has_position = db.query(Position.id).filter(Position.stock_id == stock_id).first()
    if has_position:
        raise HTTPException(400, "该股票存在持仓，请先删除持仓后再删除股票")

    # SQLite 默认可能不启用 FK 级联，手动清理提醒数据避免孤儿记录。
    rule_ids = [
        row[0]
        for row in db.query(PriceAlertRule.id).filter(
            PriceAlertRule.stock_id == stock_id
        ).all()
    ]
    if rule_ids:
        db.query(PriceAlertHit).filter(PriceAlertHit.rule_id.in_(rule_ids)).delete(
            synchronize_session=False
        )
    db.query(PriceAlertHit).filter(PriceAlertHit.stock_id == stock_id).delete(
        synchronize_session=False
    )
    db.query(PriceAlertRule).filter(PriceAlertRule.stock_id == stock_id).delete(
        synchronize_session=False
    )
    db.query(StockAgent).filter(StockAgent.stock_id == stock_id).delete(
        synchronize_session=False
    )

    db.delete(db_stock)
    db.commit()
    return {"ok": True}


@router.put("/{stock_id}/agents", response_model=StockResponse)
def update_stock_agents(stock_id: int, body: StockAgentUpdate, db: Session = Depends(get_db)):
    """更新股票关联的 Agent 列表（含调度配置和 AI/通知覆盖）"""
    db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not db_stock:
        raise HTTPException(404, "股票不存在")

    for item in body.agents:
        agent = db.query(AgentConfig).filter(AgentConfig.name == item.agent_name).first()
        if not agent:
            raise HTTPException(400, f"Agent {item.agent_name} 不存在")
        agent_kind = (agent.kind or "").strip() or infer_agent_kind(agent.name)
        if agent_kind != AGENT_KIND_WORKFLOW:
            raise HTTPException(400, f"Agent {item.agent_name} 为内部能力，不支持绑定到股票")

    # 清除旧关联，重建
    db.query(StockAgent).filter(StockAgent.stock_id == stock_id).delete()
    for item in body.agents:
        db.add(StockAgent(
            stock_id=stock_id,
            agent_name=item.agent_name,
            schedule=item.schedule,
            ai_model_id=item.ai_model_id,
            notify_channel_ids=item.notify_channel_ids,
        ))

    db.commit()
    db.refresh(db_stock)
    return _stock_to_response(db_stock)


@router.post("/{stock_id}/agents/{agent_name}/trigger")
async def trigger_stock_agent(
    stock_id: int,
    agent_name: str,
    request: Request,
    bypass_throttle: bool = False,
    bypass_market_hours: bool = False,
    allow_unbound: bool = False,
    wait: bool = False,
    force_refresh: bool = False,
    symbol: str = Query(""),
    market: str = Query("CN"),
    name: str = Query(""),
    db: Session = Depends(get_db),
):
    """手动触发单只股票 Agent。

    - 正常模式：传有效 stock_id
    - 无绑定模式：stock_id<=0 且传 symbol/market（需 allow_unbound=true）
    - 无绑定模式默认禁用通知（仅生成建议）
    - 默认异步执行（立即返回），传 wait=true 可同步等待结果
    """
    sa = None
    trigger_stock = None
    suppress_notify = stock_id <= 0

    if stock_id > 0:
        db_stock = db.query(Stock).filter(Stock.id == stock_id).first()
        if not db_stock:
            raise HTTPException(404, "股票不存在")

        sa = db.query(StockAgent).filter(
            StockAgent.stock_id == stock_id, StockAgent.agent_name == agent_name
        ).first()
        if not sa and not allow_unbound:
            raise HTTPException(400, f"股票未关联 Agent {agent_name}")
        if not sa and allow_unbound:
            # 允许无绑定触发时，至少确保 Agent 存在。
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if not agent:
                raise HTTPException(400, f"Agent {agent_name} 不存在")
        trigger_stock = db_stock
    else:
        symbol = (symbol or "").strip()
        if not symbol:
            raise HTTPException(400, "当 stock_id<=0 时，symbol 不能为空")
        if not allow_unbound:
            raise HTTPException(400, "当 stock_id<=0 时，需设置 allow_unbound=true")

        market = (market or "CN").strip().upper() or "CN"
        name = (name or "").strip() or symbol
        db_stock = db.query(Stock).filter(
            Stock.symbol == symbol, Stock.market == market
        ).first()
        if db_stock:
            sa = db.query(StockAgent).filter(
                StockAgent.stock_id == db_stock.id, StockAgent.agent_name == agent_name
            ).first()
            trigger_stock = db_stock
        else:
            # 不落库：用于详情弹窗未持仓且未关注股票的一次性分析。
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if not agent:
                raise HTTPException(400, f"Agent {agent_name} 不存在")
            trigger_stock = SimpleNamespace(
                id=0,
                symbol=symbol,
                name=name,
                market=market,
            )

    logger.info(
        f"手动触发 Agent {agent_name} - {trigger_stock.name}({trigger_stock.symbol})"
    )

    from server import trigger_agent_for_stock
    import time as _time

    # 幂等性兜底:TradingAgents 单次 3-5 分钟,前端误操作/双击可能并发触发同一标的。
    # 后端先查"该 symbol 是否有真正在跑的 TA 任务",有则返回现有 trace_id(不启新任务)。
    # force_refresh=true 时跳过去重,允许用户主动强制重跑(老任务自然终止,新 trace_id)。
    if agent_name == "tradingagents" and not force_refresh:
        from src.web.api.agents import find_active_tradingagents_trace
        existing_trace = find_active_tradingagents_trace(db, trigger_stock.symbol)
        if existing_trace:
            logger.info(
                f"[trigger 幂等] {trigger_stock.symbol} 已有在跑任务 trace={existing_trace},"
                f"复用而非启新任务"
            )
            return {
                "queued": False,
                "trace_id": existing_trace,
                "message": "已有正在执行的深度分析,返回现有任务进度",
                "deduplicated": True,
            }

        # 同日完成缓存:当天已有完成的深度分析报告 → 直接复用,不重跑。
        # 省 token/服务器资源,并防止直接打 API 恶意刷。用户要最新可传 force_refresh=true。
        try:
            from src.core.analysis_history import get_analysis
            from datetime import date as _date
            todays = get_analysis("tradingagents", trigger_stock.symbol, _date.today())
            if todays:
                logger.info(
                    f"[trigger 同日缓存] {trigger_stock.symbol} 当天已有完成报告,直接复用,不重跑"
                )
                return {
                    "queued": False,
                    "trace_id": None,
                    "message": "当天已有分析报告,直接展示",
                    "deduplicated": True,
                    "cached": True,
                }
        except Exception as _e:
            logger.warning(f"[trigger 同日缓存] 检查失败,忽略(继续正常触发): {_e}")

    # === 防滥用闸:走到这里说明要真正启一次新分析(缓存/去重未命中,或 force_refresh)===
    if agent_name == "tradingagents":
        # 1) 预算闸:本月累计成本超上限直接拒绝,硬性保护站长 DeepSeek 账单。对所有人生效。
        try:
            _agent_cfg = (
                db.query(AgentConfig).filter(AgentConfig.name == "tradingagents").first()
            )
            _cfg = (_agent_cfg.config or {}) if _agent_cfg else {}
            if str(_cfg.get("over_budget_action", "reject")) == "reject":
                from src.agents.tradingagents.cost_tracker import check_budget
                _b = check_budget(float(_cfg.get("monthly_budget_usd", 10.0)), "tradingagents")
                if _b.get("exceeded"):
                    logger.warning(
                        f"[trigger 预算闸] 本月预算已用尽 used=${_b['used']} limit=${_b['limit']},拒绝新任务"
                    )
                    raise HTTPException(
                        status_code=429,
                        detail=f"本月深度分析额度已用尽(已用 ${_b['used']:.2f} / 上限 ${_b['limit']:.2f}),请稍后再试",
                    )
        except HTTPException:
            raise
        except Exception as _e:
            logger.warning(f"[trigger 预算闸] 检查异常,放行: {_e}")

        # 2) 每 IP 限流:登录站长不限;匿名访客受滑动窗口约束,防单人换股票刷爆预算。
        if not _is_owner_request(request):
            _ip = _client_ip(request)
            _ok, _msg = _ta_rate_check_and_record(_ip)
            if not _ok:
                logger.info(f"[trigger 限流] IP={_ip} 被限流: {_msg}")
                raise HTTPException(status_code=429, detail=_msg)

    # === AI建议(intraday_monitor)防滥用:缓存复用(A) + 每IP/全局限流(B)===
    if agent_name == "intraday_monitor" and not force_refresh:
        # A) 缓存复用:该股票近 N 分钟已有建议 → 不重新生成,前端读现成的即可(所有访客共享一次)
        if _recent_intraday_suggestion_exists(
            db, trigger_stock.symbol, trigger_stock.market, _INTRADAY_CACHE_MINUTES
        ):
            logger.info(
                f"[trigger intraday缓存] {trigger_stock.symbol} 近{_INTRADAY_CACHE_MINUTES}分钟已有建议,复用不重生成"
            )
            return {
                "queued": False,
                "trace_id": None,
                "message": "近期已有 AI 建议,直接展示",
                "deduplicated": True,
                "cached": True,
            }
        # B) 每 IP + 全局限流(登录站长不限)
        if not _is_owner_request(request):
            _ok, _msg = _intraday_rate_check_and_record(_client_ip(request))
            if not _ok:
                logger.info(f"[trigger intraday限流] IP={_client_ip(request)} 被限流: {_msg}")
                raise HTTPException(status_code=429, detail=_msg)

    # 预生成 trace_id,返回给前端用于轮询进度
    trace_id = f"man-{agent_name}-{trigger_stock.symbol}-{int(_time.time() * 1000)}"

    # 立刻写一条"任务已触发"进度日志,保证前端 polling 第一拍就能看到 running。
    # 否则 trigger_agent_for_stock 内部要先 await agent.collect()(美股拉 yfinance 数据
    # 可能 30s+),期间没有任何 ta_progress 日志 → 前端 progress 接口返回 not_found
    # → 60s grace 过后前端 reset 到 idle,看起来像"进度卡死自动退回"。
    if agent_name == "tradingagents":
        try:
            from src.core.log_context import log_context
            with log_context(
                trace_id=trace_id,
                agent_name="tradingagents",
                event="ta_progress",
                tags={"stage": "task_triggered", "action": "triggered"},
            ):
                logger.info(
                    f"[TA] 任务已触发 - {trigger_stock.symbol} (trace={trace_id})"
                )
        except Exception as e:
            logger.warning(f"[TA] 写触发日志失败,不影响主流程: {e}")

    if not wait:
        # 异步模式：后台执行，立即返回
        sa_id = sa.id if sa else None

        def _runner():
            try:
                asyncio.run(trigger_agent_for_stock(
                    agent_name,
                    trigger_stock,
                    stock_agent_id=sa_id,
                    bypass_throttle=bypass_throttle,
                    bypass_market_hours=bypass_market_hours,
                    suppress_notify=suppress_notify,
                    trace_id=trace_id,
                    force_refresh=force_refresh,
                ))
                logger.info(f"Agent {agent_name} 后台执行完成 - {trigger_stock.symbol}")
            except Exception:
                logger.exception(f"Agent {agent_name} 后台执行失败 - {trigger_stock.symbol}")

        t = threading.Thread(
            target=_runner,
            name=f"stock-trigger-{agent_name}-{trigger_stock.symbol}",
            daemon=True,
        )
        t.start()
        return {"queued": True, "trace_id": trace_id, "message": "已提交后台执行"}

    # 同步模式：等待结果返回
    try:
        result = await trigger_agent_for_stock(
            agent_name,
            trigger_stock,
            stock_agent_id=sa.id if sa else None,
            bypass_throttle=bypass_throttle,
            bypass_market_hours=bypass_market_hours,
            suppress_notify=suppress_notify,
            trace_id=trace_id,
            force_refresh=force_refresh,
        )
        logger.info(f"Agent {agent_name} 执行完成 - {trigger_stock.symbol}")
        return {
            "result": result,
            "trace_id": trace_id,
            "code": int(result.get("code", 0)),
            "success": bool(result.get("success", True)),
            "message": result.get("message", "ok"),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Agent {agent_name} 执行失败 - {trigger_stock.symbol}: {e}")
        raise HTTPException(500, f"Agent 执行失败: {e}")
