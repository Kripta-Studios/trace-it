import { createElement, type ReactNode } from 'react'

/** Highlight literal matches without recursive rendering or repeatedly copying the remaining text. */
export function highlight(text: string, query: string): ReactNode {
  const needle = query.trim().toLowerCase()
  if (!needle) return text
  const lower = text.toLowerCase()
  const parts: ReactNode[] = []
  let cursor = 0
  let index = lower.indexOf(needle)
  if (index < 0) return text
  while (index >= 0) {
    if (index > cursor) parts.push(text.slice(cursor, index))
    parts.push(createElement('mark', { key: index }, text.slice(index, index + needle.length)))
    cursor = index + needle.length
    index = lower.indexOf(needle, cursor)
  }
  if (cursor < text.length) parts.push(text.slice(cursor))
  return parts
}
