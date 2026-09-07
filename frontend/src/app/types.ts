export type Plugin = {
  domain: string
  name: string
  status: string
  tool_count: number
  tools: string[]
  version?: string
}

export type CapabilityInterface = {
  status: string
  protocol: string
  docs?: string
  openapi?: string
  endpoint?: string
  transport?: string
  entrypoint?: string
  tool_count?: number
}

export type Capabilities = {
  tool_count: number
  interfaces: Record<string, CapabilityInterface>
}

export type Job = {
  job_id: string
  tool: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  created_at: string
  started_at?: string
  finished_at?: string
  result?: Record<string, unknown>
  error?: string
  cancel_requested?: boolean
  trace_id?: string
  request_id?: string
}

export type EventItem = {
  at: string
  type: string
  status: string
  detail: string
}

export type SequenceCheck = {
  name: string
  passed: boolean
  detail?: string
}

export type SequenceMolecule = 'linear' | 'circ' | 'sa'
export type SequenceMethod = 'greedy' | 'vaxpress'
export type SequenceBenchmarkRow = {
  method: string
  mrna?: string
  metrics: Record<string, unknown>
  verdict?: string
}

export type CaddHit = {
  mol_name: string
  tag: string
  affinity: number
}

export type ResearchPlanExecution = {
  ready: boolean
  missing_inputs: string[]
  evidence_provider: string
  selected_tools: string[]
  rationale: string[]
  workflow?: Record<string, unknown> | null
  workflow_preview?: Record<string, unknown> | null
}

export type ResearchPlan = {
  status: string
  task: string
  selected_domains: string[]
  capabilities: string[]
  required_inputs: Array<{ name: string; description: string }>
  evidence_provider: string
  planner?: { backend: string; mode: string; model?: string | null; fallback_reason?: string }
  execution: ResearchPlanExecution
}

export type ResearchFileSlot = 'expression' | 'metadata' | 'gene_sets' | 'vcf' | 'annotation' | 'receptor' | 'ligand_library'

export type RnaFileSlot = 'fastq_r1' | 'fastq_r2' | 'reference_fasta' | 'annotation_gtf' | 'metadata' | 'gene_sets'

export type UploadedFile = {
  file_id: string
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  path: string
  download_url: string
}

export type Project = {
  project_id: string
  name: string
  description?: string | null
  owner_subject: string
  created_at: string
}

export type RnaPreflightItem = {
  label: string
  detail: string
  ready: boolean
  required: boolean
}

export type View = 'workspace' | 'domains'
export type RunMode = 'research' | 'rnaseq' | 'variant' | 'sequence' | 'cadd'
export type ResearchPreset = 'custom' | 'bgi_multiomics' | 'online_evidence'
export type RnaInputMode = 'fixture' | 'upload'
export type PlannerMode = 'auto' | 'deterministic' | 'llm'
