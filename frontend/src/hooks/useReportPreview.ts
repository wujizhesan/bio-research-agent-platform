import { useCallback, useEffect, useRef, useState } from 'react'

export type ReportPreview = { url: string; filename: string }

export function useReportPreview() {
  const [reportPreview, setReportPreview] = useState<ReportPreview | null>(null)
  const previewUrlRef = useRef<string | null>(null)

  const closeReportPreview = useCallback(() => {
    if (previewUrlRef.current) window.URL.revokeObjectURL(previewUrlRef.current)
    previewUrlRef.current = null
    setReportPreview(null)
  }, [])

  const showReportPreview = useCallback((blob: Blob, filename: string) => {
    const url = window.URL.createObjectURL(blob)
    if (previewUrlRef.current) window.URL.revokeObjectURL(previewUrlRef.current)
    previewUrlRef.current = url
    setReportPreview({ url, filename })
  }, [])

  useEffect(() => () => {
    if (previewUrlRef.current) window.URL.revokeObjectURL(previewUrlRef.current)
  }, [])

  return { reportPreview, showReportPreview, closeReportPreview }
}
