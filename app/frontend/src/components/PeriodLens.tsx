import { useQuery } from '@tanstack/react-query'
import { extra } from '../api/extra'
import type { PeriodMetric, PeriodView } from '../api/extra'
import { formatDelta, formatMetric } from '../utils/format'

const COLUMNS: { key: 'mtd' | 'qtd' | 'ytd' | 'yoy'; label: string }[] = [
  { key: 'mtd', label: 'Month to date' },
  { key: 'qtd', label: 'Quarter to date' },
  { key: 'ytd', label: 'Year to date' },
  { key: 'yoy', label: 'Vs a year ago' },
]

function Cell({ metric, view }: { metric: PeriodMetric; view: PeriodView | null }) {
  if (!view || view.change == null) {
    return (
      <td className="period-cell none" title="Not enough history to compare">
        —
      </td>
    )
  }
  const tone = view.adverse == null ? '' : view.adverse ? ' adverse' : ' favourable'
  const flow = metric.aggregation !== 'last'
  return (
    <td className="period-cell" title={view.basis}>
      <span className={`period-change${tone}`}>{formatDelta(view.change, metric.unit)}</span>
      {flow && view.value != null && (
        <span className="period-sub">
          {metric.aggregation === 'sum' ? 'total ' : 'avg '}
          {formatMetric(view.value, metric.unit)}
        </span>
      )}
    </td>
  )
}

function PlanCell({ metric }: { metric: PeriodMetric }) {
  const p = metric.vs_plan
  if (!p) return <td className="period-cell none">—</td>
  return (
    <td className="period-cell" title={`Plan ${formatMetric(p.plan, metric.unit)}`}>
      <span className={`period-change${p.adverse ? ' adverse' : ''}`}>{formatDelta(p.gap, metric.unit)}</span>
      <span className="period-sub">{`${p.gap_pct > 0 ? '+' : ''}${p.gap_pct}% vs plan`}</span>
    </td>
  )
}

export function PeriodLens({ week }: { week?: string }) {
  const { data } = useQuery({
    queryKey: ['periods', week],
    queryFn: () => extra.periods(week),
    enabled: Boolean(week),
    staleTime: 5 * 60_000,
  })
  const metrics = data ? Object.values(data.metrics) : []
  if (!metrics.length) return null
  const hasPlan = metrics.some((m) => m.vs_plan)
  // Columns with nothing to show are left out and named in a note instead.
  const filled = COLUMNS.filter((c) => metrics.some((m) => m.periods[c.key]?.change != null))
  const missing = COLUMNS.filter((c) => !filled.includes(c)).map((c) => c.label.toLowerCase())
  // In the first month of a quarter or year the periods start on the same day; show them once.
  const sinceOf = (key: 'mtd' | 'qtd' | 'ytd') => metrics.map((m) => m.periods[key]?.since ?? null).join()
  const sameAs = (a: 'mtd' | 'qtd' | 'ytd', b: 'mtd' | 'qtd' | 'ytd') => sinceOf(a) === sinceOf(b)
  const columns = filled
    .filter((c) => !(c.key === 'qtd' && filled.some((f) => f.key === 'mtd') && sameAs('qtd', 'mtd')))
    .filter((c) => !(c.key === 'ytd' && filled.some((f) => f.key === 'qtd') && sameAs('ytd', 'qtd')))
    .map((c) => {
      if (c.key === 'mtd' && filled.some((f) => f.key === 'qtd') && sameAs('qtd', 'mtd'))
        return { ...c, label: 'Month & quarter to date' }
      return c
    })
  if (!columns.length) return null
  return (
    <section className="panel period-panel no-print" aria-label="Month, quarter and year to date">
      <h2 className="section-title">Beyond the week</h2>
      <p className="period-note">
        Change since the start of the month, quarter and financial year. Rates and cash flow are totalled or
        averaged over the period. A dash means the data does not reach back far enough to compare.
        {missing.length > 0 && ` Not shown yet, for lack of history: ${missing.join(', ')}.`}
      </p>
      <div className="period-scroll">
        <table className="data-table period-table">
          <thead>
            <tr>
              <th>Measure</th>
              {columns.map((c) => (
                <th key={c.key}>{c.label}</th>
              ))}
              {hasPlan && <th>Vs plan</th>}
            </tr>
          </thead>
          <tbody>
            {metrics.map((m) => (
              <tr key={m.id}>
                <th scope="row">{m.aggregation === 'last' ? m.label : m.label.replace(/ \(week\)$/, '')}</th>
                {columns.map((c) => (
                  <Cell key={c.key} metric={m} view={m.periods[c.key]} />
                ))}
                {hasPlan && <PlanCell metric={m} />}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
