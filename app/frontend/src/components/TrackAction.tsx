import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { extra } from '../api/extra'
import type { DigestItem } from '../api/types'

/** Turn an insight's recommended action into a tracked commitment. */
export function TrackAction({ item }: { item: DigestItem }) {
  const qc = useQueryClient()
  const existing = useQuery({ queryKey: ['actions', 'all'], queryFn: () => extra.listActions(),
    select: (rows) => rows.filter((a) => a.digest_item_id === item.id) })
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState(item.action?.split(/(?<=\.)\s/)[0]?.slice(0, 200) || item.title)
  const [owner, setOwner] = useState('')
  const [due, setDue] = useState('')
  const create = useMutation({
    mutationFn: () => extra.createAction({ title, owner, due_date: due || null, digest_item_id: item.id }),
    onSuccess: () => {
      setOpen(false)
      qc.invalidateQueries({ queryKey: ['actions'] })
    },
  })
  const tracked = existing.data ?? []
  return (
    <div className="track-action">
      {tracked.map((a) => (
        <span key={a.id} className={`badge ${a.overdue ? 'badge-red' : a.status === 'done' ? 'badge-muted' : 'badge-amber'}`}>
          Action: {a.status.replace('_', ' ')}{a.owner ? ` · ${a.owner}` : ''}{a.due_date ? ` · due ${a.due_date}` : ''}
        </span>
      ))}
      {!open ? (
        <button type="button" className="link-button" onClick={() => setOpen(true)}>
          + Track an action
        </button>
      ) : (
        <form
          className="track-action-form"
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <label>
            Action
            <input value={title} onChange={(e) => setTitle(e.target.value)} required minLength={3} />
          </label>
          <label>
            Owner
            <input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="e.g. Treasury head" />
          </label>
          <label>
            Due
            <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
          </label>
          <div className="preset-row">
            <button type="submit" className="primary" disabled={create.isPending}>
              {create.isPending ? 'Saving…' : 'Track'}
            </button>
            <button type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
          {create.isError && <p className="error-text">{(create.error as Error).message}</p>}
        </form>
      )}
    </div>
  )
}
