from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from src.web.api import (
    stocks,
    agents,
    settings,
    logs,
    providers,
    channels,
    datasources,
    accounts,
    history,
    news,
    market,
    auth,
    suggestions,
    quotes,
    klines,
    templates,
    feedback,
    discovery,
    price_alerts,
    context,
    recommendations,
    dashboard,
    paper_trading,
    chat,
)
from src.web.api import factors
from src.web.api import health
from src.web.api import insights
from src.web.api.auth import get_current_user
from src.web.api.settings import get_app_version
from src.web.response import ResponseWrapperMiddleware

app = FastAPI(
    title="PanWatch API",
    version="0.1.0",
    redirect_slashes=False,  # 避免重定向丢失 Authorization header
)

app.add_middleware(ResponseWrapperMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 认证路由（无需登录）
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
# 市场指数（公共数据，无需登录）
app.include_router(market.router, prefix="/api/market", tags=["market"])

# ============================================================================
# 公开路由(匿名可访问)——只含股票行情/深度分析等非敏感数据,无任何密钥。
# 匿名公开版核心:访客搜股票 → 看行情/K线 → 触发 AI 深度分析。
# ⚠️ 严禁把含密钥或个人数据的路由(providers/settings/datasources/channels
#    /logs/accounts/history/paper-trading/price-alerts 等)放进这一组。
# ============================================================================
app.include_router(stocks.router, prefix="/api/stocks", tags=["stocks"])
app.include_router(quotes.router, prefix="/api/quotes", tags=["quotes"])
app.include_router(klines.router, prefix="/api/klines", tags=["klines"])
app.include_router(insights.router, prefix="/api/insights", tags=["insights"])
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(news.router, prefix="/api/news", tags=["news"])
app.include_router(suggestions.router, prefix="/api/suggestions", tags=["suggestions"])
app.include_router(discovery.router, prefix="/api/discovery", tags=["discovery"])
app.include_router(
    recommendations.router, prefix="/api/recommendations", tags=["recommendations"]
)
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(factors.router, prefix="/api/factors", tags=["factors"])
# chat 公开供匿名用「问 AI / AI 助手」。send_message 端点内做每 IP 限流(烧 DeepSeek),
# list_conversations 对匿名返回空(对话无 user_id,防跨访客串对话)。
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])

# ============================================================================
# 需要登录的路由(仅站长)——含 AI 服务商密钥、站点设置、数据源配置、
# 通知渠道、审计日志、个人持仓/模拟盘/提醒等。
# providers 会在响应里返回 DeepSeek api_key,必须始终受登录保护。
# ============================================================================
protected = [Depends(get_current_user)]
app.include_router(
    accounts.router, prefix="/api", tags=["accounts"], dependencies=protected
)
app.include_router(
    providers.router,
    prefix="/api/providers",
    tags=["providers"],
    dependencies=protected,
)
app.include_router(
    channels.router, prefix="/api/channels", tags=["channels"], dependencies=protected
)
app.include_router(
    datasources.router,
    prefix="/api/datasources",
    tags=["datasources"],
    dependencies=protected,
)
app.include_router(
    settings.router, prefix="/api/settings", tags=["settings"], dependencies=protected
)
app.include_router(
    logs.router, prefix="/api/logs", tags=["logs"], dependencies=protected
)
app.include_router(
    history.router, prefix="/api", tags=["history"], dependencies=protected
)
app.include_router(
    context.router, prefix="/api", tags=["context"], dependencies=protected
)
app.include_router(
    templates.router,
    prefix="/api/templates",
    tags=["templates"],
    dependencies=protected,
)
app.include_router(
    feedback.router,
    prefix="/api/feedback",
    tags=["feedback"],
    dependencies=protected,
)
app.include_router(
    price_alerts.router,
    prefix="/api/price-alerts",
    tags=["price-alerts"],
    dependencies=protected,
)
app.include_router(
    health.router,
    prefix="/api/health",
    tags=["health"],
    dependencies=protected,
)
app.include_router(
    paper_trading.router,
    prefix="/api/paper-trading",
    tags=["paper-trading"],
    dependencies=protected,
)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/version")
async def version():
    """获取应用版本号（公开接口）"""
    return {"version": get_app_version()}
