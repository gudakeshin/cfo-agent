import type { Drivers } from '../api/types'
import { formatPaise } from '../utils/format'

/** What moved the metric: the largest contributing customers, vendors, cost centres…
 * Computed from records when the digest was generated; each line opens its evidence. */
export function DriverTable({ drivers, onCite }: { drivers?: Drivers | null; onCite: (id: string) => void }) {
  if (!drivers?.rows?.length) return null
  const hasPrevious = drivers.rows.some((r) => r.previous_paise != null)
  const gap = drivers.reconciliation?.not_in_feed_paise
  const balanceChange = drivers.reconciliation?.balance_change_paise
  return (
    <div className="driver-table">
      <div className="field-label" title={drivers.basis}>
        What moved it · {drivers.title}
      </div>
      <table>
        <thead>
          <tr>
            <th scope="col">Contributor</th>
            <th scope="col" className="num">
              Now
            </th>
            {hasPrevious && (
              <>
                <th scope="col" className="num">
                  Before
                </th>
                <th scope="col" className="num">
                  Change
                </th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {drivers.rows.map((r) => (
            <tr key={`${r.group}:${r.label}`}>
              <th scope="row">
                {r.citation ? (
                  <button type="button" className="link-button" onClick={() => onCite(r.citation!)} title="Open the source record">
                    {r.label}
                  </button>
                ) : (
                  r.label
                )}
                <span className="driver-group">
                  {r.group}
                  {r.note ? ` · ${r.note}` : ''}
                </span>
              </th>
              <td className="num">{formatPaise(r.current_paise)}</td>
              {hasPrevious && (
                <>
                  <td className="num">{r.previous_paise == null ? '—' : formatPaise(r.previous_paise)}</td>
                  <td className="num">
                    {r.change_paise == null ? '—' : `${r.change_paise > 0 ? '+' : ''}${formatPaise(r.change_paise)}`}
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {gap != null && balanceChange != null && Math.abs(gap) >= 0.1 * Math.abs(balanceChange) && (
        <p className="verification-note">
          The bank balance moved {formatPaise(balanceChange)} but receipts, payments and expenses on record net to{' '}
          {formatPaise(drivers.reconciliation?.net_recorded_flow_paise ?? 0)}. {formatPaise(gap)} is not explained by the
          records in the feed (for example financing, capex, tax or transfers).
        </p>
      )}
    </div>
  )
}
