import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  ArrowUpRight,
  Ban,
  BarChart3,
  Beaker,
  Boxes,
  Check,
  ChevronRight,
  CircleDot,
  Clock3,
  Database,
  Dna,
  Download,
  FlaskConical,
  GitBranch,
  LayoutDashboard,
  LockKeyhole,
  Play,
  Radio,
  RefreshCw,
  Server,
  ShieldCheck,
  Sparkles,
  Terminal,
  Upload,
  Workflow,
  XCircle,
} from 'lucide-react'

type Plugin = {
  domain: string
  name: string
  status: string
  tool_count: number
  tools: string[]
  version?: string
}

type CapabilityInterface = {
  status: string
  protocol: string
  docs?: string
  openapi?: string
  endpoint?: string
  transport?: string
  entrypoint?: string
  tool_count?: number
}

type Capabilities = {
  tool_count: number
  interfaces: Record<string, CapabilityInterface>
}

type Job = {
  job_id: string
  tool: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  created_at: string
  started_at?: string
  finished_at?: string
  result?: Record<string, unknown>
  error?: string
  cancel_requested?: boolean
}

type EventItem = {
  at: string
  type: string
  status: string
  detail: string
}

type SequenceCheck = {
  name: string
  passed: boolean
  detail?: string
}

type SequenceMolecule = 'linear' | 'circ' | 'sa'
type SequenceMethod = 'greedy' | 'vaxpress'
type SequenceBenchmarkRow = {
  method: string
  mrna?: string
  metrics: Record<string, unknown>
  verdict?: string
}

type CaddHit = {
  mol_name: string
  tag: string
  affinity: number
}

type ResearchPlanExecution = {
  ready: boolean
  missing_inputs: string[]
  evidence_provider: string
  selected_tools: string[]
  rationale: string[]
  workflow?: Record<string, unknown> | null
  workflow_preview?: Record<string, unknown> | null
}

type ResearchPlan = {
  status: string
  task: string
  selected_domains: string[]
  capabilities: string[]
  required_inputs: Array<{ name: string; description: string }>
  evidence_provider: string
  planner?: { backend: string; mode: string; model?: string | null; fallback_reason?: string }
  execution: ResearchPlanExecution
}

type ResearchFileSlot = 'expression' | 'metadata' | 'gene_sets' | 'vcf' | 'annotation' | 'receptor' | 'ligand_library'

type RnaFileSlot = 'fastq_r1' | 'fastq_r2' | 'reference_fasta' | 'annotation_gtf' | 'metadata' | 'gene_sets'

type UploadedFile = {
  file_id: string
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  path: string
  download_url: string
}

type RnaPreflightItem = {
  label: string
  detail: string
  ready: boolean
  required: boolean
}

type View = 'workspace' | 'domains'
type RunMode = 'research' | 'rnaseq' | 'variant' | 'sequence' | 'cadd'
type PlannerMode = 'auto' | 'deterministic' | 'llm'

const runtimeApiBase = new URLSearchParams(window.location.search).get('api') || ''
const defaultApiBase = runtimeApiBase || import.meta.env.VITE_API_BASE_URL || (
  window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? 'http://127.0.0.1:8000'
    : ''
)

const statusLabels: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
}

const providerLabels: Record<string, string> = {
  local: '本地证据',
  kegg: 'KEGG',
  ncbi_gene: 'NCBI Gene',
  pubmed: 'PubMed',
  uniprot: 'UniProt',
  ucsc: 'UCSC',
  gencode: 'GENCODE',
}

const domainLabels: Record<string, string> = {
  cadd: 'CADD',
  omics: 'Omics',
  sequence: 'mRNA / Sequence',
  literature: 'Literature',
  knowledge: 'Knowledge',
  imaging: 'Imaging / Multimodal',
}

const terminalJobStatuses = new Set<Job['status']>(['completed', 'failed', 'cancelled'])

function mergeJobState(previous: Job | undefined, next: Job) {
  if (previous && terminalJobStatuses.has(previous.status) && !terminalJobStatuses.has(next.status)) return previous
  return next
}

function mergeJobList(current: Job[], incoming: Job[]) {
  const currentById = new Map(current.map((job) => [job.job_id, job]))
  return incoming.map((job) => mergeJobState(currentById.get(job.job_id), job))
}

const domainIcons: Record<string, typeof Beaker> = {
  cadd: Beaker,
  omics: Activity,
  sequence: Dna,
  literature: FlaskConical,
  knowledge: Database,
  imaging: FlaskConical,
  research: Workflow,
}

async function apiFetch<T>(base: string, token: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `请求失败: ${response.status}`)
  }
  return payload as T
}

async function uploadFile(base: string, token: string, file: File): Promise<UploadedFile> {
  const body = new FormData()
  body.append('upload', file)
  const response = await fetch(`${base}/api/v1/files`, {
    method: 'POST',
    body,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail || payload.error || `文件上传失败: ${response.status}`)
  }
  return payload.file as UploadedFile
}

type JobEventPayload = { job?: Job; status?: string; error?: string }

type EventTicketPayload = { ticket: string; expires_in: number }

async function readJobStream(
  base: string,
  token: string,
  jobId: string,
  onEvent: (type: string, payload: JobEventPayload) => void,
  signal?: AbortSignal,
) {
  const ticketPayload = await apiFetch<EventTicketPayload>(base, token, `/api/v1/jobs/${jobId}/events/ticket`, {
    method: 'POST',
    signal,
  })
  await new Promise<void>((resolve, reject) => {
    const source = new EventSource(`${base}/api/v1/jobs/${jobId}/events?ticket=${encodeURIComponent(ticketPayload.ticket)}&interval_seconds=0.15&timeout_seconds=300`)
    let settled = false
    const cleanup = () => {
      source.close()
      signal?.removeEventListener('abort', handleAbort)
    }
    const finish = (callback: () => void) => {
      if (settled) return
      settled = true
      cleanup()
      callback()
    }
    const handleAbort = () => {
      const error = new Error('任务流已取消')
      error.name = 'AbortError'
      finish(() => reject(error))
    }
    const handleJob = (event: MessageEvent<string>) => {
      try {
        const payload = JSON.parse(event.data) as JobEventPayload
        onEvent('job', payload)
        if (payload.job && terminalJobStatuses.has(payload.job.status)) finish(resolve)
      } catch {
        finish(() => reject(new Error('任务流消息格式无效')))
      }
    }
    const handleError = () => {
      if (settled) return
      const error = new Error('任务 SSE 连接断开')
      finish(() => reject(error))
    }
    signal?.addEventListener('abort', handleAbort, { once: true })
    source.addEventListener('job', handleJob)
    source.onerror = handleError
  })
}

async function followJob(
  base: string,
  token: string,
  jobId: string,
  onEvent: (type: string, payload: JobEventPayload) => void,
  signal?: AbortSignal,
) {
  let retries = 0
  let lastEvent = ''
  while (true) {
    try {
      await readJobStream(base, token, jobId, (type, payload) => {
        const signature = `${type}:${JSON.stringify(payload)}`
        if (signature === lastEvent) return
        lastEvent = signature
        retries = 0
        onEvent(type, payload)
      }, signal)
      return
    } catch (error) {
      if (signal?.aborted || retries >= 2) throw error
      retries += 1
      await new Promise((resolve) => window.setTimeout(resolve, 500 * retries))
    }
  }
}

function formatTime(value?: string) {
  if (!value) return '--'
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatJobId(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-4)}`
}

function App() {
  const [view, setView] = useState<View>('workspace')
  const [mode, setMode] = useState<RunMode>('research')
  const [plannerMode, setPlannerMode] = useState<PlannerMode>('auto')
  const [apiBase] = useState(defaultApiBase)
  const [token, setToken] = useState(() => localStorage.getItem('bio-agent-token') || import.meta.env.VITE_API_TOKEN || '')
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [task, setTask] = useState('分析 RNA-seq 差异表达并设计 mRNA 序列')
  const [rnaseqTask, setRnaseqTask] = useState('Run FastQC and align paired-end RNA-seq reads')
  const [variantTask, setVariantTask] = useState('Annotate VCF variants and retrieve gene evidence')
  const [protein, setProtein] = useState('MKT')
  const [geneIds, setGeneIds] = useState('')
  const [sequenceMolecule, setSequenceMolecule] = useState<SequenceMolecule>('linear')
  const [sequenceMethod, setSequenceMethod] = useState<SequenceMethod>('greedy')
  const [sequenceUseVaxpress, setSequenceUseVaxpress] = useState(false)
  const [sequenceStructureId, setSequenceStructureId] = useState('')
  const [evidenceProvider, setEvidenceProvider] = useState('local')
  const [onlineEvidenceProvider, setOnlineEvidenceProvider] = useState('uniprot')
  const [onlineEvidenceGenes, setOnlineEvidenceGenes] = useState('TP53, BRCA1')
  const [variantBackend, setVariantBackend] = useState('auto')
  const [caddExhaustiveness, setCaddExhaustiveness] = useState('4')
  const [caddMaxLigands, setCaddMaxLigands] = useState('3')
  const [uploadedFiles, setUploadedFiles] = useState<Record<ResearchFileSlot, UploadedFile | null>>({ expression: null, metadata: null, gene_sets: null, vcf: null, annotation: null, receptor: null, ligand_library: null })
  const [rnaFiles, setRnaFiles] = useState<Record<RnaFileSlot, UploadedFile[]>>({ fastq_r1: [], fastq_r2: [], reference_fasta: [], annotation_gtf: [], metadata: [], gene_sets: [] })
  const [uploadingFile, setUploadingFile] = useState<ResearchFileSlot | ''>('')
  const [uploadingRnaFile, setUploadingRnaFile] = useState<RnaFileSlot | ''>('')
  const [researchPlan, setResearchPlan] = useState<ResearchPlan | null>(null)
  const [loading, setLoading] = useState(false)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  const [reportPreview, setReportPreview] = useState<{ url: string; filename: string } | null>(null)
  const streamController = useRef<AbortController | null>(null)
  const reportPreviewUrl = useRef<string | null>(null)

  function beginJobStream() {
    streamController.current?.abort()
    const controller = new AbortController()
    streamController.current = controller
    return controller
  }

  function isCurrentStream(controller: AbortController) {
    return streamController.current === controller
  }

  const refresh = useCallback(async (authToken = token) => {
    setError('')
    try {
      const [pluginPayload, jobPayload, capabilityPayload] = await Promise.all([
        apiFetch<{ plugins: Plugin[] }>(apiBase, authToken, '/api/v1/plugins'),
        apiFetch<{ jobs: Job[] }>(apiBase, authToken, '/api/v1/jobs?limit=8'),
        apiFetch<Capabilities>(apiBase, authToken, '/api/v1/capabilities'),
      ])
      setPlugins(pluginPayload.plugins || [])
      setCapabilities(capabilityPayload)
      setJobs((current) => mergeJobList(current, jobPayload.jobs || []))
      setConnected(true)
    } catch (err) {
      setConnected(false)
      setError(err instanceof Error ? err.message : '无法连接 API')
    }
  }, [apiBase, token])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => () => {
    if (reportPreviewUrl.current) window.URL.revokeObjectURL(reportPreviewUrl.current)
  }, [])

  const activeDomains = useMemo(() => plugins.filter((plugin) => plugin.status === 'available').length, [plugins])
  const toolCount = useMemo(() => plugins.reduce((total, plugin) => total + (plugin.tool_count || 0), 0), [plugins])
  const runningJobs = jobs.filter((job) => job.status === 'queued' || job.status === 'running').length
  const rnaseqPreflight = useMemo(() => {
    const taskText = rnaseqTask.toLowerCase()
    const pairedEnd = /paired[- ]?end|双端|双末端/.test(taskText)
    const alignment = /align|hisat|比对|featurecounts|计数|定量/.test(taskText)
    const differential = /differential|deseq|差异表达|差异分析/.test(taskText)
    const enrichment = /enrichment|pathway|gene set|富集|通路|基因集/.test(taskText)
    const r1Count = rnaFiles.fastq_r1.length
    const r2Count = rnaFiles.fastq_r2.length
    const pairMismatch = r2Count > 0 && (r1Count === 0 || r1Count !== r2Count)
    const checks: RnaPreflightItem[] = [
      { label: 'R1 FASTQ', detail: r1Count ? `${r1Count} 个文件` : '待上传', ready: r1Count > 0, required: true },
      { label: 'R2 FASTQ', detail: r2Count ? `${r2Count} 个文件` : pairedEnd ? '双端任务需要上传' : '未上传，按单端处理', ready: !pairedEnd && r2Count === 0 ? true : r2Count > 0 && !pairMismatch, required: pairedEnd },
      { label: 'Reference FASTA', detail: rnaFiles.reference_fasta.length ? '已上传' : alignment ? '比对任务需要上传' : 'Planner 可继续检查', ready: !alignment || rnaFiles.reference_fasta.length > 0, required: alignment },
      { label: 'Annotation GTF', detail: rnaFiles.annotation_gtf.length ? '已上传' : differential ? '差异分析前需要计数注释' : 'featureCounts / 差异分析需要', ready: !differential || rnaFiles.annotation_gtf.length > 0, required: differential },
      { label: 'Metadata CSV', detail: rnaFiles.metadata.length ? '已上传' : differential ? '差异分析需要' : '可选', ready: !differential || rnaFiles.metadata.length > 0, required: differential },
      { label: 'Gene sets CSV', detail: rnaFiles.gene_sets.length ? '已上传' : enrichment ? '富集分析需要' : '可选', ready: !enrichment || rnaFiles.gene_sets.length > 0, required: enrichment },
    ]
    return { checks, pairMismatch }
  }, [rnaFiles, rnaseqTask])

  function saveToken() {
    const normalized = token.trim()
    if (normalized) localStorage.setItem('bio-agent-token', normalized)
    else localStorage.removeItem('bio-agent-token')
    setToken(normalized)
    void refresh(normalized)
  }

  function buildResearchInputs() {
    return {
      expression_csv: uploadedFiles.expression?.path || 'examples/rnaseq/expression.csv',
      metadata_csv: uploadedFiles.metadata?.path || 'examples/rnaseq/metadata.csv',
      gene_sets_csv: uploadedFiles.gene_sets?.path || 'examples/rnaseq/gene_sets.csv',
      evidence_csv: evidenceProvider === 'local' ? 'examples/rnaseq/evidence.csv' : undefined,
      evidence_provider: evidenceProvider,
      gene_ids: geneIds.split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean).slice(0, 20),
      protein,
      output_dir: 'output/frontend_auto_research',
    }
  }

  function buildVariantInputs() {
    return {
      vcf_path: uploadedFiles.vcf?.path || 'examples/variants/variants.vcf',
      annotation_csv: uploadedFiles.annotation?.path || 'examples/variants/gene_annotations.csv',
      annotation_backend: variantBackend,
      evidence_csv: evidenceProvider === 'local' ? 'examples/rnaseq/evidence.csv' : undefined,
      evidence_provider: evidenceProvider,
      output_dir: 'output/frontend_variant_research',
    }
  }

  function buildRnaseqInputs() {
    return {
      fastq_paths: rnaFiles.fastq_r1.length ? rnaFiles.fastq_r1.map((file) => file.path) : undefined,
      fastq_r2_paths: rnaFiles.fastq_r2.length ? rnaFiles.fastq_r2.map((file) => file.path) : undefined,
      reference_fasta: rnaFiles.reference_fasta[0]?.path,
      annotation_gtf: rnaFiles.annotation_gtf[0]?.path,
      metadata_csv: rnaFiles.metadata[0]?.path,
      gene_sets_csv: rnaFiles.gene_sets[0]?.path,
      output_dir: 'output/frontend_rnaseq_custom',
      statistics_backend: 'scipy',
    }
  }

  function buildCaddInputs() {
    return {
      receptor: uploadedFiles.receptor?.path || 'data/4hjo.pdb',
      ligand_library: uploadedFiles.ligand_library?.path || 'output/bindingdb_egfr_10000.csv',
      exhaustiveness: Number(caddExhaustiveness) || 4,
      max_ligands: Number(caddMaxLigands) || 3,
      output_dir: 'output/frontend_cadd_research',
    }
  }

  async function handleResearchFileUpload(slot: ResearchFileSlot, file?: File) {
    if (!file) return
    setUploadingFile(slot)
    setError('')
    try {
      const uploaded = await uploadFile(apiBase, token, file)
      setUploadedFiles((current) => ({ ...current, [slot]: uploaded }))
      setResearchPlan(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '文件上传失败')
    } finally {
      setUploadingFile('')
    }
  }

  async function handleRnaFileUpload(slot: RnaFileSlot, files?: FileList | null) {
    if (!files?.length) return
    setUploadingRnaFile(slot)
    setError('')
    try {
      const uploaded: UploadedFile[] = []
      for (const file of Array.from(files)) uploaded.push(await uploadFile(apiBase, token, file))
      const values = slot === 'fastq_r1' || slot === 'fastq_r2' ? uploaded : uploaded.slice(0, 1)
      setRnaFiles((current) => ({ ...current, [slot]: values }))
      setResearchPlan(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'RNA-seq 文件上传失败')
    } finally {
      setUploadingRnaFile('')
    }
  }

  function extractResearchPlan(job: Job) {
    const payload = job.result
    if (!payload || typeof payload !== 'object') return null
    const candidate = payload as Record<string, unknown>
    if (candidate.status !== 'planned' || !candidate.execution || typeof candidate.execution !== 'object') return null
    return candidate as unknown as ResearchPlan
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
      const response = await apiFetch<{ job: Job }>(apiBase, token, '/api/v1/jobs', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({ tool, arguments: arguments_ }),
      })
      const job = response.job
      setSelectedJob(job)
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)])
      setEvents([{ at: formatTime(new Date().toISOString()), type: 'accepted', status: 'queued', detail: acceptedDetail }])
      await followJob(apiBase, token, job.job_id, (type, data) => {
        if (!data.job) return
        const currentJob = data.job
        setSelectedJob(currentJob)
        setJobs((current) => [currentJob, ...current.filter((item) => item.job_id !== currentJob.job_id)])
        if (currentJob.status === 'completed') onCompleted?.(currentJob)
        setEvents((current) => [
          ...current,
          {
            at: formatTime(new Date().toISOString()),
            type,
            status: currentJob.status,
            detail: type === 'timeout' ? 'SSE 订阅超时，任务仍可通过列表查询' : `状态更新为${statusLabels[currentJob.status] || currentJob.status}`,
          },
        ])
      }, controller.signal)
    } catch (err) {
      if (!isCurrentStream(controller) || (err instanceof Error && err.name === 'AbortError')) return
      setError(err instanceof Error ? err.message : '任务提交失败')
    } finally {
      if (isCurrentStream(controller)) {
        streamController.current = null
        setLoading(false)
        void refresh()
      }
    }
  }

  async function submitRun() {
    if (mode === 'research') {
      setResearchPlan(null)
      await submitToolJob(
        'research_plan',
        { task, inputs: buildResearchInputs(), planner_mode: plannerMode },
        '研究计划已进入执行队列',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
      return
    }
    if (mode === 'rnaseq') {
      setResearchPlan(null)
      await submitToolJob(
        'research_plan',
        { task: rnaseqTask, domains: ['omics'], planner_mode: 'deterministic', inputs: buildRnaseqInputs() },
        'RNA-seq 工作流规划已进入队列',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
      return
    }
    if (mode === 'variant') {
      setResearchPlan(null)
      await submitToolJob(
        'research_plan',
        { task: variantTask, domains: ['omics', 'literature'], inputs: buildVariantInputs() },
        'Variant annotation plan queued for review',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
      return
    }
    if (mode === 'sequence') {
      setResearchPlan(null)
      await submitToolJob(
        'research_plan',
        {
          task: 'Design and validate an mRNA sequence for the provided protein',
          domains: ['sequence'],
          inputs: {
            protein,
            molecule: sequenceMolecule,
            method: sequenceMethod,
            include_benchmark: true,
            use_vaxpress: sequenceUseVaxpress,
            output_dir: 'output/frontend_sequence_research',
          },
        },
        'mRNA design plan queued for review',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
      return
    }
    if (mode === 'cadd') {
      setResearchPlan(null)
      await submitToolJob(
        'research_plan',
        {
          task: 'Run a reproducible CADD virtual screening workflow and prioritize docking hits',
          domains: ['cadd'],
          inputs: buildCaddInputs(),
        },
        'CADD screening plan queued for review',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
    }
  }

  async function executeResearchPlan() {
    const execution = researchPlan?.execution
    if (!researchPlan || !execution?.ready || !execution.workflow) return
    const outputDir = mode === 'rnaseq'
      ? 'output/frontend_rnaseq_custom'
      : mode === 'variant'
        ? 'output/frontend_variant_research'
        : mode === 'sequence'
          ? 'output/frontend_sequence_research'
          : mode === 'cadd'
            ? 'output/frontend_cadd_research'
            : 'output/frontend_auto_research'
    await submitToolJob(
      'research_execute',
      {
        workflow: execution.workflow,
        domains: researchPlan.selected_domains,
        output_path: `${outputDir}_manifest.json`,
        report_path: `${outputDir}_report.md`,
        dry_run: false,
        continue_on_error: false,
      },
      '已确认计划，研究工作流进入执行队列',
    )
  }

  async function submitOmicsDemo() {
    const controller = beginJobStream()
    setLoading(true)
    setError('')
    setEvents([])
    try {
      const response = await apiFetch<{ job: Job }>(apiBase, token, '/api/v1/jobs', {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({
          tool: 'omics_run_analysis',
          arguments: {
            expression_csv: 'examples/rnaseq/expression.csv',
            metadata_csv: 'examples/rnaseq/metadata.csv',
            gene_sets_csv: 'examples/rnaseq/gene_sets.csv',
            output_dir: 'output/frontend_rnaseq_demo',
            evidence_csv: 'examples/rnaseq/evidence.csv',
            evidence_provider: 'local',
          },
        }),
      })
      const job = response.job
      setSelectedJob(job)
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)])
      setEvents([{ at: formatTime(new Date().toISOString()), type: 'accepted', status: 'queued', detail: 'RNA-seq Agent 已进入执行队列' }])
      await followJob(apiBase, token, job.job_id, (type, data) => {
        if (!data.job) return
        setSelectedJob(data.job)
        setJobs((current) => [data.job!, ...current.filter((item) => item.job_id !== data.job!.job_id)])
        setEvents((current) => [...current, {
          at: formatTime(new Date().toISOString()),
          type,
          status: data.job!.status,
          detail: type === 'timeout' ? 'SSE 订阅超时，任务仍可通过列表查询' : `状态更新为${statusLabels[data.job!.status] || data.job!.status}`,
        }])
      }, controller.signal)
    } catch (err) {
      if (!isCurrentStream(controller) || (err instanceof Error && err.name === 'AbortError')) return
      setError(err instanceof Error ? err.message : 'RNA-seq Agent 执行失败')
    } finally {
      if (isCurrentStream(controller)) {
        streamController.current = null
        setLoading(false)
        void refresh()
      }
    }
  }

  async function submitBgiMultiomicsDemo() {
    setResearchPlan(null)
    await submitToolJob(
      'research_run_preset',
      {
        preset: 'bgi_multiomics_demo',
        dry_run: false,
        output_path: 'output/frontend_bgi_multiomics_demo/workflow_manifest.json',
        report_path: 'output/frontend_bgi_multiomics_demo/workflow_report.md',
        continue_on_error: false,
      },
      'BGI multi-omics demo queued',
    )
  }

  async function submitOnlineEvidenceDemo() {
    const geneIds = onlineEvidenceGenes.split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean).slice(0, 5)
    if (!geneIds.length) {
      setError('请输入至少一个基因 ID')
      return
    }
    setResearchPlan(null)
    await submitToolJob(
      'literature_search',
      {
        gene_ids: geneIds,
        provider: onlineEvidenceProvider,
        cache_dir: 'output/frontend_online_evidence',
      },
      `在线证据查询已提交：${providerLabels[onlineEvidenceProvider] || onlineEvidenceProvider}`,
    )
  }

  async function submitFastqRnaSeqDemo() {
    setMode('research')
    setResearchPlan(null)
    await submitToolJob(
      'research_plan',
      {
        task: 'Run FastQC, align paired-end RNA-seq reads, quantify, differential expression and pathway enrichment',
        domains: ['omics'],
        planner_mode: 'deterministic',
        inputs: {
          fastq_paths: [
            'examples/omics/rnaseq_fastq_fixture/A1.fastq',
            'examples/omics/rnaseq_fastq_fixture/A2.fastq',
            'examples/omics/rnaseq_fastq_fixture/A3.fastq',
            'examples/omics/rnaseq_fastq_fixture/B1.fastq',
            'examples/omics/rnaseq_fastq_fixture/B2.fastq',
            'examples/omics/rnaseq_fastq_fixture/B3.fastq',
          ],
          fastq_r2_paths: [
            'examples/omics/rnaseq_paired_fixture/A1_R2.fastq',
            'examples/omics/rnaseq_paired_fixture/A2_R2.fastq',
            'examples/omics/rnaseq_paired_fixture/A3_R2.fastq',
            'examples/omics/rnaseq_paired_fixture/B1_R2.fastq',
            'examples/omics/rnaseq_paired_fixture/B2_R2.fastq',
            'examples/omics/rnaseq_paired_fixture/B3_R2.fastq',
          ],
          reference_fasta: 'examples/omics/rnaseq_fastq_fixture/reference.fa',
          annotation_gtf: 'examples/omics/rnaseq_fastq_fixture/genes.gtf',
          metadata_csv: 'examples/omics/rnaseq_fastq_fixture/metadata.csv',
          gene_sets_csv: 'examples/omics/rnaseq_fastq_fixture/gene_sets.csv',
          statistics_backend: 'scipy',
          paired_end: true,
          output_dir: 'output/frontend_auto_research',
        },
      },
      'RNA-seq FastQC 质控和 paired-end 分析计划已进入队列',
      (job) => setResearchPlan(extractResearchPlan(job)),
    )
  }

  async function cancelSelectedJob() {
    if (!selectedJob || !['queued', 'running'].includes(selectedJob.status) || selectedJob.cancel_requested) return
    setError('')
    try {
      const response = await apiFetch<{ job: Job }>(apiBase, token, `/api/v1/jobs/${selectedJob.job_id}/cancel`, { method: 'POST' })
      setSelectedJob(response.job)
      setJobs((current) => [response.job, ...current.filter((item) => item.job_id !== response.job.job_id)])
      setEvents((current) => [...current, {
        at: formatTime(new Date().toISOString()),
        type: 'cancel',
        status: response.job.status,
        detail: response.job.status === 'cancelled' ? '任务已取消' : '已发送取消请求，等待执行线程退出',
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
      const response = await apiFetch<{ job: Job }>(apiBase, token, `/api/v1/jobs/${sourceJob.job_id}/retry`, { method: 'POST' })
      const job = response.job
      setSelectedJob(job)
      setJobs((current) => [job, ...current.filter((item) => item.job_id !== job.job_id)])
      setEvents([{ at: formatTime(new Date().toISOString()), type: 'retry', status: job.status, detail: `任务已重试，来源 ${formatJobId(sourceJob.job_id)}` }])
      await followJob(apiBase, token, job.job_id, (type, data) => {
        if (!data.job) return
        setSelectedJob(data.job)
        setJobs((current) => [data.job!, ...current.filter((item) => item.job_id !== data.job!.job_id)])
        setEvents((current) => [...current, {
          at: formatTime(new Date().toISOString()),
          type,
          status: data.job!.status,
          detail: type === 'timeout' ? 'SSE 订阅超时，任务仍可通过列表查询' : `状态更新为${statusLabels[data.job!.status] || data.job!.status}`,
        }])
      }, controller.signal)
    } catch (err) {
      if (!isCurrentStream(controller) || (err instanceof Error && err.name === 'AbortError')) return
      setError(err instanceof Error ? err.message : '任务重试失败')
    } finally {
      if (isCurrentStream(controller)) {
        streamController.current = null
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
      const url = window.URL.createObjectURL(blob)
      if (reportPreviewUrl.current) window.URL.revokeObjectURL(reportPreviewUrl.current)
      reportPreviewUrl.current = url
      setReportPreview({ url, filename })
    } catch (err) {
      setError(err instanceof Error ? err.message : '报告预览失败')
    }
  }

  function closeReportPreview() {
    if (reportPreviewUrl.current) window.URL.revokeObjectURL(reportPreviewUrl.current)
    reportPreviewUrl.current = null
    setReportPreview(null)
  }

  return (
    <div className="min-h-screen bg-[#071417] text-[#e4f1ed]">
      <div className="pointer-events-none fixed inset-0 opacity-70 [background-image:radial-gradient(circle_at_15%_10%,rgba(46,198,166,0.14),transparent_31%),radial-gradient(circle_at_85%_0%,rgba(105,134,255,0.12),transparent_28%)]" />
      <div className="relative mx-auto flex min-h-screen max-w-[1600px]">
        <aside className="hidden w-[248px] shrink-0 flex-col border-r border-white/10 bg-[#0a1a1d]/80 px-5 py-6 lg:flex">
          <div className="flex items-center gap-3 px-2">
            <div className="grid size-10 place-items-center rounded-2xl bg-[#a8f0d2] text-[#0a2625] shadow-[0_0_32px_rgba(168,240,210,0.25)]"><Dna size={22} /></div>
            <div>
              <div className="font-mono text-[10px] tracking-[0.24em] text-[#78a69c]">BIO / 0.3</div>
              <div className="text-sm font-semibold tracking-wide">Research OS</div>
            </div>
          </div>
          <div className="mt-12 px-2 font-mono text-[10px] tracking-[0.2em] text-[#5d817c]">CONTROL PLANE</div>
          <nav className="mt-3 space-y-1">
            <button onClick={() => setView('workspace')} className={`nav-item ${view === 'workspace' ? 'nav-item-active' : ''}`}><LayoutDashboard size={17} />工作台<span className="ml-auto font-mono text-[10px] opacity-50">01</span></button>
            <button onClick={() => setView('domains')} className={`nav-item ${view === 'domains' ? 'nav-item-active' : ''}`}><Boxes size={17} />领域与插件<span className="ml-auto font-mono text-[10px] opacity-50">06</span></button>
          </nav>
          <div className="mt-auto space-y-4">
            <div className="rounded-2xl border border-[#21443f] bg-[#0d2526] p-4">
              <div className="flex items-center gap-2 text-xs font-medium"><ShieldCheck size={15} className="text-[#83e3bc]" />安全连接</div>
              <div className="mt-3 flex items-center gap-2 font-mono text-[11px] text-[#7da09a]"><span className={`size-2 rounded-full ${connected ? 'bg-[#70e3ad]' : 'bg-[#dd876d]'}`} />{connected ? 'API ONLINE' : 'API OFFLINE'}</div>
              <div className="mt-1 truncate font-mono text-[10px] text-[#557570]">{apiBase || 'same-origin'}</div>
            </div>
            <div className="px-2 font-mono text-[10px] leading-5 text-[#557570]">Traceable by default.<br />Evidence over intuition.</div>
          </div>
        </aside>

        <main className="min-w-0 flex-1 px-5 py-5 sm:px-8 lg:px-10 lg:py-8">
          <header className="flex flex-wrap items-center justify-between gap-4 border-b border-white/10 pb-5">
            <div className="flex items-center gap-2 font-mono text-[11px] tracking-[0.16em] text-[#74918c]"><span className="text-[#a8f0d2]">PLATFORM</span><ChevronRight size={13} /><span>{view === 'workspace' ? 'WORKSPACE' : 'DOMAINS'}</span></div>
            <form onSubmit={(event) => { event.preventDefault(); saveToken() }} className="flex items-center gap-3">
              <div className="hidden items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 font-mono text-[10px] text-[#8aa9a2] sm:flex"><LockKeyhole size={12} />Bearer token</div>
              <input aria-label="API Token" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} type="password" className="w-32 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1.5 font-mono text-[10px] text-[#c7ded8] outline-none transition focus:border-[#72dcb4] sm:w-48" placeholder="本地可留空，生产请输入 Token" />
              <button type="submit" className="rounded-lg bg-[#a8f0d2] px-3 py-1.5 text-xs font-semibold text-[#092521] transition hover:bg-[#c6f8e1]">连接</button>
            </form>
          </header>

          {error && <div className="mt-5 flex items-center gap-3 rounded-xl border border-[#75483d] bg-[#2b1a1b] px-4 py-3 text-sm text-[#f5b7a4]"><XCircle size={16} />{error}<button onClick={() => setError('')} className="ml-auto text-xs underline">关闭</button></div>}

          {view === 'workspace' ? (
            <>
              <section className="grid gap-7 py-9 xl:grid-cols-[1fr_0.72fr] xl:items-end">
                <div>
                  <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-[#28524b] bg-[#102b2a] px-3 py-1.5 font-mono text-[10px] tracking-[0.16em] text-[#9ce3c6]"><Sparkles size={12} />RESEARCH CONTROL PLANE</div>
                  <h1 className="max-w-3xl text-4xl font-semibold leading-[1.08] tracking-[-0.04em] text-[#eff9f5] sm:text-6xl">把科学问题，变成一条<span className="text-[#8fe5c1]">可追踪的计算路径。</span></h1>
                  <p className="mt-5 max-w-2xl text-sm leading-7 text-[#88a6a0] sm:text-base">跨 CADD、Omics、Sequence 与证据检索的统一工作台。每个任务都有状态、来源和可复现的运行记录。</p>
                </div>
                <div className="grid grid-cols-3 gap-2 xl:pb-1">
                  <Metric label="ACTIVE DOMAINS" value={String(activeDomains).padStart(2, '0')} icon={<GitBranch size={14} />} />
                  <Metric label="AVAILABLE TOOLS" value={String(toolCount).padStart(2, '0')} icon={<Terminal size={14} />} />
                  <Metric label="LIVE RUNS" value={String(runningJobs).padStart(2, '0')} icon={<Radio size={14} />} />
                </div>
              </section>
              <details data-platform-surfaces className="group mb-5 rounded-2xl border border-white/[0.08] bg-[#0b1b1e]/75">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-5 py-4 outline-none focus-visible:ring-2 focus-visible:ring-[#8fe5c1] focus-visible:ring-inset">
                  <div className="flex items-center gap-3"><ChevronRight size={16} className="transition group-open:rotate-90" /><div><div className="eyebrow">PLATFORM SURFACES</div><div className="mt-1 text-sm text-[#9bb7b0]">REST、SSE、MCP、A2A 等集成能力</div></div></div>
                  <span className="status-badge status-ok">SECONDARY</span>
                </summary>
                <div className="px-5 pb-1"><CapabilityStrip capabilities={capabilities} /></div>
              </details>
              <details data-interview-shortcuts className="group mb-5 rounded-2xl border border-[#3f527c] bg-[#0d192b]/85">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-5 py-4 outline-none focus-visible:ring-2 focus-visible:ring-[#aebfff] focus-visible:ring-inset">
                  <div className="flex items-center gap-3"><ChevronRight size={16} className="transition group-open:rotate-90" /><div><div className="eyebrow text-[#9cb9ff]">INTERVIEW SHORTCUTS</div><div className="mt-1 text-sm text-[#b9c8e8]">预置科研场景，适合面试快速演示</div></div></div>
                  <span className="status-badge border-[#3f527c] bg-[#111d32] text-[#aebfff]">OPTIONAL</span>
                </summary>
                <div className="border-t border-white/[0.08] px-3 pt-3 sm:px-5">
              <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#3f527c] bg-[#111d32]/80 px-5 py-4"><div><div className="font-mono text-[10px] tracking-[0.16em] text-[#9cb9ff]">INTERVIEW DEMO / BGI MULTI-OMICS</div><div className="mt-1 text-sm text-[#b9c8e8]">Genomics QC → 10x single-cell → microbiome → evidence → mRNA</div></div><button aria-label="Run BGI multi-omics demo" onClick={() => void submitBgiMultiomicsDemo()} disabled={loading} className="rounded-lg bg-[#aebfff] px-3 py-2 text-xs font-semibold text-[#111a34] transition hover:bg-[#c4d0ff] disabled:cursor-not-allowed disabled:opacity-50">Run BGI Demo</button></div>
              <OnlineEvidenceDemoCard provider={onlineEvidenceProvider} genes={onlineEvidenceGenes} loading={loading} onProviderChange={setOnlineEvidenceProvider} onGenesChange={setOnlineEvidenceGenes} onRun={() => void submitOnlineEvidenceDemo()} />

              <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#28524b] bg-[#102b2a]/70 px-5 py-4"><div><div className="font-mono text-[10px] tracking-[0.16em] text-[#8fe5c1]">INTERVIEW DEMO / RNA-SEQ AGENT</div><div className="mt-1 text-sm text-[#b4cdc6]">差异表达 → 通路富集 → 基因证据 → 可追溯报告</div></div><button onClick={() => void submitOmicsDemo()} disabled={loading} className="rounded-lg bg-[#8fe5c1] px-3 py-2 text-xs font-semibold text-[#092521] transition hover:bg-[#b8f4d8] disabled:cursor-not-allowed disabled:opacity-50">运行 RNA-seq Agent</button></div>

              <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#3d5a8c] bg-[#111d32]/80 px-5 py-4"><div><div className="font-mono text-[10px] tracking-[0.16em] text-[#aebfff]">INTERVIEW DEMO / NATIVE RNA-SEQ</div><div className="mt-1 text-sm text-[#c3d1f4]">FastQC → MultiQC → HISAT2 → featureCounts → 差异分析</div></div><button aria-label="Run native RNA-seq demo" onClick={() => void submitFastqRnaSeqDemo()} disabled={loading} className="rounded-lg bg-[#aebfff] px-3 py-2 text-xs font-semibold text-[#111a34] transition hover:bg-[#cbd4ff] disabled:cursor-not-allowed disabled:opacity-50">运行真实 RNA-seq 流程</button></div>

                </div>
              </details>

              {selectedJob && ['queued', 'running'].includes(selectedJob.status) && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#5c4930] bg-[#211d16] px-5 py-4"><div className="flex items-center gap-3"><Ban size={16} className="text-[#e6c875]" /><div><div className="text-sm font-medium text-[#f1dfaa]">任务控制</div><div className="mt-1 text-xs text-[#aa9767]">排队中的任务会立即取消，运行中的任务采用协作式取消。</div></div></div><button onClick={() => void cancelSelectedJob()} disabled={selectedJob.cancel_requested} className="rounded-lg border border-[#80643c] px-3 py-2 text-xs font-medium text-[#f1d889] transition hover:bg-[#392d1c] disabled:cursor-not-allowed disabled:opacity-50">{selectedJob.cancel_requested ? '取消请求已发送' : '取消任务'}</button></div>}
              {selectedJob && ['failed', 'cancelled'].includes(selectedJob.status) && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#3f527c] bg-[#111d32] px-5 py-4"><div className="flex items-center gap-3"><RefreshCw size={16} className="text-[#aebfff]" /><div><div className="text-sm font-medium text-[#d7ddff]">任务恢复</div><div className="mt-1 text-xs text-[#99a6cf]">保留原任务记录，复制原始参数重新提交。</div></div></div><button aria-label="Retry selected task" onClick={() => void retryJob(selectedJob)} disabled={loading} className="rounded-lg bg-[#aebfff] px-3 py-2 text-xs font-semibold text-[#111a34] transition hover:bg-[#c4d0ff] disabled:cursor-not-allowed disabled:opacity-50">重试任务</button></div>}

              <section className="grid gap-5 xl:grid-cols-[1.08fr_0.92fr]">
                <div className="panel p-5 sm:p-6">
                  <div className="flex items-start justify-between gap-4"><div><div className="eyebrow">01 / START A RUN</div><h2 className="mt-2 text-xl font-semibold">启动一条研究路径</h2></div><div className="rounded-xl border border-[#21443f] bg-[#102b2a] p-2.5 text-[#8fe5c1]"><Play size={17} /></div></div>
                  <div className="mt-7 grid grid-cols-2 gap-1 rounded-xl bg-[#071719] p-1 sm:grid-cols-5"><button onClick={() => setMode('research')} className={`mode-tab ${mode === 'research' ? 'mode-tab-active' : ''}`}><Workflow size={14} />研究规划</button><button onClick={() => setMode('rnaseq')} className={`mode-tab ${mode === 'rnaseq' ? 'mode-tab-active' : ''}`}><Activity size={14} />RNA-seq 上传</button><button onClick={() => setMode('variant')} className={`mode-tab ${mode === 'variant' ? 'mode-tab-active' : ''}`}><GitBranch size={14} />VCF 变异</button><button onClick={() => setMode('sequence')} className={`mode-tab ${mode === 'sequence' ? 'mode-tab-active' : ''}`}><Dna size={14} />mRNA 设计</button><button onClick={() => setMode('cadd')} className={`mode-tab ${mode === 'cadd' ? 'mode-tab-active' : ''}`}><Beaker size={14} />CADD 对接</button></div>
                  {mode === 'research' ? <>
                    <label className="mt-6 block"><span className="field-label">科学问题</span><textarea value={task} onChange={(event) => { setTask(event.target.value); setResearchPlan(null) }} rows={4} className="input-area" placeholder="描述你希望 Agent 协助完成的研究任务" /></label>
                    <div className="mt-5 grid gap-4 sm:grid-cols-[0.8fr_1.2fr]"><div><label className="field-label" htmlFor="planner-mode">Planner 模式</label><select id="planner-mode" value={plannerMode} onChange={(event) => { setPlannerMode(event.target.value as PlannerMode); setResearchPlan(null) }} className="input-control"><option value="auto">Auto：有 Key 用 LLM</option><option value="deterministic">Deterministic：规则规划</option><option value="llm">LLM：必须调用模型</option></select></div><div className="flex items-end pb-1 text-xs leading-5 text-[#688983]">Auto 会在配置模型密钥时调用 LLM；模型不可用时保留 fallback 原因并回退到确定性规划。</div></div>
                    <div className="mt-5 grid gap-4 sm:grid-cols-3">
                      <div><label className="field-label" htmlFor="protein-context">蛋白输入上下文</label><input id="protein-context" value={protein} onChange={(event) => { setProtein(event.target.value.toUpperCase()); setResearchPlan(null) }} className="input-control font-mono tracking-[0.18em]" placeholder="例如 MKT" /></div>
                      <div><label className="field-label" htmlFor="gene-ids-context">基因 ID（可选）</label><input id="gene-ids-context" value={geneIds} onChange={(event) => { setGeneIds(event.target.value); setResearchPlan(null) }} className="input-control font-mono" placeholder="例如 TP53, BRCA1" /><span className="mt-2 block text-[10px] leading-5 text-[#688983]">文献或在线证据任务会使用这里的基因 ID。</span></div>
                      <div><label className="field-label" htmlFor="evidence-provider">证据源</label><select id="evidence-provider" value={evidenceProvider} onChange={(event) => { setEvidenceProvider(event.target.value); setResearchPlan(null) }} className="input-control"><option value="local">本地证据</option><option value="kegg">KEGG</option><option value="ncbi_gene">NCBI Gene</option><option value="pubmed">PubMed</option><option value="uniprot">UniProt</option></select></div>
                    </div>
                    <div className="mt-5 grid gap-3 sm:grid-cols-3">
                      <ResearchFileField id="expression-file" label="表达矩阵 CSV" file={uploadedFiles.expression} uploading={uploadingFile === 'expression'} onChange={(file) => void handleResearchFileUpload('expression', file)} />
                      <ResearchFileField id="metadata-file" label="样本元数据 CSV" file={uploadedFiles.metadata} uploading={uploadingFile === 'metadata'} onChange={(file) => void handleResearchFileUpload('metadata', file)} />
                      <ResearchFileField id="gene-sets-file" label="基因集 CSV" file={uploadedFiles.gene_sets} uploading={uploadingFile === 'gene_sets'} onChange={(file) => void handleResearchFileUpload('gene_sets', file)} />
                    </div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">上传文件会在服务端校验、计算 SHA-256 并保存到本次研究输入目录；未上传的字段使用仓库示例数据。</p>
                  </> : mode === 'rnaseq' ? <>
                     <label className="mt-6 block"><span className="field-label">RNA-seq 工作流任务</span><textarea value={rnaseqTask} onChange={(event) => { setRnaseqTask(event.target.value); setResearchPlan(null) }} rows={3} className="input-area" placeholder="例如：Run FastQC and align paired-end RNA-seq reads" /></label>
                     <div className="mt-5 grid gap-3 sm:grid-cols-2">
                       <RnaFileField id="rna-r1-files" label="R1 FASTQ（可多选）" files={rnaFiles.fastq_r1} multiple accept=".fastq,.fq,.fastq.gz,.fq.gz,application/gzip,text/plain" uploading={uploadingRnaFile === 'fastq_r1'} onChange={(files) => void handleRnaFileUpload('fastq_r1', files)} />
                       <RnaFileField id="rna-r2-files" label="R2 FASTQ（可多选）" files={rnaFiles.fastq_r2} multiple accept=".fastq,.fq,.fastq.gz,.fq.gz,application/gzip,text/plain" uploading={uploadingRnaFile === 'fastq_r2'} onChange={(files) => void handleRnaFileUpload('fastq_r2', files)} />
                       <RnaFileField id="rna-reference-file" label="参考基因组 FASTA" files={rnaFiles.reference_fasta} accept=".fa,.fasta,.fna,text/plain" uploading={uploadingRnaFile === 'reference_fasta'} onChange={(files) => void handleRnaFileUpload('reference_fasta', files)} />
                       <RnaFileField id="rna-gtf-file" label="基因注释 GTF" files={rnaFiles.annotation_gtf} accept=".gtf,.gff,.gff3,text/plain" uploading={uploadingRnaFile === 'annotation_gtf'} onChange={(files) => void handleRnaFileUpload('annotation_gtf', files)} />
                       <RnaFileField id="rna-metadata-file" label="样本 metadata CSV（可选）" files={rnaFiles.metadata} accept=".csv,.tsv,text/csv,text/tab-separated-values" uploading={uploadingRnaFile === 'metadata'} onChange={(files) => void handleRnaFileUpload('metadata', files)} />
                       <RnaFileField id="rna-gene-sets-file" label="gene sets CSV（可选）" files={rnaFiles.gene_sets} accept=".csv,.tsv,text/csv,text/tab-separated-values" uploading={uploadingRnaFile === 'gene_sets'} onChange={(files) => void handleRnaFileUpload('gene_sets', files)} />
                     </div>
                     <RnaPreflightCard items={rnaseqPreflight.checks} pairMismatch={rnaseqPreflight.pairMismatch} />
                     <p className="mt-3 text-xs leading-5 text-[#688983]">R1/R2 可批量选择；Planner 会根据任务文本检查输入，并决定是否继续比对、featureCounts、差异分析和富集。</p>
                  </> : mode === 'variant' ? <>
                    <label className="mt-6 block"><span className="field-label">Variant research task</span><textarea value={variantTask} onChange={(event) => { setVariantTask(event.target.value); setResearchPlan(null) }} rows={3} className="input-area" placeholder="Describe the VCF annotation and evidence task" /></label>
                    <div className="mt-5 grid gap-3 sm:grid-cols-2">
                      <ResearchFileField id="vcf-file" label="VCF / VCF.GZ input" accept=".vcf,.gz,text/plain" file={uploadedFiles.vcf} uploading={uploadingFile === 'vcf'} onChange={(file) => void handleResearchFileUpload('vcf', file)} />
                      <ResearchFileField id="annotation-file" label="Gene interval CSV" accept=".csv,.tsv,text/csv,text/tab-separated-values" file={uploadedFiles.annotation} uploading={uploadingFile === 'annotation'} onChange={(file) => void handleResearchFileUpload('annotation', file)} />
                    </div>
                    <div className="mt-5 grid gap-4 sm:grid-cols-2"><div><label className="field-label" htmlFor="variant-backend">Annotation backend</label><select id="variant-backend" value={variantBackend} onChange={(event) => { setVariantBackend(event.target.value); setResearchPlan(null) }} className="input-control"><option value="auto">Auto: VCF ANN → local interval</option><option value="vcf_ann">VCF ANN only</option><option value="local">Local interval table</option></select></div><div><label className="field-label" htmlFor="variant-evidence-provider">Evidence provider</label><select id="variant-evidence-provider" value={evidenceProvider} onChange={(event) => { setEvidenceProvider(event.target.value); setResearchPlan(null) }} className="input-control"><option value="local">Local fixture</option><option value="ncbi_gene">NCBI Gene</option><option value="uniprot">UniProt</option><option value="pubmed">PubMed</option><option value="kegg">KEGG</option></select></div></div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">The demo uses reproducible fixtures when no files are uploaded. Results retain annotation source and external tool availability.</p>
                  </> : mode === 'sequence' ? <SequenceDesignInput protein={protein} molecule={sequenceMolecule} method={sequenceMethod} useVaxpress={sequenceUseVaxpress} structureId={sequenceStructureId} onProteinChange={(value) => { setProtein(value); setResearchPlan(null) }} onMoleculeChange={(value) => { setSequenceMolecule(value); setResearchPlan(null) }} onMethodChange={(value) => { setSequenceMethod(value); setResearchPlan(null) }} onUseVaxpressChange={(value) => { setSequenceUseVaxpress(value); setResearchPlan(null) }} onStructureChange={setSequenceStructureId} /> : <>
                    <label className="mt-6 block"><span className="field-label">CADD screening task</span><textarea value="Run a reproducible CADD virtual screening workflow and prioritize docking hits" readOnly rows={3} className="input-area" /></label>
                    <div className="mt-5 grid gap-3 sm:grid-cols-2"><ResearchFileField id="receptor-file" label="受体结构 PDB / PDBQT" accept=".pdb,.pdbqt,text/plain" file={uploadedFiles.receptor} uploading={uploadingFile === 'receptor'} onChange={(file) => void handleResearchFileUpload('receptor', file)} /><ResearchFileField id="ligand-library-file" label="外部分子数据集 CSV" accept=".csv,.tsv,text/csv,text/tab-separated-values" file={uploadedFiles.ligand_library} uploading={uploadingFile === 'ligand_library'} onChange={(file) => void handleResearchFileUpload('ligand_library', file)} /></div>
                    <div className="mt-5 grid gap-4 sm:grid-cols-2"><div><label className="field-label" htmlFor="cadd-max-ligands">演示候选数</label><input id="cadd-max-ligands" type="number" min="1" max="17" value={caddMaxLigands} onChange={(event) => { setCaddMaxLigands(event.target.value); setResearchPlan(null) }} className="input-control font-mono" /><span className="mt-2 block text-xs text-[#688983]">完整筛选可调到 17，演示建议 3。</span></div><div><label className="field-label" htmlFor="cadd-exhaustiveness">Vina exhaustiveness</label><input id="cadd-exhaustiveness" type="number" min="1" max="32" value={caddExhaustiveness} onChange={(event) => { setCaddExhaustiveness(event.target.value); setResearchPlan(null) }} className="input-control font-mono" /><span className="mt-2 block text-xs text-[#688983]">数值越高越稳定，但运行时间更长。</span></div></div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">CADD 入口会记录受体、数据集、Vina 参数与结果报告。Docker 优先读取本机挂载的 data/4hjo.pdb 和 output/bindingdb_egfr_10000.csv，也支持上传替换。</p>
                  </>}
                  <div className="mt-6 flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2 font-mono text-[10px] text-[#66847e]"><CircleDot size={13} className="text-[#70e3ad]" />ASYNC / TRACEABLE / REPLAYABLE</div><button onClick={submitRun} disabled={loading || (mode === 'research' ? !task.trim() : mode === 'rnaseq' ? !rnaseqTask.trim() || rnaseqPreflight.pairMismatch : mode === 'variant' ? !variantTask.trim() : mode === 'sequence' ? !protein.trim() : false)} className="group inline-flex items-center gap-2 rounded-xl bg-[#a8f0d2] px-4 py-2.5 text-sm font-semibold text-[#092521] transition hover:bg-[#c6f8e1] disabled:cursor-not-allowed disabled:opacity-50">{loading ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />}{loading ? '执行中…' : '开始运行'}<ArrowUpRight size={14} className="transition group-hover:translate-x-0.5 group-hover:-translate-y-0.5" /></button></div>
                </div>

                <div className="panel flex min-h-[326px] flex-col p-5 sm:p-6"><div className="flex items-start justify-between"><div><div className="eyebrow">02 / EXECUTION STREAM</div><h2 className="mt-2 text-xl font-semibold">实时执行轨迹</h2></div><div className="flex items-center gap-1.5 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#8fe5c1]"><span className="size-1.5 animate-pulse rounded-full bg-[#70e3ad]" />SSE</div></div>{selectedJob ? <div className="mt-7 flex flex-1 flex-col"><div className="flex items-center justify-between border-b border-white/10 pb-4"><div><div className="font-mono text-[11px] text-[#6f9189]">{formatJobId(selectedJob.job_id)}</div><div className="mt-1 text-sm font-medium">{selectedJob.tool}</div></div><StatusBadge status={selectedJob.status} /></div><div className="mt-5 space-y-3">{events.slice(-4).map((event, index) => <div key={`${event.at}-${index}`} className="flex items-start gap-3 text-xs"><div className="mt-1.5 size-1.5 rounded-full bg-[#83e3bc] shadow-[0_0_12px_#83e3bc]" /><div className="min-w-0 flex-1"><div className="text-[#b2cbc4]">{event.detail}</div><div className="mt-1 font-mono text-[10px] text-[#5f7c76]">{event.at} · {event.status}</div></div></div>)}</div><div className="mt-auto flex items-center gap-2 pt-5 font-mono text-[10px] text-[#64827b]"><Clock3 size={13} />{selectedJob.status === 'completed' ? `完成于 ${formatTime(selectedJob.finished_at)}` : '等待状态更新…'}</div></div> : <EmptyStream />}</div>
              </section>

              {selectedJob?.status === 'completed' && <JobResultSummary job={selectedJob} structureId={sequenceStructureId} onDownload={(path) => void downloadJobArtifact(selectedJob.job_id, path)} onOpenReport={(path) => void previewJobArtifact(selectedJob.job_id, path)} />}
              {reportPreview && <ReportPreviewModal preview={reportPreview} onClose={closeReportPreview} />}

              {(mode === 'research' || mode === 'rnaseq' || mode === 'variant' || mode === 'sequence' || mode === 'cadd') && selectedJob?.tool !== 'research_execute' && <ResearchPlanCard plan={researchPlan} loading={loading && selectedJob?.tool === 'research_plan'} onExecute={() => void executeResearchPlan()} />}

              <section className="panel mt-5 overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-5 sm:px-6"><div><div className="eyebrow">03 / RECENT RUNS</div><h2 className="mt-2 text-xl font-semibold">最近任务</h2></div><button onClick={() => void refresh()} className="inline-flex items-center gap-2 rounded-lg border border-white/10 px-3 py-2 text-xs text-[#9bb7b0] transition hover:border-[#4f8c7d] hover:text-[#d6eee7]"><RefreshCw size={13} />刷新</button></div>{jobs.length ? <div className="overflow-x-auto"><table className="w-full min-w-[680px] text-left text-sm"><thead className="bg-white/[0.025] font-mono text-[10px] tracking-[0.12em] text-[#63817b]"><tr><th className="px-5 py-3 font-normal sm:px-6">TASK ID</th><th className="px-5 py-3 font-normal">TOOL</th><th className="px-5 py-3 font-normal">STATUS</th><th className="px-5 py-3 font-normal">CREATED</th><th className="px-5 py-3 font-normal" /></tr></thead><tbody>{jobs.map((job) => <tr key={job.job_id} onClick={() => { setSelectedJob(job); setEvents([]) }} className="cursor-pointer border-t border-white/[0.06] transition hover:bg-white/[0.035]"><td className="px-5 py-4 font-mono text-xs text-[#81aaa1] sm:px-6">{formatJobId(job.job_id)}</td><td className="px-5 py-4 font-medium text-[#c7ddd7]">{job.tool}</td><td className="px-5 py-4"><StatusBadge status={job.status} /></td><td className="px-5 py-4 font-mono text-xs text-[#66837d]">{formatTime(job.created_at)}</td><td className="px-5 py-4 text-right text-[#6b8f87]"><ChevronRight size={15} /></td></tr>)}</tbody></table></div> : <div className="px-6 py-12 text-center text-sm text-[#66837d]">还没有运行记录，先启动一条研究路径。</div>}</section>
            </>
          ) : <DomainsView plugins={plugins} />}
        </main>
      </div>
    </div>
  )
}

function CapabilityStrip({ capabilities }: { capabilities: Capabilities | null }) {
  if (!capabilities) return null
  const cards = [
    { key: 'rest', label: 'REST / OPENAPI', icon: Server, detail: capabilities.interfaces.rest?.openapi || '/openapi.json' },
    { key: 'sse', label: 'SSE EVENTS', icon: Radio, detail: capabilities.interfaces.sse?.endpoint || 'job event stream' },
    { key: 'mcp', label: 'MCP / STDIO', icon: Terminal, detail: `${capabilities.interfaces.mcp?.tool_count || capabilities.tool_count} tools` },
    { key: 'embedded', label: 'EMBEDDED CALL', icon: Boxes, detail: capabilities.interfaces.embedded?.entrypoint || 'run_tool' },
    { key: 'a2a', label: 'A2A / JSON-RPC', icon: GitBranch, detail: capabilities.interfaces.a2a?.endpoint || '/a2a' },
  ]
  return <section className="mb-5" aria-label="Integration surfaces"><div className="mb-2 flex items-center justify-between"><div className="eyebrow">INTEGRATION SURFACES</div><div className="font-mono text-[10px] text-[#66857e]">{capabilities.tool_count} CONTRACTED TOOLS</div></div><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">{cards.map((card) => { const capability = capabilities.interfaces[card.key]; const Icon = card.icon; const available = capability?.status === 'available'; return <div key={card.key} className="rounded-xl border border-white/[0.08] bg-white/[0.035] px-3 py-3"><div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-xs font-medium text-[#c9e5dc]"><Icon size={14} className="text-[#8fe5c1]" />{card.label}</div><span className={`status-badge ${available ? 'status-ok' : 'status-failed'}`}>{available ? 'READY' : capability?.status || 'UNKNOWN'}</span></div><div className="mt-2 truncate font-mono text-[9px] text-[#66857e]" title={card.detail}>{card.detail}</div></div> })}</div></section>
}

function OnlineEvidenceDemoCard({ provider, genes, loading, onProviderChange, onGenesChange, onRun }: { provider: string; genes: string; loading: boolean; onProviderChange: (value: string) => void; onGenesChange: (value: string) => void; onRun: () => void }) {
  return <section className="mb-5 flex flex-wrap items-end gap-4 rounded-2xl border border-[#28524b] bg-[#102b2a]/70 px-5 py-4"><div className="min-w-[220px] flex-1"><div className="font-mono text-[10px] tracking-[0.16em] text-[#8fe5c1]">ONLINE EVIDENCE / PROVIDER SWITCH</div><div className="mt-1 text-sm text-[#b4cdc6]">用同一 Agent 契约切换 UniProt、NCBI Gene、PubMed 或 KEGG，并在结果区展示真实来源</div></div><div className="grid w-full gap-2 sm:w-auto sm:grid-cols-[150px_190px_auto]"><label className="sr-only" htmlFor="online-evidence-provider">Online evidence provider</label><select id="online-evidence-provider" value={provider} onChange={(event) => onProviderChange(event.target.value)} className="input-control"><option value="uniprot">UniProt</option><option value="ncbi_gene">NCBI Gene</option><option value="pubmed">PubMed</option><option value="kegg">KEGG</option></select><label className="sr-only" htmlFor="online-evidence-genes">Gene IDs</label><input id="online-evidence-genes" value={genes} onChange={(event) => onGenesChange(event.target.value)} className="input-control font-mono" placeholder="TP53, BRCA1" /><button onClick={onRun} disabled={loading} className="rounded-lg bg-[#8fe5c1] px-3 py-2 text-xs font-semibold text-[#092521] transition hover:bg-[#b8f4d8] disabled:cursor-not-allowed disabled:opacity-50">查询在线证据</button></div></section>
}

function Metric({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) {
  return <div className="rounded-2xl border border-white/10 bg-white/[0.035] p-3 sm:p-4"><div className="flex items-center gap-2 text-[#6d9189]">{icon}<span className="font-mono text-[9px] tracking-[0.12em]">{label}</span></div><div className="mt-3 font-mono text-2xl text-[#d9f3eb]">{value}</div></div>
}

function QcStatusMetric({ label, value, className }: { label: string; value: unknown; className: string }) {
  return <div className={`rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-2 ${className}`}><div className="font-mono text-[9px] tracking-[0.12em]">{label}</div><div className="mt-1 font-mono text-lg text-[#e4f1ed]">{typeof value === 'number' ? value : String(value ?? 0)}</div></div>
}

function PipelineMetric({ label, value }: { label: string; value: unknown }) {
  return <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">{label}</div><div className="mt-1 font-mono text-lg text-[#e4f1ed]">{String(value)}</div></div>
}

function StatusBadge({ status }: { status: string }) {
  const style = status === 'completed' ? 'status-ok' : status === 'failed' || status === 'cancelled' ? 'status-failed' : status === 'running' ? 'status-running' : 'status-queued'
  return <span className={`status-badge ${style}`}><span className="size-1.5 rounded-full bg-current" />{status === 'cancelled' ? '已取消' : statusLabels[status] || status}</span>
}

function ResearchFileField({ id, label, accept = '.csv,.tsv,text/csv,text/tab-separated-values', file, uploading, onChange }: { id: string; label: string; accept?: string; file: UploadedFile | null; uploading: boolean; onChange: (file?: File) => void }) {
  return <div>
    <div className="field-label">{label}</div>
    <label htmlFor={id} className="flex min-h-[76px] cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed border-[#315d55] bg-[#071719]/70 px-3 py-3 transition hover:border-[#71cba7] hover:bg-[#102b2a]">
      <input id={id} type="file" accept={accept} className="sr-only" onChange={(event) => { onChange(event.target.files?.[0]); event.currentTarget.value = '' }} />
      <div className="min-w-0"><div className="truncate text-xs font-medium text-[#b8d8ce]">{uploading ? '上传中…' : file?.filename || '选择输入文件'}</div><div className="mt-1 truncate font-mono text-[9px] text-[#668983]">{file ? `${file.size_bytes} bytes · ${file.sha256.slice(0, 12)}` : '服务端安全存储'}</div></div>
      {uploading ? <RefreshCw size={15} className="shrink-0 animate-spin text-[#8fe5c1]" /> : <Upload size={15} className="shrink-0 text-[#78cdaa]" />}
    </label>
  </div>
}

function RnaFileField({ id, label, accept, files, multiple = false, uploading, onChange }: { id: string; label: string; accept?: string; files: UploadedFile[]; multiple?: boolean; uploading: boolean; onChange: (files: FileList | null) => void }) {
  return <div>
    <div className="field-label">{label}</div>
    <label htmlFor={id} className="flex min-h-[88px] cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed border-[#315d55] bg-[#071719]/70 px-3 py-3 transition hover:border-[#71cba7] hover:bg-[#102b2a]">
      <input id={id} type="file" accept={accept} multiple={multiple} className="sr-only" onChange={(event) => { onChange(event.target.files); event.currentTarget.value = '' }} />
      <div className="min-w-0"><div className="truncate text-xs font-medium text-[#b8d8ce]">{uploading ? '上传中…' : files.length ? `${files.length} 个文件已选择` : '选择输入文件'}</div><div className="mt-1 truncate font-mono text-[9px] text-[#668983]">{files.length ? files.map((file) => file.filename).join(', ') : '服务端安全存储并计算 SHA-256'}</div></div>
      {uploading ? <RefreshCw size={15} className="shrink-0 animate-spin text-[#8fe5c1]" /> : <Upload size={15} className="shrink-0 text-[#78cdaa]" />}
    </label>
  </div>
}

function RnaPreflightCard({ items, pairMismatch }: { items: RnaPreflightItem[]; pairMismatch: boolean }) {
  const requiredCount = items.filter((item) => item.required).length
  const readyRequiredCount = items.filter((item) => item.required && item.ready).length
  const allRequiredReady = !pairMismatch && readyRequiredCount === requiredCount
  return <div className="mt-5 rounded-xl border border-[#244b45] bg-[#0a211f]/75 p-4" role="status" aria-live="polite">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="field-label">RUN PREFLIGHT</div><div className="mt-1 text-xs text-[#9bc3b8]">{readyRequiredCount}/{requiredCount} 个任务必需输入已满足</div></div><span className={`status-badge ${pairMismatch ? 'status-failed' : allRequiredReady ? 'status-ok' : 'status-running'}`}><span className="size-1.5 rounded-full bg-current" />{pairMismatch ? '配对数量不一致' : allRequiredReady ? '输入已就绪' : '待补齐输入'}</span></div>
    <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{items.map((item) => <div key={item.label} className="flex min-w-0 items-start gap-2 rounded-lg border border-white/[0.06] bg-[#071719]/70 px-2.5 py-2"><div className={`mt-0.5 shrink-0 ${item.ready ? 'text-[#70e3ad]' : item.required ? 'text-[#e6c875]' : 'text-[#6d8d86]'}`}>{item.ready ? <Check size={13} /> : <XCircle size={13} />}</div><div className="min-w-0"><div className="truncate text-[11px] font-medium text-[#b8d8ce]">{item.label}{item.required ? <span className="ml-1 text-[#e6c875]">必需</span> : <span className="ml-1 text-[#688983]">可选</span>}</div><div className="mt-0.5 truncate text-[10px] text-[#6f9189]">{item.detail}</div></div></div>)}</div>
  </div>
}

function ResearchPlanCard({ plan, loading, onExecute }: { plan: ResearchPlan | null; loading: boolean; onExecute: () => void }) {
  const execution = plan?.execution
  return <section className="panel mt-5 overflow-hidden" aria-live="polite">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-5 sm:px-6">
      <div><div className="eyebrow">02B / PLAN REVIEW</div><h2 className="mt-2 text-xl font-semibold">执行前计划检查</h2></div>
      <div className="flex items-center gap-2 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#8fe5c1]"><Workflow size={12} />HUMAN CONFIRMATION</div>
    </div>
    {!plan ? <div className="flex items-center gap-4 px-5 py-8 text-sm text-[#789791] sm:px-6"><div className="grid size-10 place-items-center rounded-xl border border-[#21443f] bg-[#102b2a] text-[#78cdaa]">{loading ? <RefreshCw size={17} className="animate-spin" /> : <Sparkles size={17} />}</div><div><div className="font-medium text-[#b7d3ca]">{loading ? 'Planner 正在检查任务…' : '提交科研问题后，这里会出现执行计划。'}</div><div className="mt-1 text-xs text-[#66857e]">计划会先展示领域、证据源、工具链和输入门槛。</div></div></div> : <div className="space-y-5 px-5 py-5 sm:px-6">
      <div className="flex flex-wrap items-center gap-2">
        {plan.selected_domains.map((domain) => <span key={domain} className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />{domainLabels[domain] || domain}</span>)}
        <span className="status-badge status-running">证据：{providerLabels[execution?.evidence_provider || plan.evidence_provider] || execution?.evidence_provider}</span>
        {plan.planner && <span className="status-badge">规划器：{plan.planner.backend === 'llm' ? 'LLM' : plan.planner.backend === 'deterministic' ? 'Deterministic' : plan.planner.backend}</span>}
        {plan.planner?.model && <span className="status-badge">Model: {plan.planner.model}</span>}
      </div>
      <div className="grid gap-4 lg:grid-cols-[0.7fr_1.3fr]">
        <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4">
          <div className="field-label">INPUT GATE</div>
          {execution?.ready ? <div className="flex items-center gap-2 text-sm text-[#9be6c5]"><Check size={15} />输入已满足，可执行</div> : <div className="text-sm text-[#efb19f]">缺少必要输入</div>}
          {!execution?.ready && <div className="mt-3 flex flex-wrap gap-1.5">{(execution?.missing_inputs || []).map((item) => <span key={item} className="rounded-md border border-[#70483f] bg-[#2b1b1b] px-2 py-1 font-mono text-[10px] text-[#e9a694]">{item}</span>)}</div>}
          {execution?.rationale?.length ? <div className="mt-4 space-y-2 text-xs leading-5 text-[#789791]">{execution.rationale.map((item) => <div key={item} className="flex gap-2"><span className="mt-2 size-1 rounded-full bg-[#78cdaa]" />{item}</div>)}</div> : null}
          {plan.planner?.fallback_reason && <div className="mt-4 rounded-lg border border-[#705b35] bg-[#251f15] px-3 py-2 text-xs leading-5 text-[#d8c18a]">Planner fallback：{plan.planner.fallback_reason}</div>}
        </div>
        <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4">
          <div className="field-label">SELECTED TOOLCHAIN</div>
          <div className="flex flex-wrap gap-2">{(execution?.selected_tools || []).map((tool, index) => <div key={`${tool}-${index}`} className="inline-flex items-center gap-2 rounded-lg border border-[#28524b] bg-[#102b2a] px-2.5 py-2 font-mono text-[10px] text-[#b9e6d5]"><span className="grid size-4 place-items-center rounded-full bg-[#8fe5c1] text-[9px] font-bold text-[#092521]">{index + 1}</span>{tool}</div>)}</div>
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/[0.08] pt-4"><div className="text-xs text-[#66857e]">规划任务：<span className="text-[#aac8bf]">{plan.task}</span></div><button onClick={onExecute} disabled={loading || !execution?.ready} className="inline-flex items-center gap-2 rounded-xl bg-[#a8f0d2] px-4 py-2.5 text-sm font-semibold text-[#092521] transition hover:bg-[#c6f8e1] disabled:cursor-not-allowed disabled:opacity-40"><Check size={15} />确认并执行</button></div>
    </div>}
  </section>
}

function EmptyStream() {
  return <div className="flex flex-1 flex-col items-center justify-center text-center"><div className="grid size-14 place-items-center rounded-2xl border border-[#21443f] bg-[#102b2a] text-[#78cdaa]"><Radio size={23} /></div><div className="mt-4 text-sm font-medium text-[#b1cbc4]">等待一条任务流</div><div className="mt-2 max-w-[220px] text-xs leading-5 text-[#64827b]">提交任务后，这里会实时显示执行状态和可追踪事件。</div></div>
}

type SequenceDesignInputProps = {
  protein: string
  molecule: SequenceMolecule
  method: SequenceMethod
  useVaxpress: boolean
  structureId: string
  onProteinChange: (value: string) => void
  onMoleculeChange: (value: SequenceMolecule) => void
  onMethodChange: (value: SequenceMethod) => void
  onUseVaxpressChange: (value: boolean) => void
  onStructureChange: (value: string) => void
}

function SequenceDesignInput({ protein, molecule, method, useVaxpress, structureId, onProteinChange, onMoleculeChange, onMethodChange, onUseVaxpressChange, onStructureChange }: SequenceDesignInputProps) {
  const moleculeOptions: Array<{ value: SequenceMolecule; label: string; name: string; detail: string }> = [
    { value: 'linear', label: 'Linear mRNA', name: '线性 mRNA', detail: '常规翻译模板' },
    { value: 'circ', label: 'Circular RNA', name: '环状 RNA', detail: '保留环状分子上下文' },
    { value: 'sa', label: 'Self-amplifying', name: '自扩增 RNA', detail: '记录分子类型' },
  ]
  const methodOptions: Array<{ value: SequenceMethod; label: string; detail: string }> = [
    { value: 'greedy', label: 'Greedy deterministic', detail: '内置规则，结果可复现' },
    { value: 'vaxpress', label: 'VaxPress adapter', detail: '外部后端可用时接入' },
  ]
  const steps = [
    { number: '01', label: 'INPUT', detail: '蛋白序列' },
    { number: '02', label: 'OPTIMIZE', detail: '密码子策略' },
    { number: '03', label: 'VERIFY', detail: '翻译回译' },
    { number: '04', label: 'BENCHMARK', detail: '基线比较' },
  ]
  return <div className="mt-6 space-y-4">
    <section className="rounded-2xl border border-[#28524b] bg-[linear-gradient(135deg,rgba(16,43,42,.82),rgba(7,23,25,.92))] p-4" aria-label="mRNA design workflow">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="field-label mb-0 text-[#8fe5c1]">mRNA / SEQUENCE DESIGN</div><h3 className="mt-1 text-base font-semibold text-[#e4f8ef]">从蛋白序列生成可验证 mRNA</h3><p className="mt-1 text-xs leading-5 text-[#82a79e]">输入目标蛋白，平台会保留优化、评分、翻译验证和 benchmark 的完整轨迹。</p></div><span className="status-badge status-ok">DESIGN PIPELINE</span></div>
      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">{steps.map((step, index) => <div key={step.number} className={`rounded-xl border px-3 py-2.5 ${index === 0 ? 'border-[#4c9c7d] bg-[#123631]' : 'border-white/[0.07] bg-[#071719]/60'}`}><div className="font-mono text-[10px] text-[#8fe5c1]">{step.number}</div><div className="mt-1 text-[11px] font-medium text-[#c9e5dc]">{step.label}</div><div className="mt-0.5 text-[10px] text-[#6f9189]">{step.detail}</div></div>)}</div>
    </section>

    <section className="rounded-2xl border border-white/[0.08] bg-[#071719]/70 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="field-label mb-0">01 / TARGET PROTEIN</div><div className="mt-1 text-sm font-medium text-[#cfe9df]">目标氨基酸序列</div></div><div className="flex items-center gap-3"><span className="font-mono text-[10px] text-[#6f9189]">{protein.length} aa</span><button type="button" onClick={() => onProteinChange('MKT')} className="rounded-lg border border-white/[0.1] px-2.5 py-1.5 text-[10px] text-[#9fc4b8] transition hover:border-[#71cba7] hover:text-[#e8fff5]">加载演示序列</button></div></div>
      <textarea aria-label="目标蛋白序列" value={protein} onChange={(event) => onProteinChange(event.target.value.toUpperCase())} rows={3} className="input-area mt-3 font-mono tracking-[0.16em]" placeholder="例如 MKT..." />
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[10px] leading-5 text-[#6f9189]"><span>支持标准单字母氨基酸符号；后端会在运行前校验序列。</span><span className="font-mono">PROTEIN → mRNA</span></div>
    </section>

    <section className="rounded-2xl border border-white/[0.08] bg-[#071719]/70 p-4">
      <div className="field-label mb-0">01B / MOLECULE FORMAT</div><div className="mt-1 text-sm font-medium text-[#cfe9df]">选择分子类型</div>
      <div className="mt-3 grid gap-2 sm:grid-cols-3" role="radiogroup" aria-label="分子类型">{moleculeOptions.map((option) => <button key={option.value} type="button" role="radio" aria-checked={molecule === option.value} onClick={() => onMoleculeChange(option.value)} className={`rounded-xl border p-3 text-left transition ${molecule === option.value ? 'border-[#4c9c7d] bg-[#123631] shadow-[0_0_0_1px_rgba(143,229,193,.12)]' : 'border-white/[0.08] bg-[#0a211f]/60 hover:border-[#376b5d]'}`}><div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-[#d1eee2]">{option.name}</span>{molecule === option.value && <Check size={14} className="text-[#8fe5c1]" />}</div><div className="mt-1 font-mono text-[10px] text-[#6f9189]">{option.label}</div><div className="mt-2 text-[10px] text-[#86aaa0]">{option.detail}</div></button>)}</div>
    </section>

    <section className="rounded-2xl border border-white/[0.08] bg-[#071719]/70 p-4">
      <div className="field-label mb-0">02 / OPTIMIZATION STRATEGY</div><div className="mt-1 text-sm font-medium text-[#cfe9df]">选择优化后端</div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="优化策略">{methodOptions.map((option) => <button key={option.value} type="button" role="radio" aria-checked={method === option.value} onClick={() => onMethodChange(option.value)} className={`rounded-xl border p-3 text-left transition ${method === option.value ? 'border-[#4c9c7d] bg-[#123631]' : 'border-white/[0.08] bg-[#0a211f]/60 hover:border-[#376b5d]'}`}><div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-[#d1eee2]">{option.label}</span>{method === option.value && <span className="status-badge status-ok">SELECTED</span>}</div><div className="mt-2 text-[10px] leading-5 text-[#86aaa0]">{option.detail}</div></button>)}</div>
    </section>

    <details className="rounded-xl border border-white/[0.08] bg-[#071719]/55">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-xs text-[#aac8bf] outline-none focus-visible:ring-2 focus-visible:ring-[#8fe5c1] focus-visible:ring-inset"><span>ADVANCED CONTEXT / 结构与外部适配器</span><span className="font-mono text-[10px] text-[#6f9189]">OPTIONAL</span></summary>
      <div className="border-t border-white/[0.07] p-4"><div className="grid gap-4 sm:grid-cols-2"><div><label className="field-label" htmlFor="sequence-structure-id">Optional PDB ID</label><input id="sequence-structure-id" value={structureId} onChange={(event) => onStructureChange(event.target.value.toUpperCase().replace(/[^0-9A-Z]/g, '').slice(0, 4))} className="input-control font-mono uppercase" placeholder="例如 1LCI" /><span className="mt-2 block text-[10px] leading-5 text-[#6f9189]">仅在结构与目标蛋白匹配时加载 Mol* 上下文。</span></div><label className="flex items-start gap-3 rounded-xl border border-white/[0.08] bg-[#0a211f]/60 px-3 py-3 text-xs text-[#a9c8be]"><input type="checkbox" checked={useVaxpress} onChange={(event) => onUseVaxpressChange(event.target.checked)} className="mt-0.5 accent-[#8fe5c1]" /><span><span className="block font-medium text-[#d1eee2]">Include VaxPress in benchmark</span><span className="mt-1 block text-[10px] leading-5 text-[#6f9189]">未配置外部 mRNA-Forge 时记录 fallback，不会把确定性结果伪装成模型结果。</span></span></label></div></div>
    </details>
    <div className="flex items-start gap-2 rounded-xl border border-[#705b35] bg-[#251f15]/70 px-3 py-3 text-[10px] leading-5 text-[#d8c18a]"><Sparkles size={13} className="mt-0.5 shrink-0" /><span>这些指标是可追溯的规则质量信号，不是经过实验数据校准的表达量预测。最终序列仍需结合宿主、UTR、修饰和实验验证。</span></div>
  </div>
}

function metricNumber(metrics: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = Number(metrics[key])
    if (Number.isFinite(value)) return value
  }
  return undefined
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function percentMetric(metrics: Record<string, unknown>, keys: string[]) {
  const value = metricNumber(metrics, keys)
  if (value === undefined) return undefined
  return value <= 1 ? value * 100 : value
}

function normalizeSequenceChecks(raw: unknown): SequenceCheck[] {
  if (!Array.isArray(raw)) return []
  return raw.map((value) => {
    if (Array.isArray(value)) {
      return { name: String(value[0] || 'check'), passed: value[1] === 'pass' || value[1] === true, detail: value[2] ? String(value[2]) : undefined }
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const item = value as Record<string, unknown>
      return { name: String(item.name || 'check'), passed: item.passed === true || item.status === 'pass', detail: item.detail ? String(item.detail) : undefined }
    }
    return { name: String(value || 'check'), passed: false }
  })
}

function normalizeSequenceBenchmark(raw: unknown): SequenceBenchmarkRow[] {
  const envelope = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, unknown> : {}
  const payload = envelope.result && typeof envelope.result === 'object' && !Array.isArray(envelope.result) ? envelope.result as Record<string, unknown> : envelope
  if (!Array.isArray(payload.rows)) return []
  return payload.rows.flatMap((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return []
    const item = value as Record<string, unknown>
    const metrics = item.metrics && typeof item.metrics === 'object' && !Array.isArray(item.metrics) ? item.metrics as Record<string, unknown> : {}
    return [{ method: String(item.method || 'unknown'), mrna: typeof item.mrna === 'string' ? item.mrna : undefined, metrics, verdict: item.verdict ? String(item.verdict) : undefined }]
  })
}

function sequenceMetricDelta(row: SequenceBenchmarkRow, baseline: SequenceBenchmarkRow | undefined, keys: string[]) {
  if (!baseline) return '--'
  const current = metricNumber(row.metrics, keys)
  const base = metricNumber(baseline.metrics, keys)
  if (current === undefined || base === undefined) return '--'
  const delta = current - base
  return `${delta >= 0 ? '+' : ''}${delta.toFixed(3)}`
}

function SequenceQualityRadar({ values }: { values: number[] }) {
  const center = 100
  const radius = 62
  const labels = ['GC', 'GC3', 'CAI', 'EXP', 'VERIFY']
  const angles = labels.map((_, index) => -Math.PI / 2 + (index * Math.PI * 2) / labels.length)
  const point = (index: number, scale: number) => {
    const angle = angles[index]
    const value = Math.min(100, Math.max(0, scale)) / 100
    return `${center + Math.cos(angle) * radius * value},${center + Math.sin(angle) * radius * value}`
  }
  const outline = angles.map((_, index) => point(index, 100)).join(' ')
  const data = values.map((value, index) => point(index, value)).join(' ')
  return <div className="min-w-0">
    <svg viewBox="0 0 200 200" className="mx-auto w-full max-w-[220px]" role="img" aria-label="Sequence quality radar">
      <polygon points={outline} fill="none" stroke="rgba(143,229,193,.22)" strokeWidth="1" />
      <polygon points={angles.map((_, index) => point(index, 66)).join(' ')} fill="none" stroke="rgba(143,229,193,.12)" strokeWidth="1" />
      {angles.map((angle, index) => <line key={`axis-${index}`} x1={center} y1={center} x2={center + Math.cos(angle) * radius} y2={center + Math.sin(angle) * radius} stroke="rgba(143,229,193,.13)" strokeWidth="1" />)}
      <polygon points={data} fill="rgba(143,229,193,.20)" stroke="#8fe5c1" strokeWidth="2" />
      {angles.map((angle, index) => <circle key={`dot-${index}`} cx={center + Math.cos(angle) * radius * Math.min(100, Math.max(0, values[index] || 0)) / 100} cy={center + Math.sin(angle) * radius * Math.min(100, Math.max(0, values[index] || 0)) / 100} r="3" fill="#b8f4d8" />)}
      {angles.map((angle, index) => <text key={`label-${index}`} x={center + Math.cos(angle) * 82} y={center + Math.sin(angle) * 82 + 3} textAnchor="middle" fill="#83aaa0" fontSize="9" fontFamily="ui-monospace, monospace">{labels[index]}</text>)}
    </svg>
  </div>
}

function SequenceInterpretationPanel({ result, checks, gc, cai, benchmarkRows, benchmarkStatus }: { result: Record<string, unknown>; checks: SequenceCheck[]; gc?: number; cai?: number; benchmarkRows: SequenceBenchmarkRow[]; benchmarkStatus: string }) {
  const baseline = benchmarkRows.find((row) => row.method === 'naive')
  const optimized = benchmarkRows.find((row) => row.method !== 'naive')
  const baselineCai = baseline ? metricNumber(baseline.metrics, ['cai', 'CAI']) : undefined
  const optimizedCai = optimized ? metricNumber(optimized.metrics, ['cai', 'CAI']) : cai
  const caiDelta = baselineCai !== undefined && optimizedCai !== undefined ? optimizedCai - baselineCai : undefined
  const verified = result.verify === true
  const checksPassed = checks.length > 0 && checks.every((check) => check.passed)
  const gcInRange = gc !== undefined && gc >= 30 && gc <= 80
  const findings = [
    { title: 'Translation fidelity', detail: verified ? '优化序列可以翻译回目标蛋白，阅读框和起始密码子检查通过。' : '翻译回译未通过，不能直接进入后续实验设计。', tone: verified ? 'status-ok' : 'status-failed' },
    { title: 'Sequence composition', detail: gc === undefined ? '缺少 GC 指标，建议先补充评分结果。' : gcInRange ? `GC ${gc.toFixed(1)}% 位于当前规则窗口 30–80% 内。` : `GC ${gc.toFixed(1)}% 超出当前规则窗口，需要人工复核。`, tone: gcInRange ? 'status-ok' : 'status-running' },
    { title: 'Codon strategy', detail: caiDelta === undefined ? '暂无可用 baseline，无法判断优化相对收益。' : `相对 naive baseline 的 CAI 变化为 ${caiDelta >= 0 ? '+' : ''}${caiDelta.toFixed(3)}，仅代表当前规则评分。`, tone: caiDelta !== undefined && caiDelta >= 0 ? 'status-ok' : 'status-running' },
    { title: 'Backend boundary', detail: benchmarkStatus === 'not_configured' ? 'VaxPress 未配置，当前结果来自确定性后端；没有把 fallback 当作模型结果。' : '当前结果已记录后端来源，可继续接入外部 mRNA-Forge。', tone: benchmarkStatus === 'not_configured' ? 'status-running' : 'status-ok' },
  ]
  const decisionReady = verified && checksPassed && gcInRange
  return <section className="mt-4 rounded-xl border border-[#3a6258] bg-[#0b2425]/80 p-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex items-start gap-3"><div className="grid size-9 place-items-center rounded-lg border border-[#28524b] bg-[#102b2a] text-[#8fe5c1]"><Sparkles size={16} /></div><div><div className="field-label mb-0">INTERPRETATION / AUDITABLE AGENT</div><h4 className="mt-1 text-sm font-semibold text-[#d8f4e8]">结果解读与下一步判断</h4></div></div><span className={`status-badge ${decisionReady ? 'status-ok' : 'status-running'}`}>{decisionReady ? 'READY FOR REVIEW' : 'HUMAN REVIEW REQUIRED'}</span></div>
    <div className="mt-4 grid gap-2 md:grid-cols-2">{findings.map((finding) => <div key={finding.title} className="rounded-lg border border-white/[0.07] bg-[#071719]/70 p-3"><div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-[#c8e6db]">{finding.title}</span><span className={`status-badge ${finding.tone}`}>{finding.tone === 'status-ok' ? 'PASS' : 'REVIEW'}</span></div><p className="mt-2 text-xs leading-5 text-[#86aaa0]">{finding.detail}</p></div>)}</div>
    <div className="mt-4 rounded-lg border border-[#28524b] bg-[#102b2a]/60 px-3 py-3 text-xs leading-5 text-[#8fb8ab]">解释来源：序列指标、规则检查、翻译验证和 benchmark 结果。它不是经过实验数据校准的表达量预测器，最终仍需结合目标宿主、UTR、修饰和实验验证。</div>
  </section>
}

function SequenceStructurePanel({ structureId }: { structureId: string }) {
  const pdbId = structureId.trim().toUpperCase()
  const valid = /^[0-9A-Z]{4}$/.test(pdbId)
  if (!valid) return <section className="mt-4 rounded-xl border border-[#70483f] bg-[#251a1a]/80 p-4 text-xs leading-5 text-[#e7ad9d]">PDB ID `{structureId}` 格式不正确。请输入四位结构编号，例如 `1LCI`。</section>
  const viewerUrl = `https://molstar.org/viewer/?pdb=${pdbId.toLowerCase()}`
  return <section className="mt-4 overflow-hidden rounded-xl border border-[#365c78] bg-[#0b1c2a]/90">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-4 py-3"><div><div className="field-label mb-0 text-[#8faecb]">STRUCTURE / MOLSTAR</div><h4 className="mt-1 text-sm font-semibold text-[#dcecff]">PDB {pdbId} 结构上下文</h4></div><a href={viewerUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-[#405b96] bg-[#152442] px-3 py-2 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white">Open Mol* <ArrowUpRight size={13} /></a></div>
    <div className="bg-[#06121b] p-2"><iframe title={`Molstar structure viewer ${pdbId}`} src={viewerUrl} loading="lazy" allow="xr-spatial-tracking" className="h-[360px] w-full rounded-lg border border-white/[0.08] bg-[#071719]" /></div>
    <div className="px-4 pb-4 text-xs leading-5 text-[#88a9be]">结构由 Mol* 官方 viewer 加载。若当前浏览器禁用 WebGL 或网络不可用，可使用右上角链接打开官方页面；平台不会把结构映射自动当成序列验证结果。</div>
  </section>
}

function SequenceResultPanel({ result, benchmark, reportPath, structureId, onDownload, onOpenReport }: { result: Record<string, unknown>; benchmark?: Record<string, unknown>; reportPath?: string; structureId?: string; onDownload: (path: string) => void; onOpenReport: (path: string) => void }) {
  const metrics = result.metrics && typeof result.metrics === 'object' && !Array.isArray(result.metrics) ? result.metrics as Record<string, unknown> : {}
  const mrna = typeof result.mrna === 'string' ? result.mrna.toUpperCase() : ''
  const codons = mrna.match(/.{1,3}/g) || []
  const checks = normalizeSequenceChecks(result.checks)
  const gc = percentMetric(metrics, ['gc', 'GC%'])
  const gc3 = percentMetric(metrics, ['gc3', 'GC3%'])
  const cai = metricNumber(metrics, ['cai', 'CAI'])
  const upA = metricNumber(metrics, ['up_a', 'UpA/kb'])
  const upU = metricNumber(metrics, ['up_u', 'UpU/kb'])
  const expression = metricNumber(metrics, ['expression_score'])
  const passedChecks = checks.filter((check) => check.passed).length
  const benchmarkPayload = benchmark || (result.benchmark && typeof result.benchmark === 'object' && !Array.isArray(result.benchmark) ? result.benchmark as Record<string, unknown> : undefined)
  const benchmarkRows = normalizeSequenceBenchmark(benchmarkPayload)
  const baseline = benchmarkRows.find((row) => row.method === 'naive')
  const windowSize = 30
  const gcWindows = Array.from({ length: Math.min(12, Math.max(1, Math.ceil(mrna.length / windowSize))) }, (_, index) => {
    const chunk = mrna.slice(index * windowSize, (index + 1) * windowSize)
    const gcValue = chunk ? ((chunk.match(/[GC]/g) || []).length / chunk.length) * 100 : 0
    return { label: `${index * windowSize + 1}-${Math.min(mrna.length, (index + 1) * windowSize)}`, value: gcValue }
  }).filter((item) => item.label.split('-')[0] !== '1' || mrna.length > 0)
  const qualityValues = [gc || 0, gc3 || 0, cai === undefined ? 0 : cai * 100, expression === undefined ? (checks.length ? (passedChecks / checks.length) * 100 : 0) : expression * 100, result.verify === true ? 100 : 0]
  const benchmarkStatus = benchmarkPayload?.vaxpress ? String(benchmarkPayload.vaxpress) : ''
  const metricCards = [
    { label: 'GC CONTENT', value: gc === undefined ? '--' : `${gc.toFixed(1)}%`, tone: 'text-[#8fe5c1]' },
    { label: 'GC3', value: gc3 === undefined ? '--' : `${gc3.toFixed(1)}%`, tone: 'text-[#aebfff]' },
    { label: 'CAI', value: cai === undefined ? '--' : cai.toFixed(3), tone: 'text-[#f0d38b]' },
    { label: 'UPA / KB', value: upA === undefined ? '--' : upA.toFixed(2), tone: 'text-[#d1a8ff]' },
    { label: 'UPU / KB', value: upU === undefined ? '--' : upU.toFixed(2), tone: 'text-[#f1a99a]' },
  ]
  return <section className="mt-5 rounded-2xl border border-[#28524b] bg-[linear-gradient(135deg,rgba(16,43,42,.96),rgba(8,25,29,.96))] p-5 sm:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><div className="eyebrow">SEQUENCE DESIGN / QUALITY PROFILE</div><h3 className="mt-2 text-lg font-semibold text-[#e4f8ef]">mRNA 优化结果</h3><p className="mt-1 text-xs text-[#7fa99e]">{String(result.molecule || 'linear')} · {String(result.method || 'greedy')} · optimize → score → verify</p></div>
      <div className="flex flex-wrap items-center justify-end gap-2"><span className={`status-badge ${result.verify === true ? 'status-ok' : 'status-running'}`}><span className="size-1.5 rounded-full bg-current" />{result.verify === true ? 'TRANSLATION VERIFIED' : String(result.verdict || 'REVIEW')}</span>{reportPath && <><button onClick={() => onOpenReport(reportPath)} className="inline-flex items-center gap-1.5 rounded-full border border-[#405b96] bg-[#152442] px-2.5 py-1 font-mono text-[10px] text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><ArrowUpRight size={12} />查看报告</button><button onClick={() => onDownload(reportPath)} className="inline-flex items-center gap-1.5 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#b9e6d5] transition hover:border-[#71cba7] hover:text-white"><Download size={12} />下载 HTML</button></>}</div>
    </div>
    <div className="mt-5 rounded-2xl border border-[#32665b] bg-[#061b1d]/80 p-4">
      <div className="flex items-center justify-between gap-3"><div className="field-label mb-0">OPTIMIZED mRNA / {String(result.mrna_len || mrna.length)} NT</div><div className="font-mono text-[10px] text-[#6e9d91]">5&apos; → 3&apos;</div></div>
      <div className="mt-3 flex flex-wrap gap-1.5">{codons.map((codon, index) => <span key={`${codon}-${index}`} className="rounded-md border border-[#2b6457] bg-[#123631] px-2.5 py-2 font-mono text-sm tracking-[0.16em] text-[#d0f7e5]">{codon}</span>)}</div>
      {!mrna && <div className="mt-2 text-xs text-[#789791]">结果中没有返回序列文本，请下载完整 JSON 查看。</div>}
    </div>
    <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">{metricCards.map((card) => <div key={card.label} className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">{card.label}</div><div className={`mt-2 font-mono text-xl ${card.tone}`}>{card.value}</div></div>)}</div>
    {expression !== undefined && <div className="mt-4 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="flex items-center justify-between text-[10px] text-[#86a59e]"><span className="font-mono tracking-[0.12em]">EXPRESSION SCORE</span><span className="font-mono text-[#d4f7e6]">{(expression * 100).toFixed(1)}%</span></div><div className="mt-2 h-2 overflow-hidden rounded-full bg-[#17312f]"><div className="h-full rounded-full bg-gradient-to-r from-[#4dba91] to-[#b3f4d4]" style={{ width: `${Math.min(100, Math.max(0, expression * 100))}%` }} /></div></div>}
    <div className="mt-4 grid gap-4 lg:grid-cols-[0.8fr_1.2fr]">
      <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="field-label mb-0">QUALITY RADAR</div><SequenceQualityRadar values={qualityValues} /></div>
      <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="flex items-center justify-between gap-3"><div className="field-label mb-0">SLIDING-WINDOW GC / {windowSize} NT</div><span className="font-mono text-[10px] text-[#83e3bc]">{gcWindows.length} windows</span></div>{gcWindows.length ? <div className="mt-5 space-y-3">{gcWindows.map((window) => <div key={window.label} className="grid grid-cols-[78px_1fr_48px] items-center gap-3"><span className="font-mono text-[10px] text-[#6f9189]">{window.label}</span><div className="h-2 overflow-hidden rounded-full bg-[#17312f]"><div className={`h-full rounded-full ${window.value >= 30 && window.value <= 80 ? 'bg-[#74d7ad]' : 'bg-[#e6c875]'}`} style={{ width: `${Math.max(2, Math.min(100, window.value))}%` }} /></div><span className="text-right font-mono text-[10px] text-[#b7dace]">{window.value.toFixed(1)}%</span></div>)}</div> : <div className="mt-5 text-xs text-[#6f9189]">暂无序列窗口。</div>}</div>
    </div>
    {checks.length > 0 && <div className="mt-4 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="flex items-center justify-between gap-3"><div className="field-label mb-0">RULE CHECKS</div><span className="font-mono text-[10px] text-[#83e3bc]">{passedChecks}/{checks.length} PASS</span></div><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{checks.map((check, index) => <div key={`${check.name}-${index}`} className="flex items-start gap-2 rounded-lg border border-white/[0.06] px-3 py-2"><Check size={13} className={`mt-0.5 shrink-0 ${check.passed ? 'text-[#70e3ad]' : 'text-[#ec9b87]'}`} /><div className="min-w-0"><div className="truncate text-xs text-[#c5e1d7]">{check.name}</div>{check.detail && <div className="mt-1 truncate text-[10px] text-[#6f9189]">{check.detail}</div>}</div></div>)}</div></div>}
    {benchmarkRows.length > 0 && <div className="mt-4 overflow-hidden rounded-xl border border-white/[0.08] bg-[#071719]/70"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-4 py-3"><div><div className="field-label mb-0">BENCHMARK / BASELINE COMPARISON</div><div className="mt-1 text-xs text-[#6f9189]">与 naive 反向翻译基线比较关键序列指标</div></div>{benchmarkStatus && <span className={`status-badge ${benchmarkStatus === 'not_configured' ? 'status-running' : 'status-ok'}`}>{benchmarkStatus === 'not_configured' ? 'VaxPress fallback recorded' : `VaxPress: ${benchmarkStatus}`}</span>}</div><div className="overflow-x-auto"><table className="w-full min-w-[620px] text-left text-xs"><thead className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]"><tr><th className="px-4 py-3 font-normal">METHOD</th><th className="px-4 py-3 font-normal">GC</th><th className="px-4 py-3 font-normal">GC3</th><th className="px-4 py-3 font-normal">CAI</th><th className="px-4 py-3 font-normal">Δ CAI</th><th className="px-4 py-3 font-normal">VERDICT</th></tr></thead><tbody>{benchmarkRows.map((row) => <tr key={row.method} className="border-t border-white/[0.06]"><td className="px-4 py-3 font-medium text-[#c8e6db]">{row.method === 'naive' ? 'Naive baseline' : row.method === 'greedy' ? 'Greedy optimized' : row.method}</td><td className="px-4 py-3 font-mono text-[#9fe5c5]">{percentMetric(row.metrics, ['gc', 'GC%'])?.toFixed(1) || '--'}%</td><td className="px-4 py-3 font-mono text-[#aebfff]">{percentMetric(row.metrics, ['gc3', 'GC3%'])?.toFixed(1) || '--'}%</td><td className="px-4 py-3 font-mono text-[#f0d38b]">{metricNumber(row.metrics, ['cai', 'CAI'])?.toFixed(3) || '--'}</td><td className="px-4 py-3 font-mono text-[#b9e6d5]">{sequenceMetricDelta(row, baseline, ['cai', 'CAI'])}</td><td className="px-4 py-3"><span className={`status-badge ${row.verdict === 'PASS' ? 'status-ok' : 'status-running'}`}>{row.verdict || 'REVIEW'}</span></td></tr>)}</tbody></table></div></div>}
    {structureId && <SequenceStructurePanel structureId={structureId} />}
    <SequenceInterpretationPanel result={result} checks={checks} gc={gc} cai={cai} benchmarkRows={benchmarkRows} benchmarkStatus={benchmarkStatus} />
  </section>
}

function CaddResultPanel({ result, onDownload }: { result: Record<string, unknown>; onDownload: (path: string) => void }) {
  const rawHits = result.hits ?? result.top_hits
  const hits: CaddHit[] = Array.isArray(rawHits) ? rawHits.flatMap((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return []
    const item = value as Record<string, unknown>
    const affinity = Number(item.affinity)
    return Number.isFinite(affinity) ? [{ mol_name: String(item.mol_name || item.name || 'unknown'), tag: String(item.tag || 'inactive'), affinity }] : []
  }) : []
  const maxAbsAffinity = Math.max(...hits.map((hit) => Math.abs(hit.affinity)), 1)
  const scorePlot = typeof result.score_plot === 'string' ? result.score_plot : ''
  const topMoleculeImage = typeof result.top_molecule_image === 'string' ? result.top_molecule_image : ''
  return <section className="mt-5 rounded-2xl border border-[#3d5a8c] bg-[linear-gradient(135deg,rgba(17,29,50,.96),rgba(11,21,38,.96))] p-5 sm:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="eyebrow text-[#8298d9]">CADD / VIRTUAL SCREENING</div><h3 className="mt-2 text-lg font-semibold text-[#eef1ff]">命中排序与结合能</h3><p className="mt-1 text-xs text-[#93a5d4]">数值越负，表示对接受体的预测结合越强</p></div><div className="grid size-10 place-items-center rounded-xl border border-[#405b96] bg-[#152442] text-[#aebfff]"><BarChart3 size={19} /></div></div>
    <div className="mt-5 grid gap-2 sm:grid-cols-3"><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">BEST HIT</div><div className="mt-2 truncate text-lg font-semibold text-[#dbe2ff]">{String(result.best_hit || hits[0]?.mol_name || '--')}</div></div><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">BEST AFFINITY</div><div className="mt-2 font-mono text-lg text-[#aebfff]">{result.best_affinity !== undefined ? `${Number(result.best_affinity).toFixed(3)} kcal/mol` : hits[0] ? `${hits[0].affinity.toFixed(3)} kcal/mol` : '--'}</div></div><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">SUCCESSFUL DOCKS</div><div className="mt-2 font-mono text-lg text-[#8fe5c1]">{String(result.rows ?? hits.length)} / {String(result.max_ligands ?? (hits.length || '--'))}</div></div></div>
    {hits.length > 0 ? <div className="mt-5 overflow-hidden rounded-xl border border-white/[0.08] bg-[#081426]/80"><div className="border-b border-white/[0.08] px-4 py-3"><div className="field-label mb-0">TOP HITS / AFFINITY PROFILE</div></div><div className="divide-y divide-white/[0.06]">{hits.map((hit, index) => <div key={`${hit.mol_name}-${index}`} className="grid gap-2 px-4 py-3 sm:grid-cols-[28px_1fr_120px_100px] sm:items-center"><div className="font-mono text-xs text-[#6f86bb]">{String(index + 1).padStart(2, '0')}</div><div className="min-w-0"><div className="flex items-center gap-2"><span className="truncate text-sm font-medium text-[#dce5ff]">{hit.mol_name}</span><span className={`status-badge ${hit.tag === 'active' ? 'status-ok' : 'status-queued'}`}>{hit.tag}</span></div><div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[#20304b]"><div className="h-full rounded-full bg-gradient-to-r from-[#718cff] to-[#aebfff]" style={{ width: `${Math.max(12, Math.round((Math.abs(hit.affinity) / maxAbsAffinity) * 100))}%` }} /></div></div><div className="font-mono text-sm text-[#b9c7ff] sm:text-right">{hit.affinity.toFixed(3)}</div><div className="font-mono text-[10px] text-[#7085b4] sm:text-right">kcal/mol</div></div>)}</div></div> : <div className="mt-5 rounded-xl border border-[#705b35] bg-[#251f15] px-4 py-3 text-xs leading-5 text-[#d8c18a]">当前结果没有携带命中明细。后续运行会返回前 10 个配体，并在这里生成排序表。</div>}
    {(scorePlot || topMoleculeImage) && <div className="mt-4 flex flex-wrap gap-2"><div className="field-label mb-0 mr-2 self-center">ARTIFACTS</div>{scorePlot && <button onClick={() => onDownload(scorePlot)} className="inline-flex items-center gap-2 rounded-lg border border-[#405b96] bg-[#152442] px-3 py-2 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><Download size={13} />打分图</button>}{topMoleculeImage && <button onClick={() => onDownload(topMoleculeImage)} className="inline-flex items-center gap-2 rounded-lg border border-[#405b96] bg-[#152442] px-3 py-2 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><Download size={13} />Top hit 结构图</button>}</div>}
  </section>
}

function AgentEvidencePanel({ evidenceMatches, evidenceCitations, knowledgeMatches, graphMetrics, provider }: { evidenceMatches: Record<string, unknown>[]; evidenceCitations: Record<string, unknown>[]; knowledgeMatches: Record<string, unknown>[]; graphMetrics: Record<string, unknown>; provider?: string }) {
  if (!evidenceMatches.length && !evidenceCitations.length && !knowledgeMatches.length && !Object.keys(graphMetrics).length && !provider) return null
  return <section data-result-evidence className="border-t border-white/[0.08] px-5 py-5 sm:px-6">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">AGENT / EVIDENCE GROUNDING</div><div className="mt-1 text-sm text-[#b9e6d5]">检索结果、知识片段和图谱关系共同支撑当前解释</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />GROUNDED</span></div>
    <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4"><PipelineMetric label="EVIDENCE MATCHES" value={evidenceMatches.length || evidenceCitations.length || '—'} /><PipelineMetric label="KNOWLEDGE HITS" value={knowledgeMatches.length || '—'} /><PipelineMetric label="GRAPH NODES" value={graphMetrics.n_nodes ?? '—'} /><PipelineMetric label="GRAPH EDGES" value={graphMetrics.n_edges ?? '—'} /></div>
    <div className="mt-4 grid gap-3 lg:grid-cols-2">
      {(evidenceMatches.length > 0 || evidenceCitations.length > 0) && <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="field-label mb-0">LITERATURE / DATABASE EVIDENCE</div><div className="mt-3 space-y-2">{(evidenceMatches.length ? evidenceMatches.slice(0, 3) : evidenceCitations.slice(0, 3)).map((item, index) => { const title = String(item.title || item.gene_id || `Evidence ${index + 1}`); const source = String(item.source || item.provider || 'source'); const url = typeof item.url === 'string' ? item.url : ''; return <div key={`${source}-${title}-${index}`} className="rounded-lg border border-white/[0.07] px-3 py-2.5"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="truncate text-xs font-medium text-[#c9e5dc]">{title}</div><div className="mt-1 font-mono text-[10px] text-[#6f9189]">{source}{item.pmid ? ` · PMID ${String(item.pmid)}` : ''}</div></div>{url && <a href={url} target="_blank" rel="noreferrer" className="shrink-0 text-[#aebfff]" aria-label={`打开 ${title}`}><ArrowUpRight size={14} /></a>}</div></div> })}</div></div>}
      {knowledgeMatches.length > 0 && <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="field-label mb-0">KNOWLEDGE RETRIEVAL / TF-IDF</div><div className="mt-3 space-y-2">{knowledgeMatches.slice(0, 3).map((item, index) => <div key={`${String(item.document_id || item.title || 'document')}-${index}`} className="rounded-lg border border-white/[0.07] px-3 py-2.5"><div className="flex items-center justify-between gap-3"><div className="truncate text-xs font-medium text-[#c9e5dc]">{String(item.title || item.document_id || 'Knowledge document')}</div><span className="font-mono text-[10px] text-[#8fe5c1]">{item.score !== undefined ? Number(item.score).toFixed(3) : '—'}</span></div>{Boolean(item.snippet) && <p className="mt-2 line-clamp-2 text-[10px] leading-5 text-[#769890]">{String(item.snippet)}</p>}</div>)}</div></div>}
    </div>
  </section>
}

function EvidenceProvenancePanel({ provider, requestedGeneIds, source, endpoint, status, fallbackReason }: { provider?: string; requestedGeneIds: string[]; source?: string; endpoint?: string; status?: string; fallbackReason?: string }) {
  if (!provider && !requestedGeneIds.length && !source && !endpoint && !fallbackReason) return null
  const evidenceSource = source || endpoint || '—'
  const reviewed = status !== 'ok' && Boolean(fallbackReason)
  return <div className="mt-3 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="grid gap-3 sm:grid-cols-3"><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">PROVIDER</div><div className="mt-1 truncate text-xs text-[#c9e5dc]">{provider ? providerLabels[provider] || provider : '—'}</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">QUERY TARGETS</div><div className="mt-1 truncate font-mono text-xs text-[#c9e5dc]" title={requestedGeneIds.join(', ')}>{requestedGeneIds.length ? requestedGeneIds.join(', ') : '—'}</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">SOURCE / ENDPOINT</div><div className="mt-1 truncate font-mono text-xs text-[#c9e5dc]" title={evidenceSource}>{evidenceSource}</div></div></div>{reviewed && <div className="mt-3 rounded-lg border border-[#705b35] bg-[#251f15] px-3 py-2 text-[10px] leading-5 text-[#d8c18a]">FALLBACK / REVIEW: {fallbackReason}</div>}</div>
}

function MultiOmicsSummaryPanel({ genomicsMetrics, singleCellMetrics, imageMetrics, metagenomicsMetrics, evidenceMatches, knowledgeMatches, graphMetrics, sequenceResult }: { genomicsMetrics: Record<string, unknown>; singleCellMetrics: Record<string, unknown>; imageMetrics: Record<string, unknown>; metagenomicsMetrics: Record<string, unknown>; evidenceMatches: Record<string, unknown>[]; knowledgeMatches: Record<string, unknown>[]; graphMetrics: Record<string, unknown>; sequenceResult: Record<string, unknown> }) {
  const hasData = Object.keys(genomicsMetrics).length > 0 || Object.keys(singleCellMetrics).length > 0 || Object.keys(imageMetrics).length > 0 || Object.keys(metagenomicsMetrics).length > 0
  if (!hasData) return null
  const display = (value: unknown) => value === undefined || value === null ? '—' : typeof value === 'number' ? value.toLocaleString() : String(value)
  const cellPassed = singleCellMetrics.n_cells_passed
  const cellInput = singleCellMetrics.n_cells_input
  const mrnaStatus = sequenceResult.verify !== undefined ? (sequenceResult.verify ? 'verified' : 'review') : sequenceResult.verdict
  return <section className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">MULTI-OMICS / AGENT HANDOFF</div><div className="mt-1 text-sm text-[#b9e6d5]">基因组、单细胞、图像和微生物组结果进入证据检索与 mRNA 设计链路</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />CROSS-MODAL TRACE</span></div><div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">GENOMICS QC</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{display(genomicsMetrics.reads)} reads</div><div className="mt-1 text-[10px] text-[#769890]">{display(genomicsMetrics.bases)} bases</div></div><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">10X SINGLE-CELL</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{display(cellPassed)} / {display(cellInput)} cells</div><div className="mt-1 text-[10px] text-[#769890]">{display(singleCellMetrics.n_gene_expression_features)} expression features</div></div><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">IMAGING QC</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{imageMetrics.width !== undefined && imageMetrics.height !== undefined ? `${display(imageMetrics.width)}×${display(imageMetrics.height)}` : '—'}</div><div className="mt-1 text-[10px] text-[#769890]">{display(imageMetrics.format)} · {display(imageMetrics.channels)} channels</div></div><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">METAGENOMICS QC</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{display(metagenomicsMetrics.n_taxa_retained)} taxa</div><div className="mt-1 text-[10px] text-[#769890]">{display(metagenomicsMetrics.n_samples)} samples retained</div></div></div><div className="mt-3 grid gap-2 rounded-xl border border-[#28524b] bg-[#102b2a]/70 p-3 sm:grid-cols-4"><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">EVIDENCE</div><div className="mt-1 text-xs text-[#c9e5dc]">{display(evidenceMatches.length)} matches</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">KNOWLEDGE</div><div className="mt-1 text-xs text-[#c9e5dc]">{display(knowledgeMatches.length)} retrieved</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">GRAPH</div><div className="mt-1 text-xs text-[#c9e5dc]">{display(graphMetrics.n_nodes)} nodes / {display(graphMetrics.n_edges)} edges</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">mRNA HANDOFF</div><div className="mt-1 text-xs text-[#8fe5c1]">{display(mrnaStatus)}</div></div></div></section>
}

function AgentExecutionAuditPanel({ status, steps, manifestPath, reportPath }: { status?: string; steps: Record<string, unknown>[]; manifestPath?: string; reportPath?: string }) {
  if (!steps.length) return null
  const completed = steps.filter((step) => step.status === 'completed').length
  const failed = steps.filter((step) => step.status === 'failed').length
  const dependencies = steps.reduce((total, step) => total + (Array.isArray(step.depends_on) ? step.depends_on.length : 0), 0)
  const tools = new Set(steps.map((step) => typeof step.tool === 'string' ? step.tool : 'unknown'))
  return <section className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">AGENT / EXECUTION AUDIT</div><div className="mt-1 text-sm text-[#b9e6d5]">每个工具调用、依赖关系和最终产物都保留在本次运行 manifest 中</div></div><span className={`status-badge ${failed ? 'status-failed' : 'status-ok'}`}><span className="size-1.5 rounded-full bg-current" />{failed ? 'REVIEW REQUIRED' : 'REPRODUCIBLE RUN'}</span></div><div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4"><PipelineMetric label="STEPS" value={steps.length} /><PipelineMetric label="COMPLETED" value={completed} /><PipelineMetric label="FAILED" value={failed} /><PipelineMetric label="DEPENDENCY EDGES" value={dependencies} /></div><div className="mt-3 grid gap-2 sm:grid-cols-3"><div className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">RUN STATUS</div><div className="mt-1 text-xs text-[#c9e5dc]">{status || 'unknown'}</div></div><div className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">TOOL CONTRACTS</div><div className="mt-1 text-xs text-[#c9e5dc]">{tools.size} unique tools</div></div><div className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">ARTIFACTS</div><div className="mt-1 text-xs text-[#8fe5c1]">{manifestPath ? 'manifest' : '—'}{reportPath ? ' + report' : ''}</div></div></div></section>
}

function ReportPreviewModal({ preview, onClose }: { preview: { url: string; filename: string }; onClose: () => void }) {
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#02090a]/80 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label="HTML 报告预览">
    <div className="flex h-[min(88vh,900px)] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-[#365c78] bg-[#0a1a1d] shadow-[0_24px_80px_rgba(0,0,0,.55)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-4">
        <div><div className="eyebrow text-[#8faecb]">ARTIFACT PREVIEW / HTML</div><div className="mt-1 truncate font-mono text-xs text-[#c8e3dc]">{preview.filename}</div></div>
        <button onClick={onClose} className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-2 text-xs text-[#b7d1c9] transition hover:border-[#ec9b87] hover:text-white"><XCircle size={14} />关闭预览</button>
      </div>
      <iframe title={`HTML report preview ${preview.filename}`} src={preview.url} className="min-h-0 flex-1 bg-white" />
      <div className="border-t border-white/10 px-5 py-3 text-[10px] leading-5 text-[#73928a]">报告来自当前任务 artifact，并通过同一鉴权接口读取；预览内容不改变原始文件。</div>
    </div>
  </div>
}

function JobResultSummary({ job, structureId, onDownload, onOpenReport }: { job: Job; structureId?: string; onDownload: (path: string) => void; onOpenReport: (path: string) => void }) {
  const payload = job.result && typeof job.result === 'object' ? job.result : {}
  const manifest = payload.manifest && typeof payload.manifest === 'object' && !Array.isArray(payload.manifest) ? payload.manifest as Record<string, unknown> : {}
  const steps = Array.isArray(manifest.steps) ? manifest.steps.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)) : []
  const annotationStep = steps.find((step) => step.tool === 'omics_annotate_variants')
  const annotationResult = annotationStep?.result && typeof annotationStep.result === 'object' && !Array.isArray(annotationStep.result) ? annotationStep.result as Record<string, unknown> : {}
  const omicsStep = steps.find((step) => step.tool === 'omics_run_analysis')
  const omicsResult = omicsStep?.result && typeof omicsStep.result === 'object' && !Array.isArray(omicsStep.result) ? omicsStep.result as Record<string, unknown> : {}
  const differential = omicsResult.differential_expression && typeof omicsResult.differential_expression === 'object' && !Array.isArray(omicsResult.differential_expression) ? omicsResult.differential_expression as Record<string, unknown> : {}
  const pathway = omicsResult.pathway_enrichment && typeof omicsResult.pathway_enrichment === 'object' && !Array.isArray(omicsResult.pathway_enrichment) ? omicsResult.pathway_enrichment as Record<string, unknown> : {}
  const omicsReport = omicsResult.report && typeof omicsResult.report === 'object' && !Array.isArray(omicsResult.report) ? omicsResult.report as Record<string, unknown> : {}
  const alignmentStep = steps.find((step) => step.tool === 'omics_run_rnaseq_alignment')
  const alignmentResult = alignmentStep?.result && typeof alignmentStep.result === 'object' && !Array.isArray(alignmentStep.result) ? alignmentStep.result as Record<string, unknown> : {}
  const alignmentSamples = Array.isArray(alignmentResult.samples) ? alignmentResult.samples.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)) : []
  const alignmentRates = alignmentSamples.map((sample) => sample.overall_alignment_rate).filter((value): value is string => typeof value === 'string').map((value) => Number.parseFloat(value)).filter((value) => Number.isFinite(value))
  const alignmentRate = alignmentRates.length ? `${(alignmentRates.reduce((total, value) => total + value, 0) / alignmentRates.length).toFixed(2)}%` : undefined
  const featureCountsStep = steps.find((step) => step.tool === 'omics_run_feature_counts')
  const featureCountsResult = featureCountsStep?.result && typeof featureCountsStep.result === 'object' && !Array.isArray(featureCountsStep.result) ? featureCountsStep.result as Record<string, unknown> : {}
  const caddStep = steps.find((step) => step.tool === 'cadd_run_screening')
  const caddEnvelope = caddStep?.result && typeof caddStep.result === 'object' && !Array.isArray(caddStep.result) ? caddStep.result as Record<string, unknown> : {}
  const caddResult = caddEnvelope.result && typeof caddEnvelope.result === 'object' && !Array.isArray(caddEnvelope.result) ? caddEnvelope.result as Record<string, unknown> : caddEnvelope
  const fastqQcStep = steps.find((step) => step.tool === 'omics_run_fastq_qc')
  const fastqQcResult = fastqQcStep?.result && typeof fastqQcStep.result === 'object' && !Array.isArray(fastqQcStep.result) ? fastqQcStep.result as Record<string, unknown> : {}
  const fastqQcReports = Array.isArray(fastqQcResult.reports) ? fastqQcResult.reports.filter((value): value is string => typeof value === 'string') : []
  const fastqQcReport = fastqQcReports.find((value) => value.toLowerCase().includes('multiqc_report.html'))
  const fastqQcSummaries = Array.isArray(fastqQcResult.fastqc_summaries) ? fastqQcResult.fastqc_summaries.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)) : []
  const fastqQcCounts = fastqQcSummaries.reduce<Record<string, number>>((counts, report) => {
    const modules = Array.isArray(report.summary) ? report.summary : []
    modules.forEach((module) => {
      if (!module || typeof module !== 'object' || Array.isArray(module)) return
      const status = (module as Record<string, unknown>).status
      if (status === 'pass' || status === 'warn' || status === 'fail') counts[status] = (counts[status] || 0) + 1
    })
    return counts
  }, { pass: 0, warn: 0, fail: 0 })
  const fastqTools = recordValue(recordValue(fastqQcResult.provenance).tools)
  const alignmentTools = recordValue(recordValue(alignmentResult.provenance).tools)
  const featureCountsTool = recordValue(recordValue(featureCountsResult.provenance).tool)
  const toolProvenance = [
    { label: 'FastQC', version: recordValue(fastqTools.fastqc).version },
    { label: 'MultiQC', version: recordValue(fastqTools.multiqc).version },
    { label: 'HISAT2', version: recordValue(alignmentTools.hisat2).version },
    { label: 'HISAT2-build', version: recordValue(alignmentTools['hisat2-build']).version },
    { label: 'SAMtools', version: recordValue(alignmentTools.samtools).version },
    { label: 'featureCounts', version: featureCountsTool.version },
    { label: 'Statistics', version: differential.backend ? `${String(differential.backend)} backend` : undefined },
  ].filter((item): item is { label: string; version: string } => typeof item.version === 'string' && item.version.length > 0)
  const genomicsStep = steps.find((step) => step.tool === 'omics_run_genomics_qc')
  const genomicsResult = genomicsStep?.result && typeof genomicsStep.result === 'object' && !Array.isArray(genomicsStep.result) ? genomicsStep.result as Record<string, unknown> : {}
  const genomicsMetrics = genomicsResult.metrics && typeof genomicsResult.metrics === 'object' && !Array.isArray(genomicsResult.metrics) ? genomicsResult.metrics as Record<string, unknown> : {}
  const imageStep = steps.find((step) => step.tool === 'imaging_inspect_image')
  const imageResult = imageStep?.result && typeof imageStep.result === 'object' && !Array.isArray(imageStep.result) ? imageStep.result as Record<string, unknown> : {}
  const imageMetrics = imageResult.metrics && typeof imageResult.metrics === 'object' && !Array.isArray(imageResult.metrics) ? imageResult.metrics as Record<string, unknown> : {}
  const singleCellStep = steps.find((step) => step.tool === 'omics_run_single_cell_10x_qc')
  const singleCellResult = singleCellStep?.result && typeof singleCellStep.result === 'object' && !Array.isArray(singleCellStep.result) ? singleCellStep.result as Record<string, unknown> : {}
  const singleCellMetrics = singleCellResult.metrics && typeof singleCellResult.metrics === 'object' && !Array.isArray(singleCellResult.metrics) ? singleCellResult.metrics as Record<string, unknown> : {}
  const singleCellOutputs = singleCellResult.outputs && typeof singleCellResult.outputs === 'object' && !Array.isArray(singleCellResult.outputs) ? singleCellResult.outputs as Record<string, unknown> : {}
  const metagenomicsStep = steps.find((step) => step.tool === 'omics_run_metagenomics_qc')
  const metagenomicsResult = metagenomicsStep?.result && typeof metagenomicsStep.result === 'object' && !Array.isArray(metagenomicsStep.result) ? metagenomicsStep.result as Record<string, unknown> : {}
  const metagenomicsMetrics = metagenomicsResult.metrics && typeof metagenomicsResult.metrics === 'object' && !Array.isArray(metagenomicsResult.metrics) ? metagenomicsResult.metrics as Record<string, unknown> : {}
  const metagenomicsOutputs = metagenomicsResult.outputs && typeof metagenomicsResult.outputs === 'object' && !Array.isArray(metagenomicsResult.outputs) ? metagenomicsResult.outputs as Record<string, unknown> : {}
  const evidenceStep = steps.find((step) => step.tool === 'literature_search')
  const directEvidenceEnvelope = job.tool === 'literature_search' ? payload : {}
  const evidenceEnvelope = evidenceStep?.result && typeof evidenceStep.result === 'object' && !Array.isArray(evidenceStep.result) ? evidenceStep.result as Record<string, unknown> : directEvidenceEnvelope
  const evidenceResult = evidenceEnvelope.result && typeof evidenceEnvelope.result === 'object' && !Array.isArray(evidenceEnvelope.result) ? evidenceEnvelope.result as Record<string, unknown> : {}
  const evidenceSummaryStep = steps.find((step) => step.tool === 'literature_summarize')
  const evidenceSummaryEnvelope = evidenceSummaryStep?.result && typeof evidenceSummaryStep.result === 'object' && !Array.isArray(evidenceSummaryStep.result) ? evidenceSummaryStep.result as Record<string, unknown> : {}
  const evidenceSummaryResult = evidenceSummaryEnvelope.result && typeof evidenceSummaryEnvelope.result === 'object' && !Array.isArray(evidenceSummaryEnvelope.result) ? evidenceSummaryEnvelope.result as Record<string, unknown> : {}
  const evidenceMatches = Array.isArray(evidenceResult.matches) ? evidenceResult.matches.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)) : []
  const evidenceCitations = Array.isArray(evidenceSummaryResult.citations) ? evidenceSummaryResult.citations.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)) : []
  const evidenceProvider = typeof evidenceResult.provider === 'string' ? evidenceResult.provider : undefined
  const evidenceRequestedGeneIds = Array.isArray(evidenceResult.requested_gene_ids) ? evidenceResult.requested_gene_ids.filter((value): value is string => typeof value === 'string') : []
  const evidenceSource = typeof evidenceResult.source_file === 'string' ? evidenceResult.source_file : undefined
  const evidenceEndpoint = typeof evidenceResult.endpoint === 'string' ? evidenceResult.endpoint : undefined
  const evidenceStatus = typeof evidenceResult.status === 'string' ? evidenceResult.status : undefined
  const evidenceFallbackReason = typeof evidenceResult.fallback_reason === 'string' ? evidenceResult.fallback_reason : undefined
  const knowledgeIngestStep = steps.find((step) => step.tool === 'knowledge_ingest_directory')
  const knowledgeIngestEnvelope = knowledgeIngestStep?.result && typeof knowledgeIngestStep.result === 'object' && !Array.isArray(knowledgeIngestStep.result) ? knowledgeIngestStep.result as Record<string, unknown> : {}
  const knowledgeIngestResult = knowledgeIngestEnvelope.result && typeof knowledgeIngestEnvelope.result === 'object' && !Array.isArray(knowledgeIngestEnvelope.result) ? knowledgeIngestEnvelope.result as Record<string, unknown> : {}
  const knowledgeSearchStep = steps.find((step) => step.tool === 'knowledge_search')
  const knowledgeSearchEnvelope = knowledgeSearchStep?.result && typeof knowledgeSearchStep.result === 'object' && !Array.isArray(knowledgeSearchStep.result) ? knowledgeSearchStep.result as Record<string, unknown> : {}
  const knowledgeSearchResult = knowledgeSearchEnvelope.result && typeof knowledgeSearchEnvelope.result === 'object' && !Array.isArray(knowledgeSearchEnvelope.result) ? knowledgeSearchEnvelope.result as Record<string, unknown> : {}
  const knowledgeMatches = Array.isArray(knowledgeSearchResult.matches) ? knowledgeSearchResult.matches.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)) : []
  const graphStep = steps.find((step) => step.tool === 'knowledge_build_graph')
  const graphEnvelope = graphStep?.result && typeof graphStep.result === 'object' && !Array.isArray(graphStep.result) ? graphStep.result as Record<string, unknown> : {}
  const graphResult = graphEnvelope.result && typeof graphEnvelope.result === 'object' && !Array.isArray(graphEnvelope.result) ? graphEnvelope.result as Record<string, unknown> : {}
  const graphMetrics = graphResult.metrics && typeof graphResult.metrics === 'object' && !Array.isArray(graphResult.metrics) ? graphResult.metrics as Record<string, unknown> : {}
  const sequenceStep = steps.find((step) => step.tool === 'sequence_pipeline')
  const sequenceEnvelope = sequenceStep?.result && typeof sequenceStep.result === 'object' && !Array.isArray(sequenceStep.result) ? sequenceStep.result as Record<string, unknown> : payload
  const sequenceResult = sequenceEnvelope.result && typeof sequenceEnvelope.result === 'object' && !Array.isArray(sequenceEnvelope.result) ? sequenceEnvelope.result as Record<string, unknown> : sequenceEnvelope
  const sequenceBenchmarkStep = steps.find((step) => step.tool === 'sequence_benchmark')
  const sequenceBenchmarkEnvelope = sequenceBenchmarkStep?.result && typeof sequenceBenchmarkStep.result === 'object' && !Array.isArray(sequenceBenchmarkStep.result) ? sequenceBenchmarkStep.result as Record<string, unknown> : {}
  const sequenceBenchmark = sequenceBenchmarkEnvelope.result && typeof sequenceBenchmarkEnvelope.result === 'object' && !Array.isArray(sequenceBenchmarkEnvelope.result) ? sequenceBenchmarkEnvelope.result as Record<string, unknown> : sequenceBenchmarkEnvelope
  const sequenceReportStep = steps.find((step) => step.tool === 'sequence_report')
  const sequenceReportEnvelope = sequenceReportStep?.result && typeof sequenceReportStep.result === 'object' && !Array.isArray(sequenceReportStep.result) ? sequenceReportStep.result as Record<string, unknown> : {}
  const sequenceReport = sequenceReportEnvelope.result && typeof sequenceReportEnvelope.result === 'object' && !Array.isArray(sequenceReportEnvelope.result) ? sequenceReportEnvelope.result as Record<string, unknown> : sequenceReportEnvelope
  const report = payload.report && typeof payload.report === 'object' && !Array.isArray(payload.report) ? payload.report as Record<string, unknown> : {}
  const summary: Record<string, unknown> = {
    status: payload.status,
    backend: payload.backend ?? annotationResult.backend,
    n_annotated: payload.n_annotated ?? annotationResult.n_annotated,
    n_unmatched: payload.n_unmatched ?? annotationResult.n_unmatched,
    n_variants: payload.n_variants ?? annotationResult.n_variants,
    n_genes: payload.n_genes ?? differential.n_genes ?? omicsReport.n_genes,
    n_significant: payload.n_significant ?? differential.n_significant ?? omicsReport.n_significant_genes,
    n_pathways: payload.n_pathways ?? pathway.n_pathways ?? omicsReport.n_pathways,
    n_significant_pathways: payload.n_significant_pathways ?? pathway.n_significant_pathways,
    statistics_backend: payload.statistics_backend ?? differential.backend,
    fallback_reason: payload.fallback_reason ?? differential.fallback_reason,
    cadd_rows: payload.rows ?? caddResult.rows,
    best_hit: payload.best_hit ?? caddResult.best_hit,
    best_affinity: payload.best_affinity ?? caddResult.best_affinity,
    cadd_exhaustiveness: payload.exhaustiveness ?? caddResult.exhaustiveness,
    cadd_max_ligands: payload.max_ligands ?? caddResult.max_ligands,
    fastq_reads: genomicsMetrics.reads,
    fastq_bases: genomicsMetrics.bases,
    fastq_manifest: genomicsResult.manifest_path,
    fastq_qc_samples: fastqQcSummaries.length || undefined,
    fastq_qc_report: fastqQcReport,
    fastq_qc_manifest: fastqQcResult.manifest_path,
    alignment_samples: alignmentSamples.length || undefined,
    alignment_rate: alignmentRate,
    counted_genes: featureCountsResult.n_genes,
    counted_samples: featureCountsResult.n_samples,
    feature_counts_csv: featureCountsResult.output_csv,
    feature_counts_summary: featureCountsResult.summary_path,
    differential_expression_csv: differential.output_csv,
    pathway_enrichment_csv: pathway.output_csv,
    omics_report: omicsReport.output_md,
    image_format: imageMetrics.format,
    image_dimensions: imageMetrics.width !== undefined && imageMetrics.height !== undefined ? `${imageMetrics.width}x${imageMetrics.height}` : undefined,
    image_channels: imageMetrics.channels,
    image_manifest: imageResult.manifest_path,
    single_cell_passed: singleCellMetrics.n_cells_passed,
    single_cell_metrics: singleCellOutputs.cell_metrics,
    metagenomics_taxa: metagenomicsMetrics.n_taxa_retained,
    metagenomics_samples: metagenomicsMetrics.n_samples,
    metagenomics_relative_abundance: metagenomicsOutputs.relative_abundance,
    metagenomics_sample_metrics: metagenomicsOutputs.sample_metrics,
    evidence_matches: evidenceResult.n_matches,
    knowledge_matches: knowledgeSearchResult.n_matches,
    knowledge_index: knowledgeIngestResult.output_path,
    knowledge_graph_nodes: graphMetrics.n_nodes,
    knowledge_graph_edges: graphMetrics.n_edges,
    knowledge_graph: graphResult.output_path,
    pipeline: payload.pipeline ?? sequenceResult.pipeline,
    mrna_len: payload.mrna_len ?? sequenceResult.mrna_len,
    verdict: payload.verdict ?? sequenceResult.verdict,
    verify: payload.verify ?? sequenceResult.verify,
    completed_steps: manifest.completed_steps,
    failed_steps: manifest.failed_steps,
    output_csv: payload.output_csv ?? annotationResult.output_csv,
    cadd_result_csv: payload.result_csv ?? caddResult.result_csv,
    manifest_path: manifest.manifest_path,
    report_path: payload.output_md ?? omicsReport.output_md ?? sequenceReport.output_html ?? sequenceResult.output_html ?? report.path,
    cadd_report: caddResult.report,
  }
  const visible = Object.entries(summary).filter(([, value]) => value !== undefined && value !== null)
  const artifactKeys = new Set(['output_csv', 'cadd_result_csv', 'manifest_path', 'report_path', 'cadd_report', 'fastq_manifest', 'fastq_qc_report', 'fastq_qc_manifest', 'feature_counts_csv', 'feature_counts_summary', 'differential_expression_csv', 'pathway_enrichment_csv', 'omics_report', 'image_manifest', 'single_cell_metrics', 'metagenomics_relative_abundance', 'metagenomics_sample_metrics', 'knowledge_index', 'knowledge_graph'])
  const traceSteps = steps.map((step, index) => ({
    index: index + 1,
    id: typeof step.id === 'string' ? step.id : `step-${index + 1}`,
    tool: typeof step.tool === 'string' ? step.tool : 'unknown',
    status: typeof step.status === 'string' ? step.status : 'unknown',
  }))
  const rawGeneIds = payload.gene_ids ?? annotationResult.gene_ids
  const geneIds = Array.isArray(rawGeneIds) ? rawGeneIds.filter((value): value is string => typeof value === 'string') : []
  const hasEvidenceView = evidenceMatches.length > 0 || evidenceCitations.length > 0 || knowledgeMatches.length > 0 || Object.keys(graphMetrics).length > 0 || Boolean(evidenceProvider)
  const hasAuditView = steps.length > 0
  const jumpTo = (selector: string) => document.querySelector(selector)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  return <section data-result-overview className="panel mt-5 overflow-hidden" aria-live="polite">
    <nav aria-label="Result sections" className="flex flex-wrap gap-2 border-b border-white/[0.08] px-5 py-3 sm:px-6"><button type="button" onClick={() => jumpTo('[data-result-overview]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">OVERVIEW</button>{hasEvidenceView && <button type="button" onClick={() => jumpTo('[data-result-evidence]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">EVIDENCE</button>}{hasAuditView && <button type="button" onClick={() => jumpTo('[data-result-audit]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">AUDIT</button>}<button type="button" onClick={() => jumpTo('details')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">RAW JSON</button></nav>
    <div className="flex items-center justify-between border-b border-white/10 px-5 py-5 sm:px-6"><div><div className="eyebrow">RESULT / PROVENANCE</div><h2 className="mt-2 text-xl font-semibold">结构化结果</h2></div><Check size={18} className="text-[#83e3bc]" /></div>
    <div className="grid gap-3 px-5 py-5 sm:grid-cols-2 lg:grid-cols-4 sm:px-6">{visible.map(([key, value]) => <div key={key} className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] uppercase tracking-[0.12em] text-[#63817b]">{key}</div>{artifactKeys.has(key) && typeof value === 'string' ? <div className="mt-2 flex flex-wrap gap-2">{key === 'report_path' && <button onClick={() => onOpenReport(value)} title={`预览 ${value}`} aria-label={`预览 ${key}`} className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[#405b96] bg-[#152442] px-2.5 py-1.5 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><ArrowUpRight size={13} /><span className="truncate">查看报告</span></button>}<button onClick={() => onDownload(value)} title={value} aria-label={`下载 ${key}`} className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[#28524b] bg-[#102b2a] px-2.5 py-1.5 text-xs text-[#b9e6d5] transition hover:border-[#71cba7] hover:text-[#ecfff7]"><Download size={13} /><span className="truncate">下载产物</span></button></div> : <div className="mt-2 truncate text-sm text-[#c9e5dc]">{String(value)}</div>}</div>)}</div>
    {Boolean(sequenceResult.mrna) && <SequenceResultPanel result={sequenceResult} benchmark={Object.keys(sequenceBenchmark).length ? sequenceBenchmark : undefined} reportPath={typeof sequenceReport.output_html === 'string' ? sequenceReport.output_html : undefined} structureId={structureId} onDownload={onDownload} onOpenReport={onOpenReport} />}
    {(Boolean(caddResult.best_hit) || Array.isArray(caddResult.hits) || Array.isArray(caddResult.top_hits)) && <CaddResultPanel result={caddResult} onDownload={onDownload} />}
    <AgentEvidencePanel evidenceMatches={evidenceMatches} evidenceCitations={evidenceCitations} knowledgeMatches={knowledgeMatches} graphMetrics={graphMetrics} provider={evidenceProvider} />
    <EvidenceProvenancePanel provider={evidenceProvider} requestedGeneIds={evidenceRequestedGeneIds} source={evidenceSource} endpoint={evidenceEndpoint} status={evidenceStatus} fallbackReason={evidenceFallbackReason} />
    <MultiOmicsSummaryPanel genomicsMetrics={genomicsMetrics} singleCellMetrics={singleCellMetrics} imageMetrics={imageMetrics} metagenomicsMetrics={metagenomicsMetrics} evidenceMatches={evidenceMatches} knowledgeMatches={knowledgeMatches} graphMetrics={graphMetrics} sequenceResult={sequenceResult} />
    <div data-result-audit className="scroll-mt-6" />
    <AgentExecutionAuditPanel status={typeof manifest.status === 'string' ? manifest.status : undefined} steps={steps} manifestPath={typeof manifest.manifest_path === 'string' ? manifest.manifest_path : undefined} reportPath={typeof sequenceReport.output_html === 'string' ? sequenceReport.output_html : typeof omicsReport.output_md === 'string' ? omicsReport.output_md : typeof report.path === 'string' ? report.path : undefined} />
     {Boolean(fastqQcResult.status) && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">FASTQ QUALITY CONTROL</div><div className="mt-1 text-sm text-[#b9e6d5]">FastQC {fastqQcSummaries.length ? `完成 ${fastqQcSummaries.length} 个报告` : '报告'} · MultiQC 汇总已生成</div></div>{fastqQcReport && <button onClick={() => onDownload(fastqQcReport)} className="inline-flex items-center gap-2 rounded-lg border border-[#3d5a8c] bg-[#111d32] px-3 py-2 text-xs font-medium text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><Download size={13} />下载 MultiQC 报告</button>}</div><div className="mt-4 grid grid-cols-3 gap-2 sm:max-w-md"><QcStatusMetric label="PASS" value={fastqQcCounts.pass} className="status-ok" /><QcStatusMetric label="WARN" value={fastqQcCounts.warn} className="status-running" /><QcStatusMetric label="FAIL" value={fastqQcCounts.fail} className="status-failed" /></div></div>}
     {toolProvenance.length > 0 && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">TOOLCHAIN PROVENANCE</div><div className="mt-1 text-sm text-[#b9e6d5]">版本信息来自本次任务的实际执行环境</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />VERIFIED RUNTIME</span></div><div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{toolProvenance.map((item) => <div key={item.label} className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">{item.label}</div><div className="mt-1 truncate text-[11px] text-[#c9e5dc]" title={item.version}>{item.version}</div></div>)}</div></div>}
     {(alignmentSamples.length > 0 || featureCountsResult.n_genes !== undefined || Boolean(differential.output_csv)) && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">RNA-SEQ PIPELINE SUMMARY</div><div className="mt-1 text-sm text-[#b9e6d5]">比对、计数和差异分析结果已汇总，可直接用于面试演示。</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />链路完成</span></div><div className="mt-4 grid grid-cols-2 gap-2 lg:grid-cols-4"><PipelineMetric label="SAMPLES" value={alignmentSamples.length || featureCountsResult.n_samples || '—'} /><PipelineMetric label="AVG ALIGNMENT" value={alignmentRate || '—'} /><PipelineMetric label="COUNTED GENES" value={featureCountsResult.n_genes ?? '—'} /><PipelineMetric label="SIGNIFICANT DEGS" value={differential.n_significant ?? '—'} /></div>{alignmentSamples.length > 0 && <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{alignmentSamples.map((sample, index) => <div key={`${String(sample.sample_id || 'sample')}-${index}`} className="flex items-center justify-between rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><span className="font-mono text-[10px] text-[#a9cbc0]">{String(sample.sample_id || `sample-${index + 1}`)}</span><span className="font-mono text-[10px] text-[#8fe5c1]">{String(sample.overall_alignment_rate || '—')}</span></div>)}</div>}</div>}
     {traceSteps.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">WORKFLOW TRACE / TOOLCHAIN</div><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{traceSteps.map((step) => <div key={`${step.index}-${step.id}`} className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-3"><div className="grid size-7 shrink-0 place-items-center rounded-lg border border-[#28524b] bg-[#102b2a] font-mono text-[10px] text-[#8fe5c1]">{String(step.index).padStart(2, '0')}</div><div className="min-w-0 flex-1"><div className="truncate text-xs font-medium text-[#c9e5dc]">{step.id}</div><div className="mt-1 truncate font-mono text-[9px] text-[#66857e]">{step.tool}</div></div><span className={`status-badge ${step.status === 'completed' ? 'status-ok' : step.status === 'failed' ? 'status-failed' : 'status-running'}`}>{step.status}</span></div>)}</div></div>}
    {geneIds.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">ANNOTATED GENE IDS</div><div className="mt-2 flex flex-wrap gap-2">{geneIds.map((geneId) => <span key={geneId} className="rounded-md border border-[#28524b] bg-[#102b2a] px-2 py-1 font-mono text-[10px] text-[#b9e6d5]">{geneId}</span>)}</div></div>}
    <details className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><summary className="cursor-pointer text-xs text-[#8fb2a8]">查看完整结果 JSON</summary><pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-[#061113] p-3 text-[10px] leading-5 text-[#91b8ac]">{JSON.stringify(payload, null, 2)}</pre></details>
  </section>
}

function DomainsView({ plugins }: { plugins: Plugin[] }) {
  return <section className="py-9"><div className="max-w-3xl"><div className="eyebrow">PLUGIN CATALOG / DISCOVERY</div><h1 className="mt-3 text-4xl font-semibold tracking-[-0.04em] sm:text-5xl">领域是能力，<span className="text-[#8fe5c1]">插件是边界。</span></h1><p className="mt-5 text-sm leading-7 text-[#88a6a0] sm:text-base">每个领域通过统一工具契约接入，状态、版本与能力在运行时可发现。研究 Agent 只编排能力，不把业务逻辑写死在对话层。</p></div><div className="mt-10 grid gap-4 md:grid-cols-2 xl:grid-cols-3">{plugins.map((plugin) => { const Icon = domainIcons[plugin.domain] || Boxes; return <div key={plugin.domain} className="panel group p-5 transition hover:-translate-y-0.5 hover:border-[#3e786a]"><div className="flex items-start justify-between"><div className="grid size-11 place-items-center rounded-xl border border-[#28524b] bg-[#102b2a] text-[#8fe5c1]"><Icon size={20} /></div><span className={`status-badge ${plugin.status === 'available' ? 'status-ok' : 'status-failed'}`}>{plugin.status === 'available' ? 'AVAILABLE' : plugin.status.toUpperCase()}</span></div><h2 className="mt-6 text-lg font-semibold capitalize">{plugin.domain}</h2><p className="mt-1 min-h-10 text-xs leading-5 text-[#6e8b85]">{plugin.name}</p><div className="mt-5 flex items-end justify-between border-t border-white/[0.07] pt-4"><div><div className="font-mono text-2xl text-[#d7f1e8]">{String(plugin.tool_count).padStart(2, '0')}</div><div className="mt-1 font-mono text-[9px] tracking-[0.15em] text-[#5f7d77]">TOOLS</div></div><div className="text-right font-mono text-[10px] text-[#63837b]">v{plugin.version || 'builtin'}</div></div></div> })}</div></section>
}

export default App
