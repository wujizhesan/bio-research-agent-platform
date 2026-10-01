import { expect, test, type APIRequestContext } from '@playwright/test'

const apiBase = 'http://127.0.0.1:8000'

async function submitHoldJob(request: APIRequestContext, durationSeconds: number) {
  const token = process.env.FULLSTACK_API_TOKEN
  if (!token) throw new Error('FULLSTACK_API_TOKEN is required')
  const headers = { Authorization: `Bearer ${token}` }
  const projectResponse = await request.post(`${apiBase}/api/v1/projects`, {
    headers,
    data: { name: `SSE recovery ${Date.now()}` },
  })
  const project = await projectResponse.json() as { project?: { project_id: string } }
  expect(projectResponse.status(), JSON.stringify(project)).toBe(201)
  const jobResponse = await request.post(`${apiBase}/api/v1/jobs`, {
    headers,
    data: {
      tool: 'ci_reliability_hold',
      arguments: { duration_seconds: durationSeconds },
      project_id: project.project?.project_id,
    },
  })
  const submission = await jobResponse.json() as { job?: { job_id: string } }
  expect(jobResponse.status(), JSON.stringify(submission)).toBe(202)
  expect(submission.job?.job_id).toBeTruthy()
  return { token, jobId: submission.job!.job_id }
}

test.describe('@fullstack browser SSE recovery', () => {
  test.describe.configure({ timeout: 120_000 })

  test.beforeEach(() => {
    test.skip(!process.env.FULLSTACK_API_TOKEN, 'requires the authenticated full-stack environment')
  })

  test('收到真实事件后断线，带游标重连且不重复交付', async ({ page, request }) => {
    const { token, jobId } = await submitHoldJob(request, 15)
    await page.goto('/')

    const result = await page.evaluate(async ({ apiBase: base, token: bearer, jobId: id }) => {
      const NativeEventSource = window.EventSource
      const urls: string[] = []
      const signatures: string[] = []
      const statuses: string[] = []
      const states: string[] = []
      let interrupted = false

      class InterruptOnceEventSource extends NativeEventSource {
        constructor(url: string | URL, options?: EventSourceInit) {
          super(url, options)
          const safeUrl = new URL(String(url))
          safeUrl.searchParams.delete('ticket')
          urls.push(safeUrl.toString())
          this.addEventListener('job', (event) => {
            if (interrupted || !event.lastEventId) return
            interrupted = true
            queueMicrotask(() => {
              this.close()
              this.onerror?.(new Event('error'))
            })
          })
        }
      }

      window.EventSource = InterruptOnceEventSource
      try {
        const moduleUrl = new URL('/src/app/api.ts', window.location.href).toString()
        const api = await import(moduleUrl)
        await api.followJob(base, bearer, id, (_type: string, payload: { job?: { status: string } }) => {
          if (!payload.job) return
          statuses.push(payload.job.status)
          signatures.push(JSON.stringify(payload.job))
        }, undefined, (state: string) => states.push(state))
        return { urls, statuses, signatures, states, interrupted }
      } finally {
        window.EventSource = NativeEventSource
      }
    }, { apiBase, token, jobId })

    expect(result.interrupted).toBe(true)
    expect(result.urls.length).toBeGreaterThanOrEqual(2)
    expect(new URL(result.urls[1]).searchParams.get('last_event_id')).toMatch(/^r-\d+$/)
    expect(result.states).toContain('reconnecting')
    expect(result.states).toContain('connected')
    expect(result.statuses.at(-1)).toBe('completed')
    expect(new Set(result.signatures).size).toBe(result.signatures.length)
  })

  test('连续断线后改用真实任务查询获得终态', async ({ page, request }) => {
    const { token, jobId } = await submitHoldJob(request, 8)
    await page.goto('/')

    const result = await page.evaluate(async ({ apiBase: base, token: bearer, jobId: id }) => {
      const NativeEventSource = window.EventSource
      const states: string[] = []
      const statuses: string[] = []
      let attempts = 0

      class DisconnectingEventSource extends NativeEventSource {
        constructor(url: string | URL, options?: EventSourceInit) {
          super(url, options)
          attempts += 1
          queueMicrotask(() => {
            this.close()
            this.onerror?.(new Event('error'))
          })
        }
      }

      window.EventSource = DisconnectingEventSource
      try {
        const moduleUrl = new URL('/src/app/api.ts', window.location.href).toString()
        const api = await import(moduleUrl)
        await api.followJob(base, bearer, id, (_type: string, payload: { job?: { status: string } }) => {
          if (payload.job) statuses.push(payload.job.status)
        }, undefined, (state: string) => states.push(state))
        return { attempts, states, statuses }
      } finally {
        window.EventSource = NativeEventSource
      }
    }, { apiBase, token, jobId })

    expect(result.attempts).toBe(3)
    expect(result.states).toContain('polling')
    expect(result.statuses.at(-1)).toBe('completed')
  })
})
