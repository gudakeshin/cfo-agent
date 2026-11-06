import { Fragment, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { LearningPanel } from '../components/LearningPanel'
import { RedLineBacktest } from '../components/RedLineBacktest'
import type { Kra, PreviewRanking, RedLine, RiskAppetite, WeightingContent } from '../api/types'
import { FaceWeightSliders } from '../components/FaceWeightSliders'
import { useMetrics } from '../hooks/useMetrics'
import { formatMetric } from '../utils/format'
import { formatWeekLabel } from '../utils/weeks'

const EVENT_TYPES = [
  { value: 'show_cause_notice', label: 'GST show-cause notice' },
  { value: 'itc_mismatch', label: 'GST input-credit mismatch' },
]

const APPETITE_HELP: Record<RiskAppetite, string> = {
  low: 'Surface more — even modest moves (Watch and above).',
  moderate: 'Surface Medium and High priority moves.',
  high: 'Surface only High priority moves.',
}

function ruleSentence(rl: RedLine, metricLabel: (id: string) => string, unit: (id: string) => string): string {
  if (rl.type === 'event') {
    const ev = EVENT_TYPES.find((e) => e.value === rl.event_type)?.label ?? rl.event_type ?? 'event'
    return `Alert on any new ${ev}.`
  }
  const op = OPERATORS.find((o) => o.value === rl.operator)?.words ?? rl.operator
  const thr = rl.threshold == null ? '…' : formatMetric(rl.threshold, unit(rl.metric ?? ''))
  return `Alert when ${metricLabel(rl.metric ?? '')} is ${op} ${thr}.`
}

function validate(redLines: RedLine[]): string[] {
  const errors: string[] = []
  const seen = new Set<string>()
  redLines.forEach((rl, i) => {
    const n = `Red line ${i + 1}`
    if (!rl.id.trim()) errors.push(`${n}: needs an ID.`)
    else if (seen.has(rl.id.trim())) errors.push(`${n}: ID “${rl.id}” is used twice.`)
    seen.add(rl.id.trim())
    if (!rl.description.trim()) errors.push(`${n}: add a short description (it becomes the alert title).`)
    if (rl.type === 'level' && (rl.threshold == null || Number.isNaN(rl.threshold)))
      errors.push(`${n}: set a threshold.`)
  })
  return errors
}

const DEFAULT_FACES = { steward: 0.25, operator: 0.25, strategist: 0.25, catalyst: 0.25 }

const LEVEL_METRICS = [
  'cash_balance_cr',
  'covenant_headroom_cr',
  'dso_days',
  'dpo_days',
  'ar_overdue_pct',
  'ap_overdue_pct',
  'gst_open_notices',
  'gst_notice_exposure_cr',
  'opex_ratio_pct',
  'free_cash_flow_cr',
]

const OPERATORS = [
  { value: 'lt', label: '<', words: 'below' },
  { value: 'lte', label: '≤', words: 'at or below' },
  { value: 'gt', label: '>', words: 'above' },
  { value: 'gte', label: '≥', words: 'at or above' },
  { value: 'eq', label: '=', words: 'exactly' },
]

function emptyLevelRule(id: string): RedLine {
  return {
    id,
    type: 'level',
    description: '',
    metric: 'cash_balance_cr',
    operator: 'lt',
    threshold: 22,
  }
}

function emptyEventRule(id: string): RedLine {
  return {
    id,
    type: 'event',
    description: '',
    trigger: 'on_new_record',
    table: 'erp_gst_events',
    event_type: 'show_cause_notice',
  }
}

export function WeightingEditor() {
  const queryClient = useQueryClient()
  const [label, setLabel] = useState('')
  const [definingPriority, setDefiningPriority] = useState('')
  const [faceWeights, setFaceWeights] = useState(DEFAULT_FACES)
  const [kraRanks, setKraRanks] = useState<string[]>([])
  const [riskAppetite, setRiskAppetite] = useState<RiskAppetite>('moderate')
  const [redLines, setRedLines] = useState<RedLine[]>([])
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null)
  const [confirmPreset, setConfirmPreset] = useState<string | null>(null)
  const [presetNote, setPresetNote] = useState<string | null>(null)
  const { byId: metricById, metrics } = useMetrics()
  const metricLabel = (id: string) => metricById.get(id)?.label ?? id
  const metricUnit = (id: string) => metricById.get(id)?.unit ?? ''

  const { data: weighting } = useQuery({
    queryKey: ['weighting'],
    queryFn: api.getWeighting,
  })

  const { data: presets = [] } = useQuery({
    queryKey: ['weighting-presets'],
    queryFn: api.getWeightingPresets,
  })

  const { data: kraFramework } = useQuery({
    queryKey: ['kra'],
    queryFn: api.getKra,
  })

  const kraMap = new Map(kraFramework?.kras.map((k) => [k.id, k]) ?? [])
  const metricOptions = metrics.length
    ? metrics.map((m) => m.id)
    : kraFramework?.metric_map
      ? Object.keys(kraFramework.metric_map)
      : LEVEL_METRICS

  const applyContent = (content: WeightingContent) => {
    setLabel(content.label)
    setDefiningPriority(content.defining_priority)
    setFaceWeights({ ...DEFAULT_FACES, ...content.face_weights })
    setKraRanks([...content.kra_ranks])
    setRiskAppetite(content.risk_appetite)
    setRedLines([...(content.red_lines ?? [])])
  }

  // Only (re)load the form when a *different* version arrives — a background
  // refetch of the same version must never wipe in-progress edits.
  useEffect(() => {
    if (weighting?.content && weighting.id !== loadedVersion) {
      applyContent(weighting.content)
      setLoadedVersion(weighting.id)
    }
  }, [weighting, loadedVersion])

  const draft: WeightingContent = useMemo(
    () => ({
      label,
      defining_priority: definingPriority,
      face_weights: faceWeights,
      kra_ranks: kraRanks,
      risk_appetite: riskAppetite,
      red_lines: redLines,
      preferences: weighting?.content.preferences ?? {},
      // Learned multipliers are changed only through approved proposals; keep them on save
      ...(weighting?.content.kra_multipliers ? { kra_multipliers: weighting.content.kra_multipliers } : {}),
    }),
    [label, definingPriority, faceWeights, kraRanks, riskAppetite, redLines, weighting],
  )

  const dirty = useMemo(() => {
    if (!weighting?.content) return false
    const c = weighting.content
    const near = (a: Record<string, number>, b: Record<string, number>) =>
      Object.keys({ ...a, ...b }).every((k) => Math.abs((a[k] ?? 0) - (b[k] ?? 0)) < 0.005)
    return (
      c.label !== label ||
      c.defining_priority !== definingPriority ||
      !near(c.face_weights, faceWeights) ||
      JSON.stringify(c.kra_ranks) !== JSON.stringify(kraRanks) ||
      c.risk_appetite !== riskAppetite ||
      JSON.stringify(c.red_lines ?? []) !== JSON.stringify(redLines)
    )
  }, [weighting, label, definingPriority, faceWeights, kraRanks, riskAppetite, redLines])

  useEffect(() => {
    if (!dirty) return
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault()
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  const errors = validate(redLines)

  const loadPresetIntoDraft = (presetId: string) => {
    const preset = presets.find((p) => p.id === presetId)
    if (!preset) return
    applyContent(preset.content)
    setConfirmPreset(null)
    setPresetNote(`“${preset.label}” loaded into the editor — not active until you save.`)
  }

  const saveMutation = useMutation({
    mutationFn: () => api.putWeighting(draft),
    onSuccess: () => {
      setPresetNote(null)
      queryClient.invalidateQueries({ queryKey: ['weighting'] })
    },
  })

  const previewMutation = useMutation({
    mutationFn: () => api.previewWeighting(draft),
  })

  const handleDragStart = (index: number) => setDragIndex(index)
  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault()
    if (dragIndex === null || dragIndex === index) return
    setKraRanks((ranks) => {
      const next = [...ranks]
      const [moved] = next.splice(dragIndex, 1)
      next.splice(index, 0, moved)
      return next
    })
    setDragIndex(index)
  }
  const handleDragEnd = () => setDragIndex(null)

  const updateRedLine = (index: number, patch: Partial<RedLine>) => {
    setRedLines((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)))
  }

  const removeRedLine = (index: number) => {
    setRedLines((rows) => rows.filter((_, i) => i !== index))
  }

  const addRedLine = (type: 'level' | 'event') => {
    const id = `RL-${Date.now().toString(36).slice(-4).toUpperCase()}`
    setRedLines((rows) => [...rows, type === 'level' ? emptyLevelRule(id) : emptyEventRule(id)])
  }

  const moveKra = (index: number, dir: -1 | 1) => {
    const next = index + dir
    if (next < 0 || next >= kraRanks.length) return
    setKraRanks((ranks) => {
      const copy = [...ranks]
      ;[copy[index], copy[next]] = [copy[next], copy[index]]
      return copy
    })
  }

  return (
    <div>
      <header className="page-header">
        <h1>Priority weighting</h1>
        <p>
          This quarter&apos;s lens on the Four Faces. Edit, preview the effect on this week, then save — each
          save is kept as a new version, and the next digest uses it.
        </p>
      </header>

      <div className="preset-row" role="group" aria-label="Start from a preset">
        <span className="preset-label">Start from a preset:</span>
        {presets.map((p) => (
          <button
            key={p.id}
            type="button"
            className="chip-toggle"
            onClick={() => (dirty ? setConfirmPreset(p.id) : loadPresetIntoDraft(p.id))}
          >
            {p.label}
          </button>
        ))}
      </div>
      {confirmPreset && (
        <div className="inline-confirm-box" role="alertdialog" aria-label="Replace unsaved edits">
          You have unsaved edits. Replace them with “{presets.find((p) => p.id === confirmPreset)?.label}”?
          <button type="button" className="primary" onClick={() => loadPresetIntoDraft(confirmPreset)}>
            Replace
          </button>
          <button type="button" onClick={() => setConfirmPreset(null)}>
            Keep my edits
          </button>
        </div>
      )}
      {presetNote && <p className="verification-note">{presetNote}</p>}

      <div className="form-section">
        <label htmlFor="label">Label</label>
        <input id="label" value={label} onChange={(e) => setLabel(e.target.value)} />
        <div style={{ marginTop: 14 }}>
          <label htmlFor="priority">Defining priority</label>
          <textarea
            id="priority"
            rows={3}
            value={definingPriority}
            onChange={(e) => setDefiningPriority(e.target.value)}
          />
        </div>
      </div>

      <div className="form-section">
        <h3>Face weights</h3>
        <FaceWeightSliders weights={faceWeights} onChange={setFaceWeights} />
      </div>

      <div className="form-section">
        <h3>KRA rank order</h3>
        <p>Use the arrows to reorder — highest priority first. Drag also works.</p>
        <ul className="kra-rank-list">
          {kraRanks.map((id, index) => {
            const kra: Kra | undefined = kraMap.get(id)
            return (
              <li
                key={id}
                className={`kra-rank-item${dragIndex === index ? ' dragging' : ''}`}
                draggable
                onDragStart={() => handleDragStart(index)}
                onDragOver={(e) => handleDragOver(e, index)}
                onDragEnd={handleDragEnd}
              >
                <span className="handle">{index + 1}</span>
                <span className="name">
                  {kra?.name ?? id}
                </span>
                {kra?.grounding === 'none' && <span className="badge">no live feed</span>}
                <button
                  type="button"
                  disabled={index === 0}
                  onClick={() => moveKra(index, -1)}
                  aria-label={`Move ${kra?.name ?? id} up`}
                >
                  ↑
                </button>
                <button
                  type="button"
                  disabled={index === kraRanks.length - 1}
                  onClick={() => moveKra(index, 1)}
                  aria-label={`Move ${kra?.name ?? id} down`}
                >
                  ↓
                </button>
              </li>
            )
          })}
        </ul>
      </div>

      <div className="form-section">
        <h3>Risk appetite</h3>
        <div className="preset-row" style={{ marginBottom: 0, marginTop: 12 }}>
          {(['low', 'moderate', 'high'] as RiskAppetite[]).map((r) => (
            <button
              key={r}
              type="button"
              className={`chip-toggle${riskAppetite === r ? ' active' : ''}`}
              onClick={() => setRiskAppetite(r)}
            >
              {r}
            </button>
          ))}
        </div>
        <p className="verification-note">{APPETITE_HELP[riskAppetite]}</p>
      </div>

      <div className="form-section">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <h3 style={{ margin: 0 }}>Red lines</h3>
          <span style={{ fontSize: 13, color: 'var(--gray-500)' }}>{redLines.length} active</span>
        </div>
        <table className="redline-table editable">
          <thead>
            <tr>
              <th>ID</th>
              <th>Type</th>
              <th>Description</th>
              <th>Rule</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {redLines.map((rl, index) => (
              <Fragment key={`${rl.id}-${index}`}>
              <tr>
                <td>
                  <input
                    value={rl.id}
                    onChange={(e) => updateRedLine(index, { id: e.target.value })}
                    style={{ width: '5rem' }}
                  />
                </td>
                <td>
                  <select
                    value={rl.type}
                    onChange={(e) => {
                      const type = e.target.value as 'level' | 'event'
                      updateRedLine(
                        index,
                        type === 'level'
                          ? {
                              type: 'level',
                              metric: rl.metric ?? 'cash_balance_cr',
                              operator: rl.operator ?? 'lt',
                              threshold: rl.threshold ?? 22,
                              trigger: undefined,
                              table: undefined,
                              event_type: undefined,
                            }
                          : {
                              type: 'event',
                              trigger: 'on_new_record',
                              table: 'erp_gst_events',
                              event_type: 'show_cause_notice',
                              metric: undefined,
                              operator: undefined,
                              threshold: undefined,
                            },
                      )
                    }}
                  >
                    <option value="level">level</option>
                    <option value="event">event</option>
                  </select>
                </td>
                <td>
                  <input
                    value={rl.description}
                    onChange={(e) => updateRedLine(index, { description: e.target.value })}
                    style={{ minWidth: '12rem' }}
                  />
                </td>
                <td>
                  {rl.type === 'level' ? (
                    <div className="redline-rule-row">
                      <select
                        value={rl.metric ?? ''}
                        onChange={(e) => updateRedLine(index, { metric: e.target.value })}
                      >
                        {metricOptions.map((m) => (
                          <option key={m} value={m}>
                            {metricLabel(m)}
                          </option>
                        ))}
                      </select>
                      <select
                        value={rl.operator ?? 'lt'}
                        onChange={(e) => updateRedLine(index, { operator: e.target.value })}
                      >
                        {OPERATORS.map((o) => (
                          <option key={o.value} value={o.value}>
                            {o.label}
                          </option>
                        ))}
                      </select>
                      <input
                        type="number"
                        step="any"
                        aria-label="Threshold"
                        value={rl.threshold ?? ''}
                        onChange={(e) =>
                          updateRedLine(index, {
                            threshold: e.target.value === '' ? undefined : Number(e.target.value),
                          })
                        }
                        style={{ width: '5rem' }}
                      />
                      <span className="unit">{metricUnit(rl.metric ?? '')}</span>
                    </div>
                  ) : (
                    <div className="redline-rule-row">
                      <select
                        aria-label="Event"
                        value={rl.event_type ?? 'show_cause_notice'}
                        onChange={(e) =>
                          updateRedLine(index, { table: 'erp_gst_events', event_type: e.target.value })
                        }
                      >
                        {EVENT_TYPES.map((ev) => (
                          <option key={ev.value} value={ev.value}>
                            {ev.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </td>
                <td>
                  <button
                    type="button"
                    onClick={() => removeRedLine(index)}
                    aria-label={`Remove red line ${rl.id}`}
                  >
                    Remove
                  </button>
                </td>
              </tr>
              <tr className="redline-sentence-row">
                <td colSpan={5}>
                  {ruleSentence(rl, metricLabel, metricUnit)}
                  <RedLineBacktest rule={rl} unit={rl.metric ? metricUnit(rl.metric) : ''} />
                </td>
              </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
        <div className="preset-row" style={{ marginTop: '0.75rem' }}>
          <button type="button" onClick={() => addRedLine('level')}>
            + Level rule
          </button>
          <button type="button" onClick={() => addRedLine('event')}>
            + Event rule
          </button>
        </div>
      </div>

      <LearningPanel />

      <div className="form-section preview-section">
        <div className="preview-head">
          <h3 style={{ margin: 0 }}>Preview the effect</h3>
          <button
            type="button"
            onClick={() => previewMutation.mutate()}
            disabled={previewMutation.isPending || errors.length > 0}
          >
            {previewMutation.isPending ? 'Re-ranking…' : 'Preview on latest week'}
          </button>
        </div>
        <p className="verification-note">
          Re-ranks the latest week&apos;s numbers under your draft, without saving and without AI calls.
        </p>
        {previewMutation.isError && <p className="error-text">{(previewMutation.error as Error).message}</p>}
        {previewMutation.data && (
          <>
            <p>
              <strong>{formatWeekLabel(previewMutation.data.week)}:</strong> the current weighting surfaces{' '}
              {previewMutation.data.active.surfaced_count} item(s); your draft would surface{' '}
              {previewMutation.data.draft.surfaced_count}.
            </p>
            <div className="preview-grid">
              <PreviewColumn title="Current weighting" ranking={previewMutation.data.active} />
              <PreviewColumn title="Your draft" ranking={previewMutation.data.draft} />
            </div>
          </>
        )}
      </div>

      <div className="save-bar">
        {dirty ? <span className="badge-dirty">Unsaved changes</span> : <span className="verification-note">All changes saved</span>}
        <button
          type="button"
          className="primary"
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending || errors.length > 0 || !dirty}
        >
          {saveMutation.isPending ? 'Saving…' : 'Save as new version'}
        </button>
      </div>
      {errors.length > 0 && (
        <ul className="error-list" role="alert">
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {saveMutation.isError && (
        <p className="error-text">{(saveMutation.error as Error).message}</p>
      )}
      {saveMutation.isSuccess && !dirty && (
        <p className="success-text">Saved. The next digest will use this weighting.</p>
      )}
    </div>
  )
}

function PreviewColumn({ title, ranking }: { title: string; ranking: PreviewRanking }) {
  return (
    <div className="preview-col">
      <h4>{title}</h4>
      {ranking.red_lines.length > 0 && (
        <p className="verification-note">
          Red lines firing: {ranking.red_lines.map((r) => r.description).join('; ')}
        </p>
      )}
      <ol>
        {ranking.top.map((t) => (
          <li key={t.id} className={t.surfaced ? 'surfaced' : 'below-bar'}>
            <span>{t.label}</span>
            <span className="preview-score">{t.surfaced ? 'surfaces' : 'below bar'}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}
