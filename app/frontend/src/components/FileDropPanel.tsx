import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ImportedFileRow } from '../api/types'

const TABLES = [
  'customers',
  'sales_invoices',
  'receipts',
  'purchase_invoices',
  'payments',
  'expenses',
  'cash_daily',
  'revenue_daily',
  'gst_events',
  'covenants',
]

const STATUS_LABEL: Record<ImportedFileRow['status'], string> = {
  loaded: 'Loaded',
  rejected: 'Rejected',
  duplicate: 'Already imported',
}

/** How the file drop works, CSV templates, and the per-file import history. */
export function FileDropPanel({ connectorId, folder }: { connectorId: string; folder: string }) {
  const { data: rows = [] } = useQuery({
    queryKey: ['connector-imports', connectorId],
    queryFn: () => api.listConnectorImports(connectorId),
    refetchInterval: 10_000,
  })

  return (
    <div className="form-section">
      <h3>How the file drop works</h3>
      <p className="verification-note">
        Export from your ERP (SAP, Oracle, Tally…) as CSV into the inbox folder <code>{folder || 'default'}</code>{' '}
        on the server. Each file is checked in full and loaded all-or-nothing; corrections to old records
        update them, and the same file is never loaded twice. Amounts are in rupees; dates as YYYY-MM-DD or
        DD-MM-YYYY. Use <strong>Test connection</strong> to check waiting files without loading them.
      </p>
      <p className="verification-note">
        Templates:{' '}
        {TABLES.map((t, i) => (
          <span key={t}>
            {i > 0 && ' · '}
            <a href={`/api/connectors/templates/${t}.csv`} download>
              {t}
            </a>
          </span>
        ))}
      </p>

      <h3 style={{ marginTop: 16 }}>Recent files</h3>
      {rows.length === 0 ? (
        <p className="verification-note">No files imported yet.</p>
      ) : (
        <div className="table-scroll">
          <table className="audit-table">
            <thead>
              <tr>
                <th>File</th>
                <th>Status</th>
                <th>New</th>
                <th>Updated</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>
                    <code>{r.file}</code>
                    {r.error_count > 0 && (
                      <details>
                        <summary>
                          {r.error_count} problem{r.error_count === 1 ? '' : 's'}
                        </summary>
                        <ul className="import-errors">
                          {r.errors.map((e, i) => (
                            <li key={i}>
                              Line {e.line}
                              {e.column ? `, ${e.column}` : ''}: {e.error}
                            </li>
                          ))}
                          {r.error_count > r.errors.length && <li>… see the .errors.csv file next to it</li>}
                        </ul>
                      </details>
                    )}
                  </td>
                  <td>
                    <span className={`run-status ${r.status === 'loaded' ? 'completed' : r.status === 'rejected' ? 'failed' : ''}`}>
                      {STATUS_LABEL[r.status] ?? r.status}
                    </span>
                  </td>
                  <td>{r.inserted}</td>
                  <td>{r.updated}</td>
                  <td>{new Date(r.imported_at + (r.imported_at.endsWith('Z') ? '' : 'Z')).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
