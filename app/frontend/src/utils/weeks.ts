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

export function formatWeekLabel(weekId: string): string {
  const [, w] = weekId.split('-W')
  return `Week ${parseInt(w, 10)} · ${weekId}`
}
