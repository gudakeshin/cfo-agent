import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { DigestDetail } from '../api/types'
import { metricIdFromSignal } from '../utils/format'
import { DrilldownChat } from './DrilldownChat'
import { GlanceList } from './GlanceList'
import { InsightCard } from './InsightCard'

/** The insight list for one digest: cards with trend lines, feedback and drill-down. */
export function DigestItems({ digest }: { digest: DigestDetail }) {
  const [chatItem, setChatItem] = useState<{ id: number; title: string } | null>(null)
  const queryClient = useQueryClient()

  const metricByItem = new Map<number, string>()
  for (const item of digest.items) {
    const m = (item.signal_ids ?? []).map(metricIdFromSignal).find(Boolean)
    if (m) metricByItem.set(item.id, m)
  }
  const metricIds = [...new Set(metricByItem.values())]

  const { data: history } = useQuery({
    queryKey: ['signal-history', digest.week, metricIds.join(',')],
    queryFn: () => api.getSignalHistory(metricIds, digest.week),
    enabled: metricIds.length > 0,
    staleTime: 5 * 60_000,
  })

  const { data: feedbackLog } = useQuery({ queryKey: ['feedback'], queryFn: api.listFeedback })
  const feedbackByItem = new Map(feedbackLog?.items.map((f) => [f.digest_item_id, f]) ?? [])

  if (digest.items.length === 0) {
    return (
      <p className="empty-state">
        Nothing met the bar this week under the current risk appetite — no padding, by design.
      </p>
    )
  }

  return (
    <>
      <GlanceList items={digest.items} />
      <div className="insight-list">
        {digest.items.map((item) => {
          const m = metricByItem.get(item.id)
          return (
            <div key={item.id} id={`item-${item.id}`} className="insight-anchor">
              <InsightCard
                item={item}
                history={m ? history?.[m] : undefined}
                feedback={feedbackByItem.get(item.id)}
                onFeedbackChange={() => queryClient.invalidateQueries({ queryKey: ['feedback'] })}
                onDrilldown={() => setChatItem({ id: item.id, title: item.title })}
              />
            </div>
          )
        })}
      </div>
      {chatItem && (
        <DrilldownChat itemId={chatItem.id} itemTitle={chatItem.title} onClose={() => setChatItem(null)} />
      )}
    </>
  )
}
