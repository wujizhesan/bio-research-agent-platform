import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useReportPreview } from './useReportPreview'

const createObjectURL = vi.fn<(blob: Blob) => string>()
const revokeObjectURL = vi.fn<(url: string) => void>()

describe('useReportPreview', () => {
  beforeEach(() => {
    let sequence = 0
    createObjectURL.mockImplementation(() => `blob:report-${++sequence}`)
    const NativeURL = window.URL
    class MockURL extends NativeURL {}
    Object.defineProperties(MockURL, {
      createObjectURL: { value: createObjectURL },
      revokeObjectURL: { value: revokeObjectURL },
    })
    vi.stubGlobal('URL', MockURL)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('替换和关闭预览时释放对象 URL', () => {
    const { result } = renderHook(() => useReportPreview())

    act(() => result.current.showReportPreview(new Blob(['first']), 'first.html'))
    expect(result.current.reportPreview).toEqual({ url: 'blob:report-1', filename: 'first.html' })

    act(() => result.current.showReportPreview(new Blob(['second']), 'second.html'))
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:report-1')
    expect(result.current.reportPreview).toEqual({ url: 'blob:report-2', filename: 'second.html' })

    act(() => result.current.closeReportPreview())
    expect(revokeObjectURL).toHaveBeenLastCalledWith('blob:report-2')
    expect(result.current.reportPreview).toBeNull()
  })

  it('组件卸载时释放当前预览 URL', () => {
    const { result, unmount } = renderHook(() => useReportPreview())
    act(() => result.current.showReportPreview(new Blob(['report']), 'report.html'))

    unmount()

    expect(revokeObjectURL).toHaveBeenCalledWith('blob:report-1')
  })
})
