import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ConnectorConfig } from '../api/types'
import { ConnectorConfigModal } from './ConnectorConfigModal'

interface Props {
  connector: ConnectorConfig
}

function statusBadgeClass(status: string): string {
  if (status === 'ok') return 'badge-connector ok'
  if (status === 'error') return 'badge-connector error'
  if (status === 'running') return 'badge-connector running'
  return 'badge-connector muted'
}

function statusLabel(status: string): string {
  if (status === 'never_run') return 'Never run'
  return status
}

export function ConnectorCard({ connector }: Props) {
  const [configOpen, setConfigOpen] = useState(false)
  const [activeRunId, setActiveRunId] = useState<number | null>(null)
  const queryClient = useQueryClient()

  const { data: run } = useQuery({
    queryKey: ['connector-run', activeRunId],
    queryFn: () => api.getConnectorRun(activeRunId!),
    enabled: activeRunId != null,
    refetchInterval: (q) => {
      const s = q.state.data?.status
      if (!s || s !== 'running') return false
      return 1500
    },
    refetchIntervalInBackground: true,
  })

  useEffect(() => {
    if (run && run.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['connectors'] })
    }
  }, [run, queryClient])

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) => api.putConnector(connector.id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['connectors'] }),
  })

  const syncMutation = useMutation({
    mutationFn: () => api.syncConnector(connector.id, 'incremental'),
    onSuccess: (data) => setActiveRunId(data.run_id),
  })

  const running = connector.last_status === 'running' || run?.status === 'running'

  return (
    <div className="connector-card">
      <div className="connector-card-head">
        <span className="connector-avatar" style={{ background: connector.brand_color }}>
          {connector.brand_initials}
        </span>
        <div className="connector-card-title">
          <strong>{connector.display_name}</strong>
          <span className="connector-vendor">{connector.vendor}</span>
        </div>
        <span className={statusBadgeClass(running ? 'running' : connector.last_status)}>
          {running && <span className="spinner" />}
          {statusLabel(running ? 'running' : connector.last_status)}
        </span>
      </div>

      <dl className="connector-meta">
        <div>
          <dt>Last sync</dt>
          <dd>{connector.last_run_at ? new Date(connector.last_run_at).toLocaleString() : '—'}</dd>
        </div>
        <div>
          <dt>Watermark week</dt>
          <dd>{connector.watermark_week ?? '—'}</dd>
        </div>
        <div>
          <dt>Owned tables</dt>
          <dd>{connector.owned_tables.length}</dd>
        </div>
      </dl>

      {syncMutation.isError && (
        <p className="error-text">{(syncMutation.error as Error).message}</p>
      )}

      <div className="connector-card-actions">
        <label className="connector-toggle">
          <input
            type="checkbox"
            checked={connector.enabled}
            onChange={(e) => toggleMutation.mutate(e.target.checked)}
            disabled={toggleMutation.isPending}
          />
          <span>Enabled</span>
        </label>
        <button
          type="button"
          className="primary"
          onClick={() => syncMutation.mutate()}
          disabled={syncMutation.isPending || running}
        >
          {running && <span className="spinner" />}
          {running ? 'Syncing…' : 'Run now'}
        </button>
        <button type="button" onClick={() => setConfigOpen(true)}>
          Configure
        </button>
      </div>

      {configOpen && (
        <ConnectorConfigModal connector={connector} onClose={() => setConfigOpen(false)} />
      )}
    </div>
  )
}
