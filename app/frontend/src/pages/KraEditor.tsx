import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { useMetrics } from '../hooks/useMetrics'
import type { FaceKey, Kra, KraRelationship } from '../api/types'
import { TemplatePicker } from '../components/TemplatePicker'

const FACES: FaceKey[] = ['steward', 'operator', 'strategist', 'catalyst']
const FACE_LABELS: Record<FaceKey, string> = {
  steward: 'Steward',
  operator: 'Operator',
  strategist: 'Strategist',
  catalyst: 'Catalyst',
}
const FACE_PREFIX: Record<FaceKey, string> = {
  steward: 'STW',
  operator: 'OPR',
  strategist: 'STR',
  catalyst: 'CAT',
}

type WorkingKra = Kra

function nextKraId(face: FaceKey, kras: WorkingKra[]): string {
  const prefix = FACE_PREFIX[face]
  const nums = kras
    .filter((k) => k.id.startsWith(`${prefix}-`))
    .map((k) => parseInt(k.id.slice(prefix.length + 1), 10))
    .filter((n) => !Number.isNaN(n))
  const next = (nums.length ? Math.max(...nums) : 0) + 1
  return `${prefix}-${next}`
}

function nextRelId(rels: KraRelationship[]): string {
  const nums = rels
    .map((r) => parseInt(r.id.replace(/^REL-/, ''), 10))
    .filter((n) => !Number.isNaN(n))
  const next = (nums.length ? Math.max(...nums) : 0) + 1
  return `REL-${next}`
}

export function KraEditor() {
  const { labels: metricLabels } = useMetrics()
  const queryClient = useQueryClient()

  const [label, setLabel] = useState('')
  const [regulatoryContext, setRegulatoryContext] = useState('')
  const [kras, setKras] = useState<WorkingKra[]>([])
  const [relationships, setRelationships] = useState<KraRelationship[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [warnings, setWarnings] = useState<string[]>([])

  const { data: framework } = useQuery({ queryKey: ['kra'], queryFn: api.getKra })
  const { data: weighting } = useQuery({ queryKey: ['weighting'], queryFn: api.getWeighting })

  const { data: versions = [] } = useQuery({
    queryKey: ['kra-versions'],
    queryFn: api.listKraVersions,
    enabled: historyOpen,
  })

  useEffect(() => {
    if (!framework) return
    setLabel(framework.label)
    setRegulatoryContext(framework.india_regulatory_context)
    setKras(framework.kras.map((k) => ({ ...k })))
    setRelationships(framework.relationships.map((r) => ({ ...r })))
    setWarnings([])
  }, [framework])

  const loadTemplate = (body: Record<string, unknown>, name: string) => {
    const faces = (body.faces ?? {}) as Record<string, { kras?: Partial<Kra>[] }>
    const loaded: WorkingKra[] = []
    for (const [face, f] of Object.entries(faces)) {
      for (const k of f.kras ?? []) {
        loaded.push({
          id: String(k.id), name: String(k.name ?? ''), face: face as FaceKey,
          grounding: String(k.grounding ?? 'erp'), metrics: [...(k.metrics ?? [])],
          concerns: [...(k.concerns ?? [])], note: k.note ?? '',
        } as WorkingKra)
      }
    }
    const rels = ((body.relationships ?? []) as KraRelationship[]).map((r) => ({ ...r }))
    setLabel(String(body.label ?? name))
    setRegulatoryContext(String(body.india_regulatory_context ?? ''))
    setKras(loaded)
    setRelationships(rels)
    setNotice(`Loaded the “${name}” template — review it, then Save to create a new framework version.`)
  }

  const registryMetrics = framework?.registry_metrics ?? []
  const rankedIds = new Set(weighting?.content.kra_ranks ?? [])

  const saveMutation = useMutation({
    mutationFn: () =>
      api.putKra({
        label,
        india_regulatory_context: regulatoryContext,
        kras: kras.map(({ id, name, face, grounding, metrics, concerns, note }) => ({
          id,
          name,
          face,
          grounding,
          metrics,
          concerns,
          note,
        })),
        relationships,
      }),
    onSuccess: (data) => {
      setWarnings(data.warnings ?? [])
      queryClient.invalidateQueries({ queryKey: ['kra'] })
      queryClient.invalidateQueries({ queryKey: ['kra-versions'] })
      queryClient.invalidateQueries({ queryKey: ['weighting'] })
      if (data.reconciled_weighting) {
        setNotice(
          `Weighting auto-reconciled — new version "${data.reconciled_weighting.label}" created (stripped ranks to deleted KRAs).`,
        )
      }
    },
  })

  const restoreMutation = useMutation({
    mutationFn: (versionId: number) => api.restoreKraVersion(versionId),
    onSuccess: (data) => {
      setWarnings(data.warnings ?? [])
      setNotice(`Restored version from #${data.restored_from} as new version.`)
      queryClient.invalidateQueries({ queryKey: ['kra'] })
      queryClient.invalidateQueries({ queryKey: ['kra-versions'] })
      queryClient.invalidateQueries({ queryKey: ['weighting'] })
    },
  })

  const hardErrors: string[] =
    saveMutation.isError && saveMutation.error
      ? (saveMutation.error as Error).message.split(';').map((s) => s.trim()).filter(Boolean)
      : []

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const updateKra = (id: string, patch: Partial<WorkingKra>) => {
    setKras((ks) => ks.map((k) => (k.id === id ? { ...k, ...patch } : k)))
  }

  const toggleMetric = (id: string, metric: string) => {
    setKras((ks) =>
      ks.map((k) => {
        if (k.id !== id) return k
        const has = k.metrics.includes(metric)
        return { ...k, metrics: has ? k.metrics.filter((m) => m !== metric) : [...k.metrics, metric] }
      }),
    )
  }

  const setGrounding = (id: string, grounding: string) => {
    updateKra(id, { grounding, metrics: grounding === 'erp' ? kras.find((k) => k.id === id)?.metrics ?? [] : [] })
  }

  const addConcern = (id: string, text: string) => {
    if (!text.trim()) return
    setKras((ks) =>
      ks.map((k) => (k.id === id ? { ...k, concerns: [...k.concerns, text.trim()] } : k)),
    )
  }

  const removeConcern = (id: string, index: number) => {
    setKras((ks) =>
      ks.map((k) => (k.id === id ? { ...k, concerns: k.concerns.filter((_, i) => i !== index) } : k)),
    )
  }

  const addKra = (face: FaceKey) => {
    const id = nextKraId(face, kras)
    setKras((ks) => [
      ...ks,
      { id, name: 'New KRA', face, grounding: 'none', metrics: [], concerns: [], note: '' },
    ])
    setExpanded((prev) => new Set(prev).add(id))
  }

  const confirmDeleteKra = (id: string) => {
    const affectedRels = relationships.filter((r) => r.from === id || r.to === id)
    setKras((ks) => ks.filter((k) => k.id !== id))
    setRelationships((rs) => rs.filter((r) => r.from !== id && r.to !== id))
    setDeleteConfirmId(null)
    if (affectedRels.length) {
      setNotice(
        `Removed KRA ${id} and ${affectedRels.length} relationship${affectedRels.length === 1 ? '' : 's'} referencing it (${affectedRels.map((r) => r.id).join(', ')}).`,
      )
    } else {
      setNotice(`Removed KRA ${id}.`)
    }
  }

  const addRelationship = () => {
    if (kras.length < 2) return
    const id = nextRelId(relationships)
    setRelationships((rs) => [
      ...rs,
      { id, from: kras[0].id, to: kras[1]?.id ?? kras[0].id, description: '' },
    ])
  }

  const updateRelationship = (id: string, patch: Partial<KraRelationship>) => {
    setRelationships((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch } : r)))
  }

  const removeRelationship = (id: string) => {
    setRelationships((rs) => rs.filter((r) => r.id !== id))
  }

  return (
    <div>
      <header className="page-header">
        <h1>KRA framework</h1>
        <p>Key Result Areas across the Four Faces — saving creates a new immutable version.</p>
      </header>

      <div style={{ display: 'flex', gap: 10, marginBottom: 20 }}>
        <button type="button" onClick={() => setHistoryOpen(true)}>
          Version history
        </button>
      </div>

      {notice && <p className="success-text">{notice}</p>}

      <TemplatePicker onLoad={loadTemplate} />

      <div className="form-section">
        <label htmlFor="kra-label">Label</label>
        <input id="kra-label" value={label} onChange={(e) => setLabel(e.target.value)} />
        <div style={{ marginTop: 14 }}>
          <label htmlFor="kra-context">India regulatory context</label>
          <textarea
            id="kra-context"
            rows={4}
            value={regulatoryContext}
            onChange={(e) => setRegulatoryContext(e.target.value)}
          />
        </div>
      </div>

      {FACES.map((face) => {
        const faceKras = kras.filter((k) => k.face === face)
        return (
          <div className="form-section" key={face}>
            <h3>{FACE_LABELS[face]}</h3>
            <ul className="kra-editor-list">
              {faceKras.map((k) => {
                const isOpen = expanded.has(k.id)
                const rankWarning = rankedIds.has(k.id)
                return (
                  <li key={k.id} className="kra-editor-row">
                    <button
                      type="button"
                      className="kra-editor-row-head"
                      onClick={() => toggleExpanded(k.id)}
                    >
                      <span className="handle">{k.id}</span>
                      <span className="name">{k.name}</span>
                      <span className="badge-muted">
                        {k.grounding === 'erp' ? 'ERP-grounded' : 'no live feed'}
                      </span>
                      {rankWarning && <span className="badge-amber">ranked in weighting</span>}
                      <span className="kra-editor-chevron">{isOpen ? '−' : '+'}</span>
                    </button>

                    {isOpen && (
                      <div className="kra-editor-row-body">
                        <div style={{ marginBottom: 14 }}>
                          <label htmlFor={`name-${k.id}`}>Name</label>
                          <input
                            id={`name-${k.id}`}
                            value={k.name}
                            onChange={(e) => updateKra(k.id, { name: e.target.value })}
                          />
                        </div>
                        <div style={{ marginBottom: 14 }}>
                          <label htmlFor={`grounding-${k.id}`}>Grounding</label>
                          <select
                            id={`grounding-${k.id}`}
                            value={k.grounding}
                            onChange={(e) => setGrounding(k.id, e.target.value)}
                          >
                            <option value="erp">ERP-grounded</option>
                            <option value="none">No live feed</option>
                          </select>
                        </div>
                        {k.grounding === 'erp' && (
                          <div style={{ marginBottom: 14 }}>
                            <label>Metrics</label>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                              {registryMetrics.map((m) => (
                                <button
                                  key={m}
                                  type="button"
                                  className={`chip-toggle${k.metrics.includes(m) ? ' active' : ''}`}
                                  onClick={() => toggleMetric(k.id, m)}
                                  aria-pressed={k.metrics.includes(m)}
                                  title={m}
                                >
                                  {metricLabels.get(m) ?? m}
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                        <div style={{ marginBottom: 14 }}>
                          <label>Concerns</label>
                          <ul className="kra-concern-list">
                            {k.concerns.map((c, idx) => (
                              <li key={idx}>
                                <span>{c}</span>
                                <button type="button" onClick={() => removeConcern(k.id, idx)}>
                                  Remove
                                </button>
                              </li>
                            ))}
                          </ul>
                          <ConcernAdder onAdd={(text) => addConcern(k.id, text)} />
                        </div>
                        <div style={{ marginBottom: 14 }}>
                          <label htmlFor={`note-${k.id}`}>Note</label>
                          <textarea
                            id={`note-${k.id}`}
                            rows={2}
                            value={k.note}
                            onChange={(e) => updateKra(k.id, { note: e.target.value })}
                          />
                        </div>

                        {deleteConfirmId === k.id ? (
                          <div className="confirm-box" role="alertdialog">
                            <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 6 }}>
                              Delete {k.id}?
                            </div>
                            <p style={{ margin: '0 0 12px', fontSize: 13.5, color: 'var(--gray-600)' }}>
                              {rankWarning &&
                                `This KRA is ranked in the active weighting — its rank will be stripped on next save. `}
                              {relationships.some((r) => r.from === k.id || r.to === k.id) &&
                                `${relationships.filter((r) => r.from === k.id || r.to === k.id).length} relationship(s) referencing it will also be removed.`}
                            </p>
                            <div style={{ display: 'flex', gap: 8 }}>
                              <button
                                type="button"
                                className="danger"
                                onClick={() => confirmDeleteKra(k.id)}
                              >
                                Yes, delete
                              </button>
                              <button type="button" onClick={() => setDeleteConfirmId(null)}>
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <button
                            type="button"
                            className="ghost-danger"
                            onClick={() => setDeleteConfirmId(k.id)}
                          >
                            Delete KRA
                          </button>
                        )}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
            <button type="button" onClick={() => addKra(face)} style={{ marginTop: 12 }}>
              + Add KRA
            </button>
          </div>
        )
      })}

      <div className="form-section">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <h3 style={{ margin: 0 }}>Relationships</h3>
          <span style={{ fontSize: 13, color: 'var(--gray-500)' }}>{relationships.length} total</span>
        </div>
        <table className="redline-table editable">
          <thead>
            <tr>
              <th>ID</th>
              <th>From</th>
              <th>To</th>
              <th>Description</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {relationships.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>
                  <select value={r.from} onChange={(e) => updateRelationship(r.id, { from: e.target.value })}>
                    {kras.map((k) => (
                      <option key={k.id} value={k.id}>
                        {k.id} · {k.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <select value={r.to} onChange={(e) => updateRelationship(r.id, { to: e.target.value })}>
                    {kras.map((k) => (
                      <option key={k.id} value={k.id}>
                        {k.id} · {k.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    value={r.description}
                    onChange={(e) => updateRelationship(r.id, { description: e.target.value })}
                    style={{ minWidth: '18rem' }}
                  />
                </td>
                <td>
                  <button type="button" onClick={() => removeRelationship(r.id)}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button type="button" onClick={addRelationship} disabled={kras.length < 2} style={{ marginTop: 12 }}>
          + Add relationship
        </button>
      </div>

      {hardErrors.length > 0 && (
        <div className="confirm-box" role="alert" style={{ marginBottom: 14 }}>
          <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 6 }}>
            Could not save — fix these first
          </div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {hardErrors.map((e, i) => (
              <li key={i} className="error-text" style={{ marginTop: 2 }}>
                {e}
              </li>
            ))}
          </ul>
        </div>
      )}

      {warnings.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
          {warnings.map((w, i) => (
            <span key={i} className="badge-amber">
              {w}
            </span>
          ))}
        </div>
      )}

      <button
        type="button"
        className="primary"
        onClick={() => saveMutation.mutate()}
        disabled={saveMutation.isPending}
      >
        Save as new version
      </button>
      {saveMutation.isSuccess && !hardErrors.length && (
        <p className="success-text">Saved as version {saveMutation.data.version.version}.</p>
      )}

      {historyOpen && (
        <div className="side-panel">
          <header>
            <strong>Version history</strong>
            <button type="button" onClick={() => setHistoryOpen(false)}>
              Close
            </button>
          </header>
          <div style={{ padding: '1rem 1.25rem', overflowY: 'auto', flex: 1 }}>
            {versions.length === 0 && <p className="empty-state">No versions yet.</p>}
            <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 10 }}>
              {versions.map((v) => (
                <li key={v.id} className="kra-version-row">
                  <div>
                    <strong>v{v.version}</strong>
                    {v.is_active && <span className="badge-muted" style={{ marginLeft: 8 }}>active</span>}
                    <div style={{ fontSize: 12, color: 'var(--gray-500)' }}>
                      {new Date(v.created_at).toLocaleString()}
                    </div>
                  </div>
                  {!v.is_active && (
                    <button
                      type="button"
                      onClick={() => restoreMutation.mutate(v.id)}
                      disabled={restoreMutation.isPending}
                    >
                      Restore
                    </button>
                  )}
                </li>
              ))}
            </ul>
            {restoreMutation.isError && (
              <p className="error-text">{(restoreMutation.error as Error).message}</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ConcernAdder({ onAdd }: { onAdd: (text: string) => void }) {
  const [text, setText] = useState('')
  return (
    <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && text.trim()) {
            onAdd(text)
            setText('')
          }
        }}
        placeholder="Add a concern…"
      />
      <button
        type="button"
        onClick={() => {
          if (text.trim()) {
            onAdd(text)
            setText('')
          }
        }}
      >
        Add
      </button>
    </div>
  )
}
