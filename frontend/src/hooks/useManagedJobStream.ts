import { useCallback, useEffect, useRef } from 'react'

export function useManagedJobStream() {
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => () => controllerRef.current?.abort(), [])

  const beginJobStream = useCallback(() => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    return controller
  }, [])

  const isCurrentStream = useCallback((controller: AbortController) => controllerRef.current === controller, [])

  const finishJobStream = useCallback((controller: AbortController) => {
    if (controllerRef.current === controller) controllerRef.current = null
  }, [])

  return { beginJobStream, isCurrentStream, finishJobStream }
}
