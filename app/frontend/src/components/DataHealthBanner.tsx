import type { DataHealth } from '../api/types'

interface Props {
  health: DataHealth | null | undefined
}

export function DataHealthBanner({ health }: Props) {
  if (!health) return null

  const variant = health.hard_failure
    ? 'error'
    : health.stale || health.warnings.length > 0
      ? 'warn'
      : health.ok
        ? 'ok'
        : 'warn'

  return (
    <div className={`health-banner ${variant}`}>
      <h4>
        Data health {health.ok ? '· OK' : health.hard_failure ? '· Hard failure' : '· Degraded'}
      </h4>
      <p style={{ margin: 0, fontSize: '0.875rem' }}>
        Invoices: {health.invoice_count}
        {health.latest_receipt_date && ` · Latest receipt: ${health.latest_receipt_date}`}
        {health.latest_cash_date && ` · Latest cash: ${health.latest_cash_date}`}
        {health.stale && ` · Stale by ${health.stale_days} days`}
      </p>
      {health.errors.length > 0 && (
        <ul>
          {health.errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}
      {health.warnings.length > 0 && (
        <ul>
          {health.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
