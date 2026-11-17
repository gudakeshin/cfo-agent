import type { ReactNode } from 'react'
import type { Permission } from '../api/types'
import { useAuth } from '../hooks/useAuth'
import { Login } from '../pages/Login'

/** Shows the sign-in page until there is a session (no-op when sign-in is off). */
export function AuthGate({ children }: { children: ReactNode }) {
  const { state, isLoading } = useAuth()
  if (isLoading) return <p className="empty-state" style={{ padding: 24 }}>Loading…</p>
  if (state && !state.user) return <Login />
  return <>{children}</>
}

/** Route guard: explains instead of showing a page of 403 errors. */
export function RequirePermission({ permission, children }: { permission: Permission; children: ReactNode }) {
  const { can } = useAuth()
  if (!can(permission)) {
    return (
      <div>
        <header className="page-header">
          <h1>Not available for your role</h1>
          <p>Ask your administrator if you need access to this page.</p>
        </header>
      </div>
    )
  }
  return <>{children}</>
}
