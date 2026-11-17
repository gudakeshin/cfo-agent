import { Link } from 'react-router-dom'

const TERMS: { term: string; body: React.ReactNode }[] = [
  {
    term: 'Four Faces',
    body: (
      <>
        Deloitte&apos;s model of the CFO role. <strong>Steward</strong> protects assets and compliance;{' '}
        <strong>Operator</strong> runs an efficient finance function; <strong>Strategist</strong> shapes
        direction, capital and deals; <strong>Catalyst</strong> drives change across the business. The weighting
        says how much each face matters this quarter.
      </>
    ),
  },
  {
    term: 'KRA (key result area)',
    body: (
      <>
        A specific outcome you are accountable for, such as working-capital discipline or acquisition readiness.
        Each KRA sits under one face and is tracked through one or more metrics. See{' '}
        <Link to="/kras">KRA framework</Link>.
      </>
    ),
  },
  {
    term: 'Weighting (the lens)',
    body: (
      <>
        This quarter&apos;s priorities: the defining priority, the weight on each face, the order of KRAs, your
        risk appetite and your red lines. Changing it changes what surfaces — the same DSO rise matters more
        before an acquisition than during a quiet quarter. See <Link to="/weighting">Weighting</Link>.
      </>
    ),
  },
  {
    term: 'Priority',
    body: (
      <>
        Each movement is scored as <em>face weight × KRA rank × size of the move</em>. Moves in the harmful
        direction count fully; improvements count half. <strong>High</strong> and <strong>Medium</strong> items
        cleared the bar for your risk appetite; <strong>Watch</strong> items only appear when your appetite is
        low.
      </>
    ),
  },
  {
    term: 'Red line',
    body: (
      <>
        A condition you never want to miss — for example cash below the buffered covenant, or any new GST
        show-cause notice. Red lines always surface, first, whatever the weighting, and also trigger an alert
        between digests.
      </>
    ),
  },
  {
    term: 'Evidence',
    body: (
      <>
        Every insight cites the ERP records or computed metrics it rests on. Click an evidence chip to see the
        record as it stood when the digest was written. An item that cannot be backed by evidence is held back,
        not shown. If a figure in the text could not be matched to its evidence, the card says so.
      </>
    ),
  },
  {
    term: 'Feedback',
    body: (
      <>
        <strong>Acted on</strong>, <strong>Useful</strong> or <strong>Set aside</strong>, plus “Already knew”.
        The pilot&apos;s success test is at least one insight a quarter you acted on and had not already
        spotted. See <Link to="/feedback">Pilot scorecard</Link>.
      </>
    ),
  },
]

export function Guide() {
  return (
    <div className="guide">
      <header className="page-header">
        <h1>How to read your briefing</h1>
        <p>The agent advises; it never acts. Five minutes here makes the Monday read faster.</p>
      </header>
      <dl className="glossary">
        {TERMS.map((t) => (
          <div key={t.term} className="glossary-row">
            <dt>{t.term}</dt>
            <dd>{t.body}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
