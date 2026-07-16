import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Activity } from 'lucide-react'
import { discoveryApi, type MoverItem, type MoversResponse } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import StockInsightModal from '@panwatch/biz-ui/components/stock-insight-modal'

type MarketCode = 'CN' | 'HK' | 'US'

const MARKET_LABEL: Record<MarketCode, string> = { CN: 'A股', HK: '港股', US: '美股' }

/** 涨=红 跌=绿(A股习惯) */
function moveColor(v?: number | null): string {
  if (v == null) return 'text-muted-foreground'
  return v > 0 ? 'text-rose-500' : v < 0 ? 'text-emerald-500' : 'text-muted-foreground'
}

/** 异动标签配色:涨停/大涨=红,跌停/大跌=绿,放量=琥珀,高换手=靛蓝 */
const TAG_CLS: Record<string, string> = {
  涨停: 'bg-rose-500/15 text-rose-600 border-rose-500/30',
  大涨: 'bg-rose-500/10 text-rose-500 border-rose-500/20',
  跌停: 'bg-emerald-500/15 text-emerald-600 border-emerald-500/30',
  大跌: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20',
  放量: 'bg-amber-500/15 text-amber-600 border-amber-500/30',
  高换手: 'bg-indigo-500/15 text-indigo-600 border-indigo-500/30',
}

function pct(v?: number | null): string {
  if (v == null || !isFinite(v)) return '--'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function compactNum(v?: number | null): string {
  if (v == null || !isFinite(v)) return '--'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return String(Math.round(v))
}

export default function MoversPage() {
  const [market, setMarket] = useState<MarketCode>('CN')
  const [data, setData] = useState<MoversResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [modal, setModal] = useState<{ open: boolean; symbol: string; market: string; name: string }>({
    open: false, symbol: '', market: 'CN', name: '',
  })

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try {
      const res = await discoveryApi.listMovers({ market, limit: 30 })
      setData(res)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '异动数据加载失败')
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [market])

  useEffect(() => { load() }, [load])

  // 盘中数据分钟级变化:每 60 秒自动刷新一次(后端有 45s 缓存,不会打爆数据源)
  useEffect(() => {
    const t = setInterval(() => { load() }, 60_000)
    return () => clearInterval(t)
  }, [load])

  const openStock = (it: MoverItem) => {
    setModal({ open: true, symbol: it.symbol, market: it.market || market, name: it.name })
  }

  const items = data?.items || []

  return (
    <div className="px-4 md:px-6 pb-6">
      <div className="mb-3 flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-[20px] md:text-[22px] font-bold text-foreground tracking-tight flex items-center gap-2">
            <Activity className="h-5 w-5 text-primary" />
            今日异动
          </h1>
          <p className="text-[12px] md:text-[13px] text-muted-foreground mt-0.5">
            涨跌幅、量比、换手率异常的股票 · 点开可看 K 线 / 新闻 / 公告 / AI 分析
            {data?.updated_at ? ` · 更新于 ${data.updated_at}` : ''}
            {data?.stale ? ' · 行情源繁忙，展示稍早数据' : ''}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={market} onValueChange={(v) => setMarket(v as MarketCode)}>
            <SelectTrigger className="h-8 w-[92px] text-[12px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(['CN', 'HK', 'US'] as MarketCode[]).map((m) => (
                <SelectItem key={m} value={m}>{MARKET_LABEL[m]}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button variant="outline" size="sm" className="h-8 px-2.5" onClick={() => load()} disabled={loading}>
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </div>

      {err && (
        <div className="card p-4 text-[13px] text-muted-foreground">{err}</div>
      )}

      {!err && items.length === 0 && (
        <div className="card p-8 text-center text-[13px] text-muted-foreground">
          {loading ? '加载异动数据中…' : '暂无异动数据（非交易时段或数据源暂不可用）'}
        </div>
      )}

      {items.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {items.map((it) => (
            <button
              key={`${it.market}:${it.symbol}`}
              type="button"
              onClick={() => openStock(it)}
              className="card p-4 text-left transition hover:border-primary/40 hover:shadow-md"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-[15px] font-semibold text-foreground truncate">{it.name}</div>
                  <div className="font-mono text-[11px] text-muted-foreground mt-0.5">
                    {it.market}:{it.symbol}
                  </div>
                </div>
                <div className="text-right shrink-0">
                  <div className={`text-[17px] font-bold font-mono ${moveColor(it.change_pct)}`}>
                    {pct(it.change_pct)}
                  </div>
                  <div className="text-[12px] font-mono text-muted-foreground">{it.price ?? '--'}</div>
                </div>
              </div>

              {it.tags?.length > 0 && (
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {it.tags.map((t) => (
                    <span
                      key={t}
                      className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${TAG_CLS[t] || 'bg-accent/20 text-muted-foreground border-border/40'}`}
                    >
                      {t}
                    </span>
                  ))}
                </div>
              )}

              <div className="mt-2.5 grid grid-cols-3 gap-2 text-[11px]">
                <div className="rounded bg-accent/15 px-2 py-1.5">
                  <div className="text-[10px] text-muted-foreground">量比</div>
                  <div className="font-mono">{it.volume_ratio ?? '--'}</div>
                </div>
                <div className="rounded bg-accent/15 px-2 py-1.5">
                  <div className="text-[10px] text-muted-foreground">换手率</div>
                  <div className="font-mono">{it.turnover_rate != null ? `${it.turnover_rate}%` : '--'}</div>
                </div>
                <div className="rounded bg-accent/15 px-2 py-1.5">
                  <div className="text-[10px] text-muted-foreground">成交额</div>
                  <div className="font-mono">{compactNum(it.turnover)}</div>
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      <p className="mt-4 text-[11px] text-muted-foreground/70 leading-relaxed">
        异动依据公开行情数据（涨跌幅 / 量比 / 换手率）由本站规则自动识别，仅为行情信息展示，不构成任何投资建议。
      </p>

      <StockInsightModal
        open={modal.open}
        onOpenChange={(o) => setModal((m) => ({ ...m, open: o }))}
        symbol={modal.symbol}
        market={modal.market}
        stockName={modal.name}
      />
    </div>
  )
}
