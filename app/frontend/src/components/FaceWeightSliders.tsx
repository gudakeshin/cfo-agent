import type { FaceKey } from '../api/types'

const FACE_LABELS: Record<FaceKey, string> = {
  steward: 'Steward',
  operator: 'Operator',
  strategist: 'Strategist',
  catalyst: 'Catalyst',
}

const FACES: FaceKey[] = ['steward', 'operator', 'strategist', 'catalyst']

function normalize(weights: Record<FaceKey, number>, changed: FaceKey, value: number): Record<FaceKey, number> {
  const next = { ...weights, [changed]: value }
  const others = FACES.filter((f) => f !== changed)
  const remaining = Math.max(0, 1 - value)
  const othersSum = others.reduce((s, f) => s + weights[f], 0)

  if (othersSum <= 0) {
    const share = remaining / others.length
    others.forEach((f) => {
      next[f] = share
    })
  } else {
    others.forEach((f) => {
      next[f] = (weights[f] / othersSum) * remaining
    })
  }

  return next
}

interface Props {
  weights: Record<FaceKey, number>
  onChange: (weights: Record<FaceKey, number>) => void
}

export function FaceWeightSliders({ weights, onChange }: Props) {
  const handleChange = (face: FaceKey, raw: number) => {
    const value = Math.max(0, Math.min(1, raw))
    onChange(normalize(weights, face, value))
  }

  const total = FACES.reduce((s, f) => s + weights[f], 0)

  return (
    <div className="face-sliders">
      {FACES.map((face) => (
        <div key={face} className="face-slider-row">
          <span>{FACE_LABELS[face]}</span>
          <input
            id={`face-${face}`}
            type="range"
            min={0}
            max={100}
            value={Math.round(weights[face] * 100)}
            onChange={(e) => handleChange(face, Number(e.target.value) / 100)}
            aria-label={FACE_LABELS[face]}
          />
          <span className="pct">{(weights[face] * 100).toFixed(0)}%</span>
        </div>
      ))}
      <p style={{ fontSize: '0.8rem', color: 'var(--gray-400)', margin: 0 }}>
        Weights auto-normalize to 100% (sum {(total * 100).toFixed(1)}%)
      </p>
    </div>
  )
}
