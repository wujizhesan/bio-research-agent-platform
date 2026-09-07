import type { Job, UploadedFile } from './types'

const terminalJobStatuses = new Set<Job['status']>(['completed', 'failed', 'cancelled'])

export async function apiFetch<T>(base: string, token: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `请求失败: ${response.status}`)
  }
  return payload as T
}
export async function uploadFile(base: string, token: string, file: File, projectId = ''): Promise<UploadedFile> {
  const body = new FormData()
  body.append('upload', file)
  if (projectId) body.append('project_id', projectId)
  const response = await fetch(`${base}/api/v1/files`, {
    method: 'POST',
    body,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `文件上传失败: ${response.status}`)
  }
  return payload.file as UploadedFile
}

type JobEventPayload = { job?: Job; status?: string; error?: string }

type EventTicketPayload = { ticket: string; expires_in: number }

async function readJobStream(
  base: string,
  token: string,
  jobId: string,
  onEvent: (type: string, payload: JobEventPayload) => void,
  signal?: AbortSignal,
) {
  const ticketPayload = await apiFetch<EventTicketPayload>(base, token, `/api/v1/jobs/${jobId}/events/ticket`, {
    method: 'POST',
    signal,
  })
  await new Promise<void>((resolve, reject) => {
    const source = new EventSource(`${base}/api/v1/jobs/${jobId}/events?ticket=${encodeURIComponent(ticketPayload.ticket)}&interval_seconds=0.15&timeout_seconds=300`)
    let settled = false
    const cleanup = () => {
      source.close()
      signal?.removeEventListener('abort', handleAbort)
    }
    const finish = (callback: () => void) => {
      if (settled) return
      settled = true
      cleanup()
      callback()
    }
    const handleAbort = () => {
      const error = new Error('任务流已取消')
      error.name = 'AbortError'
      finish(() => reject(error))
    }
    const handleJob = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as JobEventPayload
        onEvent('job', payload)
        if (payload.job && terminalJobStatuses.has(payload.job.status)) finish(resolve)
      } catch {
        finish(() => reject(new Error('任务流消息格式无效')))
      }
    }
    const handleError = () => {
      if (settled) return
      const error = new Error('任务 SSE 连接断开')
      finish(() => reject(error))
    }
    signal?.addEventListener('abort', handleAbort, { once: true })
    source.addEventListener('job', handleJob)
    source.onerror = handleError
  })
}

export async function followJob(
  base: string,
  token: string,
  jobId: string,
  onEvent: (type: string, payload: JobEventPayload) => void,
  signal?: AbortSignal,
) {
  let retries = 0
  let lastEvent = ''
  while (true) {
    try {
      await readJobStream(base, token, jobId, (type, payload) => {
        const signature = `${type}:${JSON.stringify(payload)}`
        if (signature === lastEvent) return
        lastEvent = signature
        retries = 0
        onEvent(type, payload)
      }, signal)
      return
    } catch (error) {
      if (signal?.aborted || retries >= 2) throw error
      retries += 1
      await new Promise((resolve) => window.setTimeout(resolve, 500 * retries))
    }
  }
}
