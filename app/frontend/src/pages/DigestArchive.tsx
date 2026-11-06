import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { formatWeekLabel } from '../utils/weeks'

export function DigestArchive() {
  const [q, setQ] = useState('')
  const { data: digests = [], isLoading, error } = useQuery({ queryKey: ['digests'], queryFn: api.listDigests })
  const { data: feedback } = useQuery({ queryKey: ['feedback'], queryFn: api.listFeedback })

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return digests
    return digests.filter(
      (d) =>
        d.headline.toLowerCase().includes(needle) ||
        d.week.toLowerCase().includes(needle) ||
        d.weighting_label.toLowerCase().includes(needle),
    )
  }, [digests, q])

  const avg = digests.length ? (digests.reduce((s, d) => s + d.item_count, 0) / digests.length).toFixed(1) : '—'

  return (
    <div>
      <header className="page-header">
        <h1>All digests</h1>
        <p>Every weekly digest, newest first. Re-runs of the same week under a different weighting are kept side by side.</p>
      </header>

      <div className="stats-row">
        <div className="stat-block">
          <div className="label">Digests generated</div>
          <div className="value">{digests.length}</div>
        </div>
        <div className="stat-block">
          <div className="label">Useful or acted on</div>
          <div className="value success">{feedback ? `${feedback.surfacing_precision_pct}%` : '—'}</div>
        </div>
        <div className="stat-block">
          <div className="label">Avg insights per digest</div>
          <div className="value">{avg}</div>
        </div>
      </div>

      <div className="toolbar">
        <div className="field" style={{ flex: 1 }}>
          <label htmlFor="digest-search">Search</label>
          <input
            id="digest-search"
            type="search"
            placeholder="Headline, week (e.g. W27) or weighting"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>

      {isLoading && <p className="empty-state">Loading digests…</p>}
      {error && <p className="error-text">{(error as Error).message}</p>}
      {!isLoading && digests.length === 0 && (
        <p className="empty-state">
          No digests yet. They arrive each Monday, or can be run from <Link to="/runs">Digest runs</Link>.
        </p>
      )}
      {!isLoading && digests.length > 0 && filtered.length === 0 && (
        <p className="empty-state">No digest matches “{q}”.</p>
      )}

      <ul className="digest-list">
        {filtered.map((d) => (
          <li key={d.id}>
            <Link to={`/digests/${d.id}`} className="digest-card">
              <div className="digest-card-meta-row">
                {d.empty_bar && <span className="badge-muted">Nothing met the bar</span>}
                <span className="week-mono">{formatWeekLabel(d.week)}</span>
              </div>
              <h3>{d.headline}</h3>
              <div className="meta">
                <span>
                  {d.item_count} insight{d.item_count === 1 ? '' : 's'}
                </span>
                <span>·</span>
                <span>{d.weighting_label}</span>
                <span>·</span>
                <span>{new Date(d.created_at).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}</span>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}
