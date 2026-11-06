import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ChatMessage } from '../api/types'
import { useEscape } from '../hooks/useEscape'
import { useMetrics } from '../hooks/useMetrics'
import { CitationPopover } from './CitationPopover'
import { RichText } from './RichText'

interface Props {
  itemId: number
  itemTitle: string
  onClose: () => void
}

const SUGGESTIONS = [
  'Which customers are driving the overdue receivables?',
  'What if we make a ₹5 Cr payment this week?',
  'How has this moved over the last 8 weeks?',
]

function VerificationNote({ m }: { m: ChatMessage }) {
  const v = m.verification
  if (!v) return null
  const notes: string[] = []
  if (v.unverified_figures?.length)
    notes.push(`Check before relying on: ${v.unverified_figures.join(', ')} (not matched to a source).`)
  if (v.rejected_citations?.length)
    notes.push(`${v.rejected_citations.length} unsupported source reference(s) removed.`)
  if (v.uncited && !notes.length) notes.push('No sources cited in this answer.')
  if (!notes.length) return null
  return <p className="verification-note chat-verification">{notes.join(' ')}</p>
}

export function DrilldownChat({ itemId, itemTitle, onClose }: Props) {
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [streamText, setStreamText] = useState('')
  const [toolLabel, setToolLabel] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeCitation, setActiveCitation] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const titleId = useId()
  const queryClient = useQueryClient()
  const { labels } = useMetrics()
  const close = useCallback(() => onClose(), [onClose])
  useEscape(close, activeCitation == null)

  const { data: messages = [], isLoading } = useQuery({
    queryKey: ['chat', itemId],
    queryFn: () => api.getChat(itemId),
  })

  useEffect(() => inputRef.current?.focus(), [])
  useEffect(() => () => abortRef.current?.abort(), [])
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, pending, streamText, toolLabel])

  const send = async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || busy) return
    setBusy(true)
    setError(null)
    setPending(trimmed)
    setMessage('')
    setStreamText('')
    setToolLabel(null)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    let finished = false
    let streamError: string | null = null
    try {
      await api.streamChat(
        itemId,
        trimmed,
        (e) => {
          if (e.event === 'delta') {
            setToolLabel(null)
            setStreamText((t) => t + e.data.text)
          } else if (e.event === 'reset') setStreamText('')
          else if (e.event === 'tool') setToolLabel(e.data.label)
          else if (e.event === 'error') streamError = e.data.detail
          else if (e.event === 'done') {
            finished = true
            queryClient.setQueryData<ChatMessage[]>(['chat', itemId], (old = []) => [
              ...old,
              { id: -Date.now(), role: 'user', content: trimmed, created_at: new Date().toISOString() },
              e.data.message,
            ])
          }
        },
        ctrl.signal,
      )
      if (streamError) throw new Error(streamError)
      if (!finished) throw new Error('The answer was cut off. Please try again.')
      await queryClient.invalidateQueries({ queryKey: ['chat', itemId] })
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        setError((err as Error).message)
        setMessage(trimmed) // give the question back so nothing is lost
      }
    } finally {
      setPending(null)
      setStreamText('')
      setToolLabel(null)
      setBusy(false)
    }
  }

  return (
    <aside className="side-panel" role="dialog" aria-labelledby={titleId}>
      <header>
        <div>
          <h3 id={titleId} style={{ margin: 0, fontSize: '1rem' }}>
            Ask about this insight
          </h3>
          <p style={{ margin: 0, fontSize: '0.8rem', color: 'var(--ink-muted)' }}>{itemTitle}</p>
        </div>
        <button type="button" onClick={onClose} aria-label="Close discussion">
          ✕
        </button>
      </header>

      <div className="chat-messages" aria-live="polite" aria-busy={busy}>
        {isLoading && <p className="empty-state">Loading conversation…</p>}
        {!isLoading && messages.length === 0 && !pending && (
          <div className="chat-suggestions">
            <p className="empty-state">
              Ask a follow-up. The agent can read the ERP data, trends and receivables, and run simple
              scenarios. Every figure links to its source.
            </p>
            {SUGGESTIONS.map((s) => (
              <button key={s} type="button" onClick={() => send(s)}>
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`chat-bubble ${m.role}`}>
            <div className="role">{m.role === 'user' ? 'You' : 'Agent'}</div>
            {m.role === 'assistant' ? (
              <>
                <RichText text={m.content} onCite={setActiveCitation} labels={labels} />
                <VerificationNote m={m} />
              </>
            ) : (
              <div>{m.content}</div>
            )}
          </div>
        ))}
        {pending && (
          <div className="chat-bubble user">
            <div className="role">You</div>
            <div>{pending}</div>
          </div>
        )}
        {busy && (
          <div className="chat-bubble assistant">
            <div className="role">Agent</div>
            {toolLabel && (
              <div className="tool-activity" role="status">
                <span className="spinner" /> {toolLabel}…
              </div>
            )}
            {streamText ? (
              <RichText text={streamText} labels={labels} />
            ) : (
              !toolLabel && (
                <span className="typing-dots" aria-label="Agent is thinking">
                  <span />
                  <span />
                  <span />
                </span>
              )
            )}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <form
        className="chat-input"
        onSubmit={(e) => {
          e.preventDefault()
          send(message)
        }}
      >
        <textarea
          ref={inputRef}
          rows={2}
          maxLength={2000}
          aria-label="Your question"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send(message)
            }
          }}
          placeholder="Ask about drivers, customers, scenarios… (Enter to send)"
        />
        {busy ? (
          <button type="button" onClick={() => abortRef.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="submit" className="primary" disabled={!message.trim()}>
            Send
          </button>
        )}
      </form>
      {error && (
        <p className="error-text" role="alert" style={{ padding: '0 1.25rem 1rem' }}>
          Couldn&apos;t get an answer: {error}
        </p>
      )}

      {activeCitation && (
        <CitationPopover
          citationId={activeCitation}
          digestItemId={itemId}
          onClose={() => setActiveCitation(null)}
        />
      )}
    </aside>
  )
}
