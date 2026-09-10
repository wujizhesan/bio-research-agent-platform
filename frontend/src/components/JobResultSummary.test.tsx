import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { Job } from '../app/types'
import { JobResultSummary } from './JobResultSummary'
import { ReportPreviewModal } from './ReportPreviewModal'

function completedJob(tool: string, result: Record<string, unknown>): Job {
  return {
    job_id: 'job-test-1',
    tool,
    status: 'completed',
    created_at: '2026-09-07T00:00:00Z',
    finished_at: '2026-09-07T00:01:00Z',
    result,
  }
}

describe('JobResultSummary', () => {
  it('解析直接序列结果并触发报告操作', () => {
    const onDownload = vi.fn()
    const onOpenReport = vi.fn()
    const job = completedJob('sequence_workbench', {
      status: 'completed',
      mrna: 'AUGGCCUAA',
      mrna_len: 9,
      molecule: 'linear',
      method: 'greedy',
      verify: true,
      verdict: 'PASS',
      output_html: 'output/sequence-report.html',
      metrics: {
        gc: 0.55,
        gc3: 0.66,
        cai: 0.91,
        up_a: 1.2,
        up_u: 2.3,
        expression_score: 0.8,
      },
      checks: [
        { name: 'translation', passed: true, detail: 'target protein restored' },
      ],
      benchmark: {
        rows: [
          { method: 'naive', metrics: { gc: 0.5, gc3: 0.5, cai: 0.7 }, verdict: 'PASS' },
          { method: 'greedy', metrics: { gc: 0.55, gc3: 0.66, cai: 0.91 }, verdict: 'PASS' },
        ],
      },
    })

    render(<JobResultSummary job={job} onDownload={onDownload} onOpenReport={onOpenReport} />)

    expect(screen.getByText('mRNA 优化结果')).toBeInTheDocument()
    expect(screen.getByText('翻译已验证')).toBeInTheDocument()
    expect(screen.getAllByText('55.0%').length).toBeGreaterThan(0)
    expect(screen.getAllByText('0.910').length).toBeGreaterThan(0)
    expect(screen.getByText('+0.210')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '预览 report_path' }))
    fireEvent.click(screen.getByRole('button', { name: '下载 report_path' }))

    expect(onOpenReport).toHaveBeenCalledWith('output/sequence-report.html')
    expect(onDownload).toHaveBeenCalledWith('output/sequence-report.html')
  })

  it('解析嵌套工作流中的 CADD、证据和审计信息', () => {
    const onDownload = vi.fn()
    const onOpenReport = vi.fn()
    const job = completedJob('research_execute', {
      status: 'completed',
      report: { path: 'output/research-report.html' },
      manifest: {
        status: 'completed',
        manifest_path: 'output/run-manifest.json',
        completed_steps: 2,
        failed_steps: 0,
        steps: [
          {
            id: 'dock',
            tool: 'cadd_run_screening',
            status: 'completed',
            depends_on: [],
            result: {
              result: {
                best_hit: 'Ligand-A',
                best_affinity: -9.125,
                rows: 2,
                max_ligands: 2,
                score_plot: 'output/score.png',
                hits: [
                  { mol_name: 'Ligand-A', tag: 'active', affinity: -9.125 },
                  { mol_name: 'Ligand-B', tag: 'inactive', affinity: -7.5 },
                ],
              },
            },
          },
          {
            id: 'evidence',
            tool: 'literature_search',
            status: 'completed',
            depends_on: ['dock'],
            result: {
              result: {
                provider: 'pubmed',
                requested_gene_ids: ['TP53'],
                endpoint: 'https://eutils.ncbi.nlm.nih.gov',
                status: 'fallback',
                fallback_reason: 'remote rate limit',
                matches: [
                  { title: 'TP53 evidence', source: 'PubMed', pmid: '12345' },
                ],
              },
            },
          },
        ],
      },
    })

    render(<JobResultSummary job={job} onDownload={onDownload} onOpenReport={onOpenReport} />)

    expect(screen.getByText('命中排序与结合能')).toBeInTheDocument()
    expect(screen.getAllByText('Ligand-A').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('-9.125 kcal/mol')).toBeInTheDocument()
    expect(screen.getByText('TP53 evidence')).toBeInTheDocument()
    expect(screen.getByText('PubMed')).toBeInTheDocument()
    expect(screen.getByText('TP53')).toBeInTheDocument()
    expect(screen.getByText('回退 / 复核：remote rate limit')).toBeInTheDocument()
    expect(screen.getByText('2 个唯一工具')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '预览 report_path' }))
    fireEvent.click(screen.getByText('打分图'))

    expect(onOpenReport).toHaveBeenCalledWith('output/research-report.html')
    expect(onDownload).toHaveBeenCalledWith('output/score.png')
  })
})

describe('ReportPreviewModal', () => {
  it('展示报告并允许关闭', () => {
    const onClose = vi.fn()
    render(<ReportPreviewModal preview={{ url: 'blob:report', filename: 'result.html' }} onClose={onClose} />)

    const dialog = screen.getByRole('dialog', { name: 'HTML 报告预览' })
    expect(within(dialog).getByText('result.html')).toBeInTheDocument()
    expect(within(dialog).getByTitle('HTML report preview result.html')).toHaveAttribute('src', 'blob:report')

    fireEvent.click(within(dialog).getByRole('button', { name: '关闭预览' }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('锁定焦点、支持 Escape 并在关闭后恢复触发按钮焦点', () => {
    function Harness() {
      const [open, setOpen] = useState(false)
      return <>
        <button type="button" onClick={() => setOpen(true)}>打开报告</button>
        {open && <ReportPreviewModal preview={{ url: 'blob:report', filename: 'result.html' }} onClose={() => setOpen(false)} />}
      </>
    }

    render(<Harness />)
    const trigger = screen.getByRole('button', { name: '打开报告' })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = screen.getByRole('dialog', { name: 'HTML 报告预览' })
    const closeButton = within(dialog).getByRole('button', { name: '关闭预览' })
    const frame = within(dialog).getByTitle('HTML report preview result.html')
    expect(closeButton).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(frame).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(closeButton).toHaveFocus()
    trigger.focus()
    expect(closeButton).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })
})
