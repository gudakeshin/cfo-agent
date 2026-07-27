import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { ArchivalSearch } from '../components/ArchivalSearch'
import { MemoryBlockCard } from '../components/MemoryBlockCard'
import { MemoryGraph } from '../components/MemoryGraph'

export function MemoryView() {
  const queryClient = useQueryClient()
  const [historyOpen, setHistoryOpen] = useState(false)

  const { data: blocks = [], isLoading, error } = useQuery({
    queryKey: ['memory-blocks'],
    queryFn: api.listMemoryBlocks,
  })

  const { data: consolidations = [] } = useQuery({
    queryKey: ['consolidations'],
    queryFn: () => api.listConsolidations(20),
    enabled: historyOpen,
  })

  const consolidateMutation = useMutation({
    mutationFn: api.consolidateMemory,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory-blocks'] })
      queryClient.invalidateQueries({ queryKey: ['consolidations'] })
    },
  })

  const maintenanceMutation = useMutation({
    mutationFn: api.runMaintenance,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory-blocks'] })
    },
  })

  return (
    <div>
      <header className="page-header" style={{ marginBottom: 22 }}>
        <h1>Agent memory</h1>
        <p>Priority context, revision history, and archival recall.</p>
      </header>

      <div style={{ marginBottom: 20, display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
        <button
          type="button"
          className="primary"
          onClick={() => consolidateMutation.mutate()}
          disabled={consolidateMutation.isPending}
        >
          {consolidateMutation.isPending ? 'Running…' : 'Run consolidation'}
        </button>
        <button
          type="button"
          onClick={() => maintenanceMutation.mutate()}
          disabled={maintenanceMutation.isPending}
        >
          {maintenanceMutation.isPending ? 'Running…' : 'Run maintenance'}
        </button>
        <button type="button" onClick={() => setHistoryOpen((v) => !v)}>
          {historyOpen ? 'Hide' : 'Show'} consolidation history
        </button>
      </div>
      {consolidateMutation.isSuccess && (
        <p className="success-text">
          {consolidateMutation.data.message ??
            `Consolidated — ${consolidateMutation.data.summary ?? 'done'}`}
        </p>
      )}
      {consolidateMutation.isError && (
        <p className="error-text">{(consolidateMutation.error as Error).message}</p>
      )}
      {maintenanceMutation.isSuccess && (
        <p className="success-text">
          Maintenance complete — {JSON.stringify(maintenanceMutation.data.stats)}
        </p>
      )}
      {maintenanceMutation.isError && (
        <p className="error-text">{(maintenanceMutation.error as Error).message}</p>
      )}

      {historyOpen && (
        <div className="panel" style={{ marginBottom: 24 }}>
          <h3>Consolidation history</h3>
          {consolidations.length === 0 ? (
            <p className="empty-state">No consolidation runs yet.</p>
          ) : (
            <div className="table-card">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Window</th>
                    <th>Summary</th>
                    <th>Edits</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {consolidations.map((c) => (
                    <tr key={c.id}>
                      <td>#{c.id}</td>
                      <td>
                        {new Date(c.window_start).toLocaleDateString()} –{' '}
                        {new Date(c.window_end).toLocaleDateString()}
                      </td>
                      <td>{c.summary || '—'}</td>
                      <td>{c.edits_count}</td>
                      <td>{new Date(c.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <ArchivalSearch />

      {!isLoading && blocks.length > 0 && <MemoryGraph blocks={blocks} />}

      {isLoading && <p className="empty-state">Loading memory blocks…</p>}
      {error && <p className="error-text">{(error as Error).message}</p>}

      {blocks.map((block) => (
        <MemoryBlockCard key={block.name} block={block} />
      ))}
    </div>
  )
}
