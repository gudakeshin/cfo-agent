import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { DigestItem } from '../api/types'
import { DataHealthBanner } from '../components/DataHealthBanner'
import { DigestItems } from '../components/DigestItems'
import { ExportShare } from '../components/ExportShare'
import { useAuth } from '../hooks/useAuth'
import { formatWeekLabel } from '../utils/weeks'

interface Suppressed {
  title?: string
  reason?: string
  detail?: string
}

const REASON_LABEL: Record<string, string> = {
  uncitable: 'Cited evidence that does not support it',
  no_citations: 'No evidence cited',
}

function ScoreRow({ item }: { item: DigestItem }) {
  const d = (item.score_decomp ?? {}) as Record<string, number | string>
  const n = (k: string) => (typeof d[k] === 'number' ? (d[k] as number).toFixed(2) : '—')
  return (
    <tr>
      <td>#{item.rank}</td>
      <td>{item.title}</td>
      <td>{item.is_red_line ? 'Red line' : `${n('face_weight')} × ${n('rank_boost')} × ${n('severity')}`}</td>
      <td>{item.is_red_line ? '—' : item.priority_score.toFixed(2)}</td>
    </tr>
  )
}

export function DigestDetail() {
  const { id } = useParams<{ id: string }>()
  const digestId = Number(id)
  const queryClient = useQueryClient()
  const [confirmEmail, setConfirmEmail] = useState(false)
  const { can } = useAuth()

  const { data: digest, isLoading, error } = useQuery({
    queryKey: ['digest', digestId],
    queryFn: () => api.getDigest(digestId),
    enabled: Number.isFinite(digestId),
  })

  const resendMutation = useMutation({
    mutationFn: () => api.resendDigestEmail(digestId),
    onSuccess: () => {
      setConfirmEmail(false)
      queryClient.invalidateQueries({ queryKey: ['digest', digestId] })
    },
  })

  if (isLoading) return <p className="empty-state">Loading digest…</p>
  if (error) return <p className="error-text">{(error as Error).message}</p>
  if (!digest) return <p className="empty-state">Digest not found.</p>

  const audit = digest.run_audit
  const suppressed = (audit?.suppressed ?? []) as Suppressed[]
  const usage = (audit?.token_usage ?? {}) as { calls?: number; degraded_steps?: string[] }
  const emailOk = ['sent', 'outbox', 'written'].includes(digest.email_status)

  return (
    <div>
      <Link to="/digests" className="back-link no-print">
        ← All digests
      </Link>
      <header className="page-header briefing-header">
        <div>
          <p className="eyebrow">
            {formatWeekLabel(digest.week)} · lens: {digest.weighting_label}
          </p>
          <h1>{digest.headline}</h1>
        </div>
        <div className="header-actions no-print">
          <ExportShare digestId={digest.id} />
          {!can('configure') ? null : !confirmEmail ? (
            <button type="button" onClick={() => setConfirmEmail(true)}>
              Email again…
            </button>
          ) : (
            <span className="inline-confirm" role="group" aria-label="Confirm email">
              Send this digest by email again?
              <button
                type="button"
                className="primary"
                onClick={() => resendMutation.mutate()}
                disabled={resendMutation.isPending}
              >
                {resendMutation.isPending ? 'Sending…' : 'Send'}
              </button>
              <button type="button" onClick={() => setConfirmEmail(false)}>
                Cancel
              </button>
            </span>
          )}
        </div>
      </header>
      <p className="email-line no-print">
        Email: <strong className={emailOk ? 'status-ok' : undefined}>{digest.email_status}</strong>
        {resendMutation.isSuccess && <span className="success-text"> · sent again</span>}
        {resendMutation.isError && <span className="error-text"> · {(resendMutation.error as Error).message}</span>}
      </p>

      <DataHealthBanner health={digest.data_health} />
      <DigestItems digest={digest} />

      {audit && can('configure') && (
        <details className="audit-panel no-print">
          <summary>How this digest was built (for the delegate)</summary>
          <div className="audit-body">
            <p>
              Run #{audit.run_id} · {usage.calls ?? '—'} AI calls · ${audit.cost_usd.toFixed(2)}
              {usage.degraded_steps?.length
                ? ` · AI budget ran out; system-written text used for: ${usage.degraded_steps.join(', ')}`
                : ''}
            </p>

            <h4>Why each item ranked where it did</h4>
            <p className="verification-note">
              Priority = face weight × KRA rank boost × severity of the move. Red lines skip the ranking.
            </p>
            <table className="audit-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Item</th>
                  <th>Face × rank × severity</th>
                  <th>Priority</th>
                </tr>
              </thead>
              <tbody>
                {digest.items.map((i) => (
                  <ScoreRow key={i.id} item={i} />
                ))}
              </tbody>
            </table>

            <h4>Held back ({suppressed.length})</h4>
            {suppressed.length === 0 ? (
              <p className="verification-note">Nothing was held back — every drafted item passed the evidence check.</p>
            ) : (
              <ul className="suppressed-list">
                {suppressed.map((s, i) => (
                  <li key={i}>
                    <strong>{s.title ?? 'Untitled'}</strong> — {REASON_LABEL[s.reason ?? ''] ?? s.reason}
                    {s.detail && <div className="verification-note">{s.detail}</div>}
                  </li>
                ))}
              </ul>
            )}

            <h4>Step timings</h4>
            <p className="verification-note">
              {Object.entries(audit.step_timings ?? {})
                .map(([step, ms]) => `${step} ${(ms / 1000).toFixed(1)}s`)
                .join(' · ')}
            </p>
          </div>
        </details>
      )}
    </div>
  )
}
