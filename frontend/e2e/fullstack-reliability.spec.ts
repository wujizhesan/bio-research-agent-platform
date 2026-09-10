import { closeSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { expect, test, type APIRequestContext } from '@playwright/test'


type FullStackJob = {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  attempts?: number
  retry_of?: string
  result?: Record<string, unknown>
}

const apiBase = 'http://127.0.0.1:8000'
const workspaceRoot = process.env.GITHUB_WORKSPACE || path.resolve('..')
const controlDir = path.join(workspaceRoot, 'output', 'fullstack-reliability')

function headers() {
  const token = process.env.FULLSTACK_API_TOKEN
  if (!token) throw new Error('FULLSTACK_API_TOKEN is required')
  return { Authorization: `Bearer ${token}` }
}

async function submitJob(request: APIRequestContext, tool: string, args: Record<string, unknown>) {
  const response = await request.post(`${apiBase}/api/v1/jobs`, {
    headers: headers(),
    data: { tool, arguments: args },
  })
  const body = await response.json()
  expect(response.status(), JSON.stringify(body)).toBe(202)
  return body.job as FullStackJob
}

async function getJob(request: APIRequestContext, jobId: string) {
  const response = await request.get(`${apiBase}/api/v1/jobs/${jobId}`, {
    headers: headers(),
  })
  const body = await response.json()
  expect(response.ok(), JSON.stringify(body)).toBeTruthy()
  return body.job as FullStackJob
}

async function waitForStatus(
  request: APIRequestContext,
  jobId: string,
  status: FullStackJob['status'],
  timeout = 60_000,
) {
  let current: FullStackJob | undefined
  await expect.poll(async () => {
    current = await getJob(request, jobId)
    return current.status
  }, { timeout }).toBe(status)
  return current as FullStackJob
}

async function restartWorker() {
  const pidPath = path.join(workspaceRoot, 'output', 'ci-fullstack-worker.pid')
  const workerLog = path.join(workspaceRoot, 'output', 'ci-fullstack-worker-restarted.log')
  const pid = Number(readFileSync(pidPath, 'utf8').trim())
  expect(Number.isInteger(pid) && pid > 0).toBeTruthy()
  process.kill(pid, 'SIGKILL')
  await new Promise((resolve) => setTimeout(resolve, 250))
  const log = openSync(workerLog, 'a')
  const child = spawn('python', ['-m', 'src.worker'], {
    cwd: workspaceRoot,
    detached: true,
    windowsHide: true,
    env: {
      ...process.env,
      JOB_EXECUTION_MODE: 'process',
      JOB_LEASE_SECONDS: '2',
      WORKER_METRICS_PORT: '0',
    },
    stdio: ['ignore', log, log],
  })
  child.unref()
  closeSync(log)
  expect(child.pid).toBeTruthy()
  writeFileSync(pidPath, `${child.pid}\n`, 'utf8')
}

test.describe('@fullstack Redis worker failure paths', () => {
  test.describe.configure({ mode: 'serial', timeout: 120_000 })

  test.beforeAll(() => {
    test.skip(!process.env.FULLSTACK_API_TOKEN, 'requires the authenticated full-stack environment')
    mkdirSync(controlDir, { recursive: true })
  })

  test('运行中任务可取消并终止隔离进程', async ({ request }) => {
    const submitted = await submitJob(request, 'ci_reliability_hold', {
      duration_seconds: 60,
    })
    await waitForStatus(request, submitted.job_id, 'running')
    const response = await request.post(
      `${apiBase}/api/v1/jobs/${submitted.job_id}/cancel`,
      { headers: headers() },
    )
    expect(response.status()).toBe(202)
    const cancelled = await waitForStatus(
      request,
      submitted.job_id,
      'cancelled',
      15_000,
    )
    expect(cancelled.status).toBe('cancelled')
  })

  test('失败任务通过正式重试接口恢复', async ({ request }) => {
    const statePath = path.join(controlDir, 'fail-once.state')
    rmSync(statePath, { force: true })
    const submitted = await submitJob(request, 'ci_reliability_fail_once', {
      state_path: statePath,
    })
    await waitForStatus(request, submitted.job_id, 'failed')
    const retryResponse = await request.post(
      `${apiBase}/api/v1/jobs/${submitted.job_id}/retry`,
      { headers: headers() },
    )
    const retryBody = await retryResponse.json()
    expect(retryResponse.status(), JSON.stringify(retryBody)).toBe(202)
    const retried = retryBody.job as FullStackJob
    const completed = await waitForStatus(request, retried.job_id, 'completed')
    expect(completed.retry_of).toBe(submitted.job_id)
    expect(completed.result).toMatchObject({ status: 'ok', recovered: true })
  })

  test('Worker 重启后回收租约并从持久标记续跑', async ({ request }) => {
    const statePath = path.join(controlDir, 'resume-once.state')
    rmSync(statePath, { force: true })
    const submitted = await submitJob(request, 'ci_reliability_resume_once', {
      state_path: statePath,
      duration_seconds: 30,
    })
    await waitForStatus(request, submitted.job_id, 'running')
    await expect.poll(() => existsSync(statePath), { timeout: 30_000 }).toBe(true)
    await restartWorker()
    const completed = await waitForStatus(
      request,
      submitted.job_id,
      'completed',
      90_000,
    )
    expect(completed.attempts).toBe(2)
    expect(completed.result).toMatchObject({ status: 'ok', resumed: true })
  })
})
