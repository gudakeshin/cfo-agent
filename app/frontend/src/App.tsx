import { NavLink, Outlet } from 'react-router-dom'
import { api } from './api/client'
import type { Permission } from './api/types'
import { ROLE_LABEL, useAuth } from './hooks/useAuth'
import { AlertBanner } from './components/AlertBanner'

const ICONS = {
  inbox: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M4 5h16v10.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5Z" />
      <path d="M4 5l8 6 8-6" />
    </svg>
  ),
  weighting: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M6 4v16M6 6h9l-2 3 2 3H6" />
      <path d="M18 20V4" />
    </svg>
  ),
  feedback: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M5 5h14v10H9l-4 4V5Z" />
    </svg>
  ),
  health: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M12 21s-7-4.35-9-9a5 5 0 0 1 9-3 5 5 0 0 1 9 3c-2 4.65-9 9-9 9Z" />
    </svg>
  ),
  memory: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <path d="M4 10h16M10 4v16" />
    </svg>
  ),
  news: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M5 4h11a2 2 0 0 1 2 2v13a1 1 0 0 1-1.5.87L15 19l-1.5.87a1 1 0 0 1-1 0L11 19l-1.5.87a1 1 0 0 1-1 0L7 19l-1.5.87A1 1 0 0 1 4 19V6a2 2 0 0 1 1-2Z" />
      <path d="M8 9h6M8 12.5h6M8 16h3" />
    </svg>
  ),
  connections: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <circle cx="6" cy="7" r="2.4" />
      <circle cx="18" cy="7" r="2.4" />
      <circle cx="12" cy="18" r="2.4" />
      <path d="M8.1 8.2 10.4 16M15.9 8.2 13.6 16M8.4 7h7.2" />
    </svg>
  ),
  kra: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      <path d="M12 3 4 7v5c0 5 3.6 7.6 8 9 4.4-1.4 8-4 8-9V7l-8-4Z" />
      <path d="m9 12 2.2 2.2L15.5 10" />
    </svg>
  ),
}

const ICON_BRIEFING = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
    <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
  </svg>
)
const ICON_RUNS = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
    <path d="M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4" />
  </svg>
)
const ICON_GUIDE = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17.5v.01" />
  </svg>
)

const ICON_ACTIONS = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
    <rect x="4" y="4" width="16" height="16" rx="2" />
    <path d="m8 12 3 3 5-6" />
  </svg>
)
const ICON_BRIEFS = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
    <path d="M7 3h10v18l-5-3-5 3V3Z" />
  </svg>
)

const NAV_PRIMARY = [
  { to: '/', label: 'Briefing', end: true, icon: ICON_BRIEFING },
  { to: '/digests', label: 'All digests', icon: ICONS.inbox },
  { to: '/actions', label: 'Actions', icon: ICON_ACTIONS },
  { to: '/briefs', label: 'Meeting briefs', icon: ICON_BRIEFS },
  { to: '/news', label: 'Regulatory feed', icon: ICONS.news },
  { to: '/feedback', label: 'Pilot scorecard', icon: ICONS.feedback },
  { to: '/guide', label: 'Guide', icon: ICON_GUIDE },
]

const ICON_AUDIT = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
    <path d="M6 3h9l3 3v15H6z" />
    <path d="M9 10h6M9 14h6M9 18h4" />
  </svg>
)

const NAV_SECONDARY: { to: string; label: string; icon: React.ReactNode; perm: Permission }[] = [
  { to: '/runs', label: 'Digest runs', icon: ICON_RUNS, perm: 'configure' },
  { to: '/weighting', label: 'Weighting', icon: ICONS.weighting, perm: 'configure' },
  { to: '/kras', label: 'KRA framework', icon: ICONS.kra, perm: 'configure' },
  { to: '/metrics', label: 'Metrics', icon: ICON_BRIEFING, perm: 'configure' },
  { to: '/data-health', label: 'Data health', icon: ICONS.health, perm: 'configure' },
  { to: '/memory', label: 'Memory', icon: ICONS.memory, perm: 'configure' },
  { to: '/connections', label: 'Connections', icon: ICONS.connections, perm: 'administer' },
  { to: '/audit', label: 'Audit log', icon: ICON_AUDIT, perm: 'administer' },
]

export default function App() {
  const { state, can, refresh } = useAuth()
  const user = state?.user
  const secondary = NAV_SECONDARY.filter((n) => can(n.perm))
  const signOut = async () => {
    try {
      await api.logout()
    } finally {
      refresh()
    }
  }
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="brand-row">
            <span className="brand-dot" />
            <h1 className="brand-name">Deloitte</h1>
          </div>
          <p className="brand-sub">CFO Agentic Intelligence</p>
        </div>
        <nav aria-label="Main">
          <div className="nav-section-label">For the CFO</div>
          {NAV_PRIMARY.map(({ to, label, end, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={end ?? false}
              className={({ isActive }) => (isActive ? 'active' : '')}
            >
              {icon}
              <span>{label}</span>
            </NavLink>
          ))}
          {secondary.length > 0 && (
            <div className="nav-section-label nav-section-spaced">Set-up (delegate)</div>
          )}
          {secondary.map(({ to, label, icon }) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? 'active' : '')}>
              {icon}
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          {state?.auth_mode === 'off' ? (
            <span>Demo · sign-in off</span>
          ) : (
            user && (
              <div className="user-box">
                <div className="user-name">{user.name}</div>
                <div className="user-role">{ROLE_LABEL[user.role]}</div>
                <button type="button" className="signout" onClick={signOut}>
                  Sign out
                </button>
              </div>
            )
          )}
        </div>
      </aside>
      <div className="main-area">
        <AlertBanner />
        <main className="main-content" id="main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  )
}
