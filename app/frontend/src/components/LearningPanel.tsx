import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { extra } from '../api/extra'

/** Weighting changes learned from the CFO's feedback, awaiting a delegate's decision. */
export function LearningPanel() {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: ['learning'], queryFn: extra.listProposals })
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['learning'] })
    qc.invalidateQueries({ queryKey: ['weighting'] })
  }
  const propose = useMutation({ mutationFn: extra.propose, onSuccess: refresh })
  const decide = useMutation({
    mutationFn: ({ id, d }: { id: number; d: 'approve' | 'reject' }) => extra.decide(id, d),
    onSuccess: refresh,
  })
  if (!data) return null
  const pending = data.proposals.find((p) => p.status === 'pending')
  const g = data.guardrails
  return (
    <div className="form-section">
      <h3 style={{ marginTop: 0 }}>Learned from feedback</h3>
      <p className="verification-note">
        The CFO&apos;s “acted on / useful / set aside” verdicts nudge how much each KRA counts — at most {g.max_step} per
        proposal, between ×{g.floor} and ×{g.ceiling}, and only for KRAs with {g.min_samples}+ verdicts. Red lines are
        never affected. Nothing changes until you approve.
      </p>
      {pending ? (
        <div className="learning-proposal">
          <table className="data-table">
            <thead>
              <tr>
                <th>KRA</th>
                <th>Feedback (acted / useful / set aside)</th>
                <th>Weight</th>
              </tr>
            </thead>
            <tbody>
              {pending.changes.map((c) => (
                <tr key={c.kra_id}>
                  <td>
                    {c.kra_id} · {c.kra_name}
                  </td>
                  <td>
                    {c.evidence.acted_on} / {c.evidence.useful} / {c.evidence.set_aside}
                    {c.evidence.already_knew ? ` · ${c.evidence.already_knew} already known` : ''}
                  </td>
                  <td>
                    ×{c.from.toFixed(2)} → <strong>×{c.to.toFixed(2)}</strong>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="preset-row" style={{ marginTop: '0.5rem' }}>
            <button type="button" className="primary" disabled={decide.isPending} onClick={() => decide.mutate({ id: pending.id, d: 'approve' })}>
              Approve (creates a new weighting version)
            </button>
            <button type="button" disabled={decide.isPending} onClick={() => decide.mutate({ id: pending.id, d: 'reject' })}>
              Reject
            </button>
          </div>
        </div>
      ) : (
        <div className="preset-row">
          <span className="toolbar-hint">No pending proposal.</span>
          <button type="button" onClick={() => propose.mutate()} disabled={propose.isPending}>
            Check feedback now
          </button>
          {propose.data?.message && <span className="toolbar-hint">{propose.data.message}</span>}
        </div>
      )}
      {decide.isError && <p className="error-text">{(decide.error as Error).message}</p>}
    </div>
  )
}
