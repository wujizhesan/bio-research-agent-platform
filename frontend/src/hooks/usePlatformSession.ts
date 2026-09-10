import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from '../app/api'
import {
  parseCapabilitiesPayload,
  parseJobListPayload,
  parsePluginPayload,
  parseProjectListPayload,
  parseProjectPayload,
} from '../app/platformPayloadValidation'
import type { Capabilities, Job, Plugin, Project } from '../app/types'

const terminalJobStatuses = new Set<Job['status']>(['completed', 'failed', 'cancelled'])

function mergeJobState(previous: Job | undefined, next: Job) {
  if (previous && terminalJobStatuses.has(previous.status) && !terminalJobStatuses.has(next.status)) return previous
  return next
}

function mergeJobList(current: Job[], incoming: Job[]) {
  const currentById = new Map(current.map((job) => [job.job_id, job]))
  return incoming.map((job) => mergeJobState(currentById.get(job.job_id), job))
}

export function usePlatformSession(apiBase: string, initialToken: string) {
  const [token, setToken] = useState(initialToken)
  const [tokenDraft, setTokenDraft] = useState(initialToken)
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async (authToken = token) => {
    setError('')
    try {
      const [pluginPayload, jobPayload, capabilityPayload, projectPayload] = await Promise.all([
        apiFetch<unknown>(apiBase, authToken, '/api/v1/plugins'),
        apiFetch<unknown>(apiBase, authToken, '/api/v1/jobs?limit=8'),
        apiFetch<unknown>(apiBase, authToken, '/api/v1/capabilities'),
        apiFetch<unknown>(apiBase, authToken, '/api/v1/projects'),
      ])
      const pluginResult = parsePluginPayload(pluginPayload)
      const jobResult = parseJobListPayload(jobPayload)
      const capabilityResult = parseCapabilitiesPayload(capabilityPayload)
      const projectResult = parseProjectListPayload(projectPayload)
      setPlugins(pluginResult.value)
      setCapabilities(capabilityResult.value)
      const nextProjects = projectResult.value
      setProjects(nextProjects)
      setSelectedProjectId((current) => nextProjects.some((project) => project.project_id === current) ? current : nextProjects[0]?.project_id || '')
      setJobs((current) => mergeJobList(current, jobResult.value))
      const issues = [...pluginResult.issues, ...jobResult.issues, ...capabilityResult.issues, ...projectResult.issues]
      if (issues.length) setError(`API 响应已安全降级：${issues.join('；')}`)
      setConnected(true)
    } catch (err) {
      setConnected(false)
      setError(err instanceof Error ? err.message : '无法连接 API')
    }
  }, [apiBase, token])

  useEffect(() => {
    void refresh()
  }, [refresh])

  function saveToken() {
    const normalized = tokenDraft.trim()
    if (normalized) localStorage.setItem('bio-agent-token', normalized)
    else localStorage.removeItem('bio-agent-token')
    setTokenDraft(normalized)
    setToken(normalized)
    if (normalized === token) void refresh(normalized)
  }

  async function createProject() {
    const name = window.prompt('项目名称')?.trim()
    if (!name) return
    try {
      const payload = await apiFetch<unknown>(apiBase, token, '/api/v1/projects', {
        method: 'POST',
        body: JSON.stringify({ name }),
      })
      const projectResult = parseProjectPayload(payload)
      if (!projectResult.value) {
        setError(`API 响应已安全降级：${projectResult.issues.join('；')}`)
        return
      }
      const project = projectResult.value
      setProjects((current) => [project, ...current])
      setSelectedProjectId(project.project_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '项目创建失败')
    }
  }

  const upsertJob = useCallback((job: Job) => {
    setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)])
  }, [])

  return {
    apiBase,
    token,
    tokenDraft,
    setTokenDraft,
    projects,
    selectedProjectId,
    setSelectedProjectId,
    plugins,
    capabilities,
    jobs,
    connected,
    error,
    setError,
    refresh,
    saveToken,
    createProject,
    upsertJob,
  }
}
