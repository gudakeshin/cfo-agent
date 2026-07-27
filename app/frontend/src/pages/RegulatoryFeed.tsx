import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { NewsCategory } from '../api/types'

const CATEGORIES: { value: NewsCategory; label: string }[] = [
  { value: 'rate_change', label: 'Rate change' },
  { value: 'notification', label: 'Notification' },
  { value: 'circular', label: 'Circular' },
  { value: 'council_decision', label: 'Council decision' },
  { value: 'e_invoicing', label: 'E-invoicing' },
  { value: 'itc', label: 'ITC' },
  { value: 'deadline', label: 'Deadline' },
  { value: 'sebi', label: 'SEBI' },
  { value: 'mca', label: 'MCA' },
  { value: 'rbi_fema', label: 'RBI/FEMA' },
  { value: 'ind_as', label: 'Ind AS' },
  { value: 'budget', label: 'Budget' },
  { value: 'other', label: 'Other' },
]

export function RegulatoryFeed() {
  const queryClient = useQueryClient()
  const [category, setCategory] = useState<NewsCategory | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [topicsOpen, setTopicsOpen] = useState(false)
  const [newTopic, setNewTopic] = useState('')
  const [draftTopics, setDraftTopics] = useState<string[] | null>(null)

  const { data: items = [], isLoading, error } = useQuery({
    queryKey: ['news', category],
    queryFn: () => api.listNews({ category: category ?? undefined, limit: 100 }),
  })

  const { data: status } = useQuery({
    queryKey: ['news-status'],
    queryFn: api.getNewsStatus,
    refetchInterval: (q) => (q.state.data?.running ? 2000 : false),
    refetchIntervalInBackground: true,
  })

  const { data: topicsData } = useQuery({
    queryKey: ['news-topics'],
    queryFn: api.getNewsTopics,
  })

  const topics = draftTopics ?? topicsData?.topics ?? []

  const wasRunning = useRef(false)
  useEffect(() => {
    const running = status?.running ?? false
    if (wasRunning.current && !running) {
      queryClient.invalidateQueries({ queryKey: ['news'] })
    }
    wasRunning.current = running
  }, [status, queryClient])

  const refreshMutation = useMutation({
    mutationFn: (query?: string) => api.refreshNews(query),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['news-status'] })
    },
  })

  const topicsMutation = useMutation({
    mutationFn: (t: string[]) => api.putNewsTopics(t),
    onSuccess: (data) => {
      setDraftTopics(data.topics)
      queryClient.invalidateQueries({ queryKey: ['news-topics'] })
    },
  })

  const running = status?.running ?? false
  const lastRefreshed = status?.latest_run?.finished_at ?? status?.latest_run?.started_at

  const addTopic = () => {
    const t = newTopic.trim()
    if (!t) return
    setDraftTopics([...topics, t])
    setNewTopic('')
  }

  const removeTopic = (t: string) => {
    setDraftTopics(topics.filter((x) => x !== t))
  }

  return (
    <div>
      <header className="page-header">
        <h1>Regulatory feed</h1>
        <p>Live GST/SEBI/MCA/RBI news, mapped to categories and relevance.</p>
      </header>

      <div className="toolbar" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          <button
            type="button"
            className={`chip-toggle${category === null ? ' active' : ''}`}
            onClick={() => setCategory(null)}
          >
            All
          </button>
          {CATEGORIES.map((c) => (
            <button
              key={c.value}
              type="button"
              className={`chip-toggle${category === c.value ? ' active' : ''}`}
              onClick={() => setCategory(c.value)}
            >
              {c.label}
            </button>
          ))}
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' }}>
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && searchQuery.trim()) refreshMutation.mutate(searchQuery.trim())
            }}
            placeholder="Search a topic to pull fresh results…"
            style={{ maxWidth: 320 }}
            disabled={running}
          />
          <button
            type="button"
            className="primary"
            onClick={() =>
              refreshMutation.mutate(searchQuery.trim() ? searchQuery.trim() : undefined)
            }
            disabled={running || refreshMutation.isPending}
          >
            {running && <span className="spinner" />}
            {running ? 'Refreshing…' : 'Refresh'}
          </button>
          <span className="toolbar-hint" style={{ marginLeft: 0 }}>
            {lastRefreshed
              ? `Last refreshed ${new Date(lastRefreshed).toLocaleString()}`
              : 'Never refreshed'}
            {status?.total_items != null && ` · ${status.total_items} items tracked`}
          </span>
          <button type="button" style={{ marginLeft: 'auto' }} onClick={() => setTopicsOpen((v) => !v)}>
            {topicsOpen ? 'Hide topics' : 'Edit topics'}
          </button>
        </div>

        {refreshMutation.isError && (
          <p className="error-text">{(refreshMutation.error as Error).message}</p>
        )}

        {topicsOpen && (
          <div className="form-section" style={{ marginTop: 4, marginBottom: 0 }}>
            <h3>Watch topics</h3>
            <p>Guides what the refresh searches for. Falls back to defaults if empty.</p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
              {topics.map((t) => (
                <span key={t} className="chip-toggle active topic-chip">
                  {t}
                  <button type="button" onClick={() => removeTopic(t)} aria-label={`Remove ${t}`}>
                    ×
                  </button>
                </span>
              ))}
              {topics.length === 0 && <span className="empty-state">No topics — using defaults.</span>}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                value={newTopic}
                onChange={(e) => setNewTopic(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && addTopic()}
                placeholder="Add a topic…"
                style={{ maxWidth: 320 }}
              />
              <button type="button" onClick={addTopic}>
                Add
              </button>
              <button
                type="button"
                className="primary"
                onClick={() => topicsMutation.mutate(topics)}
                disabled={topicsMutation.isPending}
              >
                Save topics
              </button>
            </div>
            {topicsMutation.isSuccess && <p className="success-text">Topics saved.</p>}
            {topicsMutation.isError && (
              <p className="error-text">{(topicsMutation.error as Error).message}</p>
            )}
          </div>
        )}
      </div>

      {isLoading && <p className="empty-state">Loading regulatory feed…</p>}
      {error && <p className="error-text">{(error as Error).message}</p>}
      {!isLoading && items.length === 0 && (
        <p className="empty-state">No items yet — try Refresh.</p>
      )}

      <ul className="news-list">
        {items.map((item) => (
          <li key={item.id} className="news-card">
            <a href={item.url} target="_blank" rel="noreferrer">
              <h3>{item.title}</h3>
            </a>
            <div className="meta">
              {item.source_name && <span>{item.source_name}</span>}
              {item.published && <span>· {item.published}</span>}
            </div>
            {item.summary && <p style={{ margin: '10px 0 0' }}>{item.summary}</p>}
            <div className="news-card-badges">
              <span className="badge-muted">
                {CATEGORIES.find((c) => c.value === item.category)?.label ?? item.category}
              </span>
              <span className={`badge-relevance ${item.relevance}`}>{item.relevance}</span>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
