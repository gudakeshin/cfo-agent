import type { MemoryBlock } from '../api/types'

interface Props {
  blocks: MemoryBlock[]
}

/** Static layout matching the Deloitte design canvas memory graph. */
export function MemoryGraph({ blocks }: Props) {
  const editable = blocks.filter((b) => b.agent_editable).slice(0, 3)
  const nodes = [
    {
      id: 'core',
      x: 24,
      y: 90,
      w: 140,
      h: 80,
      fill: 'var(--ink-900)',
      stroke: 'var(--ink-900)',
      label: 'Agent memory (core context)',
      sub: '',
      labelColor: '#fff',
    },
    ...editable.map((b, i) => ({
      id: b.name,
      x: 220,
      y: 28 + i * 72,
      w: 200,
      h: 56,
      fill: 'var(--white)',
      stroke: 'var(--green-500)',
      label: b.name,
      sub: `${Math.max(1, Math.round(b.content.length / 400))} revisions`,
      labelColor: 'var(--ink-900)',
    })),
    {
      id: 'archival',
      x: 500,
      y: 90,
      w: 196,
      h: 80,
      fill: 'var(--white)',
      stroke: 'var(--gray-300)',
      label: 'Archival search (sqlite-vec recall)',
      sub: '',
      labelColor: 'var(--ink-900)',
    },
  ]

  const midY = (n: (typeof nodes)[0]) => n.y + n.h / 2
  const edges = editable.flatMap((b) => {
    const n = nodes.find((x) => x.id === b.name)!
    const core = nodes[0]
    const arch = nodes[nodes.length - 1]
    return [
      { x1: core.x + core.w, y1: midY(core), x2: n.x, y2: midY(n) },
      { x1: n.x + n.w, y1: midY(n), x2: arch.x, y2: midY(arch) },
    ]
  })

  return (
    <div className="memory-graph">
      <h3>Memory graph</h3>
      <p>How core blocks, archival recall, and this week&apos;s digest connect.</p>
      <div className="memory-graph-canvas">
        <svg viewBox="0 0 720 260" role="img" aria-label="Memory graph connections">
          {edges.map((e, i) => (
            <line
              key={i}
              x1={e.x1}
              y1={e.y1}
              x2={e.x2}
              y2={e.y2}
              stroke="var(--gray-300)"
              strokeWidth="1.5"
            />
          ))}
          {nodes.map((n) => (
            <rect
              key={n.id}
              x={n.x}
              y={n.y}
              width={n.w}
              height={n.h}
              rx="6"
              fill={n.fill}
              stroke={n.stroke}
              strokeWidth="1.5"
            />
          ))}
        </svg>
        {nodes.map((n) => (
          <div
            key={`label-${n.id}`}
            className="memory-node-label"
            style={{
              left: `${(n.x / 720) * 100}%`,
              top: `${(n.y / 260) * 100}%`,
              width: `${(n.w / 720) * 100}%`,
              height: `${(n.h / 260) * 100}%`,
              color: n.labelColor,
            }}
          >
            <div className="node-title">{n.label}</div>
            {n.sub && <div className="node-sub">{n.sub}</div>}
          </div>
        ))}
      </div>
      <div className="memory-legend">
        <span>
          <i style={{ background: 'var(--ink-900)' }} /> Core context
        </span>
        <span>
          <i style={{ background: 'var(--green-500)' }} /> Memory block
        </span>
        <span>
          <i style={{ background: 'var(--gray-300)' }} /> Archival recall
        </span>
      </div>
    </div>
  )
}
