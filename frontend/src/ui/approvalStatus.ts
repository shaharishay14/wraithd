import type { ApprovalStatus } from '../api/types'
import type { Status } from './StatusDot'

export const APPROVAL_STATUS: Record<ApprovalStatus, { label: string; status: Status }> = {
  pending: { label: 'Waiting for your approval', status: 'busy' },
  approved: { label: 'Approved', status: 'live' },
  denied: { label: 'Denied', status: 'off' },
  expired: { label: 'Expired, not run', status: 'idle' },
  preapproved: { label: 'Ran without asking (always allowed)', status: 'live' },
}
