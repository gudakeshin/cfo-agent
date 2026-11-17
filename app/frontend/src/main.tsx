import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import App from './App'
import { AuthGate, RequirePermission } from './components/AuthGate'
import { AuthProvider } from './hooks/useAuth'
import { AuditLog } from './pages/AuditLog'
import { Connections } from './pages/Connections'
import { DataHealth } from './pages/DataHealth'
import { DigestDetail } from './pages/DigestDetail'
import { Ask } from './pages/Ask'
import { Briefing } from './pages/Briefing'
import { DigestArchive } from './pages/DigestArchive'
import { DigestRuns } from './pages/DigestRuns'
import { Guide } from './pages/Guide'
import { FeedbackLog } from './pages/FeedbackLog'
import { KraEditor } from './pages/KraEditor'
import { MemoryView } from './pages/MemoryView'
import { RegulatoryFeed } from './pages/RegulatoryFeed'
import { WeightingEditor } from './pages/WeightingEditor'
import { Actions } from './pages/Actions'
import { Briefs } from './pages/Briefs'
import { Metrics } from './pages/Metrics'
import { SharedDigest } from './pages/SharedDigest'
import './styles/global.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: 1,
    },
  },
})

// Share links are public: render them without the sign-in gate or the app shell.
const sharedMatch = window.location.pathname.match(/^\/shared\/([^/]+)\/?$/)

createRoot(document.getElementById('root')!).render(
  sharedMatch ? (
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <SharedDigest token={decodeURIComponent(sharedMatch[1])} />
      </QueryClientProvider>
    </StrictMode>
  ) : (
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
      <AuthGate>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<App />}>
            <Route index element={<Briefing />} />
            <Route path="digests" element={<DigestArchive />} />
            <Route path="runs" element={<RequirePermission permission="configure"><DigestRuns /></RequirePermission>} />
            <Route path="ask" element={<RequirePermission permission="interact"><Ask /></RequirePermission>} />
            <Route path="guide" element={<Guide />} />
            <Route path="audit" element={<RequirePermission permission="administer"><AuditLog /></RequirePermission>} />
            <Route path="digests/:id" element={<DigestDetail />} />
            <Route path="weighting" element={<RequirePermission permission="configure"><WeightingEditor /></RequirePermission>} />
            <Route path="kras" element={<RequirePermission permission="configure"><KraEditor /></RequirePermission>} />
            <Route path="feedback" element={<FeedbackLog />} />
            <Route path="connections" element={<RequirePermission permission="administer"><Connections /></RequirePermission>} />
            <Route path="news" element={<RegulatoryFeed />} />
            <Route path="data-health" element={<RequirePermission permission="configure"><DataHealth /></RequirePermission>} />
            <Route path="memory" element={<RequirePermission permission="configure"><MemoryView /></RequirePermission>} />
            <Route path="actions" element={<Actions />} />
            <Route path="briefs" element={<Briefs />} />
            <Route path="metrics" element={<RequirePermission permission="configure"><Metrics /></RequirePermission>} />
          </Route>
        </Routes>
      </BrowserRouter>
      </AuthGate>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>
  ),
)
