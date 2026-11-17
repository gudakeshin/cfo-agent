import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import type { BriefingChange, BriefingKpi } from '../api/types'
import { DataHealthBanner } from '../components/DataHealthBanner'
import { DigestItems } from '../components/DigestItems'
import { Sparkline } from '../components/Sparkline'
import { EntityTable } from '../components/EntityTable'
import { ExportShare } from '../components/ExportShare'
import { KraHeatmap } from '../components/KraHeatmap'
import { PeriodLens } from '../components/PeriodLens'
import { extra } from '../api/extra'
import { formatDelta, formatMetric, readPref, writePref } from '../utils/format'
import { formatWeekLabel } from '../utils/weeks'

const INTRO_KEY = 'cfo.intro.dismissed'

function OpenActions() {
  const { data } = useQuery({ queryKey: ['actions', 'all'], queryFn: () => extra.listActions() })
  const open = (data ?? []).filter((a) => a.status === 'open' || a.status === 'in_progress')
  if (!open.length) return null
  const overdue = open.filter((a) => a.overdue)
  return (
    <section className="panel actions-panel no-print" aria-label="Open actions">
      <div className="regulatory-watch-head">
        <h3>
          Commitments in flight · {open.length} open{overdue.length ? `, ${overdue.length} overdue` : ''}
        </h3>
        <Link to="/actions">All actions →</Link>
      </div>
      <ul>
        {open.slice(0, 5).map((a) => (
          <li key={a.id}>
            <span className={a.overdue ? 'error-text' : ''}>{a.title}</span>
            <span className="meta">
              {a.owner || 'unassigned'}
              {a.due_date ? ` · due ${a.due_date}` : ''}
              {a.overdue ? ' · overdue' : ''}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ComingDeadlines({ week }: { week?: string }) {
  const { data } = useQuery({
    queryKey: ['statutory', week],
    queryFn: () => extra.statutory(week),
    enabled: Boolean(week),
    staleTime: 60 * 60_000,
  })
  if (!data || !data.deadlines.length) return null
  return (
    <section className="panel actions-panel no-print" aria-label="Coming deadlines">
      <div className="regulatory-watch-head">
        <h3>Coming deadlines · next {data.deadlines.length}</h3>
      </div>
      <ul>
        {data.deadlines.slice(0, 5).map((d) => (
          <li key={d.id}>
            <span className={d.days_away <= 3 ? 'error-text' : ''}>{d.label}</span>
            <span className="meta">
              {d.authority} · {new Date(d.due_date + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}
              {d.days_away === 0 ? ' · today' : d.days_away === 1 ? ' · tomorrow' : ` · in ${d.days_away} days`}
            </span>
          </li>
        ))}
      </ul>
      <p className="period-note">{data.note}</p>
    </section>
  )
}

function IntroCard() {
  const [hidden, setHidden] = useState(() => readPref(INTRO_KEY) === '1')
  if (hidden) return null
  return (
    <section className="intro-card" aria-label="About this briefing">
      <div>
        <h2>Your Monday briefing</h2>
        <p>
          Each week the agent reads the finance data, ranks what moved by <em>this quarter&apos;s
          priorities</em>, and shows only what clears the bar. Every claim links to its source record —
          click any item under <strong>Evidence</strong>. Red lines you set always appear first.
        </p>
        <p>
          Mark each item <strong>Acted on</strong>, <strong>Useful</strong> or <strong>Set aside</strong>; that
          is how the agent learns what matters to you. <Link to="/guide">How to read this →</Link>
        </p>
      </div>
      <button
        type="button"
        onClick={() => {
          writePref(INTRO_KEY, '1')
          setHidden(true)
        }}
      >
        Got it
      </button>
    </section>
  )
}

function KpiTile({ kpi, history }: { kpi: BriefingKpi; history?: Parameters<typeof Sparkline>[0]['history'] }) {
  const tone = kpi.adverse == null ? '' : kpi.adverse ? ' adverse' : ' favourable'
  return (
    <div className="kpi-tile">
      <div className="label">{kpi.label}</div>
      <div className="value">{formatMetric(kpi.value, kpi.unit)}</div>
      <div className={`kpi-delta${tone}`}>
        {kpi.previous == null ? 'no prior week' : `${formatDelta(kpi.delta, kpi.unit)} on last week`}
      </div>
      {history && <Sparkline history={history} width={140} height={28} />}
    </div>
  )
}

function ChangeList({ title, items, hint }: { title: string; items: BriefingChange[]; hint: string }) {
  return (
    <div className="change-col">
      <h3>
        {title} <span className="count">{items.length}</span>
      </h3>
      {items.length === 0 ? (
        <p className="change-empty">{hint}</p>
      ) : (
        <ul>
          {items.map((c) => (
            <li key={c.item_id}>
              {c.is_red_line && <span className="dot-red" aria-label="Red line" />}
              {title === 'Dropped off' ? <span>{c.title}</span> : <a href={`#item-${c.item_id}`}>{c.title}</a>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function Briefing() {
  const { can } = useAuth()
  const { data: briefing, isLoading, error } = useQuery({ queryKey: ['briefing'], queryFn: api.getBriefing })
  const digestId = briefing?.digest_id ?? null
  const { data: digest } = useQuery({
    queryKey: ['digest', digestId],
    queryFn: () => api.getDigest(digestId!),
    enabled: digestId != null,
  })
  const kpiIds = briefing?.kpis?.map((k) => k.id) ?? []
  const { data: kpiHistory } = useQuery({
    queryKey: ['signal-history', briefing?.week, kpiIds.join(',')],
    queryFn: () => api.getSignalHistory(kpiIds, briefing?.week),
    enabled: kpiIds.length > 0,
    staleTime: 5 * 60_000,
  })

  const { data: newsItems = [] } = useQuery({
    queryKey: ['news', 'watch'],
    queryFn: () => api.listNews({ limit: 20 }),
  })
  const watchItems = newsItems.filter((n) => n.relevance === 'high').slice(0, 3)

  if (isLoading) return <p className="empty-state">Loading your briefing…</p>
  if (error) return <p className="error-text">{(error as Error).message}</p>

  if (!digestId) {
    return (
      <div>
        <IntroCard />
        <header className="page-header">
          <h1>Monday briefing</h1>
        </header>
        <p className="empty-state">
          No digest yet. The first one is generated on the weekly schedule, or your delegate can run one now
          from <Link to="/runs">Digest runs</Link>.
        </p>
      </div>
    )
  }

  const changes = briefing?.changes
  return (
    <div>
      <IntroCard />
      <header className="page-header briefing-header">
        <div>
          <p className="eyebrow">
            Monday briefing · {briefing?.week ? formatWeekLabel(briefing.week) : ''}
            {digest?.weighting_label ? ` · lens: ${digest.weighting_label}` : ''}
          </p>
          <h1>{digest?.headline ?? '…'}</h1>
        </div>
        <div className="header-actions no-print">
          {can('interact') && (
            <Link className="button" to={`/ask?digest=${digestId}`}>
              Ask about this
            </Link>
          )}
          <Link className="button" to={`/digests/${digestId}`}>
            Open as digest
          </Link>
          {digestId != null && <ExportShare digestId={digestId} />}
        </div>
      </header>

      {briefing?.kpis && (
        <section className="kpi-row" aria-label="Key numbers this week">
          {briefing.kpis.map((k) => (
            <KpiTile key={k.id} kpi={k} history={kpiHistory?.[k.id]} />
          ))}
        </section>
      )}

      <PeriodLens week={briefing?.week} />

      {changes && (
        <section className="panel changes-panel" aria-label="What changed">
          <h2>
            What changed since{' '}
            {briefing?.previous_week ? formatWeekLabel(briefing.previous_week) : 'the last digest'}
          </h2>
          {briefing?.previous_digest_id == null ? (
            <p className="change-empty">This is the first digest — everything below is new.</p>
          ) : (
            <div className="change-grid">
              <ChangeList title="New this week" items={changes.new} hint="Nothing new." />
              <ChangeList title="Still open" items={changes.continuing} hint="Nothing carried over." />
              <ChangeList title="Dropped off" items={changes.dropped} hint="Nothing dropped off." />
            </div>
          )}
        </section>
      )}

      <OpenActions />
      <ComingDeadlines week={briefing?.week} />

      {digest && <DataHealthBanner health={digest.data_health} />}
      {digest && <DigestItems digest={digest} />}

      <section className="panel no-print" aria-label="KRA trends">
        <h2 className="section-title">13-week stress by Face</h2>
        <KraHeatmap />
      </section>

      <EntityTable />

      {watchItems.length > 0 && (
        <section className="panel regulatory-watch no-print" aria-label="Regulatory watch">
          <div className="regulatory-watch-head">
            <h3>Regulatory watch · high relevance</h3>
            <Link to="/news">View all →</Link>
          </div>
          <ul>
            {watchItems.map((n) => (
              <li key={n.id}>
                <a href={n.url} target="_blank" rel="noreferrer">
                  {n.title}
                </a>
                {n.source_name && <div className="meta">{n.source_name}</div>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
