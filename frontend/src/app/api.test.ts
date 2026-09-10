import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, followJob, uploadFile } from './api'
import type { Job, UploadedFile } from './types'

type EventListener = (event: MessageEvent<string>) => void

class MockEventSource {
  static instances: MockEventSource[] = []

  readonly url: string
  onerror: ((event: Event) => void) | null = null
  closed = false
  private listeners = new Map<string, EventListener[]>()

  constructor(url: string | URL) {
    this.url = String(url)
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? []
    listeners.push(listener)
    this.listeners.set(type, listeners)
  }

  close() {
    this.closed = true
  }

  emit(type: string, payload: unknown) {
    const event = { data: typeof payload === 'string' ? payload : JSON.stringify(payload) } as MessageEvent<string>
    this.listeners.get(type)?.forEach((listener) => listener(event))
  }

  fail() {
    this.onerror?.(new Event('error'))
  }
}

function jsonResponse(payload: unknown, options: { ok?: boolean; status?: number } = {}) {
  return {
    ok: options.ok ?? true,
    status: options.status ?? 200,
    json: vi.fn().mockResolvedValue(payload),
  } as unknown as Response
}

function completedJob(overrides: Partial<Job> = {}): Job {
  return {
    job_id: 'job-1',
    tool: 'protein_purification',
    status: 'completed',
    created_at: '2026-09-07T12:00:00Z',
    ...overrides,
  }
}

async function waitForEventSources(count: number) {
  await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(count))
}

describe('apiFetch', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.stubGlobal('EventSource', MockEventSource)
    MockEventSource.instances = []
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('组合基础地址、鉴权和 JSON 请求头，并保留调用方配置', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue(jsonResponse({ job_id: 'job-1' }))
    const controller = new AbortController()

    const payload = await apiFetch<{ job_id: string }>('https://api.example.test', 'secret', '/api/v1/jobs', {
      method: 'POST',
      body: JSON.stringify({ tool: 'demo' }),
      headers: { 'X-Trace-Id': 'trace-1' },
      signal: controller.signal,
    })

    expect(payload).toEqual({ job_id: 'job-1' })
    expect(fetchMock).toHaveBeenCalledWith('https://api.example.test/api/v1/jobs', {
      method: 'POST',
      body: JSON.stringify({ tool: 'demo' }),
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer secret',
        'X-Trace-Id': 'trace-1',
      },
      signal: controller.signal,
    })
  })

  it('允许调用方覆盖默认请求头且空令牌不发送 Authorization', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))

    await apiFetch('https://api.example.test', '', '/api/v1/import', {
      method: 'POST',
      body: 'plain text',
      headers: { 'Content-Type': 'text/plain' },
    })

    expect(fetchMock).toHaveBeenCalledWith('https://api.example.test/api/v1/import', expect.objectContaining({
      headers: { 'Content-Type': 'text/plain' },
    }))
  })

  it.each([
    [{ detail: '令牌无效' }, '令牌无效'],
    [{ error: '服务不可用' }, '服务不可用'],
    [{}, '请求失败: 503'],
  ])('将失败响应转换为稳定错误信息', async (payload, expectedMessage) => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(payload, { ok: false, status: 503 }))

    await expect(apiFetch('https://api.example.test', 'secret', '/api/v1/jobs')).rejects.toThrow(expectedMessage)
  })

  it('响应体不是 JSON 时仍按状态码报告失败', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 502,
      json: vi.fn().mockRejectedValue(new SyntaxError('invalid json')),
    } as unknown as Response)

    await expect(apiFetch('https://api.example.test', '', '/health')).rejects.toThrow('请求失败: 502')
  })
})

describe('uploadFile', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('以 multipart 表单上传文件和项目编号，不手工设置 Content-Type', async () => {
    const uploaded: UploadedFile = {
      file_id: 'file-1',
      filename: 'sample.csv',
      content_type: 'text/csv',
      size_bytes: 8,
      sha256: 'abc123',
      path: '/uploads/sample.csv',
      download_url: '/api/v1/files/file-1',
    }
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue(jsonResponse({ file: uploaded }))
    const file = new File(['a,b\n1,2'], 'sample.csv', { type: 'text/csv' })

    await expect(uploadFile('https://api.example.test', 'secret', file, 'project-1')).resolves.toEqual(uploaded)

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('https://api.example.test/api/v1/files')
    expect(init).toMatchObject({
      method: 'POST',
      headers: { Authorization: 'Bearer secret' },
    })
    expect(init?.body).toBeInstanceOf(FormData)
    const body = init?.body as FormData
    expect(body.get('upload')).toBe(file)
    expect(body.get('project_id')).toBe('project-1')
    expect(Object.keys(init?.headers as Record<string, string>)).not.toContain('Content-Type')
  })

  it('上传失败时优先返回服务端错误', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ detail: '文件类型不允许' }, { ok: false, status: 415 }))

    await expect(uploadFile('https://api.example.test', '', new File(['x'], 'sample.exe'))).rejects.toThrow('文件类型不允许')
  })
})

describe('followJob', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ ticket: 'ticket +/=?', expires_in: 60 })))
    vi.stubGlobal('EventSource', MockEventSource)
    MockEventSource.instances = []
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('使用短期票据建立 SSE，收到终态任务后关闭连接', async () => {
    const onEvent = vi.fn()
    const operation = followJob('https://api.example.test', 'secret', 'job/1', onEvent)
    await waitForEventSources(1)

    expect(fetch).toHaveBeenCalledWith('https://api.example.test/api/v1/jobs/job/1/events/ticket', expect.objectContaining({
      method: 'POST',
      headers: { Authorization: 'Bearer secret' },
    }))
    expect(MockEventSource.instances[0].url).toBe(
      'https://api.example.test/api/v1/jobs/job/1/events?ticket=ticket%20%2B%2F%3D%3F&interval_seconds=0.15&timeout_seconds=300',
    )

    const job = completedJob()
    MockEventSource.instances[0].emit('job', { job })

    await expect(operation).resolves.toBeUndefined()
    expect(onEvent).toHaveBeenCalledOnce()
    expect(onEvent).toHaveBeenCalledWith('job', { job })
    expect(MockEventSource.instances[0].closed).toBe(true)
  })

  it('连接断开后按退避策略重连，并过滤跨连接重复事件', async () => {
    vi.useFakeTimers()
    const onEvent = vi.fn()
    const running = completedJob({ status: 'running' })
    const operation = followJob('https://api.example.test', 'secret', 'job-1', onEvent)
    await vi.advanceTimersByTimeAsync(0)
    expect(MockEventSource.instances).toHaveLength(1)

    MockEventSource.instances[0].emit('job', { job: running })
    MockEventSource.instances[0].fail()
    await vi.advanceTimersByTimeAsync(500)
    expect(MockEventSource.instances).toHaveLength(2)

    MockEventSource.instances[1].emit('job', { job: running })
    MockEventSource.instances[1].emit('job', { job: completedJob() })

    await expect(operation).resolves.toBeUndefined()
    expect(onEvent).toHaveBeenCalledTimes(2)
    expect(onEvent.mock.calls.map((call) => call[1].job.status)).toEqual(['running', 'completed'])
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('连续三次连接失败后停止重试并关闭全部连接', async () => {
    vi.useFakeTimers()
    const operation = followJob('https://api.example.test', 'secret', 'job-1', vi.fn())
    await vi.advanceTimersByTimeAsync(0)

    MockEventSource.instances[0].fail()
    await vi.advanceTimersByTimeAsync(500)
    MockEventSource.instances[1].fail()
    await vi.advanceTimersByTimeAsync(1000)
    MockEventSource.instances[2].fail()

    await expect(operation).rejects.toThrow('任务 SSE 连接断开')
    expect(MockEventSource.instances).toHaveLength(3)
    expect(MockEventSource.instances.every((source) => source.closed)).toBe(true)
  })

  it('拒绝格式错误的 SSE 消息', async () => {
    vi.useFakeTimers()
    const operation = followJob('https://api.example.test', 'secret', 'job-1', vi.fn())
    await vi.advanceTimersByTimeAsync(0)

    MockEventSource.instances[0].emit('job', '{bad json')
    await vi.advanceTimersByTimeAsync(500)
    MockEventSource.instances[1].emit('job', '{bad json')
    await vi.advanceTimersByTimeAsync(1000)
    MockEventSource.instances[2].emit('job', '{bad json')

    await expect(operation).rejects.toThrow('任务流消息格式无效')
  })

  it('中止信号立即关闭 SSE 且不再重连', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const operation = followJob('https://api.example.test', 'secret', 'job-1', vi.fn(), controller.signal)
    await vi.advanceTimersByTimeAsync(0)

    controller.abort()

    await expect(operation).rejects.toMatchObject({ name: 'AbortError', message: '任务流已取消' })
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].closed).toBe(true)
  })
})
