import { describe, expect, it } from 'vitest'
import { parseJobPayload } from './platformPayloadValidation'


describe('job artifact manifest', () => {
  const job = {
    job_id: 'job-1',
    tool: 'research_execute',
    status: 'completed',
    created_at: '2026-09-21T00:00:00Z',
    result: { status: 'ok' },
  }
  const artifact = {
    artifact_id: 'a'.repeat(32),
    filename: 'report.html',
    content_type: 'text/html',
    size_bytes: 128,
    sha256: 'b'.repeat(64),
    storage_backend: 's3' as const,
    storage_key: 'bio-agent/artifacts/project/job/report.html',
    version_id: 'version-1',
    reference: 'bio+s3://bucket/key?versionId=version-1',
  }

  it('preserves a valid version-locked manifest', () => {
    expect(parseJobPayload({ job: { ...job, artifacts: [artifact] } }).value?.artifacts).toEqual([artifact])
  })

  it('rejects malformed integrity metadata', () => {
    expect(parseJobPayload({
      job: { ...job, artifacts: [{ ...artifact, sha256: 'invalid' }] },
    }).value).toBeNull()
  })
})
