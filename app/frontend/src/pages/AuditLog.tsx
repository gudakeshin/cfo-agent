import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

const ACTIONS = ['', 'change', 'view', 'sign_in', 'sign_in_failed', 'sign_out', 'denied']
const ACTION_LABEL: Record<string, string> = {
  '': 'All actions',
  change: 'Changes',
  view: 'Views',
  sign_in: 'Sign-ins',
  sign_in_failed: 'Failed sign-ins',
  sign_out: 'Sign-outs',
  denied: 'Denied',
}

export function AuditLog() {
  const [action, setAction] = useState('')
  const [user, setUser] = useState('')
  const { data = [], isLoading, error } = useQuery({
    queryKey: ['audit', action, user],
    queryFn: () => api.listAudit({ action: action || undefined, user: user || undefined }),
  })

  return (
    <div>
      <header className="page-header">
        <h1>Audit log</h1>
        <p>Every change, sign-in, denied request and view of a digest or its evidence — newest first.</p>
      </header>
      <div className="toolbar">
        <div className="field">
          <label htmlFor="audit-action">Action</label>
          <select id="audit-action" value={action} onChange={(e) => setAction(e.target.value)}>
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {ACTION_LABEL[a]}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label htmlFor="audit-user">User</label>
          <input id="audit-user" type="search" value={user} onChange={(e) => setUser(e.target.value)} placeholder="Email contains…" />
        </div>
      </div>
      {isLoading && <p className="empty-state">Loading…</p>}
      {error && <p className="error-text">{(error as Error).message}</p>}
      {!isLoading && data.length === 0 && <p className="empty-state">No events match.</p>}
      {data.length > 0 && (
        <div className="table-scroll">
          <table className="audit-table">
            <thead>
              <tr>
                <th>When</th>
                <th>User</th>
                <th>Role</th>
                <th>Action</th>
                <th>Request</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {data.map((e) => (
                <tr key={e.id}>
                  <td>{new Date(e.at + (e.at.endsWith('Z') ? '' : 'Z')).toLocaleString()}</td>
                  <td>{e.user}</td>
                  <td>{e.role}</td>
                  <td>{ACTION_LABEL[e.action] ?? e.action}</td>
                  <td>
                    <code>
                      {e.method} {e.path}
                    </code>
                  </td>
                  <td>{e.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
