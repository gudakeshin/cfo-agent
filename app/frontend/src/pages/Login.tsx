import { useState } from 'react'
import { api } from '../api/client'
import { useAuth } from '../hooks/useAuth'

export function Login() {
  const { state, refresh } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get('signin') === 'failed'
      ? 'Single sign-on did not complete. Please try again.'
      : null,
  )
  const [busy, setBusy] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(email, password)
      window.history.replaceState(null, '', window.location.pathname)
      refresh()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-shell">
      <main className="login-card" aria-labelledby="login-title">
        <div className="brand-row">
          <span className="brand-dot" />
          <span className="brand-name">Deloitte</span>
        </div>
        <h1 id="login-title">CFO Agentic Intelligence</h1>
        <p className="login-sub">Sign in to see your briefing.</p>

        {state?.auth_mode === 'oidc' && (
          <a className="button primary login-sso" href="/api/auth/oidc/login">
            Sign in with your organisation account
          </a>
        )}

        {state?.auth_mode === 'local' && (
          <form onSubmit={submit} className="login-form">
            <label htmlFor="login-email">Work email</label>
            <input
              id="login-email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
            <label htmlFor="login-password">Password</label>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button type="submit" className="primary" disabled={busy}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </form>
        )}
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
      </main>
    </div>
  )
}
