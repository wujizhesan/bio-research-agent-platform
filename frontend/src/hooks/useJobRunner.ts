import type { Dispatch, SetStateAction } from 'react'
import { useCallback, useState } from 'react'
import { apiFetch, followJob } from '../app/api'
import { statusLabels } from '../app/constants'
import { parseJob, parseJobPayload } from '../app/platformPayloadValidation'
import type { EventItem, Job } from '../app/types'
import { useManagedJobStream } from './useManagedJobStream'

export function formatTime(value?: string) {
  if (!value) return '--'
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function formatJobId(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-4)}`
}

type JobRunnerOptions = {
  apiBase: string
  token: string
  selectedProjectId: string
  refresh: () => Promise<void>
  upsertJob: (job: Job) => void
  setError: Dispatch<SetStateAction<string>>
  showReportPreview: (blob: Blob, filename: string) => void
}

export function useJobRunner({ apiBase, token, selectedProjectId, refresh, upsertJob, setError, showReportPreview }: JobRunnerOptions) {
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [loading, setLoading] = useState(false)
  const { beginJobStream, isCurrentStream, finishJobStream } = useManagedJobStream()

  function updateJob(job: Job) {
    setSelectedJob(job)
    upsertJob(job)
  }

  function appendJobEvent(type: string, job: Job) {
    setEvents((current) => [
      ...current,
      {
        at: formatTime(new Date().toISOString()),
        type,
        status: job.status,
        detail: type === 'timeout' ? 'SSE 订阅超时，任务仍可通过列表查询' : `状态更新为${statusLabels[job.status] || job.status}`,
      },
    ])
  }

  async function followSubmittedJob(job: Job, controller: AbortController, onCompleted?: (job: Job) => void) {
    await followJob(apiBase, token, job.job_id, (type, data) => {
      if (!data.job) return
      const nextJob = parseJob(data.job)
      if (!nextJob) throw new Error('任务状态响应格式异常')
      updateJob(nextJob)
      if (nextJob.status === 'completed') onCompleted?.(nextJob)
      appendJobEvent(type, nextJob)
    }, controller.signal)
  }

  async function submitToolJob(
    tool: string,
    arguments_: Record<string, unknown>,
    acceptedDetail: string,
    onCompleted?: (job: Job) => void,
  ) {
    const controller = beginJobStream()
    setLoading(true)
    setError('')
    setEvents([])
    try {
      const response = await apiFetch<unknown>(apiBase, token, '/api/v1/jobs', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({ tool, arguments: arguments_, project_id: selectedProjectId || undefined }),
      })
      const jobResult = parseJobPayload(response)
      if (!jobResult.value) throw new Error(jobResult.issues.join('；'))
      updateJob(jobResult.value)
      setEvents([{ at: formatTime(new Date().toISOString()), type: 'accepted', status: 'queued', detail: acceptedDetail }])
      await followSubmittedJob(jobResult.value, controller, onCompleted)
    } catch (err) {
      if (!isCurrentStream(controller) || (err instanceof Error && err.name === 'AbortError')) return
      setError(err instanceof Error ? err.message : '任务提交失败')
    } finally {
      if (isCurrentStream(controller)) {
        finishJobStream(controller)
        setLoading(false)
        void refresh()
      }
    }
  }

  async function cancelSelectedJob() {
    if (!selectedJob || !['queued', 'running'].includes(selectedJob.status) || selectedJob.cancel_requested) return
    setError('')
    try {
      const response = await apiFetch<unknown>(apiBase, token, `/api/v1/jobs/${selectedJob.job_id}/cancel`, { method: 'POST' })
      const jobResult = parseJobPayload(response)
      if (!jobResult.value) throw new Error(jobResult.issues.join('；'))
      const nextJob = jobResult.value
      updateJob(nextJob)
      setEvents((current) => [...current, {
        at: formatTime(new Date().toISOString()),
        type: 'cancel',
        status: nextJob.status,
        detail: nextJob.status === 'cancelled' ? '任务已取消' : '已发送取消请求，等待执行线程退出',
      }])
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消任务失败')
    }
  }

  async function retryJob(sourceJob: Job) {
    if (!['failed', 'cancelled'].includes(sourceJob.status)) return
    const controller = beginJobStream()
    setLoading(true)
    setError('')
    setEvents([])
    try {
      const response = await apiFetch<unknown>(apiBase, token, `/api/v1/jobs/${sourceJob.job_id}/retry`, { method: 'POST' })
      const jobResult = parseJobPayload(response)
      if (!jobResult.value) throw new Error(jobResult.issues.join('；'))
      updateJob(jobResult.value)
      setEvents([{ at: formatTime(new Date().toISOString()), type: 'retry', status: jobResult.value.status, detail: `任务已重试，来源 ${formatJobId(sourceJob.job_id)}` }])
      await followSubmittedJob(jobResult.value, controller)
    } catch (err) {
      if (!isCurrentStream(controller) || (err instanceof Error && err.name === 'AbortError')) return
      setError(err instanceof Error ? err.message : '任务重试失败')
    } finally {
      if (isCurrentStream(controller)) {
        finishJobStream(controller)
        setLoading(false)
        void refresh()
      }
    }
  }

  async function fetchJobArtifact(jobId: string, artifactPath: string) {
    const response = await fetch(`${apiBase}/api/v1/jobs/${jobId}/artifacts?path=${encodeURIComponent(artifactPath)}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || `产物读取失败: ${response.status}`)
    }
    const blob = await response.blob()
    const disposition = response.headers.get('content-disposition') || ''
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || artifactPath.split(/[\\/]/).pop() || 'artifact'
    return { blob, filename }
  }

  async function downloadJobArtifact(jobId: string, artifactPath: string) {
    setError('')
    try {
      const { blob, filename } = await fetchJobArtifact(jobId, artifactPath)
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.URL.revokeObjectURL(url)
    } catch (err) {
      setError(err instanceof Error ? err.message : '产物下载失败')
    }
  }

  async function previewJobArtifact(jobId: string, artifactPath: string) {
    setError('')
    try {
      const { blob, filename } = await fetchJobArtifact(jobId, artifactPath)
      showReportPreview(blob, filename)
    } catch (err) {
      setError(err instanceof Error ? err.message : '报告预览失败')
    }
  }

  const selectJob = useCallback((job: Job) => {
    setSelectedJob(job)
    setEvents([])
  }, [])

  const resetJobSelection = useCallback(() => {
    setSelectedJob(null)
    setEvents([])
  }, [])

  return {
    selectedJob,
    events,
    loading,
    submitToolJob,
    cancelSelectedJob,
    retryJob,
    downloadJobArtifact,
    previewJobArtifact,
    selectJob,
    resetJobSelection,
  }
}
