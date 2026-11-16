import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { AskThread, ChatMessage } from '../api/types'
import { ChatVerificationNote } from '../components/ChatVerificationNote'
import { CitationPopover } from '../components/CitationPopover'
import { RichText } from '../components/RichText'
import { useMetrics } from '../hooks/useMetrics'
import { formatWeekLabel } from '../utils/weeks'

const SUGGESTIONS = [
  'What is the most urgent thing this week, and why?',
  'What is behind the change in cash?',
  'Which customers are driving overdue receivables?',
  'What if we make a ₹5 Cr payment this week?',
]

/** Question the whole briefing. Answers are built from the briefing's own evidence and
 * read-only ERP tools; every figure links to its source, and anything the gate could
 * not match is flagged. History is private to you. */
export function Ask() {
  const [params] = useSearchParams()
  const requested = params.get('digest') ? Number(params.get('digest')) : null
  const queryClient = useQueryClient()
  const { labels } = useMetrics()

  const [message, setMessage] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [streamText, setStreamText] = useState('')
  const [toolLabel, setToolLabel] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeCitation, setActiveCitation] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const { data, isLoading, error: loadError } = useQuery({
    queryKey: ['ask', requested],
    queryFn: () => api.getAsk(requested),
  })
  const digestId = data?.digest_id ?? null
  const messages = data?.messages ?? []

  useEffect(() => () => abortRef.current?.abort(), [])
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, pending, streamText, toolLabel])

  const send = async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || busy || digestId == null) return
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
      await api.streamAsk(
        digestId,
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
            queryClient.setQueryData<AskThread>(['ask', requested], (old) =>
              old
                ? {
                    ...old,
                    messages: [
                      ...old.messages,
                      { id: -Date.now(), role: 'user', content: trimmed, created_at: new Date().toISOString() },
                      e.data.message,
                    ],
                  }
                : old,
            )
          }
        },
        ctrl.signal,
      )
      if (streamError) throw new Error(streamError)
      if (!finished) throw new Error('The answer was cut off. Please try again.')
      await queryClient.invalidateQueries({ queryKey: ['ask', requested] })
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

  const startOver = async () => {
    if (busy || !messages.length) return
    if (!window.confirm('Delete your questions and answers about this briefing? This cannot be undone.')) return
    await api.clearAsk(digestId)
    await queryClient.invalidateQueries({ queryKey: ['ask', requested] })
  }

  if (isLoading) return <p className="empty-state">Loading…</p>
  if (loadError) return <p className="error-text">{(loadError as Error).message}</p>
  if (digestId == null)
    return (
      <div>
        <header className="page-header">
          <h1>Ask</h1>
        </header>
        <p className="empty-state">There is no briefing to ask about yet. Once the first digest is generated you can question it here.</p>
      </div>
    )

  return (
    <div className="ask-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">
            Ask · {data?.week ? formatWeekLabel(data.week) : ''}
          </p>
          <h1>Ask about this briefing</h1>
          <p>
            {data?.headline} <Link to={`/digests/${digestId}`}>Open the digest →</Link>
          </p>
        </div>
        <div className="header-actions">
          <button type="button" onClick={startOver} disabled={busy || messages.length === 0}>
            Start over
          </button>
        </div>
      </header>

      <div className="chat-messages" aria-live="polite" aria-busy={busy}>
        {messages.length === 0 && !pending && (
          <div className="chat-suggestions">
            <p className="empty-state">
              Ask anything about this week’s briefing. The agent uses what the briefing already says, reads the ERP
              data, trends and receivables, and can run simple scenarios. Every figure links to its source. Your
              questions are private to you.
            </p>
            {SUGGESTIONS.map((q) => (
              <button key={q} type="button" onClick={() => send(q)}>
                {q}
              </button>
            ))}
          </div>
        )}
        {messages.map((m: ChatMessage) => (
          <div key={m.id} className={`chat-bubble ${m.role}`}>
            <div className="role">{m.role === 'user' ? 'You' : 'Agent'}</div>
            {m.role === 'assistant' ? (
              <>
                <RichText text={m.content} onCite={setActiveCitation} labels={labels} />
                <ChatVerificationNote m={m} />
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
        <p className="error-text" role="alert">
          Couldn&apos;t get an answer: {error}
        </p>
      )}

      {activeCitation && <CitationPopover citationId={activeCitation} onClose={() => setActiveCitation(null)} />}
    </div>
  )
}
