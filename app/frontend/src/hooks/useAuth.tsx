import { createContext, useContext, useEffect, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { AuthState, Permission, Role } from '../api/types'

const RANK: Record<Role, number> = { viewer: 0, cfo: 1, delegate: 2, admin: 3 }
const NEED: Record<Permission, Role> = { read: 'viewer', interact: 'cfo', configure: 'delegate', administer: 'admin' }

interface AuthCtx {
  state: AuthState | undefined
  isLoading: boolean
  can: (p: Permission) => boolean
  refresh: () => void
}

const Ctx = createContext<AuthCtx>({ state: undefined, isLoading: true, can: () => false, refresh: () => {} })

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['auth'], queryFn: api.getMe, staleTime: 60_000, retry: 0 })

  useEffect(() => {
    const onUnauthorized = () => qc.invalidateQueries({ queryKey: ['auth'] })
    window.addEventListener('cfo:unauthorized', onUnauthorized)
    return () => window.removeEventListener('cfo:unauthorized', onUnauthorized)
  }, [qc])

  const can = (p: Permission) => {
    const role = data?.user?.role
    return role ? RANK[role] >= RANK[NEED[p]] : false
  }
  // After sign-in/out: drop every cached response (it belonged to the previous
  // identity) and refetch whatever is on screen, starting with /auth/me.
  const refresh = () => {
    void qc.resetQueries()
  }
  return <Ctx.Provider value={{ state: data, isLoading, can, refresh }}>{children}</Ctx.Provider>
}

export function useAuth() {
  return useContext(Ctx)
}

export const ROLE_LABEL: Record<Role, string> = {
  viewer: 'Viewer',
  cfo: 'CFO',
  delegate: 'Delegate',
  admin: 'Administrator',
}
