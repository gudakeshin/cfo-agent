import { useQuery } from '@tanstack/react-query'
import { Fragment, useState } from 'react'
import { extra, type HeatCell } from '../api/extra'
import { formatMetric } from '../utils/format'

function tone(score: number | null): string {
  if (score == null) return 'heat-none'
  if (score >= 2.5) return 'heat-4'
  if (score >= 1.8) return 'heat-3'
  if (score >= 1.2) return 'heat-2'
  if (score > 0) return 'heat-1'
  return 'heat-0'
}

/** 13-week stress by Face (expandable to KRAs): darker = more adverse movement. */
export function KraHeatmap() {
  const { data, isLoading, error } = useQuery({ queryKey: ['heatmap'], queryFn: () => extra.heatmap(13), staleTime: 60_000 })
  const [openFace, setOpenFace] = useState<string | null>(null)
  if (isLoading) return <p className="empty-state">Loading trends…</p>
  if (error) return <p className="error-text">{(error as Error).message}</p>
  if (!data || !data.weeks.length) return null
  const label = (c: HeatCell) => {
    if (c.score == null || !c.metric) return 'No data'
    const m = data.metrics[c.metric]
    return `${m?.label ?? c.metric}: ${formatMetric(c.value ?? null, m?.unit ?? '')} · stress ${c.score.toFixed(1)} / 3`
  }
  return (
    <div className="heatmap" role="table" aria-label="KRA stress by week">
      <div className="heatmap-row heatmap-head" role="row">
        <span role="columnheader" />
        {data.weeks.map((w) => (
          <span key={w} role="columnheader" className="heatmap-week">
            {w.split('-')[1]}
          </span>
        ))}
      </div>
      {data.faces.map((f) => (
        <Fragment key={f.id}>
          <div className="heatmap-row" role="row">
            <button type="button" className="heatmap-label link-button" aria-expanded={openFace === f.id}
              onClick={() => setOpenFace(openFace === f.id ? null : f.id)}>
              {openFace === f.id ? '▾' : '▸'} {f.label}
            </button>
            {f.cells.map((c) => (
              <span key={c.week} role="cell" className={`heatmap-cell ${tone(c.score)}`} title={label(c)} />
            ))}
          </div>
          {openFace === f.id &&
            f.kras.map((k) => (
              <div key={k.id} className="heatmap-row heatmap-sub" role="row">
                <span className="heatmap-label" title={k.name}>
                  {k.id} · {k.name}
                  {!k.grounded && <em> (no data feed)</em>}
                </span>
                {k.cells.map((c) => (
                  <span key={c.week} role="cell" className={`heatmap-cell ${tone(c.score)}`} title={label(c)} />
                ))}
              </div>
            ))}
        </Fragment>
      ))}
      <div className="heatmap-legend">
        <span>Calm</span>
        {['heat-0', 'heat-1', 'heat-2', 'heat-3', 'heat-4'].map((t) => (
          <span key={t} className={`heatmap-cell ${t}`} />
        ))}
        <span>Stressed</span>
        <span className="toolbar-hint">Darker = larger adverse move for the KRA&apos;s worst metric, or inside a covenant buffer / off plan.</span>
      </div>
    </div>
  )
}
