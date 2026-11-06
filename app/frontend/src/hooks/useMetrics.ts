import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { MetricMeta } from '../api/types'

/** Metric metadata (labels, units, adverse direction), cached for the session. */
export function useMetrics() {
  const { data = [] } = useQuery({
    queryKey: ['metrics'],
    queryFn: api.listMetrics,
    staleTime: Infinity,
  })
  const byId = new Map<string, MetricMeta>(data.map((m) => [m.id, m]))
  const labels = new Map<string, string>(data.map((m) => [m.id, m.label]))
  return { metrics: data, byId, labels }
}
