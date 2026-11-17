import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { extra, type MetricDefinition, type MetricInput } from '../api/extra'
import { formatMetric } from '../utils/format'

const SOURCE_LABEL: Record<string, string> = {
  gl_entries: 'General ledger',
  sales_invoices: 'Sales invoices',
  receipts: 'Receipts',
  credit_notes: 'Credit notes',
  purchase_invoices: 'Purchase invoices',
  payments: 'Payments',
  expenses: 'Expenses',
  revenue_daily: 'Daily revenue',
  gst_events: 'GST events',
}
const WINDOW_LABEL: Record<string, string> = {
  week: 'This week',
  as_of: 'Everything to date',
  month_to_date: 'Month to date',
  last_month: 'Last closed month',
  quarter_to_date: 'Quarter to date',
  trailing_30d: 'Last 30 days',
  trailing_90d: 'Last 90 days',
}

function blank(): MetricDefinition {
  return {
    id: '', label: '', unit: '%', adverse_direction: 'up', description: '', formula: '100 * a / b',
    inputs: { a: { agg: 'sum', source: 'expenses', window: 'week', where: {} }, b: { agg: 'sum', source: 'revenue_daily', window: 'week', where: {} } },
  }
}

export function Metrics() {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: ['metric-defs'], queryFn: extra.listMetricDefs })
  const { data: all = [] } = useQuery({ queryKey: ['metrics'], queryFn: api.listMetrics })
  const [draft, setDraft] = useState<MetricDefinition | null>(null)
  const preview = useMutation({ mutationFn: (d: MetricDefinition) => extra.previewMetric(d) })
  const save = useMutation({
    mutationFn: (d: MetricDefinition) => extra.saveMetric(d),
    onSuccess: () => {
      setDraft(null)
      qc.invalidateQueries({ queryKey: ['metric-defs'] })
      qc.invalidateQueries({ queryKey: ['metrics'] })
      qc.invalidateQueries({ queryKey: ['kra'] })
    },
  })
  const retire = useMutation({
    mutationFn: (id: string) => extra.retireMetric(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['metric-defs'] })
      qc.invalidateQueries({ queryKey: ['metrics'] })
    },
  })
  const vocab = data?.vocabulary
  const edit = (d: MetricDefinition) => {
    preview.reset()
    save.reset()
    setDraft(JSON.parse(JSON.stringify(d)))
  }
  const setInput = (name: string, patch: Partial<MetricInput>) =>
    setDraft((d) => (d ? { ...d, inputs: { ...d.inputs, [name]: { ...d.inputs[name], ...patch } } } : d))

  return (
    <div>
      <header className="page-header">
        <h1>Metrics</h1>
        <p>
          Define new KRA metrics as a formula over ERP data — no code. A metric produces weekly signals once a KRA
          lists it (KRA framework page). Built-in metrics are listed for reference.
        </p>
      </header>

      <div className="form-section">
        <div className="preview-head">
          <h3 style={{ margin: 0 }}>Custom metrics</h3>
          <button type="button" onClick={() => edit(blank())}>
            + New metric
          </button>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th>Formula</th>
                <th>Used by</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(data?.definitions ?? []).map((d) => (
                <tr key={d.id}>
                  <td>
                    <strong>{d.label}</strong>
                    <div className="toolbar-hint">
                      {d.id} · {d.unit || 'number'}
                    </div>
                  </td>
                  <td>
                    <code>{d.formula}</code>
                  </td>
                  <td>{d.used_by_kras?.length ? d.used_by_kras.join(', ') : <span className="toolbar-hint">not in any KRA</span>}</td>
                  <td className="preset-row">
                    <button type="button" onClick={() => edit(d)}>
                      Edit
                    </button>
                    {!d.used_by_kras?.length && (
                      <button type="button" className="ghost-danger" onClick={() => retire.mutate(d.id)}>
                        Retire
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {retire.isError && <p className="error-text">{(retire.error as Error).message}</p>}
      </div>

      {draft && vocab && (
        <div className="form-section metric-editor">
          <h3 style={{ marginTop: 0 }}>{draft.id ? `Edit ${draft.id}` : 'New metric'}</h3>
          <div className="metric-grid">
            <label>
              Id
              <input value={draft.id} onChange={(e) => setDraft({ ...draft, id: e.target.value.toLowerCase() })} placeholder="e.g. gross_margin_pct" />
            </label>
            <label>
              Label
              <input value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })} />
            </label>
            <label>
              Unit
              <select value={draft.unit} onChange={(e) => setDraft({ ...draft, unit: e.target.value })}>
                {vocab.units.map((u) => (
                  <option key={u} value={u}>
                    {u || 'number'}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Bad direction
              <select
                value={draft.adverse_direction ?? 'both'}
                onChange={(e) => setDraft({ ...draft, adverse_direction: e.target.value === 'both' ? null : (e.target.value as 'up' | 'down') })}
              >
                <option value="up">Rising is bad</option>
                <option value="down">Falling is bad</option>
                <option value="both">Any big move</option>
              </select>
            </label>
          </div>
          <label>
            Description
            <input value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
          </label>

          <h4>Inputs</h4>
          {Object.entries(draft.inputs).map(([name, inp]) => {
            const filters = inp.source ? vocab.sources[inp.source]?.filters ?? [] : []
            const [fk, fv] = Object.entries(inp.where ?? {})[0] ?? ['', '']
            return (
              <div key={name} className="metric-input-row">
                <code>{name}</code>
                <select value={inp.agg} onChange={(e) => setInput(name, { agg: e.target.value })} aria-label={`${name} aggregation`}>
                  {vocab.aggs.map((a) => (
                    <option key={a} value={a}>
                      {a === 'metric' ? 'another metric' : a}
                    </option>
                  ))}
                </select>
                {inp.agg === 'metric' ? (
                  <select value={inp.metric ?? ''} onChange={(e) => setInput(name, { metric: e.target.value })} aria-label={`${name} metric`}>
                    <option value="">Choose…</option>
                    {all.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                ) : (
                  <>
                    <select value={inp.source ?? ''} onChange={(e) => setInput(name, { source: e.target.value, where: {} })} aria-label={`${name} source`}>
                      {Object.keys(vocab.sources).map((s) => (
                        <option key={s} value={s}>
                          {SOURCE_LABEL[s] ?? s}
                        </option>
                      ))}
                    </select>
                    <select value={inp.window ?? 'week'} onChange={(e) => setInput(name, { window: e.target.value })} aria-label={`${name} window`}>
                      {vocab.windows.map((w) => (
                        <option key={w} value={w}>
                          {WINDOW_LABEL[w] ?? w}
                        </option>
                      ))}
                    </select>
                    <select
                      value={fk}
                      onChange={(e) => setInput(name, { where: e.target.value ? { [e.target.value]: fv } : {} })}
                      aria-label={`${name} filter column`}
                    >
                      <option value="">No filter</option>
                      {filters.map((f) => (
                        <option key={f} value={f}>
                          where {f} =
                        </option>
                      ))}
                    </select>
                    {fk && (
                      <input
                        value={fv}
                        onChange={(e) => setInput(name, { where: { [fk]: e.target.value } })}
                        aria-label={`${name} filter value`}
                        placeholder="value"
                      />
                    )}
                    <label className="inline-check">
                      <input type="checkbox" checked={inp.sign === -1} onChange={(e) => setInput(name, { sign: e.target.checked ? -1 : 1 })} />
                      flip sign
                    </label>
                  </>
                )}
                <button
                  type="button"
                  className="link-button"
                  onClick={() => setDraft((d) => {
                    if (!d) return d
                    const next = { ...d.inputs }
                    delete next[name]
                    return { ...d, inputs: next }
                  })}
                >
                  Remove
                </button>
              </div>
            )
          })}
          <button
            type="button"
            className="link-button"
            onClick={() => {
              const name = 'abcdefghijklmnopqrstuvwxyz'.split('').find((c) => !(c in draft.inputs)) ?? 'x'
              setDraft({ ...draft, inputs: { ...draft.inputs, [name]: { agg: 'sum', source: 'expenses', window: 'week', where: {} } } })
            }}
          >
            + Add input
          </button>
          <label>
            Formula (use the input names; + − × ÷, min, max, abs)
            <input className="mono-input" value={draft.formula} onChange={(e) => setDraft({ ...draft, formula: e.target.value })} />
          </label>
          <p className="toolbar-hint">Money inputs are in ₹ Cr. Ledger credits (revenue) are negative — flip the sign to use them as positive.</p>
          <div className="preset-row">
            <button type="button" onClick={() => preview.mutate(draft)} disabled={preview.isPending}>
              {preview.isPending ? 'Computing…' : 'Preview on last 8 weeks'}
            </button>
            <button type="button" className="primary" onClick={() => save.mutate(draft)} disabled={save.isPending}>
              Save metric
            </button>
            <button type="button" onClick={() => setDraft(null)}>
              Cancel
            </button>
          </div>
          {(preview.error || save.error) && <p className="error-text">{((preview.error || save.error) as Error).message}</p>}
          {preview.data && (
            <div className="metric-preview">
              {preview.data.points.map((p) => (
                <div key={p.week} className="stat-block">
                  <span className="toolbar-hint">{p.week}</span>
                  <strong>{p.value == null ? 'n/a' : formatMetric(p.value, draft.unit)}</strong>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <details className="form-section">
        <summary>Built-in metrics ({all.filter((m) => !m.custom).length})</summary>
        <ul className="editorial-list">
          {all
            .filter((m) => !m.custom)
            .map((m) => (
              <li key={m.id}>
                {m.label} <span className="toolbar-hint">{m.id} · {m.unit}{m.proxy ? ' · no value until its data feed is connected' : ''}</span>
              </li>
            ))}
        </ul>
      </details>
    </div>
  )
}
