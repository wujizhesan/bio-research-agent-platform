import type { Job, UploadedFile } from './types'
import { recordResponseTrace } from './frontendObservability'
import { browserSessionRequest } from './browserSession'
import { parseJobPayload } from './platformPayloadValidation'

const terminalJobStatuses = new Set<Job['status']>(['completed', 'failed', 'cancelled', 'indeterminate'])
const maxStreamReconnects = 2
const pollIntervalMs = 3000

export type JobConnectionState = 'connected' | 'reconnecting' | 'polling'

export class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

export async function apiFetch<T>(base: string, token: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${base}${path}`, browserSessionRequest(token, {
    ...init,
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers || {}),
    },
  }))
  recordResponseTrace(response)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new ApiRequestError(payload.detail || payload.error || `请求失败: ${response.status}`, response.status)
  }
  return payload as T
}
export async function uploadFile(base: string, token: string, file: File, projectId = ''): Promise<UploadedFile> {
  const body = new FormData()
  body.append('upload', file)
  if (projectId) body.append('project_id', projectId)
  const response = await fetch(`${base}/api/v1/files`, browserSessionRequest(token, {
    method: 'POST',
    body,
  }))
  recordResponseTrace(response)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `文件上传失败: ${response.status}`)
  }
  return payload.file as UploadedFile
}

type JobEventPayload = { job?: Job; status?: string; error?: string }

type EventTicketPayload = { ticket: string; expires_in: number }

function abortError() {
  const error = new Error('任务流已取消')
  error.name = 'AbortError'
  return error
}

function waitForJobUpdate(delayMs: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', handleAbort)
      resolve()
    }, delayMs)
    const handleAbort = () => {
      window.clearTimeout(timer)
      signal?.removeEventListener('abort', handleAbort)
      reject(abortError())
    }
    signal?.addEventListener('abort', handleAbort, { once: true })
  })
}

async function readJobStream(
  base: string,
  token: string,
  jobId: string,
  onEvent: (type: string, payload: JobEventPayload) => void,
  lastEventId: string,
  onCursor: (cursor: string) => void,
  signal?: AbortSignal,
): Promise<'terminal' | 'timeout'> {
  const ticketPayload = await apiFetch<EventTicketPayload>(base, token, `/api/v1/jobs/${jobId}/events/ticket`, {
    method: 'POST',
    signal,
  })
  return new Promise<'terminal' | 'timeout'>((resolve, reject) => {
    const cursorQuery = lastEventId ? `&last_event_id=${encodeURIComponent(lastEventId)}` : ''
    const source = new EventSource(`${base}/api/v1/jobs/${jobId}/events?ticket=${encodeURIComponent(ticketPayload.ticket)}&interval_seconds=0.15&timeout_seconds=300${cursorQuery}`, { withCredentials: true })
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
      finish(() => reject(abortError()))
    }
    const handleJob = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as JobEventPayload
        onEvent('job', payload)
        if (event.lastEventId) onCursor(event.lastEventId)
        if (payload.job && terminalJobStatuses.has(payload.job.status)) finish(() => resolve('terminal'))
      } catch {
        finish(() => reject(new Error('任务流消息格式无效')))
      }
    }
    const handleError = () => {
      if (settled) return
      const error = new Error('任务 SSE 连接断开')
      finish(() => reject(error))
    }
    if (signal?.aborted) {
      handleAbort()
      return
    }
    signal?.addEventListener('abort', handleAbort, { once: true })
    source.addEventListener('job', handleJob)
    source.addEventListener('timeout', () => finish(() => resolve('timeout')))
    source.addEventListener('access_revoked', () => finish(() => reject(new Error('任务访问权限已撤销'))))
    source.onerror = handleError
  })
}

async function pollJob(
  base: string,
  token: string,
  jobId: string,
  onEvent: (type: string, payload: JobEventPayload) => void,
  signal?: AbortSignal,
) {
  let failures = 0
  while (true) {
    let response: unknown
    try {
      response = await apiFetch<unknown>(base, token, `/api/v1/jobs/${jobId}`, { signal })
    } catch (error) {
      if (signal?.aborted) throw abortError()
      if (error instanceof ApiRequestError && [401, 403, 404].includes(error.status)) throw error
      failures += 1
      await waitForJobUpdate(Math.min(1000 * 2 ** Math.min(failures - 1, 4), 10000), signal)
      continue
    }
    const parsed = parseJobPayload(response)
    if (!parsed.value || parsed.value.job_id !== jobId) throw new Error('任务状态响应格式异常')
    failures = 0
    onEvent('job', { job: parsed.value })
    if (terminalJobStatuses.has(parsed.value.status)) return
    await waitForJobUpdate(pollIntervalMs, signal)
  }
}

export async function followJob(
  base: string,
  token: string,
  jobId: string,
  onEvent: (type: string, payload: JobEventPayload) => void,
  signal?: AbortSignal,
  onConnectionState?: (state: JobConnectionState) => void,
) {
  let retries = 0
  let lastEvent = ''
  let lastEventId = ''
  let connectionState: JobConnectionState = 'connected'
  const setConnectionState = (state: JobConnectionState) => {
    if (connectionState === state) return
    connectionState = state
    onConnectionState?.(state)
  }
  const emitEvent = (type: string, payload: JobEventPayload) => {
    const signature = `${type}:${JSON.stringify(payload)}`
    setConnectionState(connectionState === 'reconnecting' ? 'connected' : connectionState)
    if (signature === lastEvent) return
    retries = 0
    lastEvent = signature
    onEvent(type, payload)
  }
  while (true) {
    try {
      const outcome = await readJobStream(base, token, jobId, emitEvent, lastEventId, (cursor) => { lastEventId = cursor }, signal)
      if (outcome === 'terminal') return
    } catch (error) {
      if (signal?.aborted) throw abortError()
      if (error instanceof Error && ['任务访问权限已撤销', '任务流消息格式无效'].includes(error.message)) throw error
      if (retries >= maxStreamReconnects) {
        setConnectionState('polling')
        await pollJob(base, token, jobId, emitEvent, signal)
        return
      }
      retries += 1
      setConnectionState('reconnecting')
      await waitForJobUpdate(Math.min(500 * 2 ** (retries - 1), 4000), signal)
    }
  }
}
