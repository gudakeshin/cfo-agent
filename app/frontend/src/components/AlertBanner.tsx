import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { formatWeekLabel } from '../utils/weeks'

export function AlertBanner() {
  const queryClient = useQueryClient()
  const { data: alerts = [] } = useQuery({
    queryKey: ['alerts', 'active'],
    queryFn: () => api.listAlerts(true),
    refetchInterval: 30_000,
    refetchIntervalInBackground: true,
  })

  const ackMutation = useMutation({
    mutationFn: (id: number) => api.ackAlert(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
    },
  })

  if (alerts.length === 0) return null

  return (
    <div className="alert-banner" role="alert">
      <ul>
        {alerts.map((a) => (
          <li key={a.id}>
            <span className="badge-red">Red line</span>
            <strong>{a.title}</strong>
            <span style={{ color: 'var(--gray-600)' }}>· {formatWeekLabel(a.week)}</span>
            <button
              type="button"
              className="ack-btn"
              onClick={() => ackMutation.mutate(a.id)}
              disabled={ackMutation.isPending}
            >
              Acknowledge
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
