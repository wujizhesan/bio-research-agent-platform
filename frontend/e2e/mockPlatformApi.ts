import type { Page, Route } from '@playwright/test'

type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

type MockJob = {
  job_id: string
  tool: string
  status: JobStatus
  created_at: string
  finished_at?: string
  result?: Record<string, unknown>
}

const createdAt = '2026-09-07T00:00:00Z'

function job(jobId: string, tool: string, status: JobStatus, result?: Record<string, unknown>): MockJob {
  return {
    job_id: jobId,
    tool,
    status,
    created_at: createdAt,
    finished_at: status === 'completed' ? '2026-09-07T00:01:00Z' : undefined,
    result,
  }
}

export async function mockPlatformApi(page: Page) {
  const running = job('running-job-0001', 'omics_run_analysis', 'running')
  const failed = job('failed-job-00001', 'variant_annotation', 'failed')
  let jobs = [running, failed]

  const completedResult = {
    status: 'completed',
    summary: '端到端任务已完成',
    report: { path: 'output/smoke-report.html' },
  }

  await page.route('http://127.0.0.1:8000/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()

    if (path === '/api/v1/plugins') {
      await route.fulfill({ json: { plugins: [{ domain: 'research', name: 'Research', status: 'available', tool_count: 2, tools: ['research_plan', 'research_execute'] }] } })
      return
    }
    if (path === '/api/v1/capabilities') {
      await route.fulfill({ json: { tool_count: 2, interfaces: { rest: { status: 'available', protocol: 'http' }, sse: { status: 'available', protocol: 'sse' } } } })
      return
    }
    if (path === '/api/v1/projects') {
      await route.fulfill({ json: { projects: [{ project_id: 'project-smoke', name: 'Smoke Project', owner_subject: 'tester', created_at: createdAt }] } })
      return
    }
    if (path === '/api/v1/jobs' && method === 'GET') {
      await route.fulfill({ json: { jobs } })
      return
    }
    if (path === '/api/v1/jobs' && method === 'POST') {
      const submitted = job('submitted-job-01', 'research_plan', 'queued')
      jobs = [submitted, ...jobs]
      await route.fulfill({ status: 202, json: { job: submitted } })
      return
    }
    if (path.endsWith('/cancel') && method === 'POST') {
      const cancelled = job('running-job-0001', 'omics_run_analysis', 'cancelled')
      jobs = [cancelled, ...jobs.filter((item) => item.job_id !== cancelled.job_id)]
      await route.fulfill({ json: { job: cancelled } })
      return
    }
    if (path.endsWith('/retry') && method === 'POST') {
      const retried = job('retried-job-0001', 'omics_run_analysis', 'queued')
      jobs = [retried, ...jobs]
      await route.fulfill({ status: 202, json: { job: retried } })
      return
    }
    if (path.endsWith('/events/ticket') && method === 'POST') {
      await route.fulfill({ json: { ticket: 'smoke-ticket', expires_in: 60 } })
      return
    }
    if (path.endsWith('/events') && method === 'GET') {
      const jobId = path.split('/')[4]
      const completed = job(jobId, jobId.startsWith('retried') ? 'omics_run_analysis' : 'research_plan', 'completed', {
        ...completedResult,
        summary: jobId.startsWith('retried') ? '重试任务已完成' : completedResult.summary,
      })
      jobs = [completed, ...jobs.filter((item) => item.job_id !== jobId)]
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: `event: job\ndata: ${JSON.stringify({ job: completed })}\n\n`,
      })
      return
    }
    if (path.endsWith('/artifacts') && method === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'text/html',
        headers: { 'Content-Disposition': 'inline; filename="smoke-report.html"' },
        body: '<!doctype html><title>Smoke report</title><main>Report ready</main>',
      })
      return
    }

    await route.fulfill({ status: 404, json: { detail: `Unhandled mock route: ${method} ${path}` } })
  })
}
