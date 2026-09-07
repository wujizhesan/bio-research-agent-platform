import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SectionErrorBoundary } from './SectionErrorBoundary'

function UnstableRegion({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('malformed plugin payload')
  return <div>区域已恢复</div>
}

function BrokenPreview({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(true)
  return <main><div>工作台仍可使用</div>{open && <SectionErrorBoundary title="报告预览无法打开" description="报告异常" actionLabel="关闭预览" onReset={() => { setOpen(false); onClose() }} variant="overlay"><UnstableRegion shouldThrow /></SectionErrorBoundary>}</main>
}

function preventExpectedWindowError(event: ErrorEvent) {
  if (event.error instanceof Error && event.error.message === 'malformed plugin payload') event.preventDefault()
}

describe('SectionErrorBoundary', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    window.addEventListener('error', preventExpectedWindowError)
  })

  afterEach(() => {
    window.removeEventListener('error', preventExpectedWindowError)
    vi.restoreAllMocks()
  })

  it('只降级异常区域并在输入变化后恢复', () => {
    const onError = vi.fn()
    const { rerender } = render(
      <main>
        <div>工作台仍可使用</div>
        <SectionErrorBoundary boundaryName="plugin-card" title="插件页面暂时无法显示" description="插件数据异常" traceId="trace-1" jobId="job-1" pluginId="omics" onError={onError} resetKeys={['broken']}>
          <UnstableRegion shouldThrow />
        </SectionErrorBoundary>
      </main>,
    )

    expect(screen.getByText('工作台仍可使用')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('插件页面暂时无法显示')
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({
      boundary_name: 'plugin-card',
      trace_id: 'trace-1',
      job_id: 'job-1',
      plugin_id: 'omics',
      error_name: 'Error',
      message: 'malformed plugin payload',
    }))

    rerender(
      <main>
        <div>工作台仍可使用</div>
        <SectionErrorBoundary title="插件页面暂时无法显示" description="插件数据异常" resetKeys={['healthy']}>
          <UnstableRegion shouldThrow={false} />
        </SectionErrorBoundary>
      </main>,
    )

    expect(screen.getByText('区域已恢复')).toBeInTheDocument()
  })

  it('允许故障弹窗通过降级操作关闭', () => {
    const onReset = vi.fn()
    render(<BrokenPreview onClose={onReset} />)

    fireEvent.click(screen.getByRole('button', { name: '关闭预览' }))
    expect(onReset).toHaveBeenCalledOnce()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('工作台仍可使用')).toBeInTheDocument()
  })
})
