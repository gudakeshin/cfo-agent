import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'
import type { FeedbackVerdict } from '../api/types'

interface Props {
  itemId: number
  initialVerdict?: FeedbackVerdict
  initialAlreadyKnew?: boolean
  initialNote?: string
  onChange?: () => void
}

const VERDICTS: { key: FeedbackVerdict; label: string }[] = [
  { key: 'acted_on', label: 'Acted on' },
  { key: 'useful', label: 'Useful' },
  { key: 'set_aside', label: 'Set aside' },
]

export function FeedbackButtons({
  itemId,
  initialVerdict,
  initialAlreadyKnew = false,
  initialNote = '',
  onChange,
}: Props) {
  const [verdict, setVerdict] = useState<FeedbackVerdict | undefined>(initialVerdict)
  const [alreadyKnew, setAlreadyKnew] = useState(initialAlreadyKnew)
  const [note, setNote] = useState(initialNote)
  // The note box only appears once a verdict is chosen, so a typed note is never dropped.
  const [showNote, setShowNote] = useState(Boolean(initialNote))

  const mutation = useMutation({
    mutationFn: (body: { verdict: FeedbackVerdict; already_knew: boolean; note: string }) =>
      api.submitFeedback({ digest_item_id: itemId, ...body }),
    onSuccess: () => onChange?.(),
  })

  const submit = (v: FeedbackVerdict, knew = alreadyKnew, n = note) => {
    setVerdict(v)
    // One tap records the verdict. Only "Set aside" asks what was wrong, and even then it is optional.
    if (v === 'set_aside') setShowNote(true)
    mutation.mutate({ verdict: v, already_knew: knew, note: n })
  }

  const toggleKnew = () => {
    const next = !alreadyKnew
    setAlreadyKnew(next)
    if (verdict) {
      mutation.mutate({ verdict, already_knew: next, note })
    }
  }

  const saveNote = () => {
    if (verdict && note !== initialNote) {
      mutation.mutate({ verdict, already_knew: alreadyKnew, note })
    }
  }

  return (
    <div className="feedback-buttons" role="group" aria-label="Was this useful?">
      <span className="feedback-prompt">Your call:</span>
      {VERDICTS.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          className={verdict === key ? 'selected' : ''}
          aria-pressed={verdict === key}
          onClick={() => submit(key)}
          disabled={mutation.isPending}
        >
          {label}
        </button>
      ))}
      {verdict && (
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={alreadyKnew}
            onChange={toggleKnew}
            disabled={mutation.isPending}
          />
          Already knew
        </label>
      )}
      {verdict && verdict !== 'set_aside' && !showNote && (
        <button type="button" className="link-button" onClick={() => setShowNote(true)}>
          Add a note
        </button>
      )}
      {verdict && showNote && (
        <div className="feedback-note">
          <textarea
            aria-label="Feedback note"
            placeholder={
              verdict === 'set_aside'
                ? 'What was off? Optional (e.g. too much detail, wrong number, not relevant)'
                : 'Optional note'
            }
            onBlur={saveNote}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
          />
        </div>
      )}
      {mutation.isSuccess && !mutation.isPending && (
        <span className="success-text" role="status">
          Saved
        </span>
      )}
      {mutation.isError && (
        <span className="error-text">{(mutation.error as Error).message}</span>
      )}
    </div>
  )
}
