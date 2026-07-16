import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw, Activity, ChevronDown } from 'lucide-react'
import { discoveryApi, type MoverItem, type MoverGroup, type MoversResponse } from '@panwatch/api'
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

function MoverCard({ it, onOpen }: { it: MoverItem; onOpen: (it: MoverItem) => void }) {
  return (
    <button
      type="button"
      onClick={() => onOpen(it)}
      className="card p-4 text-left transition hover:border-primary/40 hover:shadow-md"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-[15px] font-semibold text-foreground truncate">{it.name}</div>
          <div className="font-mono text-[11px] text-muted-foreground mt-0.5">{it.market}:{it.symbol}</div>
        </div>
        <div className="text-right shrink-0">
          <div className={`text-[17px] font-bold font-mono ${moveColor(it.change_pct)}`}>{pct(it.change_pct)}</div>
          <div className="text-[12px] font-mono text-muted-foreground">{it.price ?? '--'}</div>
        </div>
      </div>

      {(it.tags?.length > 0 || (it.streak_count ?? 0) >= 2) && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {(it.streak_count ?? 0) >= 2 && (
            <span className="rounded border border-rose-500/40 bg-rose-500/15 px-1.5 py-0.5 text-[10px] font-bold text-rose-600">
              {it.streak_count}连板
            </span>
          )}
          {it.tags.map((t) => (
            <span key={t} className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${TAG_CLS[t] || 'bg-accent/20 text-muted-foreground border-border/40'}`}>
              {t}
            </span>
          ))}
          {(it.limit_ups_20d ?? 0) >= 3 && (
            <span className="rounded border border-border/40 bg-accent/20 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              20日{it.limit_ups_20d}板
            </span>
          )}
        </div>
      )}

      {it.analysis_tags && (
        <div className="mt-2.5 rounded-lg border border-border/30 bg-accent/10 p-2.5">
          <div className="text-[12px] font-medium text-foreground">{it.analysis_tags}</div>
          {it.analysis_text && (
            <div className="mt-1 whitespace-pre-line text-[11px] leading-relaxed text-muted-foreground line-clamp-3">
              {it.analysis_text}
            </div>
          )}
          <div className="mt-1 text-[10px] text-primary/70">点开看完整解析 →</div>
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
  )
}

function GroupSection({ group, onOpen }: { group: MoverGroup; onOpen: (it: MoverItem) => void }) {
  const [open, setOpen] = useState(true)
  // 组内最大涨跌幅,给板块头一个红/绿色调
  const topPct = useMemo(
    () => group.items.reduce((m, x) => (Math.abs(x.change_pct || 0) > Math.abs(m) ? (x.change_pct || 0) : m), 0),
    [group.items]
  )
  return (
    <section className="mb-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 py-2 border-l-2 border-primary/60 pl-3"
      >
        <span className="text-[15px] font-bold text-foreground">{group.name}</span>
        <span className={`text-[13px] font-bold font-mono ${moveColor(topPct)}`}>{group.count}</span>
        <ChevronDown className={`ml-auto h-4 w-4 text-muted-foreground transition-transform ${open ? '' : '-rotate-90'}`} />
      </button>
      {open && (
        <div className="mt-2 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {group.items.map((it) => (
            <MoverCard key={`${it.market}:${it.symbol}`} it={it} onOpen={onOpen} />
          ))}
        </div>
      )}
    </section>
  )
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
      const res = await discoveryApi.listMovers({ market, limit: 40 })
      setData(res)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '异动数据加载失败')
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [market])

  useEffect(() => { load() }, [load])

  // 盘中分钟级:每 60s 自动刷新(后端 45s 缓存兜底,不打爆数据源)
  useEffect(() => {
    const t = setInterval(() => { load() }, 60_000)
    return () => clearInterval(t)
  }, [load])

  const openStock = (it: MoverItem) => {
    setModal({ open: true, symbol: it.symbol, market: it.market || market, name: it.name })
  }

  // 优先用分组;没有 groups 时退回扁平(旧缓存兜底)
  const groups: MoverGroup[] = data?.groups?.length
    ? data.groups
    : data?.items?.length
      ? [{ name: '全部', count: data.items.length, items: data.items }]
      : []
  const total = data?.items?.length || 0

  return (
    <div className="px-4 md:px-6 pb-6">
      <div className="mb-3 flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-[20px] md:text-[22px] font-bold text-foreground tracking-tight flex items-center gap-2">
            <Activity className="h-5 w-5 text-primary" />
            今日异动
            {total > 0 && <span className="text-[13px] font-normal text-muted-foreground">· {total}</span>}
          </h1>
          <p className="text-[12px] md:text-[13px] text-muted-foreground mt-0.5">
            按板块分组 · 涨跌幅/量比/换手率异常的股票 · 点开看 K线/新闻/公告/AI解析
            {data?.updated_at ? ` · 更新于 ${data.updated_at}` : ''}
            {data?.stale ? ' · 行情源繁忙，展示稍早数据' : ''}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={market} onValueChange={(v) => setMarket(v as MarketCode)}>
            <SelectTrigger className="h-8 w-[92px] text-[12px]"><SelectValue /></SelectTrigger>
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

      {err && <div className="card p-4 text-[13px] text-muted-foreground">{err}</div>}

      {!err && groups.length === 0 && (
        <div className="card p-8 text-center text-[13px] text-muted-foreground">
          {loading ? '加载异动数据中…' : '暂无异动数据（非交易时段或数据源暂不可用）'}
        </div>
      )}

      {groups.map((g) => (
        <GroupSection key={g.name} group={g} onOpen={openStock} />
      ))}

      {groups.length > 0 && (
        <p className="mt-4 text-[11px] text-muted-foreground/70 leading-relaxed">
          异动依据公开行情数据（涨跌幅 / 量比 / 换手率）由本站规则自动识别，板块按所属行业分组，仅为行情信息展示，不构成任何投资建议。
        </p>
      )}

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
