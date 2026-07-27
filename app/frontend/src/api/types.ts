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
  citations: CitationRef[] | string[]
  is_red_line: boolean
  priority_score: number
  score_decomp: Record<string, number> | null
  relationship_ids: string[]
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
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface MemoryBlock {
  name: string
  content: string
  char_limit: number
  updated_at: string
  editable: boolean
  agent_editable: boolean
  managed_by: 'kra' | 'weighting' | null
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

export type ConnectorAuthType = 'oauth2' | 'basic' | 'api_key'

export interface ConnectorConfigField {
  key: string
  label: string
  secret: boolean
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
