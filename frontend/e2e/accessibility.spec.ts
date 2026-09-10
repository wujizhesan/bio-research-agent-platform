import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Locator, type Page } from '@playwright/test'
import { mockPlatformApi } from './mockPlatformApi'

const wcagTags = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

function violationReport(violations: Awaited<ReturnType<AxeBuilder['analyze']>>['violations']) {
  return violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map((node) => node.target),
  }))
}

async function expectAccessible(page: Page, include?: string) {
  let builder = new AxeBuilder({ page }).withTags(wcagTags)
  if (include) builder = builder.include(include)
  const results = await builder.analyze()
  expect(results.violations, JSON.stringify(violationReport(results.violations), null, 2)).toEqual([])
}

async function tabTo(page: Page, target: Locator, limit = 80) {
  for (let index = 0; index < limit; index += 1) {
    await page.keyboard.press('Tab')
    if (await target.evaluate((element) => element === document.activeElement)) return
  }
  throw new Error(`键盘 Tab 无法到达目标元素：${await target.getAttribute('aria-label') || await target.textContent()}`)
}

test('@mock 工作台与结果弹窗通过自动无障碍审计和键盘路径', async ({ page }) => {
  await mockPlatformApi(page)
  await page.goto('/')
  await expect(page.getByText('API 在线')).toBeVisible()

  await expectAccessible(page)

  const viewRunningJob = page.getByRole('button', { name: '查看任务 running-job-0001' })
  await tabTo(page, viewRunningJob)
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: '取消任务' })).toBeVisible()

  await tabTo(page, page.getByRole('button', { name: '开始运行' }))
  await page.keyboard.press('Enter')
  const previewButton = page.getByRole('button', { name: '预览 report_path' })
  await expect(previewButton).toBeVisible()
  await expectAccessible(page)

  await tabTo(page, previewButton)
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: 'HTML 报告预览' })
  await expect(dialog).toBeVisible()
  await expectAccessible(page, '[role="dialog"]')
  await expect(dialog.getByRole('button', { name: '关闭预览' })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(dialog.locator('iframe')).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: '关闭预览' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(previewButton).toBeFocused()
})
