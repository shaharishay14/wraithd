import type { NoteListItem } from '../../api/types'

export const SOURCE_LABEL: Record<NoteListItem['source'], string> = { user: 'You', distill: 'Learned from a chat', agent: 'Saved by the agent' }
