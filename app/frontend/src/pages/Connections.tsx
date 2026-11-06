import { useEffect, useState } from 'react'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { ConnectorCard } from '../components/ConnectorCard'

export function Connections() {
  const queryClient = useQueryClient()
  const [syncAllRunIds, setSyncAllRunIds] = useState<number[]>([])

  const { data, isLoading, error } = useQuery({
    queryKey: ['connectors'],
    queryFn: api.listConnectors,
  })

  const syncAllRuns = useQueries({
    queries: syncAllRunIds.map((runId) => ({
      queryKey: ['connector-run', runId],
      queryFn: () => api.getConnectorRun(runId),
      refetchInterval: (q: { state: { data?: { status?: string } } }) => {
        const s = q.state.data?.status
        if (!s || s !== 'running') return false
        return 1500
      },
      refetchIntervalInBackground: true,
    })),
  })

  const runStatuses = syncAllRuns.map((q) => q.data?.status).join(',')
  const syncAllRunning =
    syncAllRunIds.length > 0 &&
    (syncAllRuns.some((q) => !q.data) || syncAllRuns.some((q) => q.data?.status === 'running'))

  useEffect(() => {
    if (syncAllRunIds.length === 0) return
    if (syncAllRuns.length !== syncAllRunIds.length) return
    if (syncAllRuns.some((q) => !q.data)) return
    if (syncAllRuns.every((q) => q.data!.status !== 'running')) {
      queryClient.invalidateQueries({ queryKey: ['connectors'] })
      setSyncAllRunIds([])
    }
    // runStatuses captures status changes without depending on unstable query array identity
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncAllRunIds, runStatuses, queryClient])

  const syncAllMutation = useMutation({
    mutationFn: api.syncAllConnectors,
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ['connectors'] })
      if (res.run_ids.length) setSyncAllRunIds(res.run_ids)
    },
  })

  const connectors = data?.connectors ?? []
  const enabledCount = connectors.filter((c) => c.enabled).length
  const syncAllBusy = syncAllMutation.isPending || syncAllRunning

  return (
    <div>
      <header className="page-header">
        <h1>Connections</h1>
        <p>
          Data sources. <strong>File drop (CSV exports)</strong> loads real ERP exports; the other sources are
          simulated for the demo.
        </p>
      </header>

      <div className="toolbar">
        <div className="field">
          <label>Dataset seed</label>
          <span style={{ display: 'block', paddingTop: 6, fontFamily: 'var(--font-mono)' }}>
            {data ? data.erp_seed : '—'}
          </span>
        </div>
        <span className="toolbar-hint">
          {enabledCount} of {connectors.length} connectors enabled
        </span>
        <button
          type="button"
          className="primary"
          onClick={() => syncAllMutation.mutate()}
          disabled={syncAllBusy}
        >
          {syncAllBusy && <span className="spinner" />} Sync all
        </button>
      </div>

      {syncAllMutation.isError && (
        <p className="error-text">{(syncAllMutation.error as Error).message}</p>
      )}
      {(syncAllMutation.isSuccess || syncAllRunning) && syncAllMutation.data && (
        <p className={syncAllRunning ? 'toolbar-hint' : 'success-text'}>
          {syncAllRunning
            ? `Syncing ${syncAllRunIds.length} connector${syncAllRunIds.length === 1 ? '' : 's'}…`
            : `Started ${syncAllMutation.data.run_ids.length} sync${
                syncAllMutation.data.run_ids.length === 1 ? '' : 's'
              }${
                syncAllMutation.data.skipped.length > 0
                  ? ` — skipped ${syncAllMutation.data.skipped.length} already running`
                  : ''
              }.`}
        </p>
      )}

      {isLoading && <p className="empty-state">Loading connectors…</p>}
      {error && <p className="error-text">{(error as Error).message}</p>}

      <div className="connector-grid">
        {connectors.map((c) => (
          <ConnectorCard key={c.id} connector={c} />
        ))}
      </div>
    </div>
  )
}
