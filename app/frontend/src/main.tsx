import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import App from './App'
import { Connections } from './pages/Connections'
import { DataHealth } from './pages/DataHealth'
import { DigestDetail } from './pages/DigestDetail'
import { DigestInbox } from './pages/DigestInbox'
import { FeedbackLog } from './pages/FeedbackLog'
import { KraEditor } from './pages/KraEditor'
import { MemoryView } from './pages/MemoryView'
import { RegulatoryFeed } from './pages/RegulatoryFeed'
import { WeightingEditor } from './pages/WeightingEditor'
import './styles/global.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: 1,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<App />}>
            <Route index element={<DigestInbox />} />
            <Route path="digests/:id" element={<DigestDetail />} />
            <Route path="weighting" element={<WeightingEditor />} />
            <Route path="kras" element={<KraEditor />} />
            <Route path="feedback" element={<FeedbackLog />} />
            <Route path="connections" element={<Connections />} />
            <Route path="news" element={<RegulatoryFeed />} />
            <Route path="data-health" element={<DataHealth />} />
            <Route path="memory" element={<MemoryView />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
