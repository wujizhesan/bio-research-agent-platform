import { expect, test } from '@playwright/test'

test.describe.configure({ timeout: 120_000 })

test('@fullstack 真实服务完成确定性研究规划并持久化任务', async ({ page }) => {
  const expectedJobBackend = process.env.FULLSTACK_JOB_BACKEND || 'redis'
  const apiToken = process.env.FULLSTACK_API_TOKEN
  if (apiToken) {
    await page.addInitScript((token) => localStorage.setItem('bio-agent-token', token), apiToken)
  }
  const healthResponse = await page.request.get('http://127.0.0.1:8000/health')
  expect(healthResponse.ok()).toBe(true)
  await expect(healthResponse.json()).resolves.toMatchObject({
    status: 'ok',
    database: 'ok',
    job_backend: expectedJobBackend,
  })

  await page.goto('/')
  await expect(page.getByText('API 在线')).toBeVisible()
  await page.getByLabel('规划器模式').selectOption('deterministic')
  await page.getByPlaceholder('描述你希望 Agent 协助完成的研究任务').fill('为 MKT 蛋白设计一条可复现的 mRNA 研究路径')

  const submissionPromise = page.waitForResponse((response) => (
    response.url() === 'http://127.0.0.1:8000/api/v1/jobs'
    && response.request().method() === 'POST'
  ))
  await page.getByRole('button', { name: '开始运行' }).click()
  const submission = await submissionPromise
  expect(submission.status()).toBe(202)
  expect(submission.headers()['x-trace-id']).toBeTruthy()
  const submitted = await submission.json() as { job: { job_id: string } }

  await expect(page.getByRole('heading', { name: '执行前计划检查' })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('规划器：Deterministic')).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('输入已满足，可执行')).toBeVisible({ timeout: 60_000 })

  const token = await page.getByLabel('访问令牌').inputValue()
  await expect.poll(async () => {
    const response = await page.request.get('http://127.0.0.1:8000/api/v1/jobs?limit=100', {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!response.ok()) return 'unavailable'
    const payload = await response.json() as { jobs: Array<{ job_id: string; status: string }> }
    return payload.jobs.find((job) => job.job_id === submitted.job.job_id)?.status || 'missing'
  }, { timeout: 15_000 }).toBe('completed')

  await page.reload()
  await expect(page.getByText('API 在线')).toBeVisible()
  const persistedRow = page.getByRole('row').filter({ hasText: 'research_plan' }).filter({ hasText: '已完成' }).first()
  await expect(persistedRow).toBeVisible({ timeout: 15_000 })

  const persistedResponse = await page.request.get(`http://127.0.0.1:8000/api/v1/jobs/${submitted.job.job_id}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(persistedResponse.ok()).toBe(true)
  await expect(persistedResponse.json()).resolves.toMatchObject({
    job: {
      job_id: submitted.job.job_id,
      tool: 'research_plan',
      status: 'completed',
    },
  })
})
