import { useState, useEffect, useRef } from 'react'
import { Routes, Route, NavLink, useLocation } from 'react-router-dom'
import { TrendingUp, Bot, Settings, List, Database, Clock, LayoutDashboard, BellRing, Sparkles, Activity } from 'lucide-react'
import { useTheme } from '@/hooks/use-theme'
import { appApi, fetchAPI, isAuthenticated } from '@panwatch/api'
import DashboardPage from '@/pages/Dashboard'
import OpportunitiesPage from '@/pages/Opportunities'
import StocksPage from '@/pages/Stocks'
import AgentsPage from '@/pages/Agents'
import SettingsPage from '@/pages/Settings'
import DataSourcesPage from '@/pages/DataSources'
import HistoryPage from '@/pages/History'
import AnalysisDetailPage from '@/pages/AnalysisDetail'
import PriceAlertsPage from '@/pages/PriceAlerts'
import PaperTradingPage from '@/pages/PaperTrading'
import LoginPage from '@/pages/Login'
import LogsModal from '@panwatch/biz-ui/components/logs-modal'
import AmbientBackground from '@panwatch/biz-ui/components/AmbientBackground'
import ChatWidget from '@/components/ChatWidget'
import AccountMenu from '@/components/AccountMenu'
import SelfCheckModal from '@/components/SelfCheckModal'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: '首页' },
  { to: '/portfolio', icon: List, label: '持仓' },
  { to: '/opportunities', icon: Sparkles, label: '机会' },
  { to: '/paper-trading', icon: Activity, label: '模拟盘' },
  { to: '/alerts', icon: BellRing, label: '提醒' },
  { to: '/agents', icon: Bot, label: 'Agent' },
  { to: '/history', icon: Clock, label: '历史' },
  { to: '/datasources', icon: Database, label: '数据源' },
  { to: '/settings', icon: Settings, label: '设置' },
]
// 匿名公开版:只保留公共功能(首页/机会),隐藏需登录的个人功能(持仓/模拟盘/提醒)及管理项(Agent/历史/数据源/设置)
const PUBLIC_NAV_PATHS = ['/', '/opportunities']
const publicNavItems = navItems.filter((n) => PUBLIC_NAV_PATHS.includes(n.to))
const desktopPrimaryNavItems = publicNavItems
const desktopMoreNavItems: typeof navItems = []
const mobilePrimaryNavItems = publicNavItems
const mobileMoreNavItems: typeof navItems = []

function App() {
  const { mode, setMode } = useTheme()
  const location = useLocation()
  const [version, setVersion] = useState('')
  const [logsOpen, setLogsOpen] = useState(false)
  const [selfCheckOpen, setSelfCheckOpen] = useState(false)
  const [upgradeOpen, setUpgradeOpen] = useState(false)
  const [upgradeInfo, setUpgradeInfo] = useState<{ latest: string; url: string } | null>(null)
  const checkedUpdateRef = useRef(false)

  useEffect(() => {
    appApi.version()
      .then(data => setVersion(data?.version || ''))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (checkedUpdateRef.current) return
    if (!isAuthenticated()) return
    const current = String(version || '').trim()
    if (!current || current === 'dev') return
    checkedUpdateRef.current = true

    fetchAPI<any>('/settings/update-check')
      .then((res) => {
        const latest = String(res?.latest_version || '').trim()
        const shouldOpen = !!res?.update_available && !!latest
        if (!shouldOpen) return
        const dismissed = localStorage.getItem('panwatch_upgrade_dismissed_version') || ''
        if (dismissed === latest) return
        setUpgradeInfo({ latest, url: String(res?.release_url || 'https://github.com/sunxiao0721/PanWatch/releases') })
        setUpgradeOpen(true)
      })
      .catch(() => {})
  }, [version])

  // 登录页面不显示导航
  if (location.pathname === '/login') {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
      </Routes>
    )
  }

  return (
    // 匿名公开版:不再用 RequireAuth 挡整站,访客直接进首页搜股票/看深度分析。
    // 站长仍可手动访问 /login 登录以使用设置/数据源等仅站长功能(后端受保护)。
    <>
    <div className="min-h-screen pb-16 md:pb-0 relative overflow-x-clip bg-background">
      <AmbientBackground />
      {/* Desktop Floating Nav */}
      <div className="sticky top-0 z-50 px-4 md:px-6 pt-3 md:pt-4 pb-2 hidden md:block">
        <header className="card px-4 md:px-5">
          <div className="h-14 flex items-center justify-between">
            {/* Logo */}
            <NavLink to="/" className="flex items-center gap-2.5 group">
              <div className="w-8 h-8 rounded-2xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm">
                <TrendingUp className="w-4 h-4 text-white" />
              </div>
              <span className="text-[15px] font-bold text-foreground">Huipingce</span>
              {version && <span className="text-[11px] text-muted-foreground/60 font-normal">v{version}</span>}
            </NavLink>

            {/* Nav Links */}
            <nav className="flex items-center gap-1">
              {desktopPrimaryNavItems.map(({ to, icon: Icon, label }) => {
                const isActive = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
                return (
                  <NavLink
                    key={to}
                    to={to}
                    className="relative"
                  >
                    <span
                      className={`absolute inset-0 rounded-xl transition-all ${
                        isActive
                          ? 'bg-[linear-gradient(135deg,hsl(var(--primary)/0.14),hsl(var(--primary)/0.04),hsl(var(--success)/0.06))] ring-1 ring-primary/20 shadow-[0_8px_24px_-18px_hsl(var(--primary)/0.55)]'
                          : 'bg-transparent'
                      }`}
                    />
                    <span
                      className={`relative px-3.5 py-2 rounded-xl text-[13px] font-medium transition-all flex items-center gap-1.5 ${
                        isActive
                          ? 'text-foreground'
                          : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                      }`}
                    >
                      <Icon className={`w-4 h-4 ${isActive ? 'text-primary' : ''}`} />
                      {label}
                    </span>
                  </NavLink>
                )
              })}
            </nav>

            {/* 匿名公开版:隐藏 GitHub / 日志 / 账户设置等管理入口 */}
            {false && (
            <div className="flex items-center gap-1.5 px-1.5 py-1 rounded-2xl bg-accent/20 border border-border/40">
              <AccountMenu
                navItems={desktopMoreNavItems}
                mode={mode}
                onSetMode={setMode}
                onOpenSelfCheck={() => setSelfCheckOpen(true)}
              />
            </div>
            )}
          </div>
        </header>
      </div>

      {/* Mobile Top Bar */}
      <div className="sticky top-0 z-50 px-4 pt-[max(0.75rem,env(safe-area-inset-top))] pb-2 md:hidden">
        <header className="card px-4">
          <div className="h-12 flex items-center justify-between">
            <NavLink to="/" className="flex items-center gap-2 group">
              <div className="w-7 h-7 rounded-xl bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm">
                <TrendingUp className="w-3.5 h-3.5 text-white" />
              </div>
              <span className="text-[14px] font-bold text-foreground">Huipingce</span>
              {version && <span className="text-[10px] text-muted-foreground/60 font-normal">v{version}</span>}
            </NavLink>
            {/* 匿名公开版:隐藏 GitHub / 日志 / 账户设置等管理入口 */}
            {false && (
            <div className="flex items-center gap-1.5 px-1.5 py-1 rounded-2xl bg-accent/20 border border-border/40">
              <AccountMenu
                size="sm"
                navItems={mobileMoreNavItems}
                mode={mode}
                onSetMode={setMode}
                onOpenSelfCheck={() => setSelfCheckOpen(true)}
              />
            </div>
            )}
          </div>
        </header>
      </div>

      {/* Mobile Bottom Nav */}
      <nav className="fixed bottom-0 left-0 right-0 z-50 md:hidden bg-card border-t border-border px-2 pb-[env(safe-area-inset-bottom)]">
        <div className="flex items-center justify-around h-14">
          {mobilePrimaryNavItems.map(({ to, icon: Icon, label }) => {
            const isActive = to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)
            return (
              <NavLink
                key={to}
                to={to}
                className={`flex flex-col items-center justify-center gap-0.5 px-2 py-1.5 rounded-xl transition-all min-w-[56px] ${
                  isActive
                    ? 'text-primary bg-primary/8 ring-1 ring-primary/15'
                    : 'text-muted-foreground hover:bg-accent/30'
                }`}
              >
                <Icon className="w-5 h-5" />
                <span className="text-[10px] font-medium">{label}</span>
              </NavLink>
            )
          })}
        </div>
      </nav>

      {/* Content */}
      <main className="px-4 md:px-6 py-4 md:py-6 w-full">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/opportunities" element={<OpportunitiesPage />} />
          <Route path="/portfolio" element={<StocksPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/paper-trading" element={<PaperTradingPage />} />
          <Route path="/alerts" element={<PriceAlertsPage />} />
          <Route path="/datasources" element={<DataSourcesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/analysis/:symbol/:date" element={<AnalysisDetailPage />} />
        </Routes>
      </main>
      <ChatWidget />
      <LogsModal open={logsOpen} onOpenChange={setLogsOpen} />
      <SelfCheckModal open={selfCheckOpen} onClose={() => setSelfCheckOpen(false)} />
      <Dialog open={upgradeOpen} onOpenChange={setUpgradeOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>发现新版本</DialogTitle>
            <DialogDescription>
              当前版本 v{version}，可升级到 v{upgradeInfo?.latest}。
            </DialogDescription>
          </DialogHeader>
          <div className="text-[12px] text-muted-foreground">
            建议升级以获取最新功能和修复。
          </div>
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                if (upgradeInfo?.latest) localStorage.setItem('panwatch_upgrade_dismissed_version', upgradeInfo.latest)
                setUpgradeOpen(false)
              }}
            >
              稍后提醒
            </Button>
            <Button
              onClick={() => {
                const url = upgradeInfo?.url || 'https://github.com/sunxiao0721/PanWatch/releases'
                window.open(url, '_blank', 'noopener,noreferrer')
              }}
            >
              去升级
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
    </>
  )
}

export default App
