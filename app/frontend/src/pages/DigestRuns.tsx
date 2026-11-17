import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { formatWeekLabel, recentWeeks } from '../utils/weeks'

const PIPELINE_STEPS = ['map', 'weight', 'connect', 'filter', 'synthesize', 'surface'] as const

function stepStatus(
  step: string,
  currentStep: string | null,
  status: string,
  timings: Record<string, number>,
): 'pending' | 'active' | 'done' | 'failed' {
  if (status === 'failed' || status === 'data_unavailable') {
    if (currentStep === step) return 'failed'
    const idx = PIPELINE_STEPS.indexOf(step as (typeof PIPELINE_STEPS)[number])
    const curIdx = currentStep
      ? PIPELINE_STEPS.indexOf(currentStep as (typeof PIPELINE_STEPS)[number])
      : -1
    if (idx < curIdx || timings[step]) return 'done'
    return 'pending'
  }
  if (status === 'completed') return 'done'
  if (currentStep === step) return 'active'
  const idx = PIPELINE_STEPS.indexOf(step as (typeof PIPELINE_STEPS)[number])
  const curIdx = currentStep
    ? PIPELINE_STEPS.indexOf(currentStep as (typeof PIPELINE_STEPS)[number])
    : PIPELINE_STEPS.length
  if (idx < curIdx || timings[step]) return 'done'
  return 'pending'
}

export function DigestRuns() {
  const { data: weeksData } = useQuery({
    queryKey: ['data-weeks'],
    queryFn: api.getDataWeeks,
  })
  const weeks = weeksData?.weeks?.length ? weeksData.weeks : recentWeeks(13)
  const [week, setWeek] = useState('')
  const [activeRunId, setActiveRunId] = useState<number | null>(null)
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!week && weeks.length) {
      setWeek(weeks[Math.max(0, weeks.length - 2)])
    }
  }, [weeks, week])

  const { data: allRuns } = useQuery({ queryKey: ['runs'], queryFn: api.listRuns })
  useEffect(() => {
    if (activeRunId == null && allRuns && allRuns.length) {
      const latest = allRuns[0]
      if (
        ['pending', 'running'].includes(latest.status) ||
        (['failed', 'data_unavailable'].includes(latest.status) && !latest.digest_id)
      ) {
        setActiveRunId(latest.id)
      }
    }
  }, [allRuns, activeRunId])

  const { data: run } = useQuery({
    queryKey: ['run', activeRunId],
    queryFn: () => api.getRun(activeRunId!),
    enabled: activeRunId != null,
    refetchInterval: (q) => {
      const s = q.state.data?.status
      if (!s || s === 'completed' || s === 'failed' || s === 'data_unavailable') return false
      return 1500
    },
    refetchIntervalInBackground: true,
  })

  const generateMutation = useMutation({
    mutationFn: (weekOverride?: string) => api.generateDigest({ week: weekOverride || week }),
    onSuccess: (data) => {
      setActiveRunId(data.run_id)
    },
  })

  useEffect(() => {
    if (run?.status === 'completed' && run.digest_id) {
      queryClient.invalidateQueries({ queryKey: ['digests'] })
      queryClient.invalidateQueries({ queryKey: ['briefing'] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    }
  }, [run?.status, run?.digest_id, queryClient])

  const showProgress =
    activeRunId != null &&
    run &&
    !['completed', 'failed', 'data_unavailable'].includes(run.status)

  return (
    <div>
      <header className="page-header">
        <h1>Digest runs</h1>
        <p>
          Digests are generated automatically each Monday. Use this page to run one on demand — for example
          after changing the weighting — and to see the history of runs.
        </p>
      </header>

      <div className="toolbar">
        <div className="field">
          <label htmlFor="week-select">Week</label>
          <select id="week-select" value={week} onChange={(e) => setWeek(e.target.value)}>
            {weeks.map((w) => (
              <option key={w} value={w}>
                {formatWeekLabel(w)}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          className="primary"
          onClick={() => generateMutation.mutate(undefined)}
          disabled={generateMutation.isPending || !!showProgress || !week}
        >
          {showProgress ? 'Generating…' : 'Generate digest'}
        </button>
        <span className="toolbar-hint">
          Takes 1–2 minutes · six steps: map, weight, connect, filter, write, check evidence
        </span>
      </div>

      {generateMutation.isError && (
        <p className="error-text">{(generateMutation.error as Error).message}</p>
      )}

      {(showProgress || (run && activeRunId)) && run && (
        <div className="pipeline-progress">
          <div className="pipeline-progress-title">
            {showProgress && <span className="spinner" />}
            <strong>
              {showProgress
                ? `Generating digest for ${formatWeekLabel(run.week)}`
                : `Run #${run.id} · ${run.week} · ${run.status}`}
            </strong>
          </div>
          {run.error && <p className="error-text">{run.error}</p>}
          <div className="pipeline-steps">
            {PIPELINE_STEPS.map((step) => (
              <span
                key={step}
                className={`pipeline-step ${stepStatus(step, run.current_step, run.status, run.step_timings ?? {})}`}
              >
                {step}
                {run.step_timings?.[step] != null && ` (${run.step_timings[step]}ms)`}
              </span>
            ))}
          </div>
          {run.status === 'completed' && run.digest_id && (
            <p style={{ marginTop: '0.75rem' }}>
              <Link to={`/digests/${run.digest_id}`}>View digest →</Link>
            </p>
          )}
          {(run.status === 'failed' || run.status === 'data_unavailable') && (
            <p style={{ marginTop: '0.75rem' }}>
              <button
                type="button"
                className="primary"
                onClick={() => generateMutation.mutate(run.week)}
                disabled={generateMutation.isPending}
              >
                Retry
              </button>
            </p>
          )}
        </div>
      )}

      <h2 className="section-title">Recent runs</h2>
      {!allRuns?.length ? (
        <p className="empty-state">No runs yet.</p>
      ) : (
        <div className="table-scroll">
          <table className="audit-table">
            <thead>
              <tr>
                <th>Run</th>
                <th>Week</th>
                <th>Status</th>
                <th>Cost</th>
                <th>Started</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {allRuns.slice(0, 20).map((r) => (
                <tr key={r.id}>
                  <td>#{r.id}</td>
                  <td>{formatWeekLabel(r.week)}</td>
                  <td>
                    <span className={`run-status ${r.status}`}>{r.status.replace('_', ' ')}</span>
                    {r.error && <div className="verification-note">{r.error}</div>}
                  </td>
                  <td>${r.cost_usd.toFixed(2)}</td>
                  <td>{new Date(r.created_at).toLocaleString()}</td>
                  <td>{r.digest_id && <Link to={`/digests/${r.digest_id}`}>Open digest</Link>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
