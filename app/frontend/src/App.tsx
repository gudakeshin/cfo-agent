import { NavLink, Outlet } from 'react-router-dom'
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

const NAV_PRIMARY = [
  { to: '/', label: 'Digest inbox', end: true, icon: ICONS.inbox },
  { to: '/news', label: 'Regulatory feed', icon: ICONS.news },
]

const NAV_SECONDARY = [
  { to: '/weighting', label: 'Weighting', icon: ICONS.weighting },
  { to: '/kras', label: 'KRA framework', icon: ICONS.kra },
  { to: '/feedback', label: 'Feedback log', icon: ICONS.feedback },
  { to: '/connections', label: 'Connections', icon: ICONS.connections },
  { to: '/data-health', label: 'Data health', icon: ICONS.health },
  { to: '/memory', label: 'Memory', icon: ICONS.memory },
]

export default function App() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="brand-row">
            <span className="brand-dot" />
            <h1 className="brand-name">Deloitte</h1>
          </div>
          <p className="brand-sub">CFO Agentic Intelligence</p>
        </div>
        <nav>
          <div className="nav-section-label">Intelligence</div>
          {NAV_PRIMARY.map(({ to, label, end, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) => (isActive ? 'active' : '')}
            >
              {icon}
              <span>{label}</span>
            </NavLink>
          ))}
          <div className="nav-section-label nav-section-spaced">Configure</div>
          {NAV_SECONDARY.map(({ to, label, icon }) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? 'active' : '')}>
              {icon}
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">Single-user demo · localhost</div>
      </aside>
      <div className="main-area">
        <AlertBanner />
        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
