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

  const mutation = useMutation({
    mutationFn: (body: { verdict: FeedbackVerdict; already_knew: boolean; note: string }) =>
      api.submitFeedback({ digest_item_id: itemId, ...body }),
    onSuccess: () => onChange?.(),
  })

  const submit = (v: FeedbackVerdict, knew = alreadyKnew, n = note) => {
    setVerdict(v)
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
    if (verdict) {
      mutation.mutate({ verdict, already_knew: alreadyKnew, note })
    }
  }

  return (
    <div className="feedback-buttons">
      {VERDICTS.map(({ key, label }) => (
        <button
          key={key}
          type="button"
          className={verdict === key ? 'selected' : ''}
          onClick={() => submit(key)}
          disabled={mutation.isPending}
        >
          {label}
        </button>
      ))}
      <label className="toggle-row">
        <input
          type="checkbox"
          checked={alreadyKnew}
          onChange={toggleKnew}
          disabled={mutation.isPending}
        />
        Already knew
      </label>
      <div className="feedback-note">
        <textarea
          placeholder="Optional note (e.g. too much detail on GST mechanics)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={2}
        />
        {verdict && (
          <button type="button" onClick={saveNote} disabled={mutation.isPending}>
            Save note
          </button>
        )}
      </div>
      {mutation.isError && (
        <span className="error-text">{(mutation.error as Error).message}</span>
      )}
    </div>
  )
}
