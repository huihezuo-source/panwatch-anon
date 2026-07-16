import { fetchAPI } from './client'

type QueryValue = string | number | boolean | null | undefined

function withQuery(path: string, params: Record<string, QueryValue>): string {
  const q = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === undefined || v === null) return
    const sv = String(v).trim()
    if (!sv) return
    q.set(k, sv)
  })
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

export interface HotStockItem {
  symbol: string
  market: string
  name: string
  price: number | null
  change_pct: number | null
  turnover: number | null
  volume?: number | null
}

export interface HotBoardItem {
  code: string
  name: string
  change_pct: number | null
  turnover: number | null
}

/** 今日异动:涨/跌幅榜合并 + 规则打标(涨停/放量/高换手等) */
export interface MoverItem {
  symbol: string
  market: string
  name: string
  price: number | null
  change_pct: number | null
  turnover: number | null
  volume?: number | null
  turnover_rate?: number | null
  volume_ratio?: number | null
  direction: 'up' | 'down'
  tags: string[]
  /** 连板数(以最新日K为止的连续涨停数);后台按天为 Top N 生成,可能暂缺 */
  streak_count?: number
  limit_ups_20d?: number
  /** AI 题材归因(本站自有原创,来源:该股近期公告+新闻) */
  analysis_tags?: string
  analysis_text?: string
}

export interface MoversResponse {
  market: string
  updated_at: string
  items: MoverItem[]
  /** true = 实时源被限流,返回的是稍旧的缓存数据 */
  stale?: boolean
}

export const discoveryApi = {
  listMovers: (params?: { market?: 'CN' | 'HK' | 'US'; limit?: number }) =>
    fetchAPI<MoversResponse>(
      withQuery('/discovery/movers', {
        market: params?.market,
        limit: params?.limit,
      })
    ),

  listHotStocks: (params?: {
    market?: 'CN' | 'HK' | 'US'
    mode?: 'turnover' | 'gainers' | 'for_you'
    limit?: number
  }) =>
    fetchAPI<HotStockItem[]>(
      withQuery('/discovery/stocks', {
        market: params?.market,
        mode: params?.mode,
        limit: params?.limit,
      })
    ),

  listHotBoards: (params?: {
    market?: 'CN' | 'HK' | 'US'
    mode?: 'gainers' | 'turnover' | 'hot'
    limit?: number
  }) =>
    fetchAPI<HotBoardItem[]>(
      withQuery('/discovery/boards', {
        market: params?.market,
        mode: params?.mode,
        limit: params?.limit,
      })
    ),

  listBoardStocks: (
    boardCode: string,
    params?: {
      mode?: 'gainers' | 'turnover' | 'hot'
      limit?: number
    }
  ) =>
    fetchAPI<HotStockItem[]>(
      withQuery(`/discovery/boards/${encodeURIComponent(boardCode)}/stocks`, {
        mode: params?.mode,
        limit: params?.limit,
      })
    ),
}

