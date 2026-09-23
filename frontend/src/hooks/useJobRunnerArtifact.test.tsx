import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Job } from '../app/types'
import { useJobRunner } from './useJobRunner'


vi.mock('../app/api', () => ({ apiFetch: vi.fn(), followJob: vi.fn() }))

describe('useJobRunner artifact manifest', () => {
  const refresh = vi.fn(async () => undefined)
  const upsertJob = vi.fn()
  const setError = vi.fn()
  const showReportPreview = vi.fn()

  beforeEach(() => {
    setError.mockClear()
    showReportPreview.mockClear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('downloads a version-locked artifact through its artifact id', async () => {
    const report = new Blob(['<main>report</main>'], { type: 'text/html' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({
        'Content-Disposition': "attachment; filename*=UTF-8''report.html",
      }),
      blob: vi.fn().mockResolvedValue(report),
    }))
    const completed: Job = {
      job_id: 'job-1',
      tool: 'research_execute',
      status: 'completed',
      created_at: '2026-09-21T00:00:00Z',
      result: { report_path: 'bio+s3://research-results/report' },
      artifacts: [{
        artifact_id: 'a'.repeat(32),
        filename: 'report.html',
        content_type: 'text/html',
        size_bytes: 21,
        sha256: 'b'.repeat(64),
        storage_backend: 's3',
        storage_key: 'bio-agent/artifacts/project/job/report.html',
        version_id: 'version-1',
        reference: 'bio+s3://research-results/report',
      }],
    }
    const { result } = renderHook(() => useJobRunner({
      apiBase: 'https://api.example.test',
      token: 'secret',
      selectedProjectId: 'project-1',
      refresh,
      upsertJob,
      setError,
      showReportPreview,
    }))

    act(() => result.current.selectJob(completed))
    await act(() => result.current.previewJobArtifact(
      'job-1',
      'bio+s3://research-results/report',
    ))

    expect(fetch).toHaveBeenCalledWith(
      `https://api.example.test/api/v1/jobs/job-1/artifacts/${'a'.repeat(32)}`,
      expect.any(Object),
    )
    expect(showReportPreview).toHaveBeenCalledWith(report, 'report.html')
  })
})
