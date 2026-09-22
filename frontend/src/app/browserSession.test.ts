import { afterEach, describe, expect, it } from 'vitest'
import { browserSessionRequest } from './browserSession'

afterEach(() => {
  document.cookie = 'bioagent_csrf=; Max-Age=0; Path=/'
  document.cookie = '__Host-bioagent_csrf=; Max-Age=0; Path=/'
})

describe('browserSessionRequest', () => {
  it('使用临时 Bearer 时不附加 CSRF，但始终允许浏览器接收会话 Cookie', () => {
    const init = browserSessionRequest('temporary-token', { method: 'POST' })
    const headers = new Headers(init.headers)

    expect(init.credentials).toBe('include')
    expect(headers.get('Authorization')).toBe('Bearer temporary-token')
    expect(headers.has('X-CSRF-Token')).toBe(false)
  })

  it('忽略格式损坏的 CSRF Cookie', () => {
    document.cookie = 'bioagent_csrf=%E0%A4%A'

    const request = browserSessionRequest('', { method: 'POST' })
    const headers = new Headers(request.headers)

    expect(headers.has('X-CSRF-Token')).toBe(false)
  })

  it('Cookie 鉴权的写请求携带双提交 CSRF 令牌', () => {
    document.cookie = 'bioagent_csrf=csrf-value; Path=/'
    const init = browserSessionRequest('', { method: 'DELETE' })
    const headers = new Headers(init.headers)

    expect(headers.get('X-CSRF-Token')).toBe('csrf-value')
    expect(headers.has('Authorization')).toBe(false)
  })

  it('只读请求不发送 CSRF 头', () => {
    document.cookie = 'bioagent_csrf=csrf-value; Path=/'
    const headers = new Headers(browserSessionRequest('').headers)

    expect(headers.has('X-CSRF-Token')).toBe(false)
  })
})
