import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

interface Props {
  citationId: string
  onClose: () => void
}

export function CitationPopover({ citationId, onClose }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['citation', citationId],
    queryFn: () => api.getCitation(citationId),
  })

  return (
    <div className="popover-backdrop" onClick={onClose} role="presentation">
      <div className="popover-panel" onClick={(e) => e.stopPropagation()} role="dialog">
        <header>
          <div>
            <h3>Citation</h3>
            <code style={{ fontSize: '0.8rem' }}>{citationId}</code>
          </div>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </header>
        {isLoading && <p>Loading…</p>}
        {error && <p className="error-text">{(error as Error).message}</p>}
        {data && (
          <>
            <p>{data.summary}</p>
            {data.record && (
              <pre>{JSON.stringify(data.record, null, 2)}</pre>
            )}
          </>
        )}
      </div>
    </div>
  )
}
