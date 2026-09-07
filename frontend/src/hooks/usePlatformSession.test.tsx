import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from '../app/api'
import type { Job } from '../app/types'
import { usePlatformSession } from './usePlatformSession'

vi.mock('../app/api', () => ({ apiFetch: vi.fn() }))

function job(status: Job['status']): Job {
  return {
    job_id: 'job-1',
    tool: 'sequence_workbench',
    status,
    created_at: '2026-09-07T00:00:00Z',
  }
}

describe('usePlatformSession', () => {
  let remoteJobs: Job[]

  beforeEach(() => {
    localStorage.clear()
    remoteJobs = [job('running')]
    vi.mocked(apiFetch).mockReset().mockImplementation(async (_base, _token, path) => {
      if (path === '/api/v1/plugins') return { plugins: [{ domain: 'sequence', name: 'Sequence', status: 'available', tool_count: 2, tools: ['sequence_workbench'] }] }
      if (path.startsWith('/api/v1/jobs')) return { jobs: remoteJobs }
      if (path === '/api/v1/capabilities') return { tool_count: 2, interfaces: {} }
      if (path === '/api/v1/projects') return { projects: [{ project_id: 'project-1', name: 'Demo', owner_subject: 'user', created_at: '2026-09-07T00:00:00Z' }] }
      throw new Error(`unexpected path: ${path}`)
    })
  })

  it('并行刷新平台状态并保留已完成任务的终态', async () => {
    const { result } = renderHook(() => usePlatformSession('https://api.example.test', 'token-1'))

    await waitFor(() => expect(result.current.connected).toBe(true))
    expect(result.current.plugins).toHaveLength(1)
    expect(result.current.capabilities?.tool_count).toBe(2)
    expect(result.current.selectedProjectId).toBe('project-1')
    expect(result.current.jobs[0].status).toBe('running')

    act(() => result.current.upsertJob(job('completed')))
    remoteJobs = [job('running')]
    await act(() => result.current.refresh())

    expect(result.current.jobs[0].status).toBe('completed')
    expect(apiFetch).toHaveBeenCalledWith('https://api.example.test', 'token-1', '/api/v1/jobs?limit=8')
  })

  it('保存规范化令牌并以新令牌重新刷新', async () => {
    const { result } = renderHook(() => usePlatformSession('https://api.example.test', 'token-1'))
    await waitFor(() => expect(result.current.connected).toBe(true))

    act(() => result.current.setTokenDraft('  token-2  '))
    act(() => result.current.saveToken())

    await waitFor(() => expect(result.current.token).toBe('token-2'))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('https://api.example.test', 'token-2', '/api/v1/plugins'))
    expect(localStorage.getItem('bio-agent-token')).toBe('token-2')
  })

  it('隔离畸形 API 数据并保留可用记录', async () => {
    vi.mocked(apiFetch).mockImplementation(async (_base, _token, path) => {
      if (path === '/api/v1/plugins') return { plugins: { domain: 'sequence' } }
      if (path.startsWith('/api/v1/jobs')) return { jobs: [job('running'), { job_id: 42 }] }
      if (path === '/api/v1/capabilities') return { tool_count: '2', interfaces: {} }
      if (path === '/api/v1/projects') return { projects: [
        { project_id: 'project-1', name: 'Demo', owner_subject: 'user', created_at: '2026-09-07T00:00:00Z' },
        { project_id: null },
      ] }
      throw new Error(`unexpected path: ${path}`)
    })

    const { result } = renderHook(() => usePlatformSession('https://api.example.test', 'token-1'))
    await waitFor(() => expect(result.current.connected).toBe(true))

    expect(result.current.plugins).toEqual([])
    expect(result.current.jobs).toEqual([job('running')])
    expect(result.current.capabilities).toBeNull()
    expect(result.current.projects).toHaveLength(1)
    expect(result.current.selectedProjectId).toBe('project-1')
    expect(result.current.error).toContain('API 响应已安全降级')
    expect(result.current.error).toContain('插件目录不是数组')
    expect(result.current.error).toContain('任务列表含 1 条无效记录')
    expect(result.current.error).toContain('能力目录格式无效')
    expect(result.current.error).toContain('项目列表含 1 条无效记录')
  })
})
