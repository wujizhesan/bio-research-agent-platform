import { describe, expect, it } from 'vitest'
import {
  parseCapabilitiesPayload,
  parseJobListPayload,
  parseJobPayload,
  parsePluginPayload,
  parseProjectListPayload,
} from './platformPayloadValidation'

const validJob = { job_id: 'job-1', tool: 'research_plan', status: 'completed', created_at: '2026-09-07T00:00:00Z', result: { status: 'ok' } }

describe('platformPayloadValidation', () => {
  it('将错误的集合结构安全降级为空', () => {
    expect(parsePluginPayload({ plugins: { domain: 'research' } })).toEqual({
      value: [],
      issues: ['插件目录不是数组，已降级为空'],
    })
    expect(parseJobListPayload(null).value).toEqual([])
    expect(parseProjectListPayload({ projects: 'invalid' }).value).toEqual([])
  })

  it('保留有效记录并过滤混入的坏记录', () => {
    const result = parseJobListPayload({ jobs: [validJob, { ...validJob, status: 'unknown' }, null] })
    expect(result.value).toEqual([validJob])
    expect(result.issues).toEqual(['任务列表含 2 条无效记录，已忽略'])
  })

  it('校验任务包装响应和能力接口', () => {
    expect(parseJobPayload({ job: validJob }).value).toEqual(validJob)
    expect(parseJobPayload({ job: { job_id: 42 } }).value).toBeNull()
    expect(parseCapabilitiesPayload({
      tool_count: 2,
      interfaces: {
        rest: { status: 'available', protocol: 'HTTP REST', openapi: '/openapi.json' },
        broken: { status: 'available' },
      },
    })).toEqual({
      value: {
        tool_count: 2,
        interfaces: { rest: { status: 'available', protocol: 'HTTP REST', openapi: '/openapi.json' } },
      },
      issues: ['能力目录含 1 条无效接口，已忽略'],
    })
  })
})
