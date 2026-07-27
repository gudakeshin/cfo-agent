import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'

export function ArchivalSearch() {
  const [query, setQuery] = useState('')
  const searchMutation = useMutation({
    mutationFn: (q: string) => api.searchMemory({ query: q, k: 5 }),
  })

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = query.trim()
    if (!trimmed) return
    searchMutation.mutate(trimmed)
  }

  return (
    <div className="archival-search">
      <label htmlFor="archival-query">Archival search</label>
      <form onSubmit={handleSearch} className="archival-search-row">
        <input
          id="archival-query"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. GST notices in the last quarter"
        />
        <button type="submit" className="ink" disabled={searchMutation.isPending}>
          Search
        </button>
      </form>

      {searchMutation.isError && (
        <p className="error-text">{(searchMutation.error as Error).message}</p>
      )}

      {searchMutation.data && (
        <div className="search-results">
          <h4>Archival hits</h4>
          {searchMutation.data.archival.length === 0 ? (
            <p className="empty-state">No archival matches.</p>
          ) : (
            searchMutation.data.archival.map((hit, i) => (
              <div key={hit.id ?? i} className="search-hit-card">
                <div className="meta">
                  {hit.week ?? '—'}
                  {hit.distance != null && ` · dist ${Number(hit.distance).toFixed(3)}`}
                  {hit.metadata?.type != null && ` · ${String(hit.metadata.type)}`}
                </div>
                <div>{hit.text}</div>
              </div>
            ))
          )}
          <h4>Recall hits</h4>
          {searchMutation.data.recall.length === 0 ? (
            <p className="empty-state">No recall matches.</p>
          ) : (
            searchMutation.data.recall.map((hit, i) => (
              <div key={i} className="search-hit-card">
                <div className="meta">
                  {hit.role ?? 'message'}
                  {hit.digest_item_id != null && ` · item #${hit.digest_item_id}`}
                </div>
                <div>{hit.content ?? hit.text ?? JSON.stringify(hit)}</div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
