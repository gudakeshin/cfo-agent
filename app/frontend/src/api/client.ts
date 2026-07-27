import type {
  Alert,
  ChatMessage,
  CitationDetail,
  ConnectorConfig,
  ConnectorsListResponse,
  ConnectorSyncAllResponse,
  ConnectorSyncRun,
  ConnectorSyncStartResponse,
  ConnectorTestResult,
  ConsolidationRun,
  DataHealth,
  DigestDetail,
  DigestSummary,
  FeedbackLog,
  FeedbackVerdict,
  KraFramework,
  KraPutBody,
  KraPutResponse,
  KraRestoreResponse,
  KraVersionDetail,
  KraVersionSummary,
  MaintenanceResult,
  MemoryBlock,
  MemoryRevision,
  MemorySearchResult,
  NewsItem,
  NewsStatus,
  NewsTopics,
  PipelineRun,
  PipelineRunSummary,
  WeightingContent,
  WeightingPreset,
  WeightingResponse,
} from './types'

const BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? JSON.stringify(body)
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  health: () => request<{ ok: boolean }>('/health'),

  // Data
  getDataHealth: (week?: string) =>
    request<DataHealth>(week ? `/data/health?week=${encodeURIComponent(week)}` : '/data/health'),
  generateData: (body: { seed?: number; stale_days?: number; confirm?: boolean }) =>
    request<{ ok: boolean; meta: Record<string, unknown> }>('/data/generate', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getDataWeeks: () => request<{ weeks: string[]; end?: string }>('/data/weeks'),

  // KRA
  getKra: () => request<KraFramework>('/kra'),
  putKra: (body: KraPutBody) =>
    request<KraPutResponse>('/kra', { method: 'PUT', body: JSON.stringify(body) }),
  listKraVersions: () => request<KraVersionSummary[]>('/kra/versions'),
  getKraVersion: (versionId: number) =>
    request<KraVersionDetail>(`/kra/versions/${versionId}`),
  restoreKraVersion: (versionId: number) =>
    request<KraRestoreResponse>(`/kra/versions/${versionId}/restore`, { method: 'POST' }),

  // Weighting
  getWeighting: () => request<WeightingResponse>('/weighting'),
  putWeighting: (body: WeightingContent) =>
    request<WeightingResponse>('/weighting', { method: 'PUT', body: JSON.stringify(body) }),
  getWeightingPresets: () => request<WeightingPreset[]>('/weighting/presets'),
  loadWeightingPreset: (presetId: string) =>
    request<WeightingResponse>(`/weighting/presets/${encodeURIComponent(presetId)}/load`, {
      method: 'POST',
    }),

  // Digests
  listDigests: () => request<DigestSummary[]>('/digests'),
  getDigest: (id: number) => request<DigestDetail>(`/digests/${id}`),
  generateDigest: (body: { week: string; weighting_version?: number }) =>
    request<{ run_id: number; week: string; status: string }>('/digests/generate', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  resendDigestEmail: (id: number) =>
    request<{ email_status: string; email_path: string | null }>(`/digests/${id}/email`, {
      method: 'POST',
    }),

  // Runs
  listRuns: () => request<PipelineRunSummary[]>('/runs'),
  getRun: (id: number) => request<PipelineRun>(`/runs/${id}`),

  // Feedback
  listFeedback: () => request<FeedbackLog>('/feedback'),
  submitFeedback: (body: {
    digest_item_id: number
    verdict: FeedbackVerdict
    note?: string
    already_knew?: boolean
  }) =>
    request<{
      id: number
      digest_item_id: number
      verdict: FeedbackVerdict
      note: string
      already_knew: boolean
    }>('/feedback', { method: 'POST', body: JSON.stringify(body) }),

  // Chat
  getChat: (itemId: number) => request<ChatMessage[]>(`/chat/${itemId}`),
  postChat: (itemId: number, message: string) =>
    request<ChatMessage>(`/chat/${itemId}`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),

  // Citations
  getCitation: (cid: string) =>
    request<CitationDetail>(`/citations/${encodeURIComponent(cid)}`),

  // Alerts
  listAlerts: (active?: boolean) =>
    request<Alert[]>(active ? '/alerts?active=true' : '/alerts'),
  ackAlert: (id: number) =>
    request<{ id: number; status: string }>(`/alerts/${id}/ack`, { method: 'POST' }),

  // Memory
  listMemoryBlocks: () => request<MemoryBlock[]>('/memory/blocks'),
  getBlockHistory: (name: string) =>
    request<MemoryRevision[]>(`/memory/blocks/${encodeURIComponent(name)}/history`),
  updateMemoryBlock: (name: string, body: { content: string; reason?: string }) =>
    request<{ name: string; content: string; updated_at: string }>(
      `/memory/blocks/${encodeURIComponent(name)}`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),
  searchMemory: (body: { query: string; k?: number }) =>
    request<MemorySearchResult>('/memory/search', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  consolidateMemory: () =>
    request<{
      ok: boolean
      message?: string
      id?: number
      summary?: string
      edits?: unknown
      token_usage?: unknown
    }>('/memory/consolidate', { method: 'POST' }),
  listConsolidations: (limit?: number) =>
    request<ConsolidationRun[]>(
      limit ? `/memory/consolidations?limit=${limit}` : '/memory/consolidations',
    ),
  runMaintenance: () => request<MaintenanceResult>('/memory/maintenance', { method: 'POST' }),

  // Connectors
  listConnectors: () => request<ConnectorsListResponse>('/connectors'),
  putConnector: (
    id: string,
    body: { enabled?: boolean; config?: Record<string, string>; secret?: string; cron?: string },
  ) =>
    request<ConnectorConfig>(`/connectors/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  testConnector: (id: string) =>
    request<ConnectorTestResult>(`/connectors/${encodeURIComponent(id)}/test`, {
      method: 'POST',
    }),
  syncConnector: (id: string, mode: 'incremental' | 'full' = 'incremental') =>
    request<ConnectorSyncStartResponse>(
      `/connectors/${encodeURIComponent(id)}/sync?mode=${mode}`,
      { method: 'POST' },
    ),
  syncAllConnectors: () =>
    request<ConnectorSyncAllResponse>('/connectors/sync-all', { method: 'POST' }),
  getConnectorRun: (runId: number) => request<ConnectorSyncRun>(`/connectors/runs/${runId}`),
  listConnectorRuns: (id: string, limit = 20) =>
    request<ConnectorSyncRun[]>(
      `/connectors/${encodeURIComponent(id)}/runs?limit=${limit}`,
    ),

  // Regulatory news
  listNews: (opts?: { category?: string; since?: string; limit?: number }) => {
    const params = new URLSearchParams()
    if (opts?.category) params.set('category', opts.category)
    if (opts?.since) params.set('since', opts.since)
    if (opts?.limit) params.set('limit', String(opts.limit))
    const qs = params.toString()
    return request<NewsItem[]>(`/news${qs ? `?${qs}` : ''}`)
  },
  getNewsStatus: () => request<NewsStatus>('/news/status'),
  refreshNews: (query?: string) =>
    request<{ run_id: number; status: string }>('/news/refresh', {
      method: 'POST',
      body: JSON.stringify({ query: query || undefined }),
    }),
  getNewsTopics: () => request<NewsTopics>('/news/topics'),
  putNewsTopics: (topics: string[]) =>
    request<{ topics: string[] }>('/news/topics', {
      method: 'PUT',
      body: JSON.stringify({ topics }),
    }),
}
