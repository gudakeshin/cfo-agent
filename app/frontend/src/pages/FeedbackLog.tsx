import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { extra } from '../api/extra'
import type { Scorecard } from '../api/extra'
import { formatWeekLabel } from '../utils/weeks'

function pct(v: number | null): string {
  return v == null ? '—' : `${v}%`
}

function PilotScorecard({ card }: { card: Scorecard }) {
  const t = card.totals
  if (!t) return <p className="empty-state">No digests yet — the scorecard fills in from the first briefing.</p>
  const stats = [
    { value: `${t.weeks_opened} of ${t.digests}`, label: 'Weeks the briefing was opened' },
    { value: t.surfaced, label: 'Insights surfaced' },
    { value: pct(t.review_rate_pct), label: 'Insights given a verdict' },
    { value: pct(t.useful_rate_pct), label: 'Of those, acted on or useful' },
    { value: t.novel_acted_on, label: 'Acted on and not already known' },
  ]
  return (
    <section aria-label="Pilot scorecard">
      <p className={t.success_test_met ? 'success-text' : 'toolbar-hint'} role="status">
        {t.success_test_met
          ? `Success test met: ${t.novel_acted_on} insight${t.novel_acted_on === 1 ? '' : 's'} acted on that the CFO had not already spotted.`
          : 'Success test not yet met: no insight has been marked “Acted on” without “Already knew”.'}
      </p>
      <div className="stats-row" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
        {stats.map((s) => (
          <div key={s.label} className="stat-block">
            <div className="value">{s.value}</div>
            <div className="label-below">{s.label}</div>
          </div>
        ))}
      </div>
      <div className="table-card">
        <table className="data-table">
          <thead>
            <tr>
              <th>Week</th>
              <th>Opened</th>
              <th>Surfaced</th>
              <th>Reviewed</th>
              <th>Acted on</th>
              <th>Useful</th>
              <th>Set aside</th>
              <th>Already knew</th>
            </tr>
          </thead>
          <tbody>
            {[...card.weeks].reverse().map((w) => (
              <tr key={w.digest_id}>
                <td>
                  <Link to={`/digests/${w.digest_id}`}>{formatWeekLabel(w.week)}</Link>
                </td>
                <td>{w.opens > 0 ? 'Yes' : 'No'}</td>
                <td>
                  {w.surfaced}
                  {w.red_lines > 0 ? ` (${w.red_lines} red line${w.red_lines === 1 ? '' : 's'})` : ''}
                </td>
                <td>{w.reviewed}</td>
                <td>{w.acted_on}</td>
                <td>{w.useful}</td>
                <td>{w.set_aside}</td>
                <td>{w.already_knew}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function FeedbackLog() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['feedback'],
    queryFn: api.listFeedback,
  })
  const { data: scorecard } = useQuery({ queryKey: ['scorecard'], queryFn: extra.scorecard })

  if (isLoading) return <p className="empty-state">Loading…</p>
  if (error) return <p className="error-text">{(error as Error).message}</p>
  if (!data) return null

  return (
    <div>
      <header className="page-header">
        <h1>Pilot scorecard</h1>
        <p>Is the briefing being read, and is it telling the CFO something new? Last 13 weeks.</p>
      </header>

      {scorecard && <PilotScorecard card={scorecard} />}

      <h2 className="section-title" style={{ marginTop: '2rem' }}>
        Verdicts and notes
      </h2>
      {data.items.length === 0 ? (
        <p className="empty-state">No feedback recorded yet.</p>
      ) : (
        <div className="table-card">
          <table className="data-table">
            <thead>
              <tr>
                <th>Insight</th>
                <th>Verdict</th>
                <th>Note</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.id}>
                  <td style={{ fontWeight: 500 }}>{item.title ?? `#${item.digest_item_id}`}</td>
                  <td>
                    <span className={`badge-verdict ${item.verdict}`}>
                      {item.verdict.replace('_', ' ')}
                      {item.already_knew ? ' · knew' : ''}
                    </span>
                  </td>
                  <td style={{ color: 'var(--gray-500)', maxWidth: '22ch' }}>{item.note || '—'}</td>
                  <td style={{ color: 'var(--gray-500)' }}>{new Date(item.updated_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
