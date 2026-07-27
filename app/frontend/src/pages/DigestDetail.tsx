import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { DataHealthBanner } from '../components/DataHealthBanner'
import { DrilldownChat } from '../components/DrilldownChat'
import { InsightCard } from '../components/InsightCard'
import { formatWeekLabel } from '../utils/weeks'

function ScoreDecomp({ decomp }: { decomp: Record<string, number> }) {
  const face = decomp.face_weight
  const rank = decomp.rank_boost
  const sev = decomp.severity
  const product =
    typeof face === 'number' && typeof rank === 'number' && typeof sev === 'number'
      ? face * rank * sev
      : null

  return (
    <dl className="score-decomp">
      {typeof face === 'number' && (
        <div>
          <dt>face_weight</dt>
          <dd>{face.toFixed(3)}</dd>
        </div>
      )}
      {typeof rank === 'number' && (
        <div>
          <dt>rank_boost</dt>
          <dd>{rank.toFixed(3)}</dd>
        </div>
      )}
      {typeof sev === 'number' && (
        <div>
          <dt>severity</dt>
          <dd>{sev.toFixed(3)}</dd>
        </div>
      )}
      {product !== null && (
        <div className="score-product">
          <dt>product</dt>
          <dd>
            {face.toFixed(3)} × {rank.toFixed(3)} × {sev.toFixed(3)} = {product.toFixed(3)}
          </dd>
        </div>
      )}
      {Object.entries(decomp)
        .filter(([k]) => !['face_weight', 'rank_boost', 'severity'].includes(k))
        .map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{typeof v === 'number' ? v.toFixed(3) : String(v)}</dd>
          </div>
        ))}
    </dl>
  )
}

export function DigestDetail() {
  const { id } = useParams<{ id: string }>()
  const digestId = Number(id)
  const [chatItem, setChatItem] = useState<{ id: number; title: string } | null>(null)
  const queryClient = useQueryClient()

  const { data: digest, isLoading, error } = useQuery({
    queryKey: ['digest', digestId],
    queryFn: () => api.getDigest(digestId),
    enabled: Number.isFinite(digestId),
  })

  const { data: feedbackLog } = useQuery({
    queryKey: ['feedback'],
    queryFn: api.listFeedback,
  })

  const resendMutation = useMutation({
    mutationFn: () => api.resendDigestEmail(digestId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['digest', digestId] }),
  })

  const feedbackByItem = new Map(
    feedbackLog?.items.map((f) => [f.digest_item_id, f]) ?? [],
  )

  if (isLoading) return <p className="empty-state">Loading digest…</p>
  if (error) return <p className="error-text">{(error as Error).message}</p>
  if (!digest) return <p className="empty-state">Digest not found.</p>

  const audit = digest.run_audit

  const emailOk =
    digest.email_status === 'sent' ||
    digest.email_status === 'outbox' ||
    digest.email_status === 'written'

  return (
    <div>
      <Link to="/" className="back-link">
        ← Digest inbox
      </Link>
      <header className="page-header" style={{ marginBottom: 8 }}>
        <h1>{digest.headline}</h1>
        <p style={{ marginBottom: 14 }}>
          {formatWeekLabel(digest.week)} · {digest.weighting_label}
          {digest.empty_bar && ' · Empty bar week'}
        </p>
      </header>

      <div className="email-status-row">
        <span>
          Email delivery:{' '}
          <strong className={emailOk ? 'status-ok' : undefined}>{digest.email_status}</strong>
          {digest.email_path && (
            <>
              {' '}
              · outbox <code>{digest.email_path.split('/').pop()}</code>
            </>
          )}
        </span>
        <button
          type="button"
          style={{ marginLeft: 'auto' }}
          onClick={() => resendMutation.mutate()}
          disabled={resendMutation.isPending}
        >
          {resendMutation.isPending ? 'Sending…' : 'Resend email'}
        </button>
        {resendMutation.isError && (
          <span className="error-text">{(resendMutation.error as Error).message}</span>
        )}
        {resendMutation.isSuccess && (
          <span className="success-text">
            {resendMutation.data.email_status}
            {resendMutation.data.email_path ? ` → ${resendMutation.data.email_path}` : ''}
          </span>
        )}
      </div>

      <DataHealthBanner health={digest.data_health} />

      {digest.items.length === 0 ? (
        <p className="empty-state">No insights surfaced for this week.</p>
      ) : (
        <div className="insight-list">
          {digest.items.map((item) => (
            <InsightCard
              key={item.id}
              item={item}
              feedback={feedbackByItem.get(item.id)}
              onFeedbackChange={() => queryClient.invalidateQueries({ queryKey: ['feedback'] })}
              onDrilldown={() => setChatItem({ id: item.id, title: item.title })}
            />
          ))}
        </div>
      )}

      {audit && (
        <details className="audit-panel">
          <summary>Run audit · #{audit.run_id}</summary>
          <div className="audit-body">
            <dl className="audit-grid">
              <div>
                <dt>Cost</dt>
                <dd>${audit.cost_usd.toFixed(4)}</dd>
              </div>
              {Object.entries(audit.step_timings ?? {}).map(([step, ms]) => (
                <div key={step}>
                  <dt>{step}</dt>
                  <dd>{ms}ms</dd>
                </div>
              ))}
            </dl>

            {audit.suppressed && audit.suppressed.length > 0 && (
              <>
                <h4>Suppressed ({audit.suppressed.length})</h4>
                <pre>{JSON.stringify(audit.suppressed, null, 2)}</pre>
              </>
            )}

            {digest.items.some((i) => i.score_decomp) && (
              <>
                <h4>Score decomposition</h4>
                {digest.items
                  .filter((i) => i.score_decomp)
                  .map((i) => (
                    <details key={i.id} style={{ marginBottom: '0.5rem' }}>
                      <summary>
                        #{i.rank} {i.title}
                      </summary>
                      <ScoreDecomp decomp={i.score_decomp as Record<string, number>} />
                    </details>
                  ))}
              </>
            )}

            {audit.token_usage && Object.keys(audit.token_usage).length > 0 && (
              <>
                <h4>Token usage</h4>
                <pre>{JSON.stringify(audit.token_usage, null, 2)}</pre>
              </>
            )}
          </div>
        </details>
      )}

      {chatItem && (
        <DrilldownChat
          itemId={chatItem.id}
          itemTitle={chatItem.title}
          onClose={() => setChatItem(null)}
        />
      )}
    </div>
  )
}
