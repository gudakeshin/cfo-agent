import type { ChatMessage } from '../api/types'

/** What the citation gate found wrong with an answer, in plain words. */
export function ChatVerificationNote({ m }: { m: ChatMessage }) {
  const v = m.verification
  if (!v) return null
  const notes: string[] = []
  if (v.unverified_figures?.length)
    notes.push(`Check before relying on: ${v.unverified_figures.join(', ')} (not matched to a source).`)
  if (v.rejected_citations?.length)
    notes.push(`${v.rejected_citations.length} unsupported source reference(s) removed.`)
  if (v.uncited && !notes.length) notes.push('No sources cited in this answer.')
  if (!notes.length) return null
  return <p className="verification-note chat-verification">{notes.join(' ')}</p>
}
