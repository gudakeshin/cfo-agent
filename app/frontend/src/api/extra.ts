/** API calls and types for exports, sharing, heatmap, red-line backtests, custom
 * metrics, feedback learning, actions, meeting briefs, entities and templates. */
import { request } from './client'

// ── sharing ──────────────────────────────────────────────────────────────────
export interface ShareLink {
  id: number
  digest_id: number
  note: string
  created_by: string
  created_at: string
  expires_at: string
  revoked: boolean
  active: boolean
  views: number
  last_viewed_at: string | null
  url: string
}

export interface ExportItem {
  id: number
  rank: number
  title: string
  sections: Partial<Record<'what' | 'why' | 'connects' | 'action', string>>
  is_red_line: boolean
  priority: string
  kras: { id: string; name: string }[]
  metric: { id: string; label: string; unit: string; value: number | null; history: { week: string; value: number }[] } | null
  sources: { n: number; id: string; label: string }[]
  unverified_figures: string[]
}

export interface SharedDigest {
  id: number
  week: string
  headline: string
  weighting_label: string
  created_at: string
  empty_bar: boolean
  items: ExportItem[]
  red_line_count: number
  shared: { expires_at: string; shared_by: string; note: string }
}

// ── heatmap / backtest ───────────────────────────────────────────────────────
export interface HeatCell {
  week: string
  score: number | null
  metric: string | null
  value?: number | null
  delta?: number | null
}
export interface Heatmap {
  weeks: string[]
  faces: {
    id: string
    label: string
    cells: HeatCell[]
    kras: { id: string; name: string; grounded: boolean; cells: HeatCell[] }[]
  }[]
  metrics: Record<string, { label: string; unit: string }>
}

export interface Backtest {
  points: { week: string; value: number | null; fires: boolean }[]
  fired_weeks: string[]
  fired_count: number
  weeks: number
  event_types: string[]
}

export interface EventTypeOption {
  value: string
  label: string
  in_data: boolean
}

// ── pilot scorecard ──────────────────────────────────────────────────────────
export interface ScorecardWeek {
  week: string
  digest_id: number
  surfaced: number
  red_lines: number
  reviewed: number
  acted_on: number
  useful: number
  set_aside: number
  already_knew: number
  novel_acted_on: number
  opens: number
}

export interface Scorecard {
  weeks: ScorecardWeek[]
  totals: {
    digests: number
    weeks_opened: number
    surfaced: number
    reviewed: number
    review_rate_pct: number | null
    acted_on: number
    useful: number
    set_aside: number
    already_knew: number
    novel_acted_on: number
    useful_rate_pct: number | null
    success_test_met: boolean
  } | null
}

// ── custom metrics ───────────────────────────────────────────────────────────
export interface MetricInput {
  agg: string
  source?: string
  metric?: string
  where?: Record<string, string>
  window?: string
  sign?: 1 | -1
}
export interface MetricDefinition {
  id: string
  label: string
  unit: string
  adverse_direction: 'up' | 'down' | null
  description: string
  inputs: Record<string, MetricInput>
  formula: string
  used_by_kras?: string[]
  versions?: { id: number; active: boolean; created_by: string; created_at: string }[]
}
export interface MetricVocabulary {
  sources: Record<string, { filters: string[]; money: boolean }>
  aggs: string[]
  units: string[]
  windows: string[]
}

// ── learning ─────────────────────────────────────────────────────────────────
export interface KraEvidence {
  n: number
  acted_on: number
  useful: number
  set_aside: number
  already_knew: number
  mean: number
  items: { digest_item_id: number; title: string; verdict: string }[]
}
export interface Proposal {
  id: number
  status: 'pending' | 'approved' | 'rejected' | 'superseded'
  created_at: string
  changes: { kra_id: string; kra_name: string; from: number; to: number; evidence: KraEvidence }[]
  decided_by: string | null
  decided_at: string | null
}

// ── actions / briefs ─────────────────────────────────────────────────────────
export type ActionStatus = 'open' | 'in_progress' | 'done' | 'dropped'
export interface ActionItem {
  id: number
  title: string
  detail: string
  owner: string
  due_date: string | null
  status: ActionStatus
  kra_ids: string[]
  metric_id: string | null
  digest_item_id: number | null
  insight_title: string | null
  overdue: boolean
  created_by: string
  created_at: string
  closed_at: string | null
}

export interface BriefContent {
  headline: string
  sections: { heading: string; points: { text: string; citations: string[] }[] }[]
  likely_questions: { question: string; suggested_answer: string; citations: string[] }[]
}
export interface MeetingBrief {
  id: number
  title: string
  audience: string
  meeting_date: string | null
  week: string
  content: BriefContent
  verification: { dropped_uncited?: string[]; rejected_citations?: string[]; unverified_figures?: string[]; fallback?: boolean }
  evidence: Record<string, string>
  cost_usd: number
  created_by: string
  created_at: string
}
export interface BriefSummary {
  id: number
  title: string
  audience: string
  meeting_date: string | null
  week: string
  created_at: string
  created_by: string
  fallback: boolean
}

// ── entities / templates ─────────────────────────────────────────────────────
export interface EntityView {
  week: string | null
  metrics: { id: string; label: string; unit: string }[]
  group: Record<string, number | null>
  entities: { id: string; name: string; currency: string; consolidated: boolean; values: Record<string, number | null> }[]
}
export interface KraTemplate {
  id: string
  name: string
  description: string
  data_needed: string[]
  kras: number
  grounded_kras: number
}

const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const extra = {
  exportUrl: (digestId: number, fmt: 'pdf' | 'pptx') => `/api/digests/${digestId}/export.${fmt}`,
  createShare: (digestId: number, days: number, note: string) =>
    post<ShareLink>(`/digests/${digestId}/share`, { days, note }),
  listShares: (digestId: number) => request<ShareLink[]>(`/digests/${digestId}/shares`),
  revokeShare: (id: number) => post<ShareLink>(`/shares/${id}/revoke`),
  getShared: (token: string) => request<SharedDigest>(`/shared/${encodeURIComponent(token)}`),

  heatmap: (n = 13) => request<Heatmap>(`/insights/heatmap?n=${n}`),
  scorecard: () => request<Scorecard>('/feedback/scorecard'),
  eventTypes: () => request<EventTypeOption[]>('/redlines/event-types'),
  backtest: (body: { type: string; metric?: string; operator?: string; threshold?: number; event_type?: string }) =>
    post<Backtest>('/redlines/backtest', body),
  entities: () => request<EntityView>('/insights/entities'),

  listMetricDefs: () => request<{ definitions: MetricDefinition[]; vocabulary: MetricVocabulary }>('/metrics/definitions'),
  previewMetric: (definition: Partial<MetricDefinition>) =>
    post<{ definition: MetricDefinition; points: { week: string; value: number | null; sources: number }[]; sample_sources: string[] }>(
      '/metrics/preview', { definition }),
  saveMetric: (definition: Partial<MetricDefinition>) => post<{ id: number }>('/metrics/definitions', { definition }),
  retireMetric: (id: string) => post<{ ok: boolean }>(`/metrics/definitions/${id}/retire`),

  listProposals: () =>
    request<{ proposals: Proposal[]; evidence: Record<string, KraEvidence>; guardrails: { min_samples: number; floor: number; ceiling: number; max_step: number } }>(
      '/learning/proposals'),
  propose: () => post<{ proposal: Proposal | null; message: string | null }>('/learning/propose'),
  decide: (id: number, decision: 'approve' | 'reject') => post<Proposal>(`/learning/proposals/${id}/${decision}`),

  listActions: (status?: string) => request<ActionItem[]>(`/actions${status ? `?status=${status}` : ''}`),
  createAction: (body: { title: string; owner?: string; due_date?: string | null; detail?: string; digest_item_id?: number }) =>
    post<ActionItem>('/actions', body),
  updateAction: (id: number, body: Partial<Pick<ActionItem, 'title' | 'owner' | 'status' | 'detail'>> & { due_date?: string | null; clear_due_date?: boolean }) =>
    request<ActionItem>(`/actions/${id}`, { method: 'PUT', body: JSON.stringify(body) }),

  listBriefs: () => request<BriefSummary[]>('/briefs'),
  getBrief: (id: number) => request<MeetingBrief>(`/briefs/${id}`),
  audiences: () => request<{ id: string; description: string }[]>('/briefs/audiences'),
  createBrief: (body: { title: string; audience: string; meeting_date?: string | null; kra_ids: string[]; notes: string }) =>
    post<MeetingBrief>('/briefs', body),

  templates: () => request<KraTemplate[]>('/kra/templates'),
  template: (id: string) =>
    request<{ template: KraTemplate; framework: Record<string, unknown>; weighting_preset: Record<string, unknown> | null }>(`/kra/templates/${id}`),
}
