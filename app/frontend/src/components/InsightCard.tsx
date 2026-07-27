import { useState } from 'react'
import type { DigestItem } from '../api/types'
import { CitationPopover } from './CitationPopover'
import { FeedbackButtons } from './FeedbackButtons'

function citationId(c: string | { id?: string; citation_id?: string }): string {
  if (typeof c === 'string') return c
  return c.citation_id ?? c.id ?? ''
}

interface Props {
  item: DigestItem
  feedback?: { verdict: string; already_knew: boolean; note?: string } | null
  onFeedbackChange?: () => void
  onDrilldown?: () => void
}

export function InsightCard({ item, feedback, onFeedbackChange, onDrilldown }: Props) {
  const [activeCitation, setActiveCitation] = useState<string | null>(null)
  const citations = item.citations ?? []

  return (
    <article className={`insight-card${item.is_red_line ? ' red-line' : ''}`}>
      <header>
        <span className="rank">#{item.rank}</span>
        <h3>{item.title}</h3>
        {item.is_red_line && <span className="badge-red">Red line</span>}
        <span className="score">score {item.priority_score.toFixed(2)}</span>
      </header>

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
          <div>
            <div className="field-label connects">Connects</div>
            <p>{item.connects}</p>
          </div>
        )}
        {item.action && (
          <div className="action-box">
            <div className="field-label">Recommended action</div>
            <p>{item.action}</p>
          </div>
        )}
      </div>

      {citations.length > 0 && (
        <div className="citations">
          {citations.map((c, i) => {
            const id = citationId(c)
            if (!id) return null
            return (
              <button
                key={`${id}-${i}`}
                type="button"
                className="citation-chip"
                onClick={() => setActiveCitation(id)}
              >
                {id}
              </button>
            )
          })}
        </div>
      )}

      <FeedbackButtons
        itemId={item.id}
        initialVerdict={feedback?.verdict as 'acted_on' | 'useful' | 'set_aside' | undefined}
        initialAlreadyKnew={feedback?.already_knew}
        initialNote={feedback?.note ?? ''}
        onChange={onFeedbackChange}
      />

      {onDrilldown && (
        <button type="button" onClick={onDrilldown} style={{ marginTop: '0.75rem' }}>
          Discuss →
        </button>
      )}

      {activeCitation && (
        <CitationPopover citationId={activeCitation} onClose={() => setActiveCitation(null)} />
      )}
    </article>
  )
}
