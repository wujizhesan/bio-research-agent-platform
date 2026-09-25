import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Job } from '../app/types'
import { JobControl } from './JobActivity'

const baseJob: Job = {
  job_id: 'job-1',
  tool: 'research_execute',
  status: 'indeterminate',
  created_at: '2026-09-07T00:00:00Z',
}

describe('JobControl indeterminate resolution', () => {
  it('requires a review reason before approving retry', () => {
    const onResolve = vi.fn()
    render(<JobControl
      job={baseJob}
      loading={false}
      onCancel={vi.fn()}
      onRetry={vi.fn()}
      onResolve={onResolve}
    />)

    const approve = screen.getByRole('button', { name: '批准重试' })
    expect(approve).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Resolution reason'), {
      target: { value: 'verified against the external system' },
    })
    fireEvent.click(approve)
    expect(onResolve).toHaveBeenCalledWith(
      baseJob,
      'approve_retry',
      'verified against the external system',
    )
  })

  it('shows the execution action only after retry approval', () => {
    const onRetry = vi.fn()
    const approved: Job = {
      ...baseJob,
      resolution: {
        decision: 'approve_retry',
        reason: 'safe to retry',
        reviewer: 'admin',
        resolved_at: '2026-09-07T00:02:00Z',
      },
    }
    render(<JobControl
      job={approved}
      loading={false}
      onCancel={vi.fn()}
      onRetry={onRetry}
      onResolve={vi.fn()}
    />)
    fireEvent.click(screen.getByRole('button', {
      name: 'Retry approved indeterminate task',
    }))
    expect(onRetry).toHaveBeenCalledWith(approved)
  })
})
