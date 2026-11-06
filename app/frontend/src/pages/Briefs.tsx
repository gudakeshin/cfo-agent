import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api/client'
import { extra, type MeetingBrief } from '../api/extra'
import { useAuth } from '../hooks/useAuth'

const AUDIENCE_LABEL: Record<string, string> = {
  board: 'Board of Directors',
  audit_committee: 'Audit Committee',
  lenders: 'Lenders',
  investors: 'Investors / analysts',
  leadership: 'Leadership team',
}

export function Briefs() {
  const qc = useQueryClient()
  const { can } = useAuth()
  const { data: list = [] } = useQuery({ queryKey: ['briefs'], queryFn: extra.listBriefs })
  const { data: kra } = useQuery({ queryKey: ['kra'], queryFn: api.getKra })
  const [selected, setSelected] = useState<number | null>(null)
  const current = useQuery({
    queryKey: ['brief', selected],
    queryFn: () => extra.getBrief(selected!),
    enabled: selected != null,
  })
  const [title, setTitle] = useState('')
  const [audience, setAudience] = useState('board')
  const [date, setDate] = useState('')
  const [notes, setNotes] = useState('')
  const [kras, setKras] = useState<string[]>([])
  const create = useMutation({
    mutationFn: () => extra.createBrief({ title, audience, meeting_date: date || null, kra_ids: kras, notes }),
    onSuccess: (b) => {
      qc.invalidateQueries({ queryKey: ['briefs'] })
      qc.setQueryData(['brief', b.id], b)
      setSelected(b.id)
    },
  })
  const grounded = (kra?.kras ?? []).filter((k) => k.grounding === 'erp')
  return (
    <div>
      <header className="page-header">
        <h1>Meeting briefs</h1>
        <p>A one-page brief for a specific meeting, from this week&apos;s numbers, open red lines and commitments. Every point is checked against its sources.</p>
      </header>
      <div className="brief-layout">
        <div>
          {can('interact') && (
            <form
              className="form-section brief-form"
              onSubmit={(e) => {
                e.preventDefault()
                create.mutate()
              }}
            >
              <h3 style={{ marginTop: 0 }}>New brief</h3>
              <label>
                Meeting
                <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Q3 board meeting" required minLength={3} />
              </label>
              <label>
                Audience
                <select value={audience} onChange={(e) => setAudience(e.target.value)}>
                  {Object.entries(AUDIENCE_LABEL).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Date
                <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
              </label>
              <fieldset>
                <legend>Focus (optional — all KRAs if none)</legend>
                <div className="row-chips">
                  {grounded.map((k) => (
                    <button
                      key={k.id}
                      type="button"
                      className={`chip-toggle${kras.includes(k.id) ? ' active' : ''}`}
                      aria-pressed={kras.includes(k.id)}
                      onClick={() => setKras((xs) => (xs.includes(k.id) ? xs.filter((x) => x !== k.id) : [...xs, k.id]))}
                    >
                      {k.name}
                    </button>
                  ))}
                </div>
              </fieldset>
              <label>
                What you want to land (optional)
                <textarea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000} />
              </label>
              <button type="submit" className="primary" disabled={create.isPending}>
                {create.isPending ? 'Preparing… (about 30 s)' : 'Prepare brief'}
              </button>
              {create.isError && <p className="error-text">{(create.error as Error).message}</p>}
            </form>
          )}
          <h3>Earlier briefs</h3>
          <ul className="brief-list">
            {list.map((b) => (
              <li key={b.id}>
                <button type="button" className={`link-button${selected === b.id ? ' active' : ''}`} onClick={() => setSelected(b.id)}>
                  {b.title}
                </button>
                <span className="toolbar-hint">
                  {AUDIENCE_LABEL[b.audience] ?? b.audience} · {b.meeting_date ?? b.week}
                </span>
              </li>
            ))}
            {list.length === 0 && <li className="empty-state">None yet.</li>}
          </ul>
        </div>
        <div>{current.data ? <BriefView brief={current.data} /> : <p className="empty-state">Prepare or open a brief.</p>}</div>
      </div>
    </div>
  )
}

function BriefView({ brief }: { brief: MeetingBrief }) {
  const v = brief.verification
  const [open, setOpen] = useState<string | null>(null)
  const cite = (ids: string[]) =>
    ids.map((c, i) => (
      <button key={c} type="button" className="citation-chip small" title={brief.evidence[c] ?? c} onClick={() => setOpen(c)}>
        {i + 1}
      </button>
    ))
  return (
    <article className="panel brief-view">
      <p className="eyebrow">
        {AUDIENCE_LABEL[brief.audience] ?? brief.audience} · {brief.meeting_date ?? ''} · data as of {brief.week}
      </p>
      <h2>{brief.content.headline}</h2>
      {brief.content.sections.map((s) => (
        <section key={s.heading}>
          <h3>{s.heading}</h3>
          <ul>
            {s.points.map((p) => (
              <li key={p.text}>
                {p.text} {cite(p.citations)}
              </li>
            ))}
          </ul>
        </section>
      ))}
      {brief.content.likely_questions.length > 0 && (
        <section>
          <h3>Questions to expect</h3>
          <dl className="brief-qa">
            {brief.content.likely_questions.map((q) => (
              <div key={q.question}>
                <dt>{q.question}</dt>
                <dd>
                  {q.suggested_answer} {cite(q.citations)}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      {open && <p className="verification-note">Source: {brief.evidence[open] ?? open}</p>}
      {(v.fallback || (v.unverified_figures?.length ?? 0) > 0 || (v.dropped_uncited?.length ?? 0) > 0) && (
        <p className="verification-note">
          {v.fallback && 'Assembled from the numbers directly (the AI draft was unavailable or failed the checks). '}
          {(v.dropped_uncited?.length ?? 0) > 0 && `${v.dropped_uncited!.length} uncited point(s) were removed. `}
          {(v.unverified_figures?.length ?? 0) > 0 && `Check before use: ${v.unverified_figures!.join(', ')} could not be matched to source data.`}
        </p>
      )}
      <div className="preset-row no-print">
        <button type="button" onClick={() => window.print()}>
          Print / save as PDF
        </button>
      </div>
    </article>
  )
}
