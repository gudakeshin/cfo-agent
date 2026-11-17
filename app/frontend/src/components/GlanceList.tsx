import type { DigestItem } from '../api/types'
import { PriorityTag } from './PriorityTag'

/** The whole week on one screen: one line per insight, each jumping to its card.
 * Detail (why, connections, evidence, breakdown) stays on the cards below. */
export function GlanceList({ items }: { items: DigestItem[] }) {
  if (items.length < 2) return null
  return (
    <nav className="glance panel" aria-label="This week at a glance">
      <h2 className="section-title">This week at a glance</h2>
      <ol>
        {items.map((item) => (
          <li key={item.id}>
            <a href={`#item-${item.id}`}>
              <span className="glance-rank">{item.rank}</span>
              <span className="glance-title">{item.title}</span>
              <PriorityTag score={item.priority_score} isRedLine={item.is_red_line} />
            </a>
          </li>
        ))}
      </ol>
    </nav>
  )
}
