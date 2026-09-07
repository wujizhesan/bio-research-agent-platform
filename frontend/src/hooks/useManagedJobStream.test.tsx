import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { useManagedJobStream } from './useManagedJobStream'

describe('useManagedJobStream', () => {
  it('中止旧控制器并只允许当前任务流完成', () => {
    const { result } = renderHook(() => useManagedJobStream())

    const first = result.current.beginJobStream()
    expect(result.current.isCurrentStream(first)).toBe(true)

    const second = result.current.beginJobStream()
    expect(first.signal.aborted).toBe(true)
    expect(result.current.isCurrentStream(first)).toBe(false)
    expect(result.current.isCurrentStream(second)).toBe(true)

    act(() => result.current.finishJobStream(first))
    expect(result.current.isCurrentStream(second)).toBe(true)

    act(() => result.current.finishJobStream(second))
    expect(result.current.isCurrentStream(second)).toBe(false)
  })

  it('组件卸载时中止当前任务流', () => {
    const { result, unmount } = renderHook(() => useManagedJobStream())
    const controller = result.current.beginJobStream()

    unmount()

    expect(controller.signal.aborted).toBe(true)
  })
})
