import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Job } from '../app/types'
import { ExecutionStream } from './JobActivity'

const job: Job = {
  job_id: 'job-1',
  tool: 'sequence_workbench',
  status: 'running',
  created_at: '2026-09-07T00:00:00Z',
}

describe('ExecutionStream', () => {
  it.each([
    ['connected', 'SSE'],
    ['reconnecting', '重连中'],
    ['polling', '轮询'],
    ['idle', '未订阅'],
  ] as const)('显示 %s 的连接状态', (connectionMode, label) => {
    render(<ExecutionStream job={job} events={[]} connectionMode={connectionMode} />)

    expect(screen.getByText(label)).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByText(label)).toHaveAttribute('aria-atomic', 'true')
  })
})
