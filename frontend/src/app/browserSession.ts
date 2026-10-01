const csrfCookieNames = ['__Host-bioagent_csrf', 'bioagent_csrf']
const safeMethods = new Set(['GET', 'HEAD', 'OPTIONS'])

function readCookie(name: string) {
  if (typeof document === 'undefined') return ''
  const prefix = `${encodeURIComponent(name)}=`
  for (const part of document.cookie.split(';')) {
    const value = part.trim()
    if (value.startsWith(prefix)) {
      try {
        return decodeURIComponent(value.slice(prefix.length))
      } catch {
        return ''
      }
    }
  }
  return ''
}

export function browserSessionRequest(token: string, init: RequestInit = {}): RequestInit {
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const method = (init.method || 'GET').toUpperCase()
  if (!token && !safeMethods.has(method)) {
    const csrfToken = csrfCookieNames.map(readCookie).find(Boolean)
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  }
  return {
    ...init,
    credentials: 'include',
    headers,
  }
}
