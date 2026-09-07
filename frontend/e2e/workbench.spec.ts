import { expect, test } from '@playwright/test'
import { mockPlatformApi } from './mockPlatformApi'

test('@mock 提交任务、查看结果并取消和重试任务', async ({ page }) => {
  await mockPlatformApi(page)
  await page.goto('/')

  await expect(page.getByText('API 在线')).toBeVisible()
  await page.getByRole('button', { name: '开始运行' }).click()

  const previewButton = page.getByRole('button', { name: '预览 report_path' })
  await expect(previewButton).toBeVisible()
  await previewButton.click()
  const dialog = page.getByRole('dialog', { name: 'HTML 报告预览' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: '关闭预览' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(previewButton).toBeFocused()

  await page.getByRole('row').filter({ hasText: 'omics_run_analysis' }).click()
  await page.getByRole('button', { name: '取消任务' }).click()
  const retryButton = page.getByRole('button', { name: 'Retry selected task' })
  await expect(retryButton).toBeVisible()
  await retryButton.click()
  await page.getByText('查看完整结果 JSON').click()
  await expect(page.locator('pre').filter({ hasText: '重试任务已完成' })).toBeVisible()
})
