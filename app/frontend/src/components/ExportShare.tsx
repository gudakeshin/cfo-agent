import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { extra } from '../api/extra'
import { useAuth } from '../hooks/useAuth'
import { useEscape } from '../hooks/useEscape'

/** Download the digest as PDF / PowerPoint, and manage read-only share links. */
export function ExportShare({ digestId }: { digestId: number }) {
  const { can } = useAuth()
  const [open, setOpen] = useState(false)
  return (
    <>
      <a className="button" href={extra.exportUrl(digestId, 'pdf')} download>
        PDF
      </a>
      <a className="button" href={extra.exportUrl(digestId, 'pptx')} download>
        PowerPoint
      </a>
      {can('interact') && (
        <button type="button" onClick={() => setOpen(true)}>
          Share…
        </button>
      )}
      {open && <ShareDialog digestId={digestId} onClose={() => setOpen(false)} />}
    </>
  )
}

function ShareDialog({ digestId, onClose }: { digestId: number; onClose: () => void }) {
  const qc = useQueryClient()
  const closeRef = useRef<HTMLButtonElement>(null)
  useEscape(onClose)
  const [days, setDays] = useState(7)
  const [note, setNote] = useState('')
  const [copied, setCopied] = useState<number | null>(null)
  const links = useQuery({ queryKey: ['shares', digestId], queryFn: () => extra.listShares(digestId) })
  const create = useMutation({
    mutationFn: () => extra.createShare(digestId, days, note),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['shares', digestId] }),
  })
  const revoke = useMutation({
    mutationFn: (id: number) => extra.revokeShare(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['shares', digestId] }),
  })
  const copy = async (id: number, url: string) => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(id)
    } catch {
      window.prompt('Copy this link', url)
    }
  }
  return (
    <div className="popover-backdrop" onClick={onClose} role="presentation">
      <div className="popover-panel share-dialog" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="share-title">
        <header>
          <h3 id="share-title">Share a read-only copy</h3>
          <button type="button" ref={closeRef} onClick={onClose}>
            Close
          </button>
        </header>
        <p className="verification-note">
          Anyone with the link can read this digest and download its PDF — no account needed. Evidence is the
          snapshot taken when the digest was made. Every view is written to the audit log; revoke the link at any time.
        </p>
        <form
          className="share-form"
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <label>
            For
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. Board pack, 12 Nov" maxLength={256} />
          </label>
          <label>
            Expires after
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[1, 3, 7, 14, 30].map((d) => (
                <option key={d} value={d}>
                  {d} day{d > 1 ? 's' : ''}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" className="primary" disabled={create.isPending}>
            Create link
          </button>
        </form>
        {create.isError && <p className="error-text">{(create.error as Error).message}</p>}
        <ul className="share-list">
          {(links.data ?? []).map((l) => (
            <li key={l.id} className={l.active ? '' : 'inactive'}>
              <div>
                <strong>{l.note || 'Untitled link'}</strong>
                <span className="toolbar-hint">
                  {l.active ? `expires ${new Date(l.expires_at).toLocaleDateString()}` : l.revoked ? 'revoked' : 'expired'} ·{' '}
                  {l.views} view{l.views === 1 ? '' : 's'} · by {l.created_by}
                </span>
              </div>
              {l.active && (
                <div className="preset-row">
                  <button type="button" onClick={() => copy(l.id, l.url)}>
                    {copied === l.id ? 'Copied' : 'Copy link'}
                  </button>
                  <button type="button" className="ghost-danger" onClick={() => revoke.mutate(l.id)}>
                    Revoke
                  </button>
                </div>
              )}
            </li>
          ))}
          {links.data?.length === 0 && <li className="empty-state">No links yet.</li>}
        </ul>
      </div>
    </div>
  )
}
