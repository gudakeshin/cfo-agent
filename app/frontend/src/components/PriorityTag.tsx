import { PRIORITY_LABEL, priorityTier } from '../utils/format'

interface Props {
  score: number
  isRedLine: boolean
}

export function PriorityTag({ score, isRedLine }: Props) {
  const tier = priorityTier(score, isRedLine)
  return (
    <span
      className={`priority-tag ${tier}`}
      title={
        isRedLine
          ? 'A red line you set was crossed — always shown, whatever the weighting.'
          : `Priority from this quarter's weighting (score ${score.toFixed(2)}). See Guide → Priority.`
      }
    >
      {PRIORITY_LABEL[tier]}
    </span>
  )
}
