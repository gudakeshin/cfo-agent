import { useQuery } from '@tanstack/react-query'
import { extra } from '../api/extra'
import { formatMetric } from '../utils/format'

/** Per-entity key metrics next to the consolidated group view (hidden for one entity). */
export function EntityTable() {
  const { data } = useQuery({ queryKey: ['entities'], queryFn: extra.entities, staleTime: 60_000 })
  if (!data || data.entities.length < 2) return null
  return (
    <section className="panel">
      <h2 className="section-title">By entity · {data.week}</h2>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Entity</th>
              {data.metrics.map((m) => (
                <th key={m.id}>{m.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.entities.map((e) => (
              <tr key={e.id}>
                <td>
                  {e.name} <span className="toolbar-hint">{e.id}{e.currency !== 'INR' ? ` · ${e.currency}` : ''}{!e.consolidated ? ' · not consolidated' : ''}</span>
                </td>
                {data.metrics.map((m) => (
                  <td key={m.id}>{formatMetric(e.values[m.id] ?? null, m.unit)}</td>
                ))}
              </tr>
            ))}
            <tr className="total-row">
              <td>Group (consolidated, ex-intercompany)</td>
              {data.metrics.map((m) => (
                <td key={m.id}>{formatMetric(data.group[m.id] ?? null, m.unit)}</td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  )
}
