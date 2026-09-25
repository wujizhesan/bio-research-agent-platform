import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, followJob } from '../app/api'
import type { Job } from '../app/types'
import { useJobRunner } from './useJobRunner'

vi.mock('../app/api', () => ({ apiFetch: vi.fn(), followJob: vi.fn() }))

describe('useJobRunner indeterminate resolution', () => {
  const refresh = vi.fn(async () => undefined)
  const upsertJob = vi.fn()
  const setError = vi.fn()
  const showReportPreview = vi.fn()

  beforeEach(() => {
    refresh.mockClear()
    upsertJob.mockClear()
    setError.mockClear()
    vi.mocked(apiFetch).mockReset()
    vi.mocked(followJob).mockReset()
  })

  it('submits the reviewer decision before retry is allowed', async () => {
    const indeterminate: Job = {
      job_id: 'job-1',
      tool: 'research_execute',
      status: 'indeterminate',
      created_at: '2026-09-07T00:00:00Z',
    }
    const resolved: Job = {
      ...indeterminate,
      resolution: {
        decision: 'approve_retry',
        reason: 'external system confirms no result was committed',
        reviewer: 'admin',
        resolved_at: '2026-09-07T00:02:00Z',
      },
    }
    vi.mocked(apiFetch).mockResolvedValue({ job: resolved })
    const { result } = renderHook(() => useJobRunner({
      apiBase: 'https://api.example.test',
      token: 'secret',
      selectedProjectId: '',
      refresh,
      upsertJob,
      setError,
      showReportPreview,
    }))

    act(() => result.current.selectJob(indeterminate))
    await act(() => result.current.resolveIndeterminateJob(
      indeterminate,
      'approve_retry',
      'external system confirms no result was committed',
    ))

    expect(apiFetch).toHaveBeenCalledWith(
      'https://api.example.test',
      'secret',
      '/api/v1/jobs/job-1/resolve',
      expect.objectContaining({ method: 'POST' }),
    )
    const request = vi.mocked(apiFetch).mock.calls[0][3]
    expect(JSON.parse(String(request?.body))).toEqual({
      decision: 'approve_retry',
      reason: 'external system confirms no result was committed',
      evidence: {},
    })
    expect(result.current.selectedJob).toEqual(resolved)
    expect(result.current.events.at(-1)).toMatchObject({
      type: 'resolution',
      status: 'indeterminate',
    })
  })
})
