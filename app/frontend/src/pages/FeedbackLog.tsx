import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export function FeedbackLog() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['feedback'],
    queryFn: api.listFeedback,
  })

  if (isLoading) return <p className="empty-state">Loading feedback…</p>
  if (error) return <p className="error-text">{(error as Error).message}</p>
  if (!data) return null

  const stats = [
    { value: data.counts.acted_on, label: 'Acted on' },
    { value: data.counts.useful, label: 'Useful' },
    { value: data.counts.set_aside, label: 'Set aside' },
    { value: `${data.surfacing_precision_pct}%`, label: 'Surfacing precision' },
    { value: data.g1_novel_actionable, label: 'G1 novel-actionable' },
  ]

  return (
    <div>
      <header className="page-header">
        <h1>Feedback log</h1>
        <p>How surfaced insights land — precision and novel actionable signal over time.</p>
      </header>

      <div className="stats-row" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
        {stats.map((s) => (
          <div key={s.label} className="stat-block">
            <div className="value">{s.value}</div>
            <div className="label-below">{s.label}</div>
          </div>
        ))}
      </div>

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
                  <td style={{ color: 'var(--gray-500)' }}>
                    {new Date(item.updated_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
