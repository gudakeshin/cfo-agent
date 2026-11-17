import { useQuery } from '@tanstack/react-query'
import { extra } from '../api/extra'
import { formatMetric } from '../utils/format'

const LABEL: Record<string, string> = { what: 'What happened', why: 'Why it matters', connects: 'How it connects', action: 'Suggested action' }

/** Public, read-only view of a shared digest (no sign-in; the link is the credential). */
export function SharedDigest({ token }: { token: string }) {
  const { data, error, isLoading } = useQuery({ queryKey: ['shared', token], queryFn: () => extra.getShared(token), retry: false })
  if (isLoading) return <p className="empty-state" style={{ padding: 24 }}>Loading…</p>
  if (error || !data)
    return (
      <div className="login-shell">
        <div className="login-card">
          <h1>Link unavailable</h1>
          <p>This link has expired or been revoked. Ask the CFO office for a new one.</p>
        </div>
      </div>
    )
  return (
    <main className="shared-page">
      <header className="page-header">
        <p className="eyebrow">
          CFO weekly briefing · {data.week} · shared by {data.shared.shared_by}
          {data.shared.note ? ` · ${data.shared.note}` : ''}
        </p>
        <h1>{data.headline}</h1>
        <p>
          {data.items.length} insight(s), {data.red_line_count} red line(s) · link expires{' '}
          {new Date(data.shared.expires_at).toLocaleDateString()} ·{' '}
          <a href={`/api/shared/${encodeURIComponent(token)}/export.pdf`}>Download PDF</a>
        </p>
      </header>
      {data.items.map((it) => (
        <article key={it.id} className={`insight-card${it.is_red_line ? ' red-line' : ''}`}>
          <header>
            <span className="rank">#{it.rank}</span>
            <h3>{it.title}</h3>
            <span className={`badge ${it.is_red_line ? 'badge-red' : 'badge-muted'}`}>{it.priority}</span>
          </header>
          {it.metric && (
            <p className="trend-value">
              {it.metric.label}: {formatMetric(it.metric.value, it.metric.unit)}
            </p>
          )}
          <div className="insight-grid">
            {Object.entries(it.sections).map(([k, v]) => (
              <div key={k}>
                <div className="field-label">{LABEL[k] ?? k}</div>
                <p>{v}</p>
              </div>
            ))}
          </div>
          {it.sources.length > 0 && (
            <ol className="shared-sources">
              {it.sources.map((s) => (
                <li key={s.id}>{s.label}</li>
              ))}
            </ol>
          )}
        </article>
      ))}
      <p className="toolbar-hint">Read-only copy. Figures are computed from the cited ERP records; this page is logged when viewed.</p>
    </main>
  )
}
