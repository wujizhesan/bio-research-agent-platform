import type { Capabilities, CapabilityInterface, Job, JobArtifact, Plugin, Project } from './types'

export type ValidationResult<T> = {
  value: T
  issues: string[]
}

const jobStatuses = new Set<Job['status']>(['queued', 'running', 'completed', 'failed', 'cancelled', 'indeterminate'])
const resolutionDecisions = new Set(['confirm_succeeded', 'confirm_failed', 'approve_retry'])
const interfaceStringFields = ['docs', 'openapi', 'endpoint', 'transport', 'entrypoint'] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

function optionalStringIsValid(value: unknown) {
  return value === undefined || value === null || typeof value === 'string'
}

function parseCollection<T>(payload: unknown, key: string, label: string, parseItem: (value: unknown) => T | null): ValidationResult<T[]> {
  if (!isRecord(payload) || !Array.isArray(payload[key])) {
    return { value: [], issues: [`${label}不是数组，已降级为空`] }
  }
  const source = payload[key]
  const value = source.map(parseItem).filter((item): item is T => item !== null)
  const invalidCount = source.length - value.length
  return {
    value,
    issues: invalidCount ? [`${label}含 ${invalidCount} 条无效记录，已忽略`] : [],
  }
}

export function parsePlugin(value: unknown): Plugin | null {
  if (!isRecord(value)
    || !isNonEmptyString(value.domain)
    || !isNonEmptyString(value.name)
    || !isNonEmptyString(value.status)
    || !isNonNegativeInteger(value.tool_count)
    || !Array.isArray(value.tools)
    || !value.tools.every(isNonEmptyString)
    || !optionalStringIsValid(value.version)) return null
  return {
    domain: value.domain,
    name: value.name,
    status: value.status,
    tool_count: value.tool_count,
    tools: value.tools,
    ...(typeof value.version === 'string' ? { version: value.version } : {}),
  }
}

function parseJobArtifact(value: unknown): JobArtifact | null {
  if (!isRecord(value)
    || !/^[a-f0-9]{32}$/.test(String(value.artifact_id || ''))
    || !isNonEmptyString(value.filename)
    || !isNonEmptyString(value.content_type)
    || !isNonNegativeInteger(value.size_bytes)
    || !/^[a-f0-9]{64}$/.test(String(value.sha256 || ''))
    || !['local', 's3'].includes(String(value.storage_backend || ''))
    || !optionalStringIsValid(value.parameter)
    || !optionalStringIsValid(value.kind)
    || !optionalStringIsValid(value.path)
    || !optionalStringIsValid(value.storage_key)
    || !optionalStringIsValid(value.version_id)
    || !optionalStringIsValid(value.reference)) return null
  if (value.storage_backend === 'local' && !isNonEmptyString(value.path)) return null
  if (value.storage_backend === 's3' && (
    !isNonEmptyString(value.storage_key)
    || !isNonEmptyString(value.version_id)
    || !isNonEmptyString(value.reference)
  )) return null
  return {
    artifact_id: String(value.artifact_id),
    filename: value.filename,
    content_type: value.content_type,
    size_bytes: value.size_bytes,
    sha256: String(value.sha256),
    storage_backend: value.storage_backend as JobArtifact['storage_backend'],
    ...(typeof value.parameter === 'string' ? { parameter: value.parameter } : {}),
    ...(typeof value.kind === 'string' ? { kind: value.kind } : {}),
    ...(typeof value.path === 'string' ? { path: value.path } : {}),
    ...(typeof value.storage_key === 'string' ? { storage_key: value.storage_key } : {}),
    ...(typeof value.version_id === 'string' ? { version_id: value.version_id } : {}),
    ...(typeof value.reference === 'string' ? { reference: value.reference } : {}),
  }
}

export function parseJob(value: unknown): Job | null {
  const resolution = isRecord(value) ? value.resolution : undefined
  const artifacts = isRecord(value) && Array.isArray(value.artifacts)
    ? value.artifacts.map(parseJobArtifact)
    : undefined
  if (!isRecord(value)
    || !isNonEmptyString(value.job_id)
    || !isNonEmptyString(value.tool)
    || !isNonEmptyString(value.created_at)
    || !jobStatuses.has(value.status as Job['status'])
    || !optionalStringIsValid(value.started_at)
    || !optionalStringIsValid(value.finished_at)
    || !optionalStringIsValid(value.error)
    || !optionalStringIsValid(value.trace_id)
    || !optionalStringIsValid(value.request_id)
    || (value.result !== undefined && value.result !== null && !isRecord(value.result))
    || (value.artifacts !== undefined && !Array.isArray(value.artifacts))
    || (artifacts !== undefined && artifacts.some((artifact) => artifact === null))
    || (value.execution_identity !== undefined && value.execution_identity !== null && !isRecord(value.execution_identity))
    || (value.routing !== undefined && value.routing !== null && !isRecord(value.routing))
    || (value.execution !== undefined && value.execution !== null && !isRecord(value.execution))
    || (value.scheduling !== undefined && value.scheduling !== null && !isRecord(value.scheduling))
    || (value.indeterminate !== undefined && value.indeterminate !== null && !isRecord(value.indeterminate))
    || (resolution !== undefined && resolution !== null && (
      !isRecord(resolution)
      || !resolutionDecisions.has(resolution.decision as string)
      || !isNonEmptyString(resolution.reason)
      || !isNonEmptyString(resolution.reviewer)
      || !isNonEmptyString(resolution.resolved_at)
      || (resolution.evidence !== undefined && !isRecord(resolution.evidence))
    ))
    || (value.cancel_requested !== undefined && typeof value.cancel_requested !== 'boolean')) return null
  return {
    job_id: value.job_id,
    tool: value.tool,
    status: value.status as Job['status'],
    created_at: value.created_at,
    ...(typeof value.started_at === 'string' ? { started_at: value.started_at } : {}),
    ...(typeof value.finished_at === 'string' ? { finished_at: value.finished_at } : {}),
    ...(isRecord(value.result) ? { result: value.result } : {}),
    ...(artifacts !== undefined ? { artifacts: artifacts as JobArtifact[] } : {}),
    ...(typeof value.error === 'string' ? { error: value.error } : {}),
    ...(typeof value.cancel_requested === 'boolean' ? { cancel_requested: value.cancel_requested } : {}),
    ...(typeof value.trace_id === 'string' ? { trace_id: value.trace_id } : {}),
    ...(typeof value.request_id === 'string' ? { request_id: value.request_id } : {}),
    ...(isRecord(value.execution_identity) ? { execution_identity: value.execution_identity } : {}),
    ...(isRecord(value.routing) ? { routing: value.routing } : {}),
    ...(isRecord(value.execution) ? { execution: value.execution } : {}),
    ...(isRecord(value.scheduling) ? { scheduling: value.scheduling } : {}),
    ...(isRecord(value.indeterminate) ? { indeterminate: value.indeterminate } : {}),
    ...(isRecord(resolution) ? { resolution: resolution as Job['resolution'] } : {}),
  }
}

export function parseProject(value: unknown): Project | null {
  if (!isRecord(value)
    || !isNonEmptyString(value.project_id)
    || !isNonEmptyString(value.name)
    || !isNonEmptyString(value.owner_subject)
    || !isNonEmptyString(value.created_at)
    || !optionalStringIsValid(value.description)) return null
  return {
    project_id: value.project_id,
    name: value.name,
    owner_subject: value.owner_subject,
    created_at: value.created_at,
    ...(value.description === null || typeof value.description === 'string' ? { description: value.description } : {}),
  }
}

function parseCapabilityInterface(value: unknown): CapabilityInterface | null {
  if (!isRecord(value)
    || !isNonEmptyString(value.status)
    || !isNonEmptyString(value.protocol)
    || (value.tool_count !== undefined && !isNonNegativeInteger(value.tool_count))
    || interfaceStringFields.some((field) => !optionalStringIsValid(value[field]))) return null
  const capability: CapabilityInterface = { status: value.status, protocol: value.protocol }
  for (const field of interfaceStringFields) {
    if (typeof value[field] === 'string') capability[field] = value[field]
  }
  if (typeof value.tool_count === 'number') capability.tool_count = value.tool_count
  return capability
}

export function parsePluginPayload(payload: unknown) {
  return parseCollection(payload, 'plugins', '插件目录', parsePlugin)
}

export function parseJobListPayload(payload: unknown) {
  return parseCollection(payload, 'jobs', '任务列表', parseJob)
}

export function parseProjectListPayload(payload: unknown) {
  return parseCollection(payload, 'projects', '项目列表', parseProject)
}

export function parseCapabilitiesPayload(payload: unknown): ValidationResult<Capabilities | null> {
  if (!isRecord(payload) || !isNonNegativeInteger(payload.tool_count) || !isRecord(payload.interfaces)) {
    return { value: null, issues: ['能力目录格式无效，已降级为空'] }
  }
  const interfaces: Record<string, CapabilityInterface> = {}
  let invalidCount = 0
  for (const [key, value] of Object.entries(payload.interfaces)) {
    const capability = parseCapabilityInterface(value)
    if (capability) interfaces[key] = capability
    else invalidCount += 1
  }
  return {
    value: { tool_count: payload.tool_count, interfaces },
    issues: invalidCount ? [`能力目录含 ${invalidCount} 条无效接口，已忽略`] : [],
  }
}

export function parseJobPayload(payload: unknown): ValidationResult<Job | null> {
  const value = isRecord(payload) ? parseJob(payload.job) : null
  return { value, issues: value ? [] : ['任务响应格式无效'] }
}

export function parseProjectPayload(payload: unknown): ValidationResult<Project | null> {
  const value = isRecord(payload) ? parseProject(payload.project) : null
  return { value, issues: value ? [] : ['项目响应格式无效'] }
}
