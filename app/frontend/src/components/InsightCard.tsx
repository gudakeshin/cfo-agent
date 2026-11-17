import { useState } from 'react'
import type { DigestItem, Impact, MetricHistory } from '../api/types'
import { useAuth } from '../hooks/useAuth'
import { useMetrics } from '../hooks/useMetrics'
import { citationLabel, formatDelta, formatMetric } from '../utils/format'
import { CitationPopover } from './CitationPopover'
import { FeedbackButtons } from './FeedbackButtons'
import { PriorityTag } from './PriorityTag'
import { Sparkline } from './Sparkline'
import { ContextChips } from './ContextChips'
import { DriverTable } from './DriverTable'

const shortWeek = (w?: string) => (w ?? '').replace(/^\d{4}-/, '')
import { TrackAction } from './TrackAction'

function citationId(c: string | { id?: string; citation_id?: string }): string {
  if (typeof c === 'string') return c
  return c.citation_id ?? c.id ?? ''
}

/** Flatten per-section citation lists into one de-duplicated list of IDs. */
function flattenCitations(raw: DigestItem['citations'] | undefined): string[] {
  if (!raw) return []
  const list = Array.isArray(raw) ? raw : Object.values(raw).flat()
  return [...new Set(list.map(citationId).filter(Boolean))]
}

interface Props {
  item: DigestItem
  feedback?: { verdict: string; already_knew: boolean; note?: string } | null
  onFeedbackChange?: () => void
  onDrilldown?: () => void
  history?: MetricHistory
}

function ImpactLine({ impact }: { impact: Impact }) {
  const bits: string[] = []
  if (impact.amount_inr) bits.push(`Worth ${impact.amount_inr} ${impact.label}`)
  if (impact.per_day_inr) bits.push(`${impact.per_day_inr} per day`)
  if (impact.deadline) bits.push(`by ${impact.deadline}`)
  return (
    <p className="impact-line" title={[impact.basis, impact.deadline_basis && `Deadline: ${impact.deadline_basis}`].filter(Boolean).join(' ')}>
      {bits.join(' · ')}
    </p>
  )
}

export function InsightCard({ item, feedback, onFeedbackChange, onDrilldown, history }: Props) {
  const { labels } = useMetrics()
  const { can } = useAuth()
  const canInteract = can('interact')
  const [activeCitation, setActiveCitation] = useState<string | null>(null)
  const citations = flattenCitations(item.citations)
  const unverified = item.verification?.unverified_figures ?? []
  const continuity = item.score_decomp?.continuity
  const conflicts = item.verification?.continuity_conflicts ?? []

  return (
    <article className={`insight-card${item.is_red_line ? ' red-line' : ''}`}>
      <header>
        <span className="rank">#{item.rank}</span>
        <h3>{item.title}</h3>
        <PriorityTag score={item.priority_score} isRedLine={item.is_red_line} />
        {continuity && !continuity.first_briefing && (
          <span
            className={`badge ${continuity.status === 'continuing' ? 'badge-muted' : 'badge-amber'}`}
            title={
              continuity.status === 'continuing'
                ? `Also in the ${shortWeek(continuity.last_week)} briefing`
                : 'Not in any earlier briefing'
            }
          >
            {continuity.status === 'continuing'
              ? `Continuing since ${shortWeek(continuity.since_week)}`
              : 'New this week'}
          </span>
        )}
      </header>

      {history && <TrendStrip history={history} />}
      <ContextChips context={item.score_decomp?.context} unit={history?.unit ?? ''} />

      <div className="insight-grid">
        <div>
          <div className="field-label what">What</div>
          <p>{item.what}</p>
        </div>
        <div>
          <div className="field-label why">Why it matters</div>
          <p>{item.why}</p>
        </div>
        {item.connects && (
          <details className="connects-detail">
            <summary>Connects to other areas</summary>
            <p>{item.connects}</p>
          </details>
        )}
        {item.action && (
          <div className="action-box">
            <div className="field-label">Recommended action</div>
            <p>{item.action}</p>
            {item.score_decomp?.impact && <ImpactLine impact={item.score_decomp.impact} />}
          </div>
        )}
      </div>

      <DriverTable drivers={item.score_decomp?.drivers} onCite={setActiveCitation} />

      {citations.length > 0 && (
        <div className="citations" aria-label="Evidence">
          <span className="citations-label">Evidence</span>
          {citations.map((id) => {
            return (
              <button
                key={id}
                type="button"
                className="citation-chip"
                onClick={() => setActiveCitation(id)}
                title={id}
              >
                {citationLabel(id, labels)}
              </button>
            )
          })}
        </div>
      )}

      {conflicts.length > 0 && (
        <p className="verification-note">Check the wording: {conflicts.join(' ')}</p>
      )}

      {(unverified.length > 0 || item.verification?.fallback) && (
        <p className="verification-note">
          {item.verification?.fallback && 'Shown with system-generated wording (the AI text failed the citation check). '}
          {unverified.length > 0 &&
            `Check before acting: ${unverified.join(', ')} could not be matched to a cited figure.`}
        </p>
      )}

      {canInteract && <FeedbackButtons
        itemId={item.id}
        initialVerdict={feedback?.verdict as 'acted_on' | 'useful' | 'set_aside' | undefined}
        initialAlreadyKnew={feedback?.already_knew}
        initialNote={feedback?.note ?? ''}
        onChange={onFeedbackChange}
      />}

      {canInteract && <TrackAction item={item} />}

      {onDrilldown && canInteract && (
        <button type="button" onClick={onDrilldown} style={{ marginTop: '0.75rem' }}>
          Discuss →
        </button>
      )}

      {activeCitation && (
        <CitationPopover citationId={activeCitation} digestItemId={item.id} onClose={() => setActiveCitation(null)} />
      )}
    </article>
  )
}

function TrendStrip({ history }: { history: MetricHistory }) {
  const pts = history.points.filter((p) => p.value != null)
  if (pts.length < 2) return null
  const last = pts[pts.length - 1].value as number
  const prev = pts[pts.length - 2].value as number
  return (
    <div className="trend-strip">
      <div>
        <div className="trend-label">{history.label}</div>
        <div className="trend-value">
          {formatMetric(last, history.unit)}{' '}
          <span className="trend-delta">({formatDelta(last - prev, history.unit)} on last week)</span>
        </div>
      </div>
      <Sparkline history={history} />
      <span className="trend-caption">{pts.length}-week trend</span>
    </div>
  )
}
