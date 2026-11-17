/** ISO week helpers for digest week selector. */

export function isoWeekId(d: Date): string {
  const target = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()))
  const day = target.getUTCDay() || 7
  target.setUTCDate(target.getUTCDate() + 4 - day)
  const yearStart = new Date(Date.UTC(target.getUTCFullYear(), 0, 1))
  const week = Math.ceil(((target.getTime() - yearStart.getTime()) / 86400000 + 1) / 7)
  return `${target.getUTCFullYear()}-W${String(week).padStart(2, '0')}`
}

export function lastCompletedSunday(from = new Date()): Date {
  const d = new Date(from)
  const day = d.getDay()
  if (day === 0) {
    d.setDate(d.getDate() - 7)
  } else {
    d.setDate(d.getDate() - day)
  }
  d.setHours(0, 0, 0, 0)
  return d
}

export function recentWeeks(count = 13): string[] {
  const end = lastCompletedSunday()
  const weeks: string[] = []
  const cursor = new Date(end)
  for (let i = 0; i < count; i++) {
    weeks.unshift(isoWeekId(cursor))
    cursor.setDate(cursor.getDate() - 7)
  }
  return weeks
}

/** Monday of an ISO week ("2026-W28" → Mon 6 Jul 2026, UTC). */
export function isoWeekMonday(weekId: string): Date | null {
  const m = /^(\d{4})-W(\d{1,2})$/.exec(weekId)
  if (!m) return null
  const year = Number(m[1])
  const week = Number(m[2])
  const jan4 = new Date(Date.UTC(year, 0, 4))
  const day = jan4.getUTCDay() || 7
  const monday = new Date(jan4)
  monday.setUTCDate(jan4.getUTCDate() - day + 1 + (week - 1) * 7)
  return monday
}

/** "2026-W28" → "Week 28 (6–12 Jul)". */
export function formatWeekLabel(weekId: string): string {
  const [, w] = weekId.split('-W')
  const mon = isoWeekMonday(weekId)
  if (!mon || !w) return weekId
  const sun = new Date(mon)
  sun.setUTCDate(mon.getUTCDate() + 6)
  const fmt = (d: Date, withMonth: boolean) =>
    d.toLocaleDateString('en-IN', { day: 'numeric', ...(withMonth ? { month: 'short' } : {}), timeZone: 'UTC' })
  const sameMonth = mon.getUTCMonth() === sun.getUTCMonth()
  const range = sameMonth ? `${fmt(mon, false)}–${fmt(sun, true)}` : `${fmt(mon, true)} – ${fmt(sun, true)}`
  return `Week ${parseInt(w, 10)} (${range})`
}
