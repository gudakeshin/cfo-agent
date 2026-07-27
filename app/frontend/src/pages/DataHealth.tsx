import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { DataHealthBanner } from '../components/DataHealthBanner'

export function DataHealth() {
  const [staleDays, setStaleDays] = useState(0)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const queryClient = useQueryClient()

  const { data: health, isLoading, error } = useQuery({
    queryKey: ['data-health'],
    queryFn: () => api.getDataHealth(),
  })

  const generateMutation = useMutation({
    mutationFn: () => api.generateData({ seed: 42, stale_days: staleDays, confirm: true }),
    onSuccess: () => {
      setConfirmOpen(false)
      queryClient.invalidateQueries({ queryKey: ['data-health'] })
      queryClient.invalidateQueries({ queryKey: ['digests'] })
    },
  })

  return (
    <div>
      <header className="page-header">
        <h1>Data health</h1>
        <p>ERP feed freshness and integrity.</p>
      </header>

      {isLoading && <p className="empty-state">Checking data health…</p>}
      {error && <p className="error-text">{(error as Error).message}</p>}
      {health && <DataHealthBanner health={health} />}

      <div className="form-section">
        <h3>Regenerate ERP data</h3>
        <p>
          Regeneration cascades wipe digests, alerts, signals, feedback, and archival memory. Memory
          blocks are kept.
        </p>

        <div
          style={{
            display: 'flex',
            alignItems: 'flex-end',
            gap: 14,
            flexWrap: 'wrap',
            marginBottom: 14,
          }}
        >
          <label style={{ display: 'block' }}>
            <span
              style={{
                display: 'block',
                marginBottom: 6,
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
                color: 'var(--gray-500)',
              }}
            >
              Stale days offset
            </span>
            <input
              type="number"
              min={0}
              max={30}
              value={staleDays}
              onChange={(e) => setStaleDays(Number(e.target.value))}
              style={{ width: 120 }}
            />
          </label>
          <button type="button" className="ghost-danger" onClick={() => setConfirmOpen(true)}>
            Regenerate data
          </button>
        </div>

        {confirmOpen && (
          <div className="confirm-box" role="alertdialog">
            <div style={{ fontWeight: 700, fontSize: 14, marginBottom: 6 }}>Confirm cascade wipe</div>
            <p style={{ margin: '0 0 12px', fontSize: 13.5, color: 'var(--gray-600)' }}>
              This deletes all digests, alerts, signals, feedback, and chat history. This cannot be
              undone.
            </p>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className="danger"
                onClick={() => generateMutation.mutate()}
                disabled={generateMutation.isPending}
              >
                Yes, regenerate
              </button>
              <button type="button" onClick={() => setConfirmOpen(false)}>
                Cancel
              </button>
            </div>
            {generateMutation.isError && (
              <p className="error-text">{(generateMutation.error as Error).message}</p>
            )}
            {generateMutation.isSuccess && (
              <p className="success-text">ERP data regenerated successfully.</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
