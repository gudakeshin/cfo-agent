import type { ReactNode } from 'react'
import { citationLabel } from '../utils/format'

type OnCite = ((cid: string) => void) | undefined
type Labels = Map<string, string> | undefined

/** Minimal, safe Markdown subset for assistant replies: paragraphs, bullet and
 * numbered lists, **bold**, *italic* and `code`. Builds React nodes — no HTML injection. */
function inline(text: string, keyBase: string, onCite?: OnCite, labels?: Labels): ReactNode[] {
  const out: ReactNode[] = []
  const re = /(\[(?:erp|metric):[^[\]\s]+\]|\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    const key = `${keyBase}-${i++}`
    if (tok.startsWith('[')) {
      const cid = tok.slice(1, -1)
      out.push(
        onCite ? (
          <button key={key} type="button" className="citation-chip inline" onClick={() => onCite(cid)} title={cid}>
            {citationLabel(cid, labels)}
          </button>
        ) : (
          <span key={key} className="citation-chip inline" title={cid}>
            {citationLabel(cid, labels)}
          </span>
        ),
      )
    } else if (tok.startsWith('**')) out.push(<strong key={key}>{tok.slice(2, -2)}</strong>)
    else if (tok.startsWith('`')) out.push(<code key={key}>{tok.slice(1, -1)}</code>)
    else out.push(<em key={key}>{tok.slice(1, -1)}</em>)
    last = m.index + tok.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

export function RichText({ text, onCite, labels }: { text: string; onCite?: OnCite; labels?: Labels }) {
  const blocks = text.replace(/\r\n/g, '\n').split(/\n{2,}/)
  return (
    <div className="rich-text">
      {blocks.map((block, bi) => {
        const lines = block.split('\n').filter((l) => l.trim() !== '')
        if (lines.length && lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
          return (
            <ul key={bi}>
              {lines.map((l, li) => (
                <li key={li}>{inline(l.replace(/^\s*[-*•]\s+/, ''), `${bi}-${li}`, onCite, labels)}</li>
              ))}
            </ul>
          )
        }
        if (lines.length && lines.every((l) => /^\s*\d+[.)]\s+/.test(l))) {
          return (
            <ol key={bi}>
              {lines.map((l, li) => (
                <li key={li}>{inline(l.replace(/^\s*\d+[.)]\s+/, ''), `${bi}-${li}`, onCite, labels)}</li>
              ))}
            </ol>
          )
        }
        const heading = /^#{1,4}\s+(.*)$/.exec(lines[0] ?? '')
        if (heading && lines.length === 1) return <h4 key={bi}>{inline(heading[1], `${bi}`, onCite, labels)}</h4>
        return (
          <p key={bi}>
            {lines.map((l, li) => (
              <span key={li}>
                {li > 0 && <br />}
                {inline(l.replace(/^#{1,4}\s+/, ''), `${bi}-${li}`, onCite, labels)}
              </span>
            ))}
          </p>
        )
      })}
    </div>
  )
}
