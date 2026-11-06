import { useCallback, useEffect, useId, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useEscape } from '../hooks/useEscape'
import { useMetrics } from '../hooks/useMetrics'
import { formatDelta, formatMetric, formatNumber, formatPaise, humanizeKey } from '../utils/format'

interface Props {
  citationId: string
  digestItemId?: number
  onClose: () => void
}

const HIDDEN_FIELDS = new Set(['id', 'customer_id', 'invoice_id', 'purchase_invoice_id', 'vendor_id', 'lineage'])

function formatField(key: string, value: unknown): string {
  if (value == null || value === '') return '—'
  if (key.endsWith('_paise') && typeof value === 'number') return formatPaise(value)
  if (typeof value === 'number') return formatNumber(value)
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}/.test(value)) {
    const d = new Date(value)
    if (!Number.isNaN(d.getTime()))
      return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })
  }
  return String(value)
}

function sourceLabel(cid: string): string {
  const [kind, table] = cid.split(':')
  if (kind === 'metric') return 'Computed metric'
  const names: Record<string, string> = {
    erp_sales_invoices: 'Sales invoice',
    erp_receipts: 'Customer receipt',
    erp_purchase_invoices: 'Purchase invoice',
    erp_payments: 'Supplier payment',
    erp_expenses: 'Expense',
    erp_cash_daily: 'Daily cash balance',
    erp_revenue_daily: 'Daily revenue',
    erp_gst_events: 'GST event',
    erp_covenants: 'Loan covenant',
    erp_customers: 'Customer',
  }
  return names[table] ?? 'ERP record'
}

/** Readable evidence card for one citation (formerly a raw JSON dump). */
export function CitationPopover({ citationId, digestItemId, onClose }: Props) {
  const titleId = useId()
  const closeRef = useRef<HTMLButtonElement>(null)
  const { byId } = useMetrics()
  const { data, isLoading, error } = useQuery({
    queryKey: ['citation', citationId, digestItemId ?? null],
    queryFn: () => api.getCitation(citationId, digestItemId),
  })
  const close = useCallback(() => onClose(), [onClose])
  useEscape(close)
  useEffect(() => closeRef.current?.focus(), [])

  const record = (data?.record ?? {}) as Record<string, unknown>
  const isMetric = citationId.startsWith('metric:')
  const meta = isMetric ? byId.get(String(record.metric_id ?? '')) : undefined
  const lineage = Array.isArray(record.lineage) ? (record.lineage as string[]) : []

  return (
    <div className="popover-backdrop" onClick={onClose} role="presentation">
      <div
        className="popover-panel evidence-card"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header>
          <div>
            <div className="evidence-kind">{sourceLabel(citationId)}</div>
            <h3 id={titleId}>{isMetric ? (meta?.label ?? humanizeKey(String(record.metric_id ?? ''))) : citationId.split(':').pop()}</h3>
          </div>
          <button type="button" ref={closeRef} onClick={onClose}>
            Close
          </button>
        </header>
        {isLoading && <p>Loading…</p>}
        {error && <p className="error-text">{(error as Error).message}</p>}
        {data && isMetric && (
          <dl className="evidence-fields">
            <div>
              <dt>Week</dt>
              <dd>{String(record.week ?? '—')}</dd>
            </div>
            <div>
              <dt>Value</dt>
              <dd>{formatMetric(record.value as number, meta?.unit ?? '')}</dd>
            </div>
            <div>
              <dt>Change on prior week</dt>
              <dd>{record.delta_wow == null ? '—' : formatDelta(record.delta_wow as number, meta?.unit ?? '')}</dd>
            </div>
            <div>
              <dt>Built from</dt>
              <dd>
                {lineage.length} source record{lineage.length === 1 ? '' : 's'}
                {lineage.length > 0 && (
                  <span className="evidence-lineage"> — {lineage.slice(0, 4).map((l) => l.split(':').pop()).join(', ')}{lineage.length > 4 ? '…' : ''}</span>
                )}
              </dd>
            </div>
          </dl>
        )}
        {data && !isMetric && (
          <dl className="evidence-fields">
            {Object.entries(record)
              .filter(([k]) => !HIDDEN_FIELDS.has(k))
              .map(([k, v]) => (
                <div key={k}>
                  <dt>{humanizeKey(k)}</dt>
                  <dd>{formatField(k, v)}</dd>
                </div>
              ))}
          </dl>
        )}
        {data && (
          <p className="verification-note">
            {data.snapshot
              ? 'As recorded when this digest was generated.'
              : 'Live record (no snapshot stored for this digest).'}{' '}
            Source ID <code>{citationId}</code>
          </p>
        )}
      </div>
    </div>
  )
}
