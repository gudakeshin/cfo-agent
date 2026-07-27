import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'

interface Props {
  itemId: number
  itemTitle: string
  onClose: () => void
}

export function DrilldownChat({ itemId, itemTitle, onClose }: Props) {
  const [message, setMessage] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  const { data: messages = [], isLoading } = useQuery({
    queryKey: ['chat', itemId],
    queryFn: () => api.getChat(itemId),
  })

  const sendMutation = useMutation({
    mutationFn: (msg: string) => api.postChat(itemId, msg),
    onSuccess: () => {
      setMessage('')
      queryClient.invalidateQueries({ queryKey: ['chat', itemId] })
    },
  })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = message.trim()
    if (!trimmed || sendMutation.isPending) return
    sendMutation.mutate(trimmed)
  }

  return (
    <aside className="side-panel">
      <header>
        <div>
          <h3 style={{ margin: 0, fontSize: '1rem' }}>Drilldown</h3>
          <p style={{ margin: 0, fontSize: '0.8rem', color: 'var(--ink-muted)' }}>{itemTitle}</p>
        </div>
        <button type="button" onClick={onClose}>
          ✕
        </button>
      </header>

      <div className="chat-messages">
        {isLoading && <p className="empty-state">Loading chat…</p>}
        {!isLoading && messages.length === 0 && (
          <p className="empty-state">Ask a follow-up about this insight.</p>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`chat-bubble ${m.role}`}>
            <div className="role">{m.role}</div>
            <div>{m.content}</div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <form className="chat-input" onSubmit={handleSend}>
        <input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="Ask about drivers, covenants, next steps…"
          disabled={sendMutation.isPending}
        />
        <button type="submit" className="primary" disabled={sendMutation.isPending || !message.trim()}>
          Send
        </button>
      </form>
      {sendMutation.isError && (
        <p className="error-text" style={{ padding: '0 1.25rem 1rem' }}>
          {(sendMutation.error as Error).message}
        </p>
      )}
    </aside>
  )
}
