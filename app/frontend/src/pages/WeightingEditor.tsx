import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { Kra, RedLine, RiskAppetite, WeightingContent } from '../api/types'
import { FaceWeightSliders } from '../components/FaceWeightSliders'

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
  { value: 'lt', label: '<' },
  { value: 'lte', label: '≤' },
  { value: 'gt', label: '>' },
  { value: 'gte', label: '≥' },
  { value: 'eq', label: '=' },
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
  const metricOptions = kraFramework?.metric_map
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

  useEffect(() => {
    if (weighting?.content) applyContent(weighting.content)
  }, [weighting])

  const loadPresetMutation = useMutation({
    mutationFn: (presetId: string) => api.loadWeightingPreset(presetId),
    onSuccess: (data) => {
      applyContent(data.content)
      queryClient.invalidateQueries({ queryKey: ['weighting'] })
    },
  })

  const saveMutation = useMutation({
    mutationFn: () =>
      api.putWeighting({
        label,
        defining_priority: definingPriority,
        face_weights: faceWeights,
        kra_ranks: kraRanks,
        risk_appetite: riskAppetite,
        red_lines: redLines,
        preferences: weighting?.content.preferences ?? {},
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['weighting'] }),
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
        <p>This quarter&apos;s lens on the Four Faces — saving creates a new immutable version.</p>
      </header>

      <div className="preset-row">
        {presets.map((p) => (
          <button
            key={p.id}
            type="button"
            className="chip-toggle"
            onClick={() => loadPresetMutation.mutate(p.id)}
            disabled={loadPresetMutation.isPending}
          >
            {p.label}
          </button>
        ))}
      </div>

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
                <button type="button" disabled={index === 0} onClick={() => moveKra(index, -1)}>
                  ↑
                </button>
                <button
                  type="button"
                  disabled={index === kraRanks.length - 1}
                  onClick={() => moveKra(index, 1)}
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
              <tr key={`${rl.id}-${index}`}>
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
                            {m}
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
                        value={rl.threshold ?? 0}
                        onChange={(e) =>
                          updateRedLine(index, { threshold: Number(e.target.value) })
                        }
                        style={{ width: '5rem' }}
                      />
                    </div>
                  ) : (
                    <div className="redline-rule-row">
                      <input
                        value={rl.table ?? ''}
                        onChange={(e) => updateRedLine(index, { table: e.target.value })}
                        placeholder="table"
                        style={{ width: '8rem' }}
                      />
                      <input
                        value={rl.event_type ?? ''}
                        onChange={(e) => updateRedLine(index, { event_type: e.target.value })}
                        placeholder="event_type"
                        style={{ width: '10rem' }}
                      />
                    </div>
                  )}
                </td>
                <td>
                  <button type="button" onClick={() => removeRedLine(index)}>
                    Remove
                  </button>
                </td>
              </tr>
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

      <button
        type="button"
        className="primary"
        onClick={() => saveMutation.mutate()}
        disabled={saveMutation.isPending}
      >
        Save weighting
      </button>
      {saveMutation.isError && (
        <p className="error-text">{(saveMutation.error as Error).message}</p>
      )}
      {saveMutation.isSuccess && <p className="success-text">Weighting saved as new version.</p>}
    </div>
  )
}
