import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, followJob } from '../app/api'
import type { Job } from '../app/types'
import { useJobRunner } from './useJobRunner'

vi.mock('../app/api', () => ({ apiFetch: vi.fn(), followJob: vi.fn() }))

function job(status: Job['status'], jobId = 'job-1'): Job {
  return {
    job_id: jobId,
    tool: 'sequence_workbench',
    status,
    created_at: '2026-09-07T00:00:00Z',
    finished_at: status === 'completed' ? '2026-09-07T00:01:00Z' : undefined,
  }
}

describe('useJobRunner', () => {
  const refresh = vi.fn(async () => undefined)
  const upsertJob = vi.fn()
  const setError = vi.fn()
  const showReportPreview = vi.fn()

  beforeEach(() => {
    refresh.mockClear()
    upsertJob.mockClear()
    setError.mockClear()
    showReportPreview.mockClear()
    vi.mocked(apiFetch).mockReset()
    vi.mocked(followJob).mockReset()
  })

  it('提交任务后接收 SSE 终态并刷新任务列表', async () => {
    const queued = job('queued')
    const completed = job('completed')
    vi.mocked(apiFetch).mockResolvedValue({ job: queued })
    vi.mocked(followJob).mockImplementation(async (_base, _token, _jobId, onEvent) => {
      onEvent('job', { job: completed })
    })
    const { result } = renderHook(() => useJobRunner({
      apiBase: 'https://api.example.test',
      token: 'secret',
      selectedProjectId: 'project-1',
      refresh,
      upsertJob,
      setError,
      showReportPreview,
    }))
    const onCompleted = vi.fn()

    await act(() => result.current.submitToolJob('sequence_workbench', { protein: 'MKT' }, '任务已接收', onCompleted))

    expect(apiFetch).toHaveBeenCalledWith('https://api.example.test', 'secret', '/api/v1/jobs', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'Idempotency-Key': expect.any(String) }),
    }))
    const request = vi.mocked(apiFetch).mock.calls[0][3]
    expect(JSON.parse(String(request?.body))).toEqual({
      tool: 'sequence_workbench',
      arguments: { protein: 'MKT' },
      project_id: 'project-1',
    })
    expect(result.current.selectedJob).toEqual(completed)
    expect(result.current.events.map((event) => event.type)).toEqual(['accepted', 'job'])
    expect(result.current.loading).toBe(false)
    expect(upsertJob).toHaveBeenNthCalledWith(1, queued)
    expect(upsertJob).toHaveBeenNthCalledWith(2, completed)
    expect(onCompleted).toHaveBeenCalledWith(completed)
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('取消运行任务后同步任务和事件状态', async () => {
    const running = job('running')
    const cancelled = job('cancelled')
    vi.mocked(apiFetch).mockResolvedValue({ job: cancelled })
    const { result } = renderHook(() => useJobRunner({
      apiBase: 'https://api.example.test',
      token: 'secret',
      selectedProjectId: '',
      refresh,
      upsertJob,
      setError,
      showReportPreview,
    }))

    act(() => result.current.selectJob(running))
    await act(() => result.current.cancelSelectedJob())

    expect(apiFetch).toHaveBeenCalledWith('https://api.example.test', 'secret', '/api/v1/jobs/job-1/cancel', { method: 'POST' })
    expect(result.current.selectedJob).toEqual(cancelled)
    expect(result.current.events.at(-1)).toMatchObject({ type: 'cancel', status: 'cancelled', detail: '任务已取消' })
    expect(upsertJob).toHaveBeenCalledWith(cancelled)
  })

  it('拒绝写入畸形任务响应', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ job: { job_id: 42, status: 'queued' } })
    const { result } = renderHook(() => useJobRunner({
      apiBase: 'https://api.example.test',
      token: 'secret',
      selectedProjectId: '',
      refresh,
      upsertJob,
      setError,
      showReportPreview,
    }))

    await act(() => result.current.submitToolJob('sequence_workbench', { protein: 'MKT' }, '任务已接收'))

    expect(result.current.selectedJob).toBeNull()
    expect(upsertJob).not.toHaveBeenCalled()
    expect(setError).toHaveBeenCalledWith('任务响应格式无效')
    expect(refresh).toHaveBeenCalledOnce()
  })
})
