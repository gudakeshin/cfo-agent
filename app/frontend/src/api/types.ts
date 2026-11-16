export type FaceKey = 'steward' | 'operator' | 'strategist' | 'catalyst'
export type RiskAppetite = 'low' | 'moderate' | 'high'
export type FeedbackVerdict = 'acted_on' | 'useful' | 'set_aside'

export interface DataHealth {
  ok: boolean
  hard_failure: boolean
  stale: boolean
  stale_days: number
  warnings: string[]
  errors: string[]
  latest_receipt_date: string | null
  latest_cash_date: string | null
  invoice_count: number
  as_of_week: string | null
}

export interface DigestSummary {
  id: number
  week: string
  headline: string
  weighting_label: string
  empty_bar: boolean
  email_status: string
  email_path: string | null
  created_at: string
  item_count: number
}

export interface CitationRef {
  id?: string
  citation_id?: string
  label?: string
  text?: string
}

export interface DigestItem {
  id: number
  rank: number
  title: string
  what: string
  why: string
  connects: string
  action: string
  kra_ids: string[]
  signal_ids: string[]
  /** API returns per-section lists ({what: [...], why: [...]}); older shapes were flat arrays. */
  citations: Record<string, string[]> | CitationRef[] | string[]
  is_red_line: boolean
  priority_score: number
  score_decomp: (Record<string, unknown> & { context?: SignalContext; drivers?: Drivers | null; continuity?: Continuity }) | null
  relationship_ids: string[]
  verification?: {
    unverified_figures?: string[]
    continuity_conflicts?: string[]
    fallback?: boolean
    notes?: string[]
  }
}

export interface RunAudit {
  run_id: number | null
  step_timings: Record<string, number>
  token_usage: Record<string, unknown>
  cost_usd: number
  suppressed: unknown[]
}

export interface DigestDetail extends DigestSummary {
  data_health: DataHealth | null
  items: DigestItem[]
  run_audit: RunAudit | null
}

export interface PipelineRun {
  id: number
  week: string
  status: string
  current_step: string | null
  step_timings: Record<string, number>
  token_usage: Record<string, unknown>
  cost_usd: number
  suppressed: unknown[]
  data_health: DataHealth | null
  digest_id: number | null
  error: string | null
  created_at: string
  completed_at: string | null
}

export interface PipelineRunSummary {
  id: number
  week: string
  status: string
  current_step: string | null
  cost_usd: number
  digest_id: number | null
  error: string | null
  created_at: string
  completed_at: string | null
}

export interface RedLine {
  id: string
  type: string
  description: string
  metric?: string
  operator?: string
  threshold?: number
  trigger?: string
  table?: string
  event_type?: string
}

export interface WeightingContent {
  label: string
  defining_priority: string
  face_weights: Record<FaceKey, number>
  kra_ranks: string[]
  risk_appetite: RiskAppetite
  red_lines: RedLine[]
  preferences: Record<string, unknown>
  kra_multipliers?: Record<string, number>
}

export interface WeightingResponse {
  id: number
  label: string
  content: WeightingContent
  created_at?: string
}

export interface WeightingPreset {
  id: string
  label: string
  path: string
  content: WeightingContent
}

export interface Kra {
  id: string
  name: string
  face: string
  grounding: string
  metrics: string[]
  concerns: string[]
  note: string
}

export interface KraRelationship {
  id: string
  from: string
  to: string
  description: string
}

export interface KraFramework {
  version: string
  db_version_id: number
  label: string
  india_regulatory_context: string
  kras: Kra[]
  faces: Record<string, { label?: string; kras: unknown[] }>
  relationships: KraRelationship[]
  metric_map: Record<string, string | string[]>
  registry_metrics: string[]
}

export interface KraVersionSummary {
  id: number
  version: string
  content_hash: string
  created_at: string
  is_active: boolean
}

export interface KraVersionDetail {
  id: number
  version: string
  content_hash: string
  created_at: string
  content: {
    version: string
    label: string
    india_regulatory_context: string
    faces: Record<string, { label?: string; kras: unknown[] }>
    relationships: KraRelationship[]
    metric_map: Record<string, string | string[]>
  }
}

export interface KraPutBody {
  version?: string
  label: string
  india_regulatory_context: string
  kras: Kra[]
  relationships: KraRelationship[]
}

export interface KraPutResponse {
  version: KraFramework
  warnings: string[]
  db_version_id: number
  reconciled_weighting: { id: number; label: string } | null
}

export interface KraRestoreResponse {
  version: KraFramework
  warnings: string[]
  restored_from: number
  new_version_id: number
  reconciled_weighting: { id: number; label: string } | null
}

export interface FeedbackItem {
  id: number
  digest_item_id: number
  title: string | null
  verdict: FeedbackVerdict
  note: string
  already_knew: boolean
  updated_at: string
}

export interface FeedbackLog {
  items: FeedbackItem[]
  counts: Record<FeedbackVerdict, number>
  surfacing_precision_pct: number
  g1_novel_actionable: number
}

export interface Alert {
  id: number
  rule_id: string
  title: string
  description: string
  week: string
  as_of_date: string
  status: string
  evidence: unknown
  synthesis: unknown
  email_status: string
  email_path: string | null
  created_at: string
}

export interface CitationDetail {
  citation_id: string
  summary: string
  record: Record<string, unknown> | null
  /** true when served from the digest's frozen evidence snapshot */
  snapshot?: boolean
}

export interface ChatVerification {
  unverified_figures?: string[]
  rejected_citations?: string[]
  uncited?: boolean
  tools_used?: string[]
  as_of_week?: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
  citations?: string[]
  verification?: ChatVerification
}

export type ChatStreamEvent =
  | { event: 'delta'; data: { text: string } }
  | { event: 'reset'; data: Record<string, never> }
  | { event: 'tool'; data: { name: string; label: string } }
  | { event: 'done'; data: { message: ChatMessage } }
  | { event: 'error'; data: { detail: string } }

export interface MemoryBlock {
  name: string
  content: string
  char_limit: number
  updated_at: string
  editable: boolean
  agent_editable: boolean
  managed_by: 'kra' | 'weighting' | null
  provenance?: 'cfo-office' | 'model-inferred'
}

export interface MemoryRevision {
  version: number
  content: string
  edited_by: string
  reason: string
  created_at: string
}

export interface MemorySearchHit {
  id?: number
  text?: string
  content?: string
  week?: string | null
  distance?: number | null
  role?: string
  digest_item_id?: number
  metadata?: Record<string, unknown>
}

export interface MemorySearchResult {
  archival: MemorySearchHit[]
  recall: MemorySearchHit[]
}

export interface ConsolidationRun {
  id: number
  window_start: string
  window_end: string
  summary: string
  token_usage: Record<string, unknown>
  edits_count: number
  created_at: string
}

export interface MaintenanceResult {
  ok: boolean
  stats: Record<string, unknown>
}

// ─── Connectors ─────────────────────────────────────────────────────────────

export type ConnectorAuthType = 'oauth2' | 'basic' | 'api_key' | 'file' | 'none' | 'db_url'

export interface ConnectorConfigField {
  key: string
  label: string
  secret: boolean
  multiline?: boolean
  placeholder?: string
}

export interface ConnectorConfig {
  id: string
  vendor: string
  display_name: string
  auth_type: ConnectorAuthType
  brand_initials: string
  brand_color: string
  owned_tables: string[]
  config_fields: ConnectorConfigField[]
  enabled: boolean
  config: Record<string, string>
  secret_set: boolean
  secret_masked: string | null
  cron: string | null
  watermark_week: string | null
  watermark?: string | null
  kind?: 'simulated' | 'file_drop' | 'extractor'
  last_run_at: string | null
  last_status: string
}

export interface ConnectorsListResponse {
  connectors: ConnectorConfig[]
  erp_seed: number
}

export interface ConnectorSyncRun {
  id: number
  connector_id: string
  trigger: string
  mode: string
  status: string
  started_at: string
  finished_at: string | null
  rows_pulled: Record<string, number | { created?: number; updated?: number; deleted?: number; total?: number }>
  error: string | null
}

export interface ConnectorTestResult {
  ok: boolean
  connector_id: string
  message?: string
  missing_fields?: string[]
  latency_ms?: number
}

export interface ConnectorSyncStartResponse {
  run_id: number
  connector_id: string
  mode: string
  status: string
}

export interface ConnectorSyncAllResponse {
  run_ids: number[]
  skipped: string[]
}

// ─── Regulatory news ────────────────────────────────────────────────────────

export type NewsCategory =
  | 'rate_change'
  | 'notification'
  | 'circular'
  | 'council_decision'
  | 'e_invoicing'
  | 'itc'
  | 'deadline'
  | 'sebi'
  | 'mca'
  | 'rbi_fema'
  | 'ind_as'
  | 'budget'
  | 'other'

export type NewsRelevance = 'high' | 'medium' | 'low'

export interface NewsItem {
  id: number
  url: string
  title: string
  summary: string
  source_name: string
  published: string | null
  category: NewsCategory
  relevance: NewsRelevance
  fetched_at: string
}

export interface NewsRefreshRun {
  id: number
  trigger: string
  query: string | null
  status: string
  items_found: number
  items_new: number
  items_dropped: number
  error: string | null
  started_at: string
  finished_at: string | null
}

export interface NewsStatus {
  running: boolean
  running_run_id: number | null
  total_items: number
  latest_run: NewsRefreshRun | null
}

export interface NewsTopics {
  topics: string[]
  defaults: string[]
}

// ── CFO read models ──────────────────────────────────────────────────────────

export interface MetricMeta {
  id: string
  label: string
  unit: string
  adverse_direction: 'up' | 'down' | null
  custom?: boolean
  proxy?: boolean
  description?: string
}

export interface SignalContext {
  base_severity?: number
  anomaly?: { z: number; is_anomaly: boolean }
  plan?: { plan: number; gap: number; gap_pct: number; adverse: boolean; severity: number; citation: string }
  covenant?: {
    name: string; threshold: number; buffer: number; operator: string; headroom: number
    breached: boolean; in_buffer: boolean; projected_breach_in_weeks: number | null; citation: string
  }
  forecast?: { h: number; value: number; low: number; high: number }[]
}

export interface MetricHistory extends MetricMeta {
  points: { week: string; value: number | null }[]
}

export interface BriefingChange {
  item_id: number
  title: string
  is_red_line: boolean
  rank: number
}

export interface BriefingKpi extends MetricMeta {
  value: number | null
  previous: number | null
  delta: number | null
  adverse: boolean | null
}

export interface Briefing {
  digest_id: number | null
  week?: string
  previous_digest_id?: number | null
  previous_week?: string | null
  changes?: { new: BriefingChange[]; continuing: BriefingChange[]; dropped: BriefingChange[] }
  kpis?: BriefingKpi[]
}

export interface PreviewRanking {
  top: (MetricMeta & { score: number; surfaced: boolean })[]
  red_lines: { rule_id: string; description: string }[]
  surfaced_count: number
  empty_bar: boolean
}

export interface WeightingPreview {
  week: string
  active: PreviewRanking
  draft: PreviewRanking
}

// ── Auth & audit ─────────────────────────────────────────────────────────────

export type Role = 'viewer' | 'cfo' | 'delegate' | 'admin'
export type Permission = 'read' | 'interact' | 'configure' | 'administer'

export interface AuthUser {
  email: string
  name: string
  role: Role
}

export interface AuthState {
  auth_mode: 'off' | 'local' | 'oidc'
  demo_mode: boolean
  user: AuthUser | null
}

export interface AuditEvent {
  id: number
  at: string
  user: string
  role: string
  action: string
  method: string
  path: string
  status: number
  ip: string | null
  request_id: string | null
}

export interface ImportedFileRow {
  id: number
  run_id: number | null
  file: string
  table: string
  status: 'loaded' | 'rejected' | 'duplicate'
  inserted: number
  updated: number
  error_count: number
  errors: { line: number; column: string; error: string }[]
  imported_at: string
}

/** What moved a metric: largest contributors, computed when the digest was generated. */
export interface DriverRow {
  label: string
  group: string
  current_paise: number
  previous_paise: number | null
  change_paise: number | null
  citation: string | null
  note: string
}

export interface Drivers {
  title: string
  basis: string
  rows: DriverRow[]
  reconciliation?: Record<string, number> | null
}

/** Whether a finding was in earlier briefings (computed from the stored digests). */
export interface Continuity {
  status: 'new' | 'continuing'
  first_briefing?: boolean
  last_week?: string
  since_week?: string
  weeks_running?: number
}

/** The signed-in user's conversation about one briefing. */
export interface AskThread {
  digest_id: number | null
  week: string | null
  headline: string | null
  messages: ChatMessage[]
}
