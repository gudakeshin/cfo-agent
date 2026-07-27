import { useEffect, useMemo, useState } from 'react'
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

export function DigestInbox() {
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

  const { data: digests = [], isLoading, error } = useQuery({
    queryKey: ['digests'],
    queryFn: api.listDigests,
  })

  const { data: alerts = [] } = useQuery({
    queryKey: ['alerts', 'active'],
    queryFn: () => api.listAlerts(true),
  })

  const { data: feedback } = useQuery({
    queryKey: ['feedback'],
    queryFn: api.listFeedback,
  })

  const { data: newsItems = [] } = useQuery({
    queryKey: ['news', 'watch'],
    queryFn: () => api.listNews({ limit: 20 }),
  })
  const watchItems = newsItems.filter((n) => n.relevance !== 'low').slice(0, 3)

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
    }
  }, [run?.status, run?.digest_id, queryClient])

  const showProgress =
    activeRunId != null &&
    run &&
    !['completed', 'failed', 'data_unavailable'].includes(run.status)

  const avgInsights = useMemo(() => {
    if (!digests.length) return '—'
    const avg = digests.reduce((s, d) => s + d.item_count, 0) / digests.length
    return avg.toFixed(1)
  }, [digests])

  return (
    <div>
      <header className="page-header">
        <h1>Digest inbox</h1>
        <p>
          Weekly CFO intelligence — mapped to the Four Faces framework, weighted by this quarter&apos;s
          priorities.
        </p>
      </header>

      <div className="stats-row">
        <div className="stat-block">
          <div className="label">Digests this quarter</div>
          <div className="value">{digests.length}</div>
        </div>
        <div className="stat-block accent-top">
          <div className="label">Active red lines</div>
          <div className="value danger">{alerts.length}</div>
        </div>
        <div className="stat-block">
          <div className="label">Surfacing precision</div>
          <div className="value success">
            {feedback ? `${feedback.surfacing_precision_pct}%` : '—'}
          </div>
        </div>
        <div className="stat-block">
          <div className="label">Avg insights / week</div>
          <div className="value">{avgInsights}</div>
        </div>
      </div>

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
          Pipeline: Map → Weight → Connect → Filter → Synthesize → Surface
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

      {watchItems.length > 0 && (
        <div className="panel regulatory-watch">
          <div className="regulatory-watch-head">
            <h3>Regulatory watch</h3>
            <Link to="/news">View all →</Link>
          </div>
          <ul>
            {watchItems.map((n) => (
              <li key={n.id}>
                <a href={n.url} target="_blank" rel="noreferrer">
                  {n.title}
                </a>
                <div className="meta">
                  {n.source_name && <span>{n.source_name}</span>}
                  <span className={`badge-relevance ${n.relevance}`}>{n.relevance}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {isLoading && <p className="empty-state">Loading digests…</p>}
      {error && <p className="error-text">{(error as Error).message}</p>}

      {!isLoading && digests.length === 0 && (
        <p className="empty-state">No digests yet — generate one for the selected week.</p>
      )}

      <ul className="digest-list">
        {digests.map((d) => (
          <li key={d.id}>
            <Link to={`/digests/${d.id}`} className="digest-card">
              <div className="digest-card-meta-row">
                {d.empty_bar && <span className="badge-muted">Empty bar</span>}
                <span className="week-mono">{formatWeekLabel(d.week)}</span>
              </div>
              <h3>{d.headline}</h3>
              <div className="meta">
                <span>{d.item_count} insights</span>
                <span>·</span>
                <span>{d.weighting_label}</span>
                <span>·</span>
                <span>{new Date(d.created_at).toLocaleString()}</span>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}
