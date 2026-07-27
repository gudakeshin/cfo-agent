import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { MemoryBlock } from '../api/types'

interface Props {
  block: MemoryBlock
}

const MANAGED_LABEL: Record<string, { label: string; to: string }> = {
  kra: { label: 'KRA editor', to: '/kras' },
  weighting: { label: 'Weighting editor', to: '/weighting' },
}

export function MemoryBlockCard({ block }: Props) {
  const [content, setContent] = useState(block.content)
  const [reason, setReason] = useState('Human edit')
  const [expanded, setExpanded] = useState(false)
  const queryClient = useQueryClient()
  const readOnly = block.editable === false || !!block.managed_by

  const { data: history = [] } = useQuery({
    queryKey: ['memory-history', block.name],
    queryFn: () => api.getBlockHistory(block.name),
    enabled: expanded,
  })

  const saveMutation = useMutation({
    mutationFn: () => api.updateMemoryBlock(block.name, { content, reason }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memory-blocks'] })
      queryClient.invalidateQueries({ queryKey: ['memory-history', block.name] })
    },
  })

  const managed = block.managed_by ? MANAGED_LABEL[block.managed_by] : null

  return (
    <div className="memory-block">
      <header>
        <strong>{block.name}</strong>
        <span>
          {content.length}/{block.char_limit} chars
          {readOnly ? ' · read only' : block.agent_editable ? ' · agent editable' : ' · human only'}
        </span>
      </header>
      <div className="content-area">
        {readOnly ? (
          <>
            <pre className="memory-block-readonly">{block.content || '(empty)'}</pre>
            {managed && (
              <p className="label-below" style={{ marginTop: 8 }}>
                Managed by the <Link to={managed.to}>{managed.label}</Link> — edit it there instead.
              </p>
            )}
            <div style={{ marginTop: '0.5rem' }}>
              <button type="button" onClick={() => setExpanded(!expanded)}>
                {expanded ? 'Hide' : 'Show'} revision history
              </button>
            </div>
          </>
        ) : (
          <>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              maxLength={block.char_limit}
            />
            <div style={{ marginTop: '0.5rem' }}>
              <label htmlFor={`reason-${block.name}`}>Edit reason</label>
              <input
                id={`reason-${block.name}`}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </div>
            <div style={{ marginTop: '0.75rem', display: 'flex', gap: '0.5rem' }}>
              <button
                type="button"
                className="primary"
                onClick={() => saveMutation.mutate()}
                disabled={saveMutation.isPending || content === block.content}
              >
                Save block
              </button>
              <button type="button" onClick={() => setExpanded(!expanded)}>
                {expanded ? 'Hide' : 'Show'} revision history
              </button>
            </div>
            {saveMutation.isError && (
              <p className="error-text">{(saveMutation.error as Error).message}</p>
            )}
            {saveMutation.isSuccess && <p className="success-text">Saved.</p>}
          </>
        )}

        {expanded && (
          <div className="revision-list">
            {history.length === 0 && <p className="empty-state">No revisions yet.</p>}
            {[...history].reverse().map((rev, idx, newestFirst) => {
              const prev = newestFirst[idx + 1] // next older revision
              return (
                <details key={rev.version}>
                  <summary>
                    v{rev.version} · {rev.edited_by} · {new Date(rev.created_at).toLocaleString()} —{' '}
                    {rev.reason}
                  </summary>
                  {prev ? (
                    <div className="revision-diff">
                      <div>
                        <div className="diff-label">Before (v{prev.version})</div>
                        <div className="diff-pane">{prev.content || '(empty)'}</div>
                      </div>
                      <div>
                        <div className="diff-label">After (v{rev.version})</div>
                        <div className="diff-pane">{rev.content || '(empty)'}</div>
                      </div>
                    </div>
                  ) : (
                    <pre>{rev.content}</pre>
                  )}
                </details>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
