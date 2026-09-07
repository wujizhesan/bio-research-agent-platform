export type FrontendErrorEvent = {
  boundary_name: string
  error_name: string
  message: string
  component_stack: string
  trace_id?: string
  job_id?: string
  plugin_id?: string
  path?: string
  occurred_at: string
}

type BoundaryErrorContext = {
  boundaryName: string
  traceId?: string
  jobId?: string
  pluginId?: string
}

let latestTraceId = ''

function sanitizeIdentifier(value: unknown) {
  if (typeof value !== 'string') return undefined
  const normalized = value.trim()
  return /^[A-Za-z0-9._:-]{1,128}$/.test(normalized) ? normalized : undefined
}

export function sanitizeTelemetryText(value: unknown, limit: number) {
  let text = typeof value === 'string' ? value : String(value ?? '')
  text = text.replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]')
  text = text.replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g, '[REDACTED_JWT]')
  text = text.replace(/\b(token|api[_-]?key|password|passwd|secret)=([^&\s]+)/gi, '$1=[REDACTED]')
  return text.slice(0, limit)
}

export function recordResponseTrace(response: { headers?: Pick<Headers, 'get'> }) {
  if (!response.headers || typeof response.headers.get !== 'function') return
  const traceId = sanitizeIdentifier(response.headers.get('x-trace-id'))
  if (traceId) latestTraceId = traceId
}

export function createFrontendErrorEvent(context: BoundaryErrorContext, error: Error, componentStack?: string): FrontendErrorEvent {
  const traceId = sanitizeIdentifier(context.traceId) || latestTraceId
  const jobId = sanitizeIdentifier(context.jobId)
  const pluginId = sanitizeIdentifier(context.pluginId)
  return {
    boundary_name: sanitizeTelemetryText(context.boundaryName, 128),
    error_name: sanitizeTelemetryText(error.name || 'Error', 128),
    message: sanitizeTelemetryText(error.message, 1024),
    component_stack: sanitizeTelemetryText(componentStack, 4096),
    ...(traceId ? { trace_id: traceId } : {}),
    ...(jobId ? { job_id: jobId } : {}),
    ...(pluginId ? { plugin_id: pluginId } : {}),
    path: sanitizeTelemetryText(window.location.pathname, 512),
    occurred_at: new Date().toISOString(),
  }
}

export async function reportFrontendError(apiBase: string, token: string, event: FrontendErrorEvent) {
  try {
    const response = await fetch(`${apiBase}/api/v1/telemetry/frontend-errors`, {
      method: 'POST',
      keepalive: true,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(event.trace_id ? { 'X-Trace-ID': event.trace_id } : {}),
      },
      body: JSON.stringify(event),
    })
    recordResponseTrace(response)
    return response.ok
  } catch {
    console.warn('前端异常上报失败')
    return false
  }
}
