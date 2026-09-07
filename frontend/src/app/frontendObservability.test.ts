import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createFrontendErrorEvent,
  recordResponseTrace,
  reportFrontendError,
  sanitizeTelemetryText,
} from './frontendObservability'

describe('frontendObservability', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('关联最近 Trace 并清除错误文本中的凭据', () => {
    recordResponseTrace({ headers: new Headers({ 'X-Trace-ID': 'trace-response-1' }) })
    const event = createFrontendErrorEvent(
      { boundaryName: 'job-result', jobId: 'job-1', pluginId: 'sequence' },
      new Error('Bearer secret-token token=abc eyJabcdefgh.ijklmnop.qrstuvwx'),
      'component api_key=private-value',
    )

    expect(event).toMatchObject({
      boundary_name: 'job-result',
      trace_id: 'trace-response-1',
      job_id: 'job-1',
      plugin_id: 'sequence',
      path: '/',
    })
    expect(JSON.stringify(event)).not.toContain('secret-token')
    expect(JSON.stringify(event)).not.toContain('private-value')
    expect(event.message).toContain('Bearer [REDACTED]')
    expect(event.message).toContain('[REDACTED_JWT]')
  })

  it('用鉴权和同一 Trace 上报固定字段', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, headers: new Headers({ 'X-Trace-ID': 'trace-server-2' }) })
    vi.stubGlobal('fetch', fetchMock)
    const event = createFrontendErrorEvent(
      { boundaryName: 'plugin-card', traceId: 'trace-job-1', pluginId: 'omics' },
      new Error('render failed'),
      'at PluginCard',
    )

    await expect(reportFrontendError('https://api.example.test', 'jwt-secret', event)).resolves.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('https://api.example.test/api/v1/telemetry/frontend-errors', expect.objectContaining({
      method: 'POST',
      keepalive: true,
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer jwt-secret',
        'X-Trace-ID': 'trace-job-1',
      },
    }))
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body).toMatchObject({ boundary_name: 'plugin-card', trace_id: 'trace-job-1', plugin_id: 'omics' })
    expect(body).not.toHaveProperty('token')
  })

  it('限制遥测字符串长度', () => {
    expect(sanitizeTelemetryText('x'.repeat(2000), 128)).toHaveLength(128)
  })
})
