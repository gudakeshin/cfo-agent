import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { extra } from '../api/extra'

/** Start the KRA framework from an industry template (loads into the editor; nothing is saved). */
export function TemplatePicker({ onLoad }: { onLoad: (framework: Record<string, unknown>, name: string) => void }) {
  const { data } = useQuery({ queryKey: ['kra-templates'], queryFn: extra.templates })
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  if (!data) return null
  const load = async (id: string, name: string) => {
    if (!window.confirm(`Replace the draft in the editor with the “${name}” template? Nothing is saved until you click Save.`)) return
    setBusy(id)
    setErr(null)
    try {
      const t = await extra.template(id)
      onLoad(t.framework, name)
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(null)
    }
  }
  return (
    <div className="form-section">
      <h3 style={{ marginTop: 0 }}>Start from an industry template</h3>
      <div className="template-grid">
        {data.map((t) => (
          <div key={t.id} className="template-card">
            <strong>{t.name}</strong>
            <p>{t.description}</p>
            <p className="toolbar-hint">
              {t.grounded_kras} of {t.kras} KRAs have data · needs: {t.data_needed.join(', ')}
            </p>
            <button type="button" disabled={busy !== null} onClick={() => load(t.id, t.name)}>
              {busy === t.id ? 'Loading…' : 'Load into editor'}
            </button>
          </div>
        ))}
      </div>
      {err && <p className="error-text">{err}</p>}
    </div>
  )
}
