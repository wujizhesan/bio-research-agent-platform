import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

function response(payload: unknown, headers: Record<string, string> = {}) {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(payload),
    blob: vi.fn().mockResolvedValue(new Blob(['artifact'])),
    headers: new Headers(headers),
  } as unknown as Response
}

describe('App 工作台编排', () => {
  let pluginPayload: unknown

  beforeEach(() => {
    localStorage.clear()
    pluginPayload = { plugins: [{ domain: 'sequence', name: 'Sequence', status: 'available', tool_count: 2, tools: ['sequence_workbench'] }] }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/v1/plugins')) {
        return response(pluginPayload)
      }
      if (url.includes('/api/v1/jobs?limit=8')) {
        return response({ jobs: [
          { job_id: 'running-job-0001', tool: 'omics_run_analysis', status: 'running', created_at: '2026-09-07T00:00:00Z' },
          { job_id: 'complete-job-001', tool: 'sequence_workbench', status: 'completed', created_at: '2026-09-07T00:00:00Z', finished_at: '2026-09-07T00:01:00Z', result: { status: 'completed', mrna: 'AUGGCCUAA', mrna_len: 9, verify: true, metrics: { gc: 0.55 } } },
        ] })
      }
      if (url.endsWith('/api/v1/capabilities')) {
        return response({ tool_count: 2, interfaces: { rest: { status: 'available', protocol: 'http' }, sse: { status: 'available', protocol: 'sse' } } })
      }
      if (url.endsWith('/api/v1/projects') && init?.method === 'POST') {
        return response({ project: { project_id: 'project-2', name: 'New Project', owner_subject: 'user', created_at: '2026-09-07T00:00:00Z' } })
      }
      if (url.endsWith('/api/v1/projects')) {
        return response({ projects: [{ project_id: 'project-1', name: 'Demo Project', owner_subject: 'user', created_at: '2026-09-07T00:00:00Z' }] })
      }
      if (url.endsWith('/api/v1/files')) {
        return response({ file: { file_id: 'file-1', filename: 'expression.csv', content_type: 'text/csv', size_bytes: 8, sha256: 'abcdef1234567890', path: '/uploads/expression.csv', download_url: '/api/v1/files/file-1' } })
      }
      throw new Error(`unexpected request: ${url}`)
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('刷新平台、切换视图并保存访问令牌', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('New Project')
    render(<App />)

    await screen.findByText('API 在线')
    expect(screen.getByText('Demo Project')).toBeInTheDocument()
    expect(screen.getByText('sequence_workbench')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /领域与插件/ }))
    expect(await screen.findByText('插件目录 / 能力发现')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /工作台/ }))

    fireEvent.change(screen.getByLabelText('访问令牌'), { target: { value: 'new-token' } })
    fireEvent.click(screen.getByRole('button', { name: '连接' }))
    await waitFor(() => expect(localStorage.getItem('bio-agent-token')).toBe('new-token'))

    fireEvent.click(screen.getByRole('button', { name: '新建项目' }))
    await screen.findByText('New Project')
  })

  it('在研究、RNA-seq、变异和 CADD 模式间维护输入', async () => {
    const { container } = render(<App />)
    await screen.findByText('API 在线')

    fireEvent.change(screen.getByLabelText('研究场景'), { target: { value: 'online_evidence' } })
    expect(screen.getByDisplayValue('TP53, BRCA1')).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('描述你希望 Agent 协助完成的研究任务'), { target: { value: '新的研究问题' } })
    fireEvent.change(screen.getByLabelText('规划器模式'), { target: { value: 'llm' } })
    fireEvent.change(screen.getByLabelText('蛋白输入上下文'), { target: { value: 'mktw' } })
    expect(screen.getByDisplayValue('MKTW')).toBeInTheDocument()

    const expressionInput = container.querySelector<HTMLInputElement>('#expression-file')
    fireEvent.change(expressionInput!, { target: { files: [new File(['a,b\n1,2'], 'expression.csv', { type: 'text/csv' })] } })
    await screen.findByText('expression.csv')

    fireEvent.click(screen.getByRole('button', { name: /RNA-seq 上传/ }))
    fireEvent.change(screen.getByLabelText('输入来源'), { target: { value: 'upload' } })
    fireEvent.change(screen.getByPlaceholderText('例如：双端 RNA-seq，完成质控、比对和表达分析'), { target: { value: '运行单端质控' } })
    expect(screen.getByText(/个任务必需输入已满足/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /VCF 变异/ }))
    fireEvent.change(screen.getByLabelText('注释后端'), { target: { value: 'vcf_ann' } })
    fireEvent.change(screen.getByLabelText('证据来源'), { target: { value: 'pubmed' } })

    fireEvent.click(screen.getByRole('button', { name: /CADD 对接/ }))
    fireEvent.change(screen.getByLabelText('演示候选数'), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText('Vina 搜索强度'), { target: { value: '12' } })
    expect(screen.getByDisplayValue('7')).toBeInTheDocument()

    fireEvent.click(screen.getByText('sequence_workbench'))
    expect(await screen.findByText('mRNA 优化结果')).toBeInTheDocument()
  })

  it('插件目录返回非数组时保持工作台可用', async () => {
    pluginPayload = { plugins: { domain: 'sequence' } }
    render(<App />)

    await screen.findByText('API 在线')
    expect(screen.getByText(/API 响应已安全降级/)).toHaveTextContent('插件目录不是数组')
    expect(screen.getByRole('heading', { name: /把科学问题/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /领域与插件/ }))
    expect(await screen.findByText('插件目录 / 能力发现')).toBeInTheDocument()
  })
})
