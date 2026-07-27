import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ConnectorConfig } from '../api/types'

interface Props {
  connector: ConnectorConfig
  onClose: () => void
}

const CRON_PRESETS: { label: string; value: string }[] = [
  { label: 'Off', value: '' },
  { label: 'Hourly', value: '0 * * * *' },
  { label: 'Daily', value: '0 6 * * *' },
]

function presetForCron(cron: string | null): string {
  const match = CRON_PRESETS.find((p) => p.value === (cron ?? ''))
  return match ? match.value : 'custom'
}

export function ConnectorConfigModal({ connector, onClose }: Props) {
  const queryClient = useQueryClient()
  const [config, setConfig] = useState<Record<string, string>>({ ...connector.config })
  const [secret, setSecret] = useState('')
  const [cronPreset, setCronPreset] = useState(presetForCron(connector.cron))
  const [customCron, setCustomCron] = useState(connector.cron ?? '')
  const [resyncOpen, setResyncOpen] = useState(false)
  const [fullRunId, setFullRunId] = useState<number | null>(null)

  const { data: runs = [] } = useQuery({
    queryKey: ['connector-runs', connector.id],
    queryFn: () => api.listConnectorRuns(connector.id, 10),
  })

  const { data: fullRun } = useQuery({
    queryKey: ['connector-run', fullRunId],
    queryFn: () => api.getConnectorRun(fullRunId!),
    enabled: fullRunId != null,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 1500 : false),
    refetchIntervalInBackground: true,
  })

  useEffect(() => {
    if (fullRun && fullRun.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['connectors'] })
      queryClient.invalidateQueries({ queryKey: ['connector-runs', connector.id] })
    }
  }, [fullRun, queryClient, connector.id])

  const effectiveCron = cronPreset === 'custom' ? customCron : cronPreset

  const saveMutation = useMutation({
    mutationFn: () =>
      api.putConnector(connector.id, {
        config,
        secret: secret || undefined,
        cron: effectiveCron,
      }),
    onSuccess: () => {
      setSecret('')
      queryClient.invalidateQueries({ queryKey: ['connectors'] })
    },
  })

  const testMutation = useMutation({
    mutationFn: () => api.testConnector(connector.id),
  })

  const fullSyncMutation = useMutation({
    mutationFn: () => api.syncConnector(connector.id, 'full'),
    onSuccess: (data) => {
      setFullRunId(data.run_id)
      setResyncOpen(false)
    },
  })

  const fullSyncRunning = fullRun?.status === 'running'

  return (
    <div className="popover-backdrop" onClick={onClose} role="presentation">
      <div
        className="popover-panel connector-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
      >
        <header>
          <div>
            <h3>{connector.display_name}</h3>
            <span style={{ fontSize: 12.5, color: 'var(--gray-500)' }}>
              {connector.vendor} · {connector.auth_type}
            </span>
          </div>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </header>

        <div className="form-section">
          <h3>Credentials</h3>
          {connector.config_fields.map((field) =>
            field.secret ? (
              <div key={field.key} style={{ marginBottom: 14 }}>
                <label htmlFor={`cfg-${field.key}`}>{field.label}</label>
                <input
                  id={`cfg-${field.key}`}
                  type="password"
                  value={secret}
                  onChange={(e) => setSecret(e.target.value)}
                  placeholder={connector.secret_masked ?? 'Not set'}
                />
              </div>
            ) : (
              <div key={field.key} style={{ marginBottom: 14 }}>
                <label htmlFor={`cfg-${field.key}`}>{field.label}</label>
                <input
                  id={`cfg-${field.key}`}
                  value={config[field.key] ?? ''}
                  onChange={(e) => setConfig((c) => ({ ...c, [field.key]: e.target.value }))}
                />
              </div>
            ),
          )}

          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
            <button
              type="button"
              className="primary"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
            >
              Save credentials
            </button>
            <button
              type="button"
              onClick={() => testMutation.mutate()}
              disabled={testMutation.isPending}
            >
              Test connection
            </button>
          </div>
          {saveMutation.isSuccess && <p className="success-text">Saved.</p>}
          {saveMutation.isError && (
            <p className="error-text">{(saveMutation.error as Error).message}</p>
          )}
          {testMutation.data && (
            <p className={testMutation.data.ok ? 'success-text' : 'error-text'}>
              {testMutation.data.ok
                ? testMutation.data.message
                : `Missing: ${(testMutation.data.missing_fields ?? []).join(', ')}`}
            </p>
          )}
          {testMutation.isError && (
            <p className="error-text">{(testMutation.error as Error).message}</p>
          )}
        </div>

        <div className="form-section">
          <h3>Schedule</h3>
          <div className="preset-row" style={{ marginBottom: 12 }}>
            {CRON_PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                className={`chip-toggle${cronPreset === p.value ? ' active' : ''}`}
                onClick={() => setCronPreset(p.value)}
              >
                {p.label}
              </button>
            ))}
            <button
              type="button"
              className={`chip-toggle${cronPreset === 'custom' ? ' active' : ''}`}
              onClick={() => setCronPreset('custom')}
            >
              Custom
            </button>
          </div>
          {cronPreset === 'custom' && (
            <input
              value={customCron}
              onChange={(e) => setCustomCron(e.target.value)}
              placeholder="m h dom mon dow"
              style={{ maxWidth: 220 }}
            />
          )}
          <div style={{ marginTop: 12 }}>
            <button
              type="button"
              className="primary"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
            >
              Save schedule
            </button>
          </div>
        </div>

        <div className="form-section">
          <h3>Full resync</h3>
          <p>
            Re-pulls every row this connector owns from scratch, overwriting incremental
            watermarks.
          </p>
          {!resyncOpen ? (
            <button type="button" className="ghost-danger" onClick={() => setResyncOpen(true)}>
              Full resync
            </button>
          ) : (
            <div className="confirm-box" role="alertdialog">
              <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 6 }}>
                Confirm full resync
              </div>
              <p style={{ margin: '0 0 12px', fontSize: 13.5, color: 'var(--gray-600)' }}>
                This replaces all rows owned by {connector.display_name}. This cannot be undone.
              </p>
              <div style={{ display: 'flex', gap: 8 }}>
                <button
                  type="button"
                  className="danger"
                  onClick={() => fullSyncMutation.mutate()}
                  disabled={fullSyncMutation.isPending}
                >
                  Yes, resync
                </button>
                <button type="button" onClick={() => setResyncOpen(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
          {fullSyncMutation.isError && (
            <p className="error-text">{(fullSyncMutation.error as Error).message}</p>
          )}
          {fullRunId && (
            <p style={{ marginTop: 10 }}>
              {fullSyncRunning && <span className="spinner" />}{' '}
              Run #{fullRunId} · {fullRun?.status ?? 'starting…'}
              {fullRun?.error && <span className="error-text"> — {fullRun.error}</span>}
            </p>
          )}
        </div>

        <div className="form-section">
          <h3>Sync history</h3>
          {runs.length === 0 ? (
            <p className="empty-state">No syncs yet.</p>
          ) : (
            <div className="table-card">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Trigger</th>
                    <th>Mode</th>
                    <th>Duration</th>
                    <th>Rows</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((r) => {
                    const duration =
                      r.finished_at && r.started_at
                        ? `${Math.max(
                            0,
                            Math.round(
                              (new Date(r.finished_at).getTime() -
                                new Date(r.started_at).getTime()) /
                                1000,
                            ),
                          )}s`
                        : r.status === 'running'
                          ? '…'
                          : '—'
                    const chips = Object.entries(r.rows_pulled ?? {}).flatMap(([table, stats]) => {
                      if (typeof stats === 'number') {
                        return stats > 0 ? [{ table: table.replace(/^erp_/, ''), n: stats }] : []
                      }
                      if (table === 'referenced_masters' && stats && typeof stats === 'object') {
                        return Object.entries(stats as Record<string, number>)
                          .filter(([, n]) => n > 0)
                          .map(([t, n]) => ({ table: `ref:${t.replace(/^erp_/, '')}`, n }))
                      }
                      const created = (stats as { created?: number }).created ?? 0
                      const updated = (stats as { updated?: number }).updated ?? 0
                      const n = created + updated
                      return n > 0 ? [{ table: table.replace(/^erp_/, ''), n }] : []
                    })
                    return (
                      <tr key={r.id}>
                        <td>#{r.id}</td>
                        <td>{r.trigger}</td>
                        <td>{r.mode}</td>
                        <td>{duration}</td>
                        <td>
                          <div className="row-chips">
                            {chips.length === 0 ? (
                              <span className="badge-muted">0</span>
                            ) : (
                              chips.map((c) => (
                                <span key={c.table} className="row-chip">
                                  {c.table} {c.n}
                                </span>
                              ))
                            )}
                          </div>
                        </td>
                        <td>{r.status}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
