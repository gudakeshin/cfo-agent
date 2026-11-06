import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { extra, type ActionItem, type ActionStatus } from '../api/extra'
import { useAuth } from '../hooks/useAuth'

const STATUS_LABEL: Record<ActionStatus, string> = { open: 'Open', in_progress: 'In progress', done: 'Done', dropped: 'Dropped' }

export function Actions() {
  const qc = useQueryClient()
  const { can } = useAuth()
  const [filter, setFilter] = useState<'active' | 'all'>('active')
  const { data = [], isLoading } = useQuery({ queryKey: ['actions', 'all'], queryFn: () => extra.listActions() })
  const rows = filter === 'active' ? data.filter((a) => a.status === 'open' || a.status === 'in_progress') : data
  const update = useMutation({
    mutationFn: ({ id, status }: { id: number; status: ActionStatus }) => extra.updateAction(id, { status }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['actions'] }),
  })
  const [title, setTitle] = useState('')
  const [owner, setOwner] = useState('')
  const [due, setDue] = useState('')
  const create = useMutation({
    mutationFn: () => extra.createAction({ title, owner, due_date: due || null }),
    onSuccess: () => {
      setTitle('')
      setOwner('')
      setDue('')
      qc.invalidateQueries({ queryKey: ['actions'] })
    },
  })
  const overdue = data.filter((a) => a.overdue).length
  return (
    <div>
      <header className="page-header">
        <h1>Actions</h1>
        <p>
          Commitments made from insights, with owners and dates.{' '}
          {overdue > 0 && <strong className="error-text">{overdue} overdue.</strong>} Open actions also appear in
          meeting briefs.
        </p>
      </header>
      {can('interact') && (
        <form
          className="form-section track-action-form inline"
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <label>
            New action
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="What will be done" required minLength={3} />
          </label>
          <label>
            Owner
            <input value={owner} onChange={(e) => setOwner(e.target.value)} />
          </label>
          <label>
            Due
            <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
          </label>
          <button type="submit" className="primary" disabled={create.isPending}>
            Add
          </button>
        </form>
      )}
      <div className="toolbar">
        <button type="button" className={`chip-toggle${filter === 'active' ? ' active' : ''}`} onClick={() => setFilter('active')}>
          Open
        </button>
        <button type="button" className={`chip-toggle${filter === 'all' ? ' active' : ''}`} onClick={() => setFilter('all')}>
          All
        </button>
      </div>
      {isLoading ? (
        <p className="empty-state">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="empty-state">No actions yet. Use “Track an action” on any insight.</p>
      ) : (
        <div className="table-card table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Action</th>
                <th>Owner</th>
                <th>Due</th>
                <th>From insight</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a: ActionItem) => (
                <tr key={a.id} className={a.overdue ? 'overdue-row' : ''}>
                  <td>{a.title}</td>
                  <td>{a.owner || '—'}</td>
                  <td>
                    {a.due_date ?? '—'}
                    {a.overdue && <span className="badge badge-red"> overdue</span>}
                  </td>
                  <td>{a.insight_title ? <span title={a.kra_ids.join(', ')}>{a.insight_title}</span> : '—'}</td>
                  <td>
                    {can('interact') ? (
                      <select
                        aria-label={`Status of ${a.title}`}
                        value={a.status}
                        onChange={(e) => update.mutate({ id: a.id, status: e.target.value as ActionStatus })}
                      >
                        {(Object.keys(STATUS_LABEL) as ActionStatus[]).map((s) => (
                          <option key={s} value={s}>
                            {STATUS_LABEL[s]}
                          </option>
                        ))}
                      </select>
                    ) : (
                      STATUS_LABEL[a.status]
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="toolbar-hint">
        <Link to="/briefs">Prepare a meeting brief →</Link>
      </p>
    </div>
  )
}
