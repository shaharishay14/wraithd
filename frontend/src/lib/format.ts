export const formatNumber = (value: number | null | undefined) => (value ?? 0).toLocaleString('en')

export const formatDuration = (ms: number | null | undefined) =>
  ms == null ? '—' : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`

// "4 min ago", "3 h ago", "Oct 2"; future times read "in 2 h"
export const formatWhen = (iso: string | null | undefined, now = Date.now()) => {
  if (!iso) return ''
  const date = new Date(iso)
  const minutes = Math.round((now - date.getTime()) / 60_000)
  const future = minutes < 0
  const abs = Math.abs(minutes)
  const phrase = (text: string) => (future ? `in ${text}` : `${text} ago`)
  if (abs < 1) return future ? 'in a moment' : 'just now'
  if (abs < 60) return phrase(`${abs} min`)
  if (abs < 24 * 60) return phrase(`${Math.round(abs / 60)} h`)
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

// Full local date and time, for titles and tooltips
export const formatExact = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' }) : ''

// An argument value as the terminal's approval card prints it: strings as they are, the rest as JSON
export const argumentText = (value: unknown) => (typeof value === 'string' ? value : JSON.stringify(value, null, 2))
