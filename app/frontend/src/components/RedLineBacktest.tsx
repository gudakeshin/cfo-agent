import { useMutation } from '@tanstack/react-query'
import { extra } from '../api/extra'
import type { RedLine } from '../api/types'
import { formatMetric } from '../utils/format'

/** "Would this rule have fired?" — replays a draft red line over recent weeks. */
export function RedLineBacktest({ rule, unit }: { rule: RedLine; unit: string }) {
  const m = useMutation({
    mutationFn: () =>
      extra.backtest(
        rule.type === 'level'
          ? { type: 'level', metric: rule.metric, operator: rule.operator, threshold: Number(rule.threshold) }
          : { type: 'event', event_type: rule.event_type },
      ),
  })
  const ready = rule.type === 'event' || (rule.metric && rule.operator && rule.threshold != null)
  return (
    <div className="backtest">
      <button type="button" className="link-button" disabled={!ready || m.isPending} onClick={() => m.mutate()}>
        {m.isPending ? 'Checking…' : 'Test on the last 13 weeks'}
      </button>
      {m.isError && <span className="error-text"> {(m.error as Error).message}</span>}
      {m.data && (
        <div className="backtest-result">
          <span>
            {m.data.fired_count === 0
              ? `Would not have fired in the last ${m.data.weeks} weeks.`
              : `Would have fired in ${m.data.fired_count} of ${m.data.weeks} weeks${m.data.fired_count > m.data.weeks / 2 ? ' — likely too sensitive' : ''}.`}
          </span>
          <span className="backtest-strip" aria-label="Weekly results">
            {m.data.points.map((p) => (
              <span
                key={p.week}
                className={`backtest-cell${p.fires ? ' fires' : ''}`}
                title={`${p.week}: ${rule.type === 'level' ? formatMetric(p.value, unit) : `${p.value ?? 0} event(s)`}${p.fires ? ' — fires' : ''}`}
              />
            ))}
          </span>
        </div>
      )}
    </div>
  )
}
