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

const luciferaseDemoProtein = 'MEDAKNIKKGPAPFYPLEDGTAGEQLHKAMKRYALVPGTIAFTDAHIEVNITYAEYFEMSVRLAEAMKRYGLNTNHRIVVCSENSLQFFMPVLGALFIGVAVAPANDIYNERELLNSMNISQPTVVFVSKKGLQKILNVQKKLPIIQKIIIMDSKTDYQGFQSMYTFVTSHLPPGFNEYDFVPESFDRDKTIALIMNSSGSTGLPKGVALPHRTACVRFSHARDPIFGNQIIPDTAILSVVPFHHGFGMFTTLGYLICGFRVVLMYRFEEELFLRSLQDYKIQSALLVPTLFSFFAKSTLIDKYDLSNLHEIASGGAPLSKEVGEAVAKRFHLPGIRQGYGLTETTSAILITPEGDDKPGAVGKVVPFFEAKVVDLDTGKTLGVNQRGELCVRGPMIMSGYVNNPEATNALIDKDGWLHSGDIAYWDEDEHFFIVDRLKSLIKYKGYQVAPAELESILLQHPNIFDAGVAGLPDDDAGELPAAVVVLEHGKTMTEKEIVDYVASQVTTAKKLRGGVVFVDEVPKGLTGKLDARKIREILIKAKKGGKSKL'

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

type Project = {
  project_id: string
  name: string
  description?: string | null
  owner_subject: string
  created_at: string
}

type RnaPreflightItem = {
  label: string
  detail: string
  ready: boolean
  required: boolean
}

type View = 'workspace' | 'domains'
type RunMode = 'research' | 'rnaseq' | 'variant' | 'sequence' | 'cadd'
type ResearchPreset = 'custom' | 'bgi_multiomics' | 'online_evidence'
type RnaInputMode = 'fixture' | 'upload'
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
  omics: '组学',
  sequence: 'mRNA / 序列',
  literature: '文献',
  knowledge: '知识库',
  imaging: '成像 / 多模态',
  research: '研究编排',
}

const pluginDescriptions: Record<string, string> = {
  cadd: '计算机辅助药物设计',
  omics: '组学分析与质量控制',
  research: '生物信息学研究代理',
  literature: '文献与证据检索',
  knowledge: '本地科研知识检索',
  imaging: '显微成像与图像质控',
  sequence: 'mRNA-Forge 序列设计',
}

const rnaseqFixture = {
  fastqPaths: [
    'examples/omics/rnaseq_fastq_fixture/A1.fastq',
    'examples/omics/rnaseq_fastq_fixture/A2.fastq',
    'examples/omics/rnaseq_fastq_fixture/A3.fastq',
    'examples/omics/rnaseq_fastq_fixture/B1.fastq',
    'examples/omics/rnaseq_fastq_fixture/B2.fastq',
    'examples/omics/rnaseq_fastq_fixture/B3.fastq',
  ],
  fastqR2Paths: [
    'examples/omics/rnaseq_paired_fixture/A1_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/A2_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/A3_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/B1_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/B2_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/B3_R2.fastq',
  ],
  referenceFasta: 'examples/omics/rnaseq_fastq_fixture/reference.fa',
  annotationGtf: 'examples/omics/rnaseq_fastq_fixture/genes.gtf',
  metadataCsv: 'examples/omics/rnaseq_fastq_fixture/metadata.csv',
  geneSetsCsv: 'examples/omics/rnaseq_fastq_fixture/gene_sets.csv',
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

async function uploadFile(base: string, token: string, file: File, projectId = ''): Promise<UploadedFile> {
  const body = new FormData()
  body.append('upload', file)
  if (projectId) body.append('project_id', projectId)
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
  const [researchPreset, setResearchPreset] = useState<ResearchPreset>('custom')
  const [plannerMode, setPlannerMode] = useState<PlannerMode>('auto')
  const [apiBase] = useState(defaultApiBase)
  const [token, setToken] = useState(() => localStorage.getItem('bio-agent-token') || import.meta.env.VITE_API_TOKEN || '')
  const [tokenDraft, setTokenDraft] = useState(() => token)
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [task, setTask] = useState('分析 RNA-seq 差异表达并设计 mRNA 序列')
  const [rnaseqTask, setRnaseqTask] = useState('运行 FastQC 并比对双端 RNA-seq 读段')
  const [variantTask, setVariantTask] = useState('注释 VCF 变异并检索基因证据')
  const [protein, setProtein] = useState('MKT')
  const [geneIds, setGeneIds] = useState('')
  const [sequenceMolecule, setSequenceMolecule] = useState<SequenceMolecule>('linear')
  const [sequenceMethod, setSequenceMethod] = useState<SequenceMethod>('greedy')
  const [sequenceUseVaxpress, setSequenceUseVaxpress] = useState(false)
  const [sequenceStructureId, setSequenceStructureId] = useState('')
  const [evidenceProvider, setEvidenceProvider] = useState('local')
  const [variantBackend, setVariantBackend] = useState('auto')
  const [rnaInputMode, setRnaInputMode] = useState<RnaInputMode>('fixture')
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
  const sequenceDemoStarted = useRef(false)

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
      const [pluginPayload, jobPayload, capabilityPayload, projectPayload] = await Promise.all([
        apiFetch<{ plugins: Plugin[] }>(apiBase, authToken, '/api/v1/plugins'),
        apiFetch<{ jobs: Job[] }>(apiBase, authToken, '/api/v1/jobs?limit=8'),
        apiFetch<Capabilities>(apiBase, authToken, '/api/v1/capabilities'),
        apiFetch<{ projects: Project[] }>(apiBase, authToken, '/api/v1/projects'),
      ])
      setPlugins(pluginPayload.plugins || [])
      setCapabilities(capabilityPayload)
      const nextProjects = projectPayload.projects || []
      setProjects(nextProjects)
      setSelectedProjectId((current) => nextProjects.some((project) => project.project_id === current) ? current : nextProjects[0]?.project_id || '')
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

  useEffect(() => {
    setResearchPlan(null)
    setSelectedJob(null)
    setEvents([])
  }, [mode])

  useEffect(() => {
    if (mode !== 'sequence' || sequenceDemoStarted.current) return
    sequenceDemoStarted.current = true
    void submitSequenceDemo()
  }, [mode])

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
    const fixtureMode = rnaInputMode === 'fixture'
    const r1Count = fixtureMode ? rnaseqFixture.fastqPaths.length : rnaFiles.fastq_r1.length
    const r2Count = fixtureMode ? rnaseqFixture.fastqR2Paths.length : rnaFiles.fastq_r2.length
    const pairMismatch = r2Count > 0 && (r1Count === 0 || r1Count !== r2Count)
    const checks: RnaPreflightItem[] = [
      { label: 'R1 FASTQ', detail: fixtureMode ? `${r1Count} 个仓库样例文件` : r1Count ? `${r1Count} 个文件` : '待上传', ready: r1Count > 0, required: true },
      { label: 'R2 FASTQ', detail: fixtureMode ? `${r2Count} 个仓库样例文件` : r2Count ? `${r2Count} 个文件` : pairedEnd ? '双端任务需要上传' : '未上传，按单端处理', ready: !pairedEnd && r2Count === 0 ? true : r2Count > 0 && !pairMismatch, required: pairedEnd },
      { label: '参考基因组 FASTA', detail: fixtureMode ? '仓库样例已就绪' : rnaFiles.reference_fasta.length ? '已上传' : alignment ? '比对任务需要上传' : '当前管线可跳过比对', ready: !alignment || fixtureMode || rnaFiles.reference_fasta.length > 0, required: alignment },
      { label: '基因注释 GTF', detail: fixtureMode ? '仓库样例已就绪' : rnaFiles.annotation_gtf.length ? '已上传' : differential ? '差异分析前需要计数注释' : 'featureCounts / 差异分析需要', ready: !differential || fixtureMode || rnaFiles.annotation_gtf.length > 0, required: differential },
      { label: '样本元数据 CSV', detail: fixtureMode ? '仓库样例已就绪' : rnaFiles.metadata.length ? '已上传' : differential ? '差异分析需要' : '可选', ready: !differential || fixtureMode || rnaFiles.metadata.length > 0, required: differential },
      { label: '基因集 CSV', detail: fixtureMode ? '仓库样例已就绪' : rnaFiles.gene_sets.length ? '已上传' : enrichment ? '富集分析需要' : '可选', ready: !enrichment || fixtureMode || rnaFiles.gene_sets.length > 0, required: enrichment },
    ]
    return { checks, pairMismatch }
  }, [rnaFiles, rnaInputMode, rnaseqTask])

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
      const payload = await apiFetch<{ project: Project }>(apiBase, token, '/api/v1/projects', {
        method: 'POST',
        body: JSON.stringify({ name }),
      })
      setProjects((current) => [payload.project, ...current])
      setSelectedProjectId(payload.project.project_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '项目创建失败')
    }
  }

  function applyResearchPreset(preset: ResearchPreset) {
    setResearchPreset(preset)
    setResearchPlan(null)
    if (preset === 'bgi_multiomics') {
      setTask('运行 BGI 多组学研究流程：基因组质控、10x 单细胞、显微成像、微生物组、证据检索和 mRNA 设计')
      setGeneIds('GeneA, GeneB')
      setProtein('MKT')
      setEvidenceProvider('local')
      setPlannerMode('deterministic')
    } else if (preset === 'online_evidence') {
      setTask('检索目标基因的在线证据并生成可追溯摘要')
      setGeneIds('TP53, BRCA1')
      setEvidenceProvider('uniprot')
      setPlannerMode('deterministic')
    } else {
      setTask('分析 RNA-seq 差异表达并设计 mRNA 序列')
      setGeneIds('')
      setEvidenceProvider('local')
      setPlannerMode('auto')
    }
  }

  function buildResearchInputs() {
    const inputs: Record<string, unknown> = {
      expression_csv: uploadedFiles.expression?.path || 'examples/rnaseq/expression.csv',
      metadata_csv: uploadedFiles.metadata?.path || 'examples/rnaseq/metadata.csv',
      gene_sets_csv: uploadedFiles.gene_sets?.path || 'examples/rnaseq/gene_sets.csv',
      evidence_csv: evidenceProvider === 'local' ? 'examples/rnaseq/evidence.csv' : undefined,
      evidence_provider: evidenceProvider,
      gene_ids: geneIds.split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean).slice(0, 20),
      protein,
      output_dir: 'output/frontend_auto_research',
    }
    if (researchPreset === 'bgi_multiomics') {
      Object.assign(inputs, {
        fastq_paths: 'examples/omics/reads.fastq',
        input_type: 'fastq',
        matrix_mtx: 'examples/omics/tenx/matrix.mtx',
        barcodes_tsv: 'examples/omics/tenx/barcodes.tsv',
        features_tsv: 'examples/omics/tenx/features.tsv',
        abundance_csv: 'examples/omics/metagenome_abundance.csv',
        image_path: 'examples/omics/cell_microscopy.svg',
        image_modality: 'microscopy_demo',
        documents_dir: 'examples/knowledge',
        top_k: 3,
        multiomics: true,
        output_dir: 'output/frontend_bgi_multiomics',
      })
    }
    return inputs
  }

  function researchDomains() {
    if (researchPreset === 'bgi_multiomics') return ['omics', 'imaging', 'literature', 'knowledge', 'sequence']
    if (researchPreset === 'online_evidence') return ['literature']
    return undefined
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
    const fixtureMode = rnaInputMode === 'fixture'
    return {
      fastq_paths: fixtureMode ? rnaseqFixture.fastqPaths : rnaFiles.fastq_r1.length ? rnaFiles.fastq_r1.map((file) => file.path) : undefined,
      fastq_r2_paths: fixtureMode ? rnaseqFixture.fastqR2Paths : rnaFiles.fastq_r2.length ? rnaFiles.fastq_r2.map((file) => file.path) : undefined,
      reference_fasta: fixtureMode ? rnaseqFixture.referenceFasta : rnaFiles.reference_fasta[0]?.path,
      annotation_gtf: fixtureMode ? rnaseqFixture.annotationGtf : rnaFiles.annotation_gtf[0]?.path,
      metadata_csv: fixtureMode ? rnaseqFixture.metadataCsv : rnaFiles.metadata[0]?.path,
      gene_sets_csv: fixtureMode ? rnaseqFixture.geneSetsCsv : rnaFiles.gene_sets[0]?.path,
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
      const uploaded = await uploadFile(apiBase, token, file, selectedProjectId)
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
      for (const file of Array.from(files)) uploaded.push(await uploadFile(apiBase, token, file, selectedProjectId))
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
        body: JSON.stringify({ tool, arguments: arguments_, project_id: selectedProjectId || undefined }),
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
        { task, domains: researchDomains(), inputs: buildResearchInputs(), planner_mode: plannerMode },
        '研究计划已进入执行队列',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
      return
    }
    if (mode === 'rnaseq') {
      setResearchPlan(null)
      await submitToolJob(
        'omics_run_rnaseq_workbench',
        buildRnaseqInputs(),
        'RNA-seq 专属分析管线已进入执行队列',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
      return
    }
    if (mode === 'variant') {
      setResearchPlan(null)
      await submitToolJob(
        'omics_run_variant_workbench',
        buildVariantInputs(),
        'VCF 专属变异工作台已进入执行队列',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
      return
    }
    if (mode === 'sequence') {
      setResearchPlan(null)
      await submitToolJob(
        'sequence_workbench',
        {
          protein,
          molecule: sequenceMolecule,
          method: sequenceMethod,
          include_benchmark: true,
          use_vaxpress: sequenceUseVaxpress,
          output_dir: 'output/frontend_sequence_research',
        },
        'mRNA 专属序列工作台已进入执行队列',
      )
      return
    }
    if (mode === 'cadd') {
      setResearchPlan(null)
      const caddInputs = buildCaddInputs()
      await submitToolJob(
        'cadd_run_screening',
        {
          receptor: caddInputs.receptor,
          external_dataset: caddInputs.ligand_library,
          out: caddInputs.output_dir,
          exhaustiveness: caddInputs.exhaustiveness,
          max_ligands: caddInputs.max_ligands,
        },
        'CADD 专属筛选管线已进入执行队列',
        (job) => setResearchPlan(extractResearchPlan(job)),
      )
    }
  }

  async function submitSequenceDemo() {
    setProtein(luciferaseDemoProtein)
    setSequenceStructureId('1LCI')
    setResearchPlan(null)
    await submitToolJob(
      'sequence_workbench',
      {
        protein: luciferaseDemoProtein,
        molecule: sequenceMolecule,
        method: sequenceMethod,
        include_benchmark: true,
        use_vaxpress: sequenceUseVaxpress,
        output_dir: 'output/frontend_sequence_research',
      },
      'mRNA 专属序列工作台已进入执行队列',
    )
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
              <div className="text-sm font-semibold tracking-wide">研究操作系统</div>
            </div>
          </div>
          <div className="mt-12 px-2 font-mono text-[10px] tracking-[0.2em] text-[#5d817c]">控制平面</div>
          <nav className="mt-3 space-y-1">
            <button onClick={() => setView('workspace')} className={`nav-item ${view === 'workspace' ? 'nav-item-active' : ''}`}><LayoutDashboard size={17} />工作台<span className="ml-auto font-mono text-[10px] opacity-50">01</span></button>
            <button onClick={() => setView('domains')} className={`nav-item ${view === 'domains' ? 'nav-item-active' : ''}`}><Boxes size={17} />领域与插件<span className="ml-auto font-mono text-[10px] opacity-50">06</span></button>
          </nav>
          <div className="mt-auto space-y-4">
            <div className="rounded-2xl border border-[#21443f] bg-[#0d2526] p-4">
              <div className="flex items-center gap-2 text-xs font-medium"><ShieldCheck size={15} className="text-[#83e3bc]" />安全连接</div>
              <div className="mt-3 flex items-center gap-2 font-mono text-[11px] text-[#7da09a]"><span className={`size-2 rounded-full ${connected ? 'bg-[#70e3ad]' : 'bg-[#dd876d]'}`} />{connected ? 'API 在线' : 'API 离线'}</div>
              <div className="mt-1 truncate font-mono text-[10px] text-[#557570]">{apiBase || 'same-origin'}</div>
            </div>
            <div className="px-2 font-mono text-[10px] leading-5 text-[#557570]">默认可追溯。<br />证据优先于直觉。</div>
          </div>
        </aside>

        <main className="min-w-0 flex-1 px-5 py-5 sm:px-8 lg:px-10 lg:py-8">
          <header className="flex flex-wrap items-center justify-between gap-4 border-b border-white/10 pb-5">
            <div className="flex items-center gap-2 font-mono text-[11px] tracking-[0.16em] text-[#74918c]"><span className="text-[#a8f0d2]">平台</span><ChevronRight size={13} /><span>{view === 'workspace' ? '工作台' : '领域'}</span></div>
            <div className="flex items-center gap-2">
              <select aria-label="当前项目" value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)} className="max-w-44 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1.5 text-xs text-[#c7ded8] outline-none focus:border-[#72dcb4]">
                <option value="">未选择项目</option>
                {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
              </select>
              <button type="button" onClick={() => void createProject()} className="rounded-lg border border-[#28524b] px-2.5 py-1.5 text-xs text-[#a8f0d2] transition hover:bg-[#102b2a]">新建项目</button>
            </div>
            <form onSubmit={(event) => { event.preventDefault(); saveToken() }} className="flex items-center gap-3">
              <div className="hidden items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 font-mono text-[10px] text-[#8aa9a2] sm:flex"><LockKeyhole size={12} />访问令牌</div>
              <input aria-label="访问令牌" autoComplete="off" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} type="password" className="w-32 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1.5 font-mono text-[10px] text-[#c7ded8] outline-none transition focus:border-[#72dcb4] sm:w-48" placeholder="本地可留空，生产请输入 Token" />
              <button type="submit" className="rounded-lg bg-[#a8f0d2] px-3 py-1.5 text-xs font-semibold text-[#092521] transition hover:bg-[#c6f8e1]">连接</button>
            </form>
          </header>

          {error && <div className="mt-5 flex items-center gap-3 rounded-xl border border-[#75483d] bg-[#2b1a1b] px-4 py-3 text-sm text-[#f5b7a4]"><XCircle size={16} />{error}<button onClick={() => setError('')} className="ml-auto text-xs underline">关闭</button></div>}

          {view === 'workspace' ? (
            <>
              <section className="grid gap-7 py-9 xl:grid-cols-[1fr_0.72fr] xl:items-end">
                <div>
                  <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-[#28524b] bg-[#102b2a] px-3 py-1.5 font-mono text-[10px] tracking-[0.16em] text-[#9ce3c6]"><Sparkles size={12} />研究控制平面</div>
                  <h1 className="max-w-3xl text-4xl font-semibold leading-[1.08] tracking-[-0.04em] text-[#eff9f5] sm:text-6xl">把科学问题，变成一条<span className="text-[#8fe5c1]">可追踪的计算路径。</span></h1>
                  <p className="mt-5 max-w-2xl text-sm leading-7 text-[#88a6a0] sm:text-base">跨 CADD、组学、序列与证据检索的统一工作台。每个任务都有状态、来源和可复现的运行记录。</p>
                </div>
                <div className="grid grid-cols-3 gap-2 xl:pb-1">
                  <Metric label="活跃领域" value={String(activeDomains).padStart(2, '0')} icon={<GitBranch size={14} />} />
                  <Metric label="可用工具" value={String(toolCount).padStart(2, '0')} icon={<Terminal size={14} />} />
                  <Metric label="运行中任务" value={String(runningJobs).padStart(2, '0')} icon={<Radio size={14} />} />
                </div>
              </section>
              <details data-platform-surfaces className="group mb-5 rounded-2xl border border-white/[0.08] bg-[#0b1b1e]/75">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-5 py-4 outline-none focus-visible:ring-2 focus-visible:ring-[#8fe5c1] focus-visible:ring-inset">
                  <div className="flex items-center gap-3"><ChevronRight size={16} className="transition group-open:rotate-90" /><div><div className="eyebrow">平台能力</div><div className="mt-1 text-sm text-[#9bb7b0]">REST、SSE、MCP、A2A 等集成能力</div></div></div>
                  <span className="status-badge status-ok">次要</span>
                </summary>
                <div className="px-5 pb-1"><CapabilityStrip capabilities={capabilities} /></div>
              </details>
              {selectedJob && ['queued', 'running'].includes(selectedJob.status) && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#5c4930] bg-[#211d16] px-5 py-4"><div className="flex items-center gap-3"><Ban size={16} className="text-[#e6c875]" /><div><div className="text-sm font-medium text-[#f1dfaa]">任务控制</div><div className="mt-1 text-xs text-[#aa9767]">排队中的任务会立即取消，运行中的任务采用协作式取消。</div></div></div><button onClick={() => void cancelSelectedJob()} disabled={selectedJob.cancel_requested} className="rounded-lg border border-[#80643c] px-3 py-2 text-xs font-medium text-[#f1d889] transition hover:bg-[#392d1c] disabled:cursor-not-allowed disabled:opacity-50">{selectedJob.cancel_requested ? '取消请求已发送' : '取消任务'}</button></div>}
              {selectedJob && ['failed', 'cancelled'].includes(selectedJob.status) && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#3f527c] bg-[#111d32] px-5 py-4"><div className="flex items-center gap-3"><RefreshCw size={16} className="text-[#aebfff]" /><div><div className="text-sm font-medium text-[#d7ddff]">任务恢复</div><div className="mt-1 text-xs text-[#99a6cf]">保留原任务记录，复制原始参数重新提交。</div></div></div><button aria-label="Retry selected task" onClick={() => void retryJob(selectedJob)} disabled={loading} className="rounded-lg bg-[#aebfff] px-3 py-2 text-xs font-semibold text-[#111a34] transition hover:bg-[#c4d0ff] disabled:cursor-not-allowed disabled:opacity-50">重试任务</button></div>}

              <section className={`grid gap-5 xl:grid-cols-[1.08fr_0.92fr] ${mode === 'sequence' ? 'xl:items-start' : ''}`}>
                <div className="panel p-5 sm:p-6">
                  <div className="flex items-start justify-between gap-4"><div><div className="eyebrow">01 / 启动任务</div><h2 className="mt-2 text-xl font-semibold">启动一条研究路径</h2></div><div className="rounded-xl border border-[#21443f] bg-[#102b2a] p-2.5 text-[#8fe5c1]"><Play size={17} /></div></div>
                  <div className="mt-7 grid grid-cols-2 gap-1 rounded-xl bg-[#071719] p-1 sm:grid-cols-5"><button onClick={() => setMode('research')} className={`mode-tab ${mode === 'research' ? 'mode-tab-active' : ''}`}><Workflow size={14} />研究规划</button><button onClick={() => setMode('rnaseq')} className={`mode-tab ${mode === 'rnaseq' ? 'mode-tab-active' : ''}`}><Activity size={14} />RNA-seq 上传</button><button onClick={() => setMode('variant')} className={`mode-tab ${mode === 'variant' ? 'mode-tab-active' : ''}`}><GitBranch size={14} />VCF 变异</button><button onClick={() => setMode('sequence')} className={`mode-tab ${mode === 'sequence' ? 'mode-tab-active' : ''}`}><Dna size={14} />mRNA 设计</button><button onClick={() => setMode('cadd')} className={`mode-tab ${mode === 'cadd' ? 'mode-tab-active' : ''}`}><Beaker size={14} />CADD 对接</button></div>
                  {mode === 'research' ? <>
                    <div className="mt-6 rounded-xl border border-[#28524b] bg-[#102b2a]/60 p-4"><label className="field-label" htmlFor="research-preset">研究场景</label><select id="research-preset" value={researchPreset} onChange={(event) => applyResearchPreset(event.target.value as ResearchPreset)} className="input-control"><option value="custom">通用研究规划</option><option value="bgi_multiomics">BGI 多组学</option><option value="online_evidence">在线证据检索</option></select><p className="mt-2 text-xs leading-5 text-[#789791]">场景只负责填入默认任务和样例输入，后续内容仍可修改，并统一进入计划检查。</p></div>
                    <label className="mt-6 block"><span className="field-label">科学问题</span><textarea value={task} onChange={(event) => { setTask(event.target.value); setResearchPlan(null) }} rows={4} className="input-area" placeholder="描述你希望 Agent 协助完成的研究任务" /></label>
                    <div className="mt-5 grid gap-4 sm:grid-cols-[0.8fr_1.2fr]"><div><label className="field-label" htmlFor="planner-mode">规划器模式</label><select id="planner-mode" value={plannerMode} onChange={(event) => { setPlannerMode(event.target.value as PlannerMode); setResearchPlan(null) }} className="input-control"><option value="auto">自动：配置密钥时使用模型</option><option value="deterministic">确定性：规则规划</option><option value="llm">LLM：必须调用模型</option></select></div><div className="flex items-end pb-1 text-xs leading-5 text-[#688983]">自动模式会在配置模型密钥时调用 LLM；模型不可用时保留回退原因并使用确定性规划。</div></div>
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
                     <div className="mt-6 rounded-xl border border-[#28524b] bg-[#102b2a]/60 p-4"><label className="field-label" htmlFor="rnaseq-input-mode">输入来源</label><select id="rnaseq-input-mode" value={rnaInputMode} onChange={(event) => { const next = event.target.value as RnaInputMode; setRnaInputMode(next); if (next === 'fixture') setRnaFiles({ fastq_r1: [], fastq_r2: [], reference_fasta: [], annotation_gtf: [], metadata: [], gene_sets: [] }); setResearchPlan(null) }} className="input-control"><option value="fixture">仓库样例：原生 RNA-seq</option><option value="upload">上传自定义文件</option></select><p className="mt-2 text-xs leading-5 text-[#789791]">仓库样例会自动使用双端 FASTQ、参考基因组、GTF、元数据和基因集；切换为自定义后可上传自己的文件。</p></div>
                     <label className="mt-6 block"><span className="field-label">RNA-seq 分析说明（可选）</span><textarea value={rnaseqTask} onChange={(event) => { setRnaseqTask(event.target.value); setResearchPlan(null) }} rows={3} className="input-area" placeholder="例如：双端 RNA-seq，完成质控、比对和表达分析" /></label>
                     <div className="mt-5 grid gap-3 sm:grid-cols-2">
                       <RnaFileField id="rna-r1-files" label="R1 FASTQ（可多选）" files={rnaFiles.fastq_r1} fixture={rnaInputMode === 'fixture' ? '6 个仓库样例文件' : undefined} multiple accept=".fastq,.fq,.fastq.gz,.fq.gz,application/gzip,text/plain" uploading={uploadingRnaFile === 'fastq_r1'} onChange={(files) => void handleRnaFileUpload('fastq_r1', files)} />
                       <RnaFileField id="rna-r2-files" label="R2 FASTQ（可多选）" files={rnaFiles.fastq_r2} fixture={rnaInputMode === 'fixture' ? '6 个仓库样例文件' : undefined} multiple accept=".fastq,.fq,.fastq.gz,.fq.gz,application/gzip,text/plain" uploading={uploadingRnaFile === 'fastq_r2'} onChange={(files) => void handleRnaFileUpload('fastq_r2', files)} />
                       <RnaFileField id="rna-reference-file" label="参考基因组 FASTA" files={rnaFiles.reference_fasta} fixture={rnaInputMode === 'fixture' ? '仓库样例 reference.fa' : undefined} accept=".fa,.fasta,.fna,text/plain" uploading={uploadingRnaFile === 'reference_fasta'} onChange={(files) => void handleRnaFileUpload('reference_fasta', files)} />
                       <RnaFileField id="rna-gtf-file" label="基因注释 GTF" files={rnaFiles.annotation_gtf} fixture={rnaInputMode === 'fixture' ? '仓库样例 genes.gtf' : undefined} accept=".gtf,.gff,.gff3,text/plain" uploading={uploadingRnaFile === 'annotation_gtf'} onChange={(files) => void handleRnaFileUpload('annotation_gtf', files)} />
                       <RnaFileField id="rna-metadata-file" label="样本元数据 CSV（可选）" files={rnaFiles.metadata} fixture={rnaInputMode === 'fixture' ? '仓库样例 metadata.csv' : undefined} accept=".csv,.tsv,text/csv,text/tab-separated-values" uploading={uploadingRnaFile === 'metadata'} onChange={(files) => void handleRnaFileUpload('metadata', files)} />
                       <RnaFileField id="rna-gene-sets-file" label="基因集 CSV（可选）" files={rnaFiles.gene_sets} fixture={rnaInputMode === 'fixture' ? '仓库样例 gene_sets.csv' : undefined} accept=".csv,.tsv,text/csv,text/tab-separated-values" uploading={uploadingRnaFile === 'gene_sets'} onChange={(files) => void handleRnaFileUpload('gene_sets', files)} />
                     </div>
                     <RnaPreflightCard items={rnaseqPreflight.checks} pairMismatch={rnaseqPreflight.pairMismatch} />
                     <p className="mt-3 text-xs leading-5 text-[#688983]">R1/R2 可批量选择；实际执行链由已提供的 FASTQ、参考基因组、GTF、元数据和基因集决定。</p>
                  </> : mode === 'variant' ? <>
                    <label className="mt-6 block"><span className="field-label">VCF 分析说明（可选）</span><textarea value={variantTask} onChange={(event) => { setVariantTask(event.target.value); setResearchPlan(null) }} rows={3} className="input-area" placeholder="例如：注释 VCF 并检索相关基因证据" /></label>
                    <div className="mt-5 grid gap-3 sm:grid-cols-2">
                      <ResearchFileField id="vcf-file" label="VCF / VCF.GZ 输入文件" accept=".vcf,.gz,text/plain" file={uploadedFiles.vcf} uploading={uploadingFile === 'vcf'} onChange={(file) => void handleResearchFileUpload('vcf', file)} />
                      <ResearchFileField id="annotation-file" label="基因区间 CSV" accept=".csv,.tsv,text/csv,text/tab-separated-values" file={uploadedFiles.annotation} uploading={uploadingFile === 'annotation'} onChange={(file) => void handleResearchFileUpload('annotation', file)} />
                    </div>
                    <div className="mt-5 grid gap-4 sm:grid-cols-2"><div><label className="field-label" htmlFor="variant-backend">注释后端</label><select id="variant-backend" value={variantBackend} onChange={(event) => { setVariantBackend(event.target.value); setResearchPlan(null) }} className="input-control"><option value="auto">自动：VCF ANN → 本地区间</option><option value="vcf_ann">仅使用 VCF ANN</option><option value="local">本地区间表</option></select></div><div><label className="field-label" htmlFor="variant-evidence-provider">证据来源</label><select id="variant-evidence-provider" value={evidenceProvider} onChange={(event) => { setEvidenceProvider(event.target.value); setResearchPlan(null) }} className="input-control"><option value="local">本地样例</option><option value="ncbi_gene">NCBI Gene</option><option value="uniprot">UniProt</option><option value="pubmed">PubMed</option><option value="kegg">KEGG</option></select></div></div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">未上传文件时使用可复现样例；结果会保留注释来源和外部工具可用性。</p>
                  </> : mode === 'sequence' ? <SequenceDesignInput protein={protein} molecule={sequenceMolecule} method={sequenceMethod} useVaxpress={sequenceUseVaxpress} structureId={sequenceStructureId} onProteinChange={(value) => { setProtein(value); setResearchPlan(null) }} onMoleculeChange={(value) => { setSequenceMolecule(value); setResearchPlan(null) }} onMethodChange={(value) => { setSequenceMethod(value); setResearchPlan(null) }} onUseVaxpressChange={(value) => { setSequenceUseVaxpress(value); setResearchPlan(null) }} onStructureChange={setSequenceStructureId} /> : <>
                    <label className="mt-6 block"><span className="field-label">CADD 筛选任务</span><textarea value="运行可复现的 CADD 虚拟筛选流程并优先排序对接命中物" readOnly rows={3} className="input-area" /></label>
                    <div className="mt-5 grid gap-3 sm:grid-cols-2"><ResearchFileField id="receptor-file" label="受体结构 PDB / PDBQT" accept=".pdb,.pdbqt,text/plain" file={uploadedFiles.receptor} uploading={uploadingFile === 'receptor'} onChange={(file) => void handleResearchFileUpload('receptor', file)} /><ResearchFileField id="ligand-library-file" label="外部分子数据集 CSV" accept=".csv,.tsv,text/csv,text/tab-separated-values" file={uploadedFiles.ligand_library} uploading={uploadingFile === 'ligand_library'} onChange={(file) => void handleResearchFileUpload('ligand_library', file)} /></div>
                    <div className="mt-5 grid gap-4 sm:grid-cols-2"><div><label className="field-label" htmlFor="cadd-max-ligands">演示候选数</label><input id="cadd-max-ligands" type="number" min="1" max="17" value={caddMaxLigands} onChange={(event) => { setCaddMaxLigands(event.target.value); setResearchPlan(null) }} className="input-control font-mono" /><span className="mt-2 block text-xs text-[#688983]">完整筛选可调到 17，演示建议 3。</span></div><div><label className="field-label" htmlFor="cadd-exhaustiveness">Vina 搜索强度</label><input id="cadd-exhaustiveness" type="number" min="1" max="32" value={caddExhaustiveness} onChange={(event) => { setCaddExhaustiveness(event.target.value); setResearchPlan(null) }} className="input-control font-mono" /><span className="mt-2 block text-xs text-[#688983]">数值越高越稳定，但运行时间更长。</span></div></div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">CADD 入口会记录受体、数据集、Vina 参数与结果报告。Docker 优先读取本机挂载的 data/4hjo.pdb 和 output/bindingdb_egfr_10000.csv，也支持上传替换。</p>
                  </>}
                  <div className="mt-6 flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2 font-mono text-[10px] text-[#66847e]"><CircleDot size={13} className="text-[#70e3ad]" />异步 / 可追溯 / 可重放</div><button onClick={submitRun} disabled={loading || (mode === 'research' ? !task.trim() : mode === 'rnaseq' ? !rnaseqTask.trim() || rnaseqPreflight.pairMismatch : mode === 'variant' ? !variantTask.trim() : mode === 'sequence' ? !protein.trim() : false)} className="group inline-flex items-center gap-2 rounded-xl bg-[#a8f0d2] px-4 py-2.5 text-sm font-semibold text-[#092521] transition hover:bg-[#c6f8e1] disabled:cursor-not-allowed disabled:opacity-50">{loading ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />}{loading ? '执行中…' : '开始运行'}<ArrowUpRight size={14} className="transition group-hover:translate-x-0.5 group-hover:-translate-y-0.5" /></button></div>
                </div>

                <div className="panel flex min-h-[326px] flex-col p-5 sm:p-6"><div className="flex items-start justify-between"><div><div className="eyebrow">02 / 执行流</div><h2 className="mt-2 text-xl font-semibold">实时执行轨迹</h2></div><div className="flex items-center gap-1.5 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#8fe5c1]"><span className="size-1.5 animate-pulse rounded-full bg-[#70e3ad]" />SSE</div></div>{selectedJob ? <div className="mt-7 flex flex-1 flex-col"><div className="flex items-center justify-between border-b border-white/10 pb-4"><div><div className="font-mono text-[11px] text-[#6f9189]">{formatJobId(selectedJob.job_id)}</div><div className="mt-1 text-sm font-medium">{selectedJob.tool}</div></div><StatusBadge status={selectedJob.status} /></div><div className="mt-5 space-y-3">{events.slice(-4).map((event, index) => <div key={`${event.at}-${index}`} className="flex items-start gap-3 text-xs"><div className="mt-1.5 size-1.5 rounded-full bg-[#83e3bc] shadow-[0_0_12px_#83e3bc]" /><div className="min-w-0 flex-1"><div className="text-[#b2cbc4]">{event.detail}</div><div className="mt-1 font-mono text-[10px] text-[#5f7c76]">{event.at} · {event.status}</div></div></div>)}</div><div className="mt-auto flex items-center gap-2 pt-5 font-mono text-[10px] text-[#64827b]"><Clock3 size={13} />{selectedJob.status === 'completed' ? `完成于 ${formatTime(selectedJob.finished_at)}` : '等待状态更新…'}</div></div> : <EmptyStream />}</div>
              </section>

              {selectedJob?.status === 'completed' && <JobResultSummary job={selectedJob} structureId={sequenceStructureId} onDownload={(path) => void downloadJobArtifact(selectedJob.job_id, path)} onOpenReport={(path) => void previewJobArtifact(selectedJob.job_id, path)} />}
              {reportPreview && <ReportPreviewModal preview={reportPreview} onClose={closeReportPreview} />}

              {(mode !== 'sequence' && (mode === 'research' || mode === 'rnaseq' || mode === 'variant' || mode === 'cadd')) && selectedJob?.tool !== 'research_execute' && <ResearchPlanCard plan={researchPlan} loading={loading && selectedJob?.tool === 'research_plan'} onExecute={() => void executeResearchPlan()} />}

              <section className="panel mt-5 overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-5 sm:px-6"><div><div className="eyebrow">03 / 最近任务</div><h2 className="mt-2 text-xl font-semibold">最近任务</h2></div><button onClick={() => void refresh()} className="inline-flex items-center gap-2 rounded-lg border border-white/10 px-3 py-2 text-xs text-[#9bb7b0] transition hover:border-[#4f8c7d] hover:text-[#d6eee7]"><RefreshCw size={13} />刷新</button></div>{jobs.length ? <div className="overflow-x-auto"><table className="w-full min-w-[680px] text-left text-sm"><thead className="bg-white/[0.025] font-mono text-[10px] tracking-[0.12em] text-[#63817b]"><tr><th className="px-5 py-3 font-normal sm:px-6">任务 ID</th><th className="px-5 py-3 font-normal">工具</th><th className="px-5 py-3 font-normal">状态</th><th className="px-5 py-3 font-normal">创建时间</th><th className="px-5 py-3 font-normal" /></tr></thead><tbody>{jobs.map((job) => <tr key={job.job_id} onClick={() => { setSelectedJob(job); setEvents([]) }} className="cursor-pointer border-t border-white/[0.06] transition hover:bg-white/[0.035]"><td className="px-5 py-4 font-mono text-xs text-[#81aaa1] sm:px-6">{formatJobId(job.job_id)}</td><td className="px-5 py-4 font-medium text-[#c7ddd7]">{job.tool}</td><td className="px-5 py-4"><StatusBadge status={job.status} /></td><td className="px-5 py-4 font-mono text-xs text-[#66837d]">{formatTime(job.created_at)}</td><td className="px-5 py-4 text-right text-[#6b8f87]"><ChevronRight size={15} /></td></tr>)}</tbody></table></div> : <div className="px-6 py-12 text-center text-sm text-[#66837d]">还没有运行记录，先启动一条研究路径。</div>}</section>
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
    { key: 'rest', label: 'REST / OpenAPI', icon: Server, detail: capabilities.interfaces.rest?.openapi || '/openapi.json' },
    { key: 'sse', label: 'SSE 事件流', icon: Radio, detail: capabilities.interfaces.sse?.endpoint || '任务事件流' },
    { key: 'mcp', label: 'MCP / STDIO', icon: Terminal, detail: `${capabilities.interfaces.mcp?.tool_count || capabilities.tool_count} 个工具` },
    { key: 'embedded', label: '嵌入式调用', icon: Boxes, detail: capabilities.interfaces.embedded?.entrypoint || 'run_tool' },
    { key: 'a2a', label: 'A2A / JSON-RPC', icon: GitBranch, detail: capabilities.interfaces.a2a?.endpoint || '/a2a' },
  ]
  return <section className="mb-5" aria-label="集成能力"><div className="mb-2 flex items-center justify-between"><div className="eyebrow">集成能力</div><div className="font-mono text-[10px] text-[#66857e]">{capabilities.tool_count} 个工具契约</div></div><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">{cards.map((card) => { const capability = capabilities.interfaces[card.key]; const Icon = card.icon; const available = capability?.status === 'available'; return <div key={card.key} className="rounded-xl border border-white/[0.08] bg-white/[0.035] px-3 py-3"><div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-xs font-medium text-[#c9e5dc]"><Icon size={14} className="text-[#8fe5c1]" />{card.label}</div><span className={`status-badge ${available ? 'status-ok' : 'status-failed'}`}>{available ? '就绪' : capability?.status || '未知'}</span></div><div className="mt-2 truncate font-mono text-[9px] text-[#66857e]" title={card.detail}>{card.detail}</div></div> })}</div></section>
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
      <div className="min-w-0"><div className="truncate text-xs font-medium text-[#b8d8ce]">{uploading ? '上传中…' : file?.filename || '选择输入文件'}</div><div className="mt-1 truncate font-mono text-[9px] text-[#668983]">{file ? `${file.size_bytes} 字节 · ${file.sha256.slice(0, 12)}` : '服务端安全存储'}</div></div>
      {uploading ? <RefreshCw size={15} className="shrink-0 animate-spin text-[#8fe5c1]" /> : <Upload size={15} className="shrink-0 text-[#78cdaa]" />}
    </label>
  </div>
}

function RnaFileField({ id, label, accept, files, fixture, multiple = false, uploading, onChange }: { id: string; label: string; accept?: string; files: UploadedFile[]; fixture?: string; multiple?: boolean; uploading: boolean; onChange: (files: FileList | null) => void }) {
  const fixtureActive = Boolean(fixture) && files.length === 0
  return <div>
    <div className="field-label">{label}</div>
    <label htmlFor={id} className="flex min-h-[88px] cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed border-[#315d55] bg-[#071719]/70 px-3 py-3 transition hover:border-[#71cba7] hover:bg-[#102b2a]">
      <input id={id} type="file" accept={accept} multiple={multiple} className="sr-only" onChange={(event) => { onChange(event.target.files); event.currentTarget.value = '' }} />
      <div className="min-w-0"><div className="truncate text-xs font-medium text-[#b8d8ce]">{uploading ? '上传中…' : fixtureActive ? fixture : files.length ? `${files.length} 个文件已选择` : '选择输入文件'}</div><div className="mt-1 truncate font-mono text-[9px] text-[#668983]">{fixtureActive ? '使用仓库样例，可切换为自定义上传' : files.length ? files.map((file) => file.filename).join(', ') : '服务端安全存储并计算 SHA-256'}</div></div>
      {uploading ? <RefreshCw size={15} className="shrink-0 animate-spin text-[#8fe5c1]" /> : <Upload size={15} className="shrink-0 text-[#78cdaa]" />}
    </label>
  </div>
}

function RnaPreflightCard({ items, pairMismatch }: { items: RnaPreflightItem[]; pairMismatch: boolean }) {
  const requiredCount = items.filter((item) => item.required).length
  const readyRequiredCount = items.filter((item) => item.required && item.ready).length
  const allRequiredReady = !pairMismatch && readyRequiredCount === requiredCount
  return <div className="mt-5 rounded-xl border border-[#244b45] bg-[#0a211f]/75 p-4" role="status" aria-live="polite">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="field-label">运行前检查</div><div className="mt-1 text-xs text-[#9bc3b8]">{readyRequiredCount}/{requiredCount} 个任务必需输入已满足</div></div><span className={`status-badge ${pairMismatch ? 'status-failed' : allRequiredReady ? 'status-ok' : 'status-running'}`}><span className="size-1.5 rounded-full bg-current" />{pairMismatch ? '配对数量不一致' : allRequiredReady ? '输入已就绪' : '待补齐输入'}</span></div>
    <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{items.map((item) => <div key={item.label} className="flex min-w-0 items-start gap-2 rounded-lg border border-white/[0.06] bg-[#071719]/70 px-2.5 py-2"><div className={`mt-0.5 shrink-0 ${item.ready ? 'text-[#70e3ad]' : item.required ? 'text-[#e6c875]' : 'text-[#6d8d86]'}`}>{item.ready ? <Check size={13} /> : <XCircle size={13} />}</div><div className="min-w-0"><div className="truncate text-[11px] font-medium text-[#b8d8ce]">{item.label}{item.required ? <span className="ml-1 text-[#e6c875]">必需</span> : <span className="ml-1 text-[#688983]">可选</span>}</div><div className="mt-0.5 truncate text-[10px] text-[#6f9189]">{item.detail}</div></div></div>)}</div>
  </div>
}

function ResearchPlanCard({ plan, loading, onExecute }: { plan: ResearchPlan | null; loading: boolean; onExecute: () => void }) {
  const execution = plan?.execution
  if (!plan && !loading) return null
  return <section className="panel mt-5 overflow-hidden" aria-live="polite">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-5 sm:px-6">
      <div><div className="eyebrow">02B / 计划检查</div><h2 className="mt-2 text-xl font-semibold">执行前计划检查</h2></div>
      <div className="flex items-center gap-2 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#8fe5c1]"><Workflow size={12} />人工确认</div>
    </div>
    {!plan ? <div className="flex items-center gap-4 px-5 py-8 text-sm text-[#789791] sm:px-6"><div className="grid size-10 place-items-center rounded-xl border border-[#21443f] bg-[#102b2a] text-[#78cdaa]">{loading ? <RefreshCw size={17} className="animate-spin" /> : <Sparkles size={17} />}</div><div><div className="font-medium text-[#b7d3ca]">{loading ? '规划器正在检查任务…' : '提交科研问题后，这里会出现执行计划。'}</div><div className="mt-1 text-xs text-[#66857e]">计划会先展示领域、证据源、工具链和输入门槛。</div></div></div> : <div className="space-y-5 px-5 py-5 sm:px-6">
      <div className="flex flex-wrap items-center gap-2">
        {plan.selected_domains.map((domain) => <span key={domain} className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />{domainLabels[domain] || domain}</span>)}
        <span className="status-badge status-running">证据：{providerLabels[execution?.evidence_provider || plan.evidence_provider] || execution?.evidence_provider}</span>
        {plan.planner && <span className="status-badge">规划器：{plan.planner.backend === 'llm' ? 'LLM' : plan.planner.backend === 'deterministic' ? 'Deterministic' : plan.planner.backend}</span>}
        {plan.planner?.model && <span className="status-badge">模型：{plan.planner.model}</span>}
      </div>
      <div className="grid gap-4 lg:grid-cols-[0.7fr_1.3fr]">
        <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4">
          <div className="field-label">输入门槛</div>
          {execution?.ready ? <div className="flex items-center gap-2 text-sm text-[#9be6c5]"><Check size={15} />输入已满足，可执行</div> : <div className="text-sm text-[#efb19f]">缺少必要输入</div>}
          {!execution?.ready && <div className="mt-3 flex flex-wrap gap-1.5">{(execution?.missing_inputs || []).map((item) => <span key={item} className="rounded-md border border-[#70483f] bg-[#2b1b1b] px-2 py-1 font-mono text-[10px] text-[#e9a694]">{item}</span>)}</div>}
          {execution?.rationale?.length ? <div className="mt-4 space-y-2 text-xs leading-5 text-[#789791]">{execution.rationale.map((item) => <div key={item} className="flex gap-2"><span className="mt-2 size-1 rounded-full bg-[#78cdaa]" />{item}</div>)}</div> : null}
          {plan.planner?.fallback_reason && <div className="mt-4 rounded-lg border border-[#705b35] bg-[#251f15] px-3 py-2 text-xs leading-5 text-[#d8c18a]">规划器回退：{plan.planner.fallback_reason}</div>}
        </div>
        <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4">
          <div className="field-label">已选工具链</div>
          <div className="flex flex-wrap gap-2">{(execution?.selected_tools || []).map((tool, index) => <div key={`${tool}-${index}`} className="inline-flex items-center gap-2 rounded-lg border border-[#28524b] bg-[#102b2a] px-2.5 py-2 font-mono text-[10px] text-[#b9e6d5]"><span className="grid size-4 place-items-center rounded-full bg-[#8fe5c1] text-[9px] font-bold text-[#092521]">{index + 1}</span>{tool}</div>)}</div>
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-white/[0.08] pt-4"><div className="text-xs text-[#66857e]">规划任务：<span className="text-[#aac8bf]">{plan.task}</span></div><button onClick={onExecute} disabled={loading || !execution?.ready} className="inline-flex items-center gap-2 rounded-xl bg-[#a8f0d2] px-4 py-2.5 text-sm font-semibold text-[#092521] transition hover:bg-[#c6f8e1] disabled:cursor-not-allowed disabled:opacity-40"><Check size={15} />确认并执行</button></div>
    </div>}
  </section>
}

function EmptyStream() {
  return <div className="flex flex-1 flex-col items-center justify-center text-center"><div className="grid size-14 place-items-center rounded-2xl border border-[#21443f] bg-[#102b2a] text-[#78cdaa]"><Radio size={23} /></div><div className="mt-4 text-sm font-medium text-[#b1cbc4]">等待任务流</div><div className="mt-2 max-w-[220px] text-xs leading-5 text-[#64827b]">提交任务后，这里会实时显示状态和可追溯事件。</div></div>
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
    { value: 'linear', label: '线性 mRNA', name: '线性 mRNA', detail: '常规翻译模板' },
    { value: 'circ', label: '环状 RNA', name: '环状 RNA', detail: '保留环状分子上下文' },
    { value: 'sa', label: '自扩增 RNA', name: '自扩增 RNA', detail: '记录分子类型' },
  ]
  const methodOptions: Array<{ value: SequenceMethod; label: string; detail: string }> = [
    { value: 'greedy', label: '确定性贪心', detail: '内置规则，结果可复现' },
    { value: 'vaxpress', label: 'VaxPress 适配器', detail: '外部后端可用时接入' },
  ]
  const steps = [
    { number: '01', label: '输入', detail: '蛋白序列' },
    { number: '02', label: '优化', detail: '密码子策略' },
    { number: '03', label: '验证', detail: '翻译回译' },
    { number: '04', label: '基准比较', detail: '基线比较' },
  ]
  return <div className="mt-6 space-y-4">
    <section className="rounded-2xl border border-[#28524b] bg-[linear-gradient(135deg,rgba(16,43,42,.82),rgba(7,23,25,.92))] p-4" aria-label="mRNA 设计流程">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="field-label mb-0 text-[#8fe5c1]">mRNA-Forge / 序列优化工作区</div><h3 className="mt-1 text-base font-semibold text-[#e4f8ef]">从蛋白序列生成可验证 mRNA</h3><p className="mt-1 text-xs leading-5 text-[#82a79e]">保留独立项目的确定性计算、质量画像和报告能力，并接入统一任务闭环。</p></div><div className="flex flex-wrap items-center gap-1.5"><span className="status-badge status-ok">可审计</span><span className="status-badge status-queued">可复现</span></div></div>
      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">{steps.map((step, index) => <div key={step.number} className={`rounded-xl border px-3 py-2.5 ${index === 0 ? 'border-[#4c9c7d] bg-[#123631]' : 'border-white/[0.07] bg-[#071719]/60'}`}><div className="font-mono text-[10px] text-[#8fe5c1]">{step.number}</div><div className="mt-1 text-[11px] font-medium text-[#c9e5dc]">{step.label}</div><div className="mt-0.5 text-[10px] text-[#6f9189]">{step.detail}</div></div>)}</div>
    </section>

    <section className="rounded-2xl border border-white/[0.08] bg-[#071719]/70 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="field-label mb-0">01 / 目标蛋白</div><div className="mt-1 text-sm font-medium text-[#cfe9df]">目标氨基酸序列</div></div><div className="flex items-center gap-3"><span className="font-mono text-[10px] text-[#6f9189]">{protein.length} aa</span><button type="button" onClick={() => onProteinChange(luciferaseDemoProtein)} className="rounded-lg border border-white/[0.1] px-2.5 py-1.5 text-[10px] text-[#9fc4b8] transition hover:border-[#71cba7] hover:text-[#e8fff5]">加载荧光素酶示例（550 aa）</button></div></div>
      <textarea aria-label="目标蛋白序列" value={protein} onChange={(event) => onProteinChange(event.target.value.toUpperCase())} rows={3} className="input-area mt-3 font-mono tracking-[0.16em]" placeholder="例如 MKT..." />
      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[10px] leading-5 text-[#6f9189]"><span>支持标准单字母氨基酸符号；后端会在运行前校验序列。</span><span className="font-mono">蛋白质 → mRNA</span></div>
    </section>

    <section className="rounded-2xl border border-white/[0.08] bg-[#071719]/70 p-4">
      <div className="field-label mb-0">01B / 分子形式</div><div className="mt-1 text-sm font-medium text-[#cfe9df]">选择分子类型</div>
      <div className="mt-3 grid gap-2 sm:grid-cols-3" role="radiogroup" aria-label="分子类型">{moleculeOptions.map((option) => <button key={option.value} type="button" role="radio" aria-checked={molecule === option.value} onClick={() => onMoleculeChange(option.value)} className={`rounded-xl border p-3 text-left transition ${molecule === option.value ? 'border-[#4c9c7d] bg-[#123631] shadow-[0_0_0_1px_rgba(143,229,193,.12)]' : 'border-white/[0.08] bg-[#0a211f]/60 hover:border-[#376b5d]'}`}><div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-[#d1eee2]">{option.name}</span>{molecule === option.value && <Check size={14} className="text-[#8fe5c1]" />}</div><div className="mt-1 font-mono text-[10px] text-[#6f9189]">{option.label}</div><div className="mt-2 text-[10px] text-[#86aaa0]">{option.detail}</div></button>)}</div>
    </section>

    <section className="rounded-2xl border border-white/[0.08] bg-[#071719]/70 p-4">
      <div className="field-label mb-0">02 / 优化策略</div><div className="mt-1 text-sm font-medium text-[#cfe9df]">选择优化后端</div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="优化策略">{methodOptions.map((option) => <button key={option.value} type="button" role="radio" aria-checked={method === option.value} onClick={() => onMethodChange(option.value)} className={`rounded-xl border p-3 text-left transition ${method === option.value ? 'border-[#4c9c7d] bg-[#123631]' : 'border-white/[0.08] bg-[#0a211f]/60 hover:border-[#376b5d]'}`}><div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-[#d1eee2]">{option.label}</span>{method === option.value && <span className="status-badge status-ok">已选</span>}</div><div className="mt-2 text-[10px] leading-5 text-[#86aaa0]">{option.detail}</div></button>)}</div>
    </section>

    <details className="rounded-xl border border-white/[0.08] bg-[#071719]/55">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-xs text-[#aac8bf] outline-none focus-visible:ring-2 focus-visible:ring-[#8fe5c1] focus-visible:ring-inset"><span>高级上下文 / 结构与外部适配器</span><span className="font-mono text-[10px] text-[#6f9189]">可选</span></summary>
      <div className="border-t border-white/[0.07] p-4"><div className="grid gap-4 sm:grid-cols-2"><div><label className="field-label" htmlFor="sequence-structure-id">可选 PDB ID</label><input id="sequence-structure-id" value={structureId} onChange={(event) => onStructureChange(event.target.value.toUpperCase().replace(/[^0-9A-Z]/g, '').slice(0, 4))} className="input-control font-mono uppercase" placeholder="例如 1LCI" /><span className="mt-2 block text-[10px] leading-5 text-[#6f9189]">仅在结构与目标蛋白匹配时加载 Mol* 上下文。</span></div><label className="flex items-start gap-3 rounded-xl border border-white/[0.08] bg-[#0a211f]/60 px-3 py-3 text-xs text-[#a9c8be]"><input type="checkbox" checked={useVaxpress} onChange={(event) => onUseVaxpressChange(event.target.checked)} className="mt-0.5 accent-[#8fe5c1]" /><span><span className="block font-medium text-[#d1eee2]">纳入 VaxPress 基准比较</span><span className="mt-1 block text-[10px] leading-5 text-[#6f9189]">未配置外部 mRNA-Forge 时记录回退，不会把确定性结果伪装成模型结果。</span></span></label></div></div>
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
    { title: '翻译一致性', detail: verified ? '优化序列可以翻译回目标蛋白，阅读框和起始密码子检查通过。' : '翻译回译未通过，不能直接进入后续实验设计。', tone: verified ? 'status-ok' : 'status-failed' },
    { title: '序列组成', detail: gc === undefined ? '缺少 GC 指标，建议先补充评分结果。' : gcInRange ? `GC ${gc.toFixed(1)}% 位于当前规则窗口 30–80% 内。` : `GC ${gc.toFixed(1)}% 超出当前规则窗口，需要人工复核。`, tone: gcInRange ? 'status-ok' : 'status-running' },
    { title: '密码子策略', detail: caiDelta === undefined ? '暂无可用基线，无法判断优化相对收益。' : `相对朴素基线的 CAI 变化为 ${caiDelta >= 0 ? '+' : ''}${caiDelta.toFixed(3)}，仅代表当前规则评分。`, tone: caiDelta !== undefined && caiDelta >= 0 ? 'status-ok' : 'status-running' },
    { title: '后端边界', detail: benchmarkStatus === 'not_configured' ? 'VaxPress 未配置，当前结果来自确定性后端；没有把回退结果当作模型结果。' : '当前结果已记录后端来源，可继续接入外部 mRNA-Forge。', tone: benchmarkStatus === 'not_configured' ? 'status-running' : 'status-ok' },
  ]
  const decisionReady = verified && checksPassed && gcInRange
  return <section id="sequence-interpretation" className="mt-4 scroll-mt-6 rounded-xl border border-[#3a6258] bg-[#0b2425]/80 p-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="flex items-start gap-3"><div className="grid size-9 place-items-center rounded-lg border border-[#28524b] bg-[#102b2a] text-[#8fe5c1]"><Sparkles size={16} /></div><div><div className="field-label mb-0">解读 / 可审计代理</div><h4 className="mt-1 text-sm font-semibold text-[#d8f4e8]">结果解读与下一步判断</h4></div></div><span className={`status-badge ${decisionReady ? 'status-ok' : 'status-running'}`}>{decisionReady ? '可供复核' : '需要人工复核'}</span></div>
    <div className="mt-4 grid gap-2 md:grid-cols-2">{findings.map((finding) => <div key={finding.title} className="rounded-lg border border-white/[0.07] bg-[#071719]/70 p-3"><div className="flex items-center justify-between gap-2"><span className="text-xs font-medium text-[#c8e6db]">{finding.title}</span><span className={`status-badge ${finding.tone}`}>{finding.tone === 'status-ok' ? '通过' : '复核'}</span></div><p className="mt-2 text-xs leading-5 text-[#86aaa0]">{finding.detail}</p></div>)}</div>
    <div className="mt-4 rounded-lg border border-[#28524b] bg-[#102b2a]/60 px-3 py-3 text-xs leading-5 text-[#8fb8ab]">解释来源：序列指标、规则检查、翻译验证和 benchmark 结果。它不是经过实验数据校准的表达量预测器，最终仍需结合目标宿主、UTR、修饰和实验验证。</div>
  </section>
}

function SequenceStructurePanel({ structureId }: { structureId: string }) {
  const [expanded, setExpanded] = useState(true)
  const pdbId = structureId.trim().toUpperCase()
  const valid = /^[0-9A-Z]{4}$/.test(pdbId)
  if (!valid) return <section className="mt-4 rounded-xl border border-[#70483f] bg-[#251a1a]/80 p-4 text-xs leading-5 text-[#e7ad9d]">PDB ID `{structureId}` 格式不正确。请输入四位结构编号，例如 `1LCI`。</section>
  const viewerUrl = `https://molstar.org/viewer/?pdb=${pdbId.toLowerCase()}`
  const embedUrl = `${viewerUrl}&hide-controls=1`
  return <section id="sequence-structure" className="mt-4 scroll-mt-6 overflow-hidden rounded-xl border border-[#365c78] bg-[#0b1c2a]/90">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-4 py-3"><div><div className="field-label mb-0 text-[#8faecb]">结构 / Mol*</div><h4 className="mt-1 text-sm font-semibold text-[#dcecff]">PDB {pdbId} 结构上下文</h4><p className="mt-1 text-[10px] text-[#7598ae]">交互式 3D 视图默认折叠，避免结果页被结构控制台打断。</p></div><div className="flex items-center gap-2"><button type="button" onClick={() => setExpanded((current) => !current)} className="inline-flex items-center gap-1.5 rounded-lg border border-[#365c78] bg-[#10263a] px-3 py-2 text-xs text-[#cbd4ff] transition hover:border-[#8fb8ff] hover:text-white">{expanded ? '收起 3D' : '查看 3D'}</button><a href={viewerUrl} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-[#405b96] bg-[#152442] px-3 py-2 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white">打开 Mol* <ArrowUpRight size={13} /></a></div></div>
    {expanded && <><div className="bg-[#06121b] p-2"><iframe title={`Molstar structure viewer ${pdbId}`} src={embedUrl} loading="lazy" allow="xr-spatial-tracking" className="h-[400px] w-full rounded-lg border border-white/[0.08] bg-[#071719]" /></div><div className="px-4 pb-4 text-xs leading-5 text-[#88a9be]">结构由 Mol* 官方 viewer 加载。若当前浏览器禁用 WebGL 或网络不可用，可使用右上角链接打开官方页面；平台不会把结构映射自动当成序列验证结果。</div></>}
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
  const moleculeLabels: Record<string, string> = { linear: '线性 mRNA', circ: '环状 RNA', sa: '自扩增 RNA' }
  const methodLabels: Record<string, string> = { greedy: '确定性贪心', vaxpress: 'VaxPress 适配器' }
  const expressionPercent = expression === undefined ? undefined : Math.min(100, Math.max(0, expression <= 1 ? expression * 100 : expression))
  const visibleCodons = codons.slice(0, 18)
  const remainingCodons = codons.slice(18)
  const windowSize = 30
  const gcWindows = Array.from({ length: Math.min(12, Math.max(1, Math.ceil(mrna.length / windowSize))) }, (_, index) => {
    const chunk = mrna.slice(index * windowSize, (index + 1) * windowSize)
    const gcValue = chunk ? ((chunk.match(/[GC]/g) || []).length / chunk.length) * 100 : 0
    return { label: `${index * windowSize + 1}-${Math.min(mrna.length, (index + 1) * windowSize)}`, value: gcValue }
  }).filter((item) => item.label.split('-')[0] !== '1' || mrna.length > 0)
  const qualityValues = [gc || 0, gc3 || 0, cai === undefined ? 0 : cai * 100, expressionPercent === undefined ? (checks.length ? (passedChecks / checks.length) * 100 : 0) : expressionPercent, result.verify === true ? 100 : 0]
  const benchmarkStatus = benchmarkPayload?.vaxpress ? String(benchmarkPayload.vaxpress) : ''
  const metricCards = [
    { label: 'GC 含量', value: gc === undefined ? '--' : `${gc.toFixed(1)}%`, tone: 'text-[#8fe5c1]' },
    { label: 'GC3', value: gc3 === undefined ? '--' : `${gc3.toFixed(1)}%`, tone: 'text-[#aebfff]' },
    { label: 'CAI', value: cai === undefined ? '--' : cai.toFixed(3), tone: 'text-[#f0d38b]' },
    { label: 'UpA / kb', value: upA === undefined ? '--' : upA.toFixed(2), tone: 'text-[#d1a8ff]' },
    { label: 'UpU / kb', value: upU === undefined ? '--' : upU.toFixed(2), tone: 'text-[#f1a99a]' },
    { label: '表达评分', value: expressionPercent === undefined ? '--' : `${expressionPercent.toFixed(1)}%`, tone: 'text-[#b3f4d4]' },
  ]
  return <section className="mt-5 rounded-2xl border border-[#28524b] bg-[linear-gradient(135deg,rgba(16,43,42,.96),rgba(8,25,29,.96))] p-5 sm:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><div className="eyebrow">序列设计 / 质量概览</div><h3 className="mt-2 text-lg font-semibold text-[#e4f8ef]">mRNA 优化结果</h3><p className="mt-1 text-xs text-[#7fa99e]">{moleculeLabels[String(result.molecule || 'linear')] || String(result.molecule || 'linear')} · {methodLabels[String(result.method || 'greedy')] || String(result.method || 'greedy')} · 优化 → 评分 → 验证</p></div>
      <div className="flex flex-wrap items-center justify-end gap-2"><span className={`status-badge ${result.verify === true ? 'status-ok' : 'status-running'}`}><span className="size-1.5 rounded-full bg-current" />{result.verify === true ? '翻译已验证' : String(result.verdict || '待复核')}</span>{reportPath && <><button onClick={() => onOpenReport(reportPath)} className="inline-flex items-center gap-1.5 rounded-full border border-[#405b96] bg-[#152442] px-2.5 py-1 font-mono text-[10px] text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><ArrowUpRight size={12} />查看报告</button><button onClick={() => onDownload(reportPath)} className="inline-flex items-center gap-1.5 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#b9e6d5] transition hover:border-[#71cba7] hover:text-white"><Download size={12} />下载 HTML</button></>}</div>
    </div>
    <nav aria-label="mRNA 结果导航" className="mt-4 flex flex-wrap gap-1.5 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-1.5 text-[10px]"><a href="#sequence-core-metrics" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">核心指标</a><a href="#sequence-quality" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">质量画像</a>{benchmarkRows.length > 0 && <a href="#sequence-benchmark" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">基准比较</a>}{structureId && <a href="#sequence-structure" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">结构</a>}<a href="#sequence-interpretation" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">解读</a></nav>
    <div className="mt-5 rounded-2xl border border-[#32665b] bg-[#061b1d]/80 p-4">
      <div className="flex items-center justify-between gap-3"><div className="field-label mb-0">优化后的 mRNA / {String(result.mrna_len || mrna.length)} nt</div><div className="font-mono text-[10px] text-[#6e9d91]">5&apos; → 3&apos;</div></div>
      <div className="mt-3 flex flex-wrap gap-1.5">{visibleCodons.map((codon, index) => <span key={`${codon}-${index}`} className="rounded-md border border-[#2b6457] bg-[#123631] px-2.5 py-2 font-mono text-sm tracking-[0.16em] text-[#d0f7e5]">{codon}</span>)}</div>
      {remainingCodons.length > 0 && <details className="mt-3 rounded-lg border border-white/[0.08] bg-[#071719]/70"><summary className="cursor-pointer px-3 py-2.5 text-xs text-[#9fc4b8]">查看完整序列（剩余 {remainingCodons.length} 个密码子）</summary><div className="border-t border-white/[0.07] p-3"><pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-5 tracking-[0.08em] text-[#b9e6d5]">{mrna}</pre></div></details>}
      {!mrna && <div className="mt-2 text-xs text-[#789791]">结果中没有返回序列文本，请下载完整 JSON 查看。</div>}
    </div>
    <div id="sequence-core-metrics" className="mt-4 scroll-mt-6 grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">{metricCards.map((card) => <div key={card.label} className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">{card.label}</div><div className={`mt-2 font-mono text-xl ${card.tone}`}>{card.value}</div></div>)}</div>
    {expressionPercent !== undefined && <div className="mt-4 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="flex items-center justify-between text-[10px] text-[#86a59e]"><span className="font-mono tracking-[0.12em]">表达评分 · 启发式</span><span className="font-mono text-[#d4f7e6]">{expressionPercent.toFixed(1)}%</span></div><div className="mt-2 h-2 overflow-hidden rounded-full bg-[#17312f]"><div className="h-full rounded-full bg-gradient-to-r from-[#4dba91] to-[#b3f4d4]" style={{ width: `${expressionPercent}%` }} /></div></div>}
    <div id="sequence-quality" className="mt-4 scroll-mt-6 grid gap-4 lg:grid-cols-[0.8fr_1.2fr]">
      <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="field-label mb-0">质量雷达</div><SequenceQualityRadar values={qualityValues} /></div>
      <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="flex items-center justify-between gap-3"><div className="field-label mb-0">滑动窗口 GC / {windowSize} nt</div><span className="font-mono text-[10px] text-[#83e3bc]">{gcWindows.length} 个窗口</span></div>{gcWindows.length ? <div className="mt-5 space-y-3">{gcWindows.map((window) => <div key={window.label} className="grid grid-cols-[78px_1fr_48px] items-center gap-3"><span className="font-mono text-[10px] text-[#6f9189]">{window.label}</span><div className="h-2 overflow-hidden rounded-full bg-[#17312f]"><div className={`h-full rounded-full ${window.value >= 30 && window.value <= 80 ? 'bg-[#74d7ad]' : 'bg-[#e6c875]'}`} style={{ width: `${Math.max(2, Math.min(100, window.value))}%` }} /></div><span className="text-right font-mono text-[10px] text-[#b7dace]">{window.value.toFixed(1)}%</span></div>)}</div> : <div className="mt-5 text-xs text-[#6f9189]">暂无序列窗口。</div>}</div>
    </div>
    {checks.length > 0 && <div className="mt-4 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="flex items-center justify-between gap-3"><div className="field-label mb-0">规则检查</div><span className="font-mono text-[10px] text-[#83e3bc]">{passedChecks}/{checks.length} 通过</span></div><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{checks.map((check, index) => <div key={`${check.name}-${index}`} className="flex items-start gap-2 rounded-lg border border-white/[0.06] px-3 py-2"><Check size={13} className={`mt-0.5 shrink-0 ${check.passed ? 'text-[#70e3ad]' : 'text-[#ec9b87]'}`} /><div className="min-w-0"><div className="truncate text-xs text-[#c5e1d7]">{check.name}</div>{check.detail && <div className="mt-1 truncate text-[10px] text-[#6f9189]">{check.detail}</div>}</div></div>)}</div></div>}
    {benchmarkRows.length > 0 && <div className="mt-4 overflow-hidden rounded-xl border border-white/[0.08] bg-[#071719]/70"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-4 py-3"><div><div className="field-label mb-0">基准 / 基线比较</div><div className="mt-1 text-xs text-[#6f9189]">与朴素反向翻译基线比较关键序列指标</div></div>{benchmarkStatus && <span className={`status-badge ${benchmarkStatus === 'not_configured' ? 'status-running' : 'status-ok'}`}>{benchmarkStatus === 'not_configured' ? '已记录 VaxPress 回退' : `VaxPress：${benchmarkStatus}`}</span>}</div><div className="overflow-x-auto"><table className="w-full min-w-[620px] text-left text-xs"><thead className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]"><tr><th className="px-4 py-3 font-normal">方法</th><th className="px-4 py-3 font-normal">GC</th><th className="px-4 py-3 font-normal">GC3</th><th className="px-4 py-3 font-normal">CAI</th><th className="px-4 py-3 font-normal">Δ CAI</th><th className="px-4 py-3 font-normal">结论</th></tr></thead><tbody>{benchmarkRows.map((row) => <tr key={row.method} className="border-t border-white/[0.06]"><td className="px-4 py-3 font-medium text-[#c8e6db]">{row.method === 'naive' ? '朴素基线' : row.method === 'greedy' ? '贪心优化' : row.method}</td><td className="px-4 py-3 font-mono text-[#9fe5c5]">{percentMetric(row.metrics, ['gc', 'GC%'])?.toFixed(1) || '--'}%</td><td className="px-4 py-3 font-mono text-[#aebfff]">{percentMetric(row.metrics, ['gc3', 'GC3%'])?.toFixed(1) || '--'}%</td><td className="px-4 py-3 font-mono text-[#f0d38b]">{metricNumber(row.metrics, ['cai', 'CAI'])?.toFixed(3) || '--'}</td><td className="px-4 py-3 font-mono text-[#b9e6d5]">{sequenceMetricDelta(row, baseline, ['cai', 'CAI'])}</td><td className="px-4 py-3"><span className={`status-badge ${row.verdict === 'PASS' ? 'status-ok' : 'status-running'}`}>{row.verdict === 'PASS' ? '通过' : '复核'}</span></td></tr>)}</tbody></table></div></div>}
    {benchmarkRows.length > 0 && <div id="sequence-benchmark" className="scroll-mt-6" aria-hidden="true" />}
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
    <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="eyebrow text-[#8298d9]">CADD / 虚拟筛选</div><h3 className="mt-2 text-lg font-semibold text-[#eef1ff]">命中排序与结合能</h3><p className="mt-1 text-xs text-[#93a5d4]">数值越负，表示对接受体的预测结合越强</p></div><div className="grid size-10 place-items-center rounded-xl border border-[#405b96] bg-[#152442] text-[#aebfff]"><BarChart3 size={19} /></div></div>
    <div className="mt-5 grid gap-2 sm:grid-cols-3"><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">最佳命中</div><div className="mt-2 truncate text-lg font-semibold text-[#dbe2ff]">{String(result.best_hit || hits[0]?.mol_name || '--')}</div></div><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">最佳亲和力</div><div className="mt-2 font-mono text-lg text-[#aebfff]">{result.best_affinity !== undefined ? `${Number(result.best_affinity).toFixed(3)} kcal/mol` : hits[0] ? `${hits[0].affinity.toFixed(3)} kcal/mol` : '--'}</div></div><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">成功对接数</div><div className="mt-2 font-mono text-lg text-[#8fe5c1]">{String(result.rows ?? hits.length)} / {String(result.max_ligands ?? (hits.length || '--'))}</div></div></div>
    {hits.length > 0 ? <div className="mt-5 overflow-hidden rounded-xl border border-white/[0.08] bg-[#081426]/80"><div className="border-b border-white/[0.08] px-4 py-3"><div className="field-label mb-0">热门命中 / 亲和力概览</div></div><div className="divide-y divide-white/[0.06]">{hits.map((hit, index) => <div key={`${hit.mol_name}-${index}`} className="grid gap-2 px-4 py-3 sm:grid-cols-[28px_1fr_120px_100px] sm:items-center"><div className="font-mono text-xs text-[#6f86bb]">{String(index + 1).padStart(2, '0')}</div><div className="min-w-0"><div className="flex items-center gap-2"><span className="truncate text-sm font-medium text-[#dce5ff]">{hit.mol_name}</span><span className={`status-badge ${hit.tag === 'active' ? 'status-ok' : 'status-queued'}`}>{hit.tag === 'active' ? '活性' : '非活性'}</span></div><div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[#20304b]"><div className="h-full rounded-full bg-gradient-to-r from-[#718cff] to-[#aebfff]" style={{ width: `${Math.max(12, Math.round((Math.abs(hit.affinity) / maxAbsAffinity) * 100))}%` }} /></div></div><div className="font-mono text-sm text-[#b9c7ff] sm:text-right">{hit.affinity.toFixed(3)}</div><div className="font-mono text-[10px] text-[#7085b4] sm:text-right">kcal/mol</div></div>)}</div></div> : <div className="mt-5 rounded-xl border border-[#705b35] bg-[#251f15] px-4 py-3 text-xs leading-5 text-[#d8c18a]">当前结果没有携带命中明细。后续运行会返回前 10 个配体，并在这里生成排序表。</div>}
    {(scorePlot || topMoleculeImage) && <div className="mt-4 flex flex-wrap gap-2"><div className="field-label mb-0 mr-2 self-center">产物</div>{scorePlot && <button onClick={() => onDownload(scorePlot)} className="inline-flex items-center gap-2 rounded-lg border border-[#405b96] bg-[#152442] px-3 py-2 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><Download size={13} />打分图</button>}{topMoleculeImage && <button onClick={() => onDownload(topMoleculeImage)} className="inline-flex items-center gap-2 rounded-lg border border-[#405b96] bg-[#152442] px-3 py-2 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><Download size={13} />最佳命中结构图</button>}</div>}
  </section>
}

function AgentEvidencePanel({ evidenceMatches, evidenceCitations, knowledgeMatches, graphMetrics, provider }: { evidenceMatches: Record<string, unknown>[]; evidenceCitations: Record<string, unknown>[]; knowledgeMatches: Record<string, unknown>[]; graphMetrics: Record<string, unknown>; provider?: string }) {
  if (!evidenceMatches.length && !evidenceCitations.length && !knowledgeMatches.length && !Object.keys(graphMetrics).length && !provider) return null
  return <section data-result-evidence className="border-t border-white/[0.08] px-5 py-5 sm:px-6">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">代理 / 证据支撑</div><div className="mt-1 text-sm text-[#b9e6d5]">检索结果、知识片段和图谱关系共同支撑当前解释</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />已有证据</span></div>
    <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4"><PipelineMetric label="证据匹配" value={evidenceMatches.length || evidenceCitations.length || '—'} /><PipelineMetric label="知识命中" value={knowledgeMatches.length || '—'} /><PipelineMetric label="图谱节点" value={graphMetrics.n_nodes ?? '—'} /><PipelineMetric label="图谱边" value={graphMetrics.n_edges ?? '—'} /></div>
    <div className="mt-4 grid gap-3 lg:grid-cols-2">
      {(evidenceMatches.length > 0 || evidenceCitations.length > 0) && <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="field-label mb-0">文献 / 数据库证据</div><div className="mt-3 space-y-2">{(evidenceMatches.length ? evidenceMatches.slice(0, 3) : evidenceCitations.slice(0, 3)).map((item, index) => { const title = String(item.title || item.gene_id || `证据 ${index + 1}`); const source = String(item.source || item.provider || '来源'); const url = typeof item.url === 'string' ? item.url : ''; return <div key={`${source}-${title}-${index}`} className="rounded-lg border border-white/[0.07] px-3 py-2.5"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="truncate text-xs font-medium text-[#c9e5dc]">{title}</div><div className="mt-1 font-mono text-[10px] text-[#6f9189]">{source}{item.pmid ? ` · PMID ${String(item.pmid)}` : ''}</div></div>{url && <a href={url} target="_blank" rel="noreferrer" className="shrink-0 text-[#aebfff]" aria-label={`打开 ${title}`}><ArrowUpRight size={14} /></a>}</div></div> })}</div></div>}
      {knowledgeMatches.length > 0 && <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="field-label mb-0">知识检索 / TF-IDF</div><div className="mt-3 space-y-2">{knowledgeMatches.slice(0, 3).map((item, index) => <div key={`${String(item.document_id || item.title || 'document')}-${index}`} className="rounded-lg border border-white/[0.07] px-3 py-2.5"><div className="flex items-center justify-between gap-3"><div className="truncate text-xs font-medium text-[#c9e5dc]">{String(item.title || item.document_id || '知识文档')}</div><span className="font-mono text-[10px] text-[#8fe5c1]">{item.score !== undefined ? Number(item.score).toFixed(3) : '—'}</span></div>{Boolean(item.snippet) && <p className="mt-2 line-clamp-2 text-[10px] leading-5 text-[#769890]">{String(item.snippet)}</p>}</div>)}</div></div>}
    </div>
  </section>
}

function EvidenceProvenancePanel({ provider, requestedGeneIds, source, endpoint, status, fallbackReason }: { provider?: string; requestedGeneIds: string[]; source?: string; endpoint?: string; status?: string; fallbackReason?: string }) {
  if (!provider && !requestedGeneIds.length && !source && !endpoint && !fallbackReason) return null
  const evidenceSource = source || endpoint || '—'
  const reviewed = status !== 'ok' && Boolean(fallbackReason)
  return <div className="mt-3 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="grid gap-3 sm:grid-cols-3"><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">来源</div><div className="mt-1 truncate text-xs text-[#c9e5dc]">{provider ? providerLabels[provider] || provider : '—'}</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">查询目标</div><div className="mt-1 truncate font-mono text-xs text-[#c9e5dc]" title={requestedGeneIds.join(', ')}>{requestedGeneIds.length ? requestedGeneIds.join(', ') : '—'}</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">来源 / 接口</div><div className="mt-1 truncate font-mono text-xs text-[#c9e5dc]" title={evidenceSource}>{evidenceSource}</div></div></div>{reviewed && <div className="mt-3 rounded-lg border border-[#705b35] bg-[#251f15] px-3 py-2 text-[10px] leading-5 text-[#d8c18a]">回退 / 复核：{fallbackReason}</div>}</div>
}

function MultiOmicsSummaryPanel({ genomicsMetrics, singleCellMetrics, imageMetrics, metagenomicsMetrics, evidenceMatches, knowledgeMatches, graphMetrics, sequenceResult }: { genomicsMetrics: Record<string, unknown>; singleCellMetrics: Record<string, unknown>; imageMetrics: Record<string, unknown>; metagenomicsMetrics: Record<string, unknown>; evidenceMatches: Record<string, unknown>[]; knowledgeMatches: Record<string, unknown>[]; graphMetrics: Record<string, unknown>; sequenceResult: Record<string, unknown> }) {
  const hasData = Object.keys(genomicsMetrics).length > 0 || Object.keys(singleCellMetrics).length > 0 || Object.keys(imageMetrics).length > 0 || Object.keys(metagenomicsMetrics).length > 0
  if (!hasData) return null
  const display = (value: unknown) => value === undefined || value === null ? '—' : typeof value === 'number' ? value.toLocaleString() : String(value)
  const cellPassed = singleCellMetrics.n_cells_passed
  const cellInput = singleCellMetrics.n_cells_input
  const mrnaStatus = sequenceResult.verify !== undefined ? (sequenceResult.verify ? 'verified' : 'review') : sequenceResult.verdict
  return <section className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">多组学 / 代理交接</div><div className="mt-1 text-sm text-[#b9e6d5]">基因组、单细胞、图像和微生物组结果进入证据检索与 mRNA 设计链路</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />跨模态追踪</span></div><div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4"><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">基因组质控</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{display(genomicsMetrics.reads)} 条读段</div><div className="mt-1 text-[10px] text-[#769890]">{display(genomicsMetrics.bases)} 个碱基</div></div><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">10X 单细胞</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{display(cellPassed)} / {display(cellInput)} 个细胞</div><div className="mt-1 text-[10px] text-[#769890]">{display(singleCellMetrics.n_gene_expression_features)} 个表达特征</div></div><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">成像质控</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{imageMetrics.width !== undefined && imageMetrics.height !== undefined ? `${display(imageMetrics.width)}×${display(imageMetrics.height)}` : '—'}</div><div className="mt-1 text-[10px] text-[#769890]">{display(imageMetrics.format)} · {display(imageMetrics.channels)} 个通道</div></div><div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">微生物组质控</div><div className="mt-2 font-mono text-lg text-[#e4f1ed]">{display(metagenomicsMetrics.n_taxa_retained)} 个分类单元</div><div className="mt-1 text-[10px] text-[#769890]">保留 {display(metagenomicsMetrics.n_samples)} 个样本</div></div></div><div className="mt-3 grid gap-2 rounded-xl border border-[#28524b] bg-[#102b2a]/70 p-3 sm:grid-cols-4"><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">证据</div><div className="mt-1 text-xs text-[#c9e5dc]">{display(evidenceMatches.length)} 条匹配</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">知识</div><div className="mt-1 text-xs text-[#c9e5dc]">{display(knowledgeMatches.length)} 条检索结果</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">图谱</div><div className="mt-1 text-xs text-[#c9e5dc]">{display(graphMetrics.n_nodes)} 个节点 / {display(graphMetrics.n_edges)} 条边</div></div><div><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">mRNA 交接</div><div className="mt-1 text-xs text-[#8fe5c1]">{display(mrnaStatus)}</div></div></div></section>
}

function AgentExecutionAuditPanel({ status, steps, manifestPath, reportPath }: { status?: string; steps: Record<string, unknown>[]; manifestPath?: string; reportPath?: string }) {
  if (!steps.length) return null
  const completed = steps.filter((step) => step.status === 'completed').length
  const failed = steps.filter((step) => step.status === 'failed').length
  const dependencies = steps.reduce((total, step) => total + (Array.isArray(step.depends_on) ? step.depends_on.length : 0), 0)
  const tools = new Set(steps.map((step) => typeof step.tool === 'string' ? step.tool : 'unknown'))
  return <section className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">代理 / 执行审计</div><div className="mt-1 text-sm text-[#b9e6d5]">每个工具调用、依赖关系和最终产物都保留在本次运行 manifest 中</div></div><span className={`status-badge ${failed ? 'status-failed' : 'status-ok'}`}><span className="size-1.5 rounded-full bg-current" />{failed ? '需要复核' : '可复现运行'}</span></div><div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4"><PipelineMetric label="步骤" value={steps.length} /><PipelineMetric label="已完成" value={completed} /><PipelineMetric label="失败" value={failed} /><PipelineMetric label="依赖边" value={dependencies} /></div><div className="mt-3 grid gap-2 sm:grid-cols-3"><div className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">运行状态</div><div className="mt-1 text-xs text-[#c9e5dc]">{status || '未知'}</div></div><div className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">工具契约</div><div className="mt-1 text-xs text-[#c9e5dc]">{tools.size} 个唯一工具</div></div><div className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">产物</div><div className="mt-1 text-xs text-[#8fe5c1]">{manifestPath ? 'manifest' : '—'}{reportPath ? ' + report' : ''}</div></div></div></section>
}

function ReportPreviewModal({ preview, onClose }: { preview: { url: string; filename: string }; onClose: () => void }) {
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#02090a]/80 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label="HTML 报告预览">
    <div className="flex h-[min(88vh,900px)] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-[#365c78] bg-[#0a1a1d] shadow-[0_24px_80px_rgba(0,0,0,.55)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-4">
        <div><div className="eyebrow text-[#8faecb]">产物预览 / HTML</div><div className="mt-1 truncate font-mono text-xs text-[#c8e3dc]">{preview.filename}</div></div>
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
  const directCaddEnvelope = job.tool === 'cadd_run_screening' ? payload : {}
  const caddEnvelope = caddStep?.result && typeof caddStep.result === 'object' && !Array.isArray(caddStep.result) ? caddStep.result as Record<string, unknown> : directCaddEnvelope
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
  const isSequenceResult = Boolean(sequenceResult.mrna)
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
  const metadataGrid = <div className="grid gap-3 px-5 py-5 sm:grid-cols-2 lg:grid-cols-4 sm:px-6">{visible.map(([key, value]) => <div key={key} className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] uppercase tracking-[0.12em] text-[#63817b]">{key}</div>{artifactKeys.has(key) && typeof value === 'string' ? <div className="mt-2 flex flex-wrap gap-2">{key === 'report_path' && <button onClick={() => onOpenReport(value)} title={`预览 ${value}`} aria-label={`预览 ${key}`} className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[#405b96] bg-[#152442] px-2.5 py-1.5 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><ArrowUpRight size={13} /><span className="truncate">查看报告</span></button>}<button onClick={() => onDownload(value)} title={value} aria-label={`下载 ${key}`} className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[#28524b] bg-[#102b2a] px-2.5 py-1.5 text-xs text-[#b9e6d5] transition hover:border-[#71cba7] hover:text-[#ecfff7]"><Download size={13} /><span className="truncate">下载产物</span></button></div> : <div className="mt-2 truncate text-sm text-[#c9e5dc]">{String(value)}</div>}</div>)}</div>
  return <section data-result-overview className="panel mt-5 overflow-hidden" aria-live="polite">
    <nav aria-label="结果分区" className="flex flex-wrap gap-2 border-b border-white/[0.08] px-5 py-3 sm:px-6"><button type="button" onClick={() => jumpTo('[data-result-overview]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">概览</button>{hasEvidenceView && <button type="button" onClick={() => jumpTo('[data-result-evidence]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">证据</button>}{hasAuditView && <button type="button" onClick={() => jumpTo('[data-result-audit]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">审计</button>}<button type="button" onClick={() => jumpTo('details')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">原始 JSON</button></nav>
    <div className="flex items-center justify-between border-b border-white/10 px-5 py-5 sm:px-6"><div><div className="eyebrow">{isSequenceResult ? 'mRNA-Forge / 结果工作区' : '结果 / 溯源'}</div><h2 className="mt-2 text-xl font-semibold">{isSequenceResult ? '序列优化结果' : '结构化结果'}</h2></div><Check size={18} className="text-[#83e3bc]" /></div>
    {isSequenceResult ? <details className="border-b border-white/[0.08] bg-[#061719]/45"><summary className="cursor-pointer list-none px-5 py-3 text-xs text-[#9bb9b0] outline-none focus-visible:ring-2 focus-visible:ring-[#8fe5c1] focus-visible:ring-inset sm:px-6">运行溯源与产物 <span className="ml-2 font-mono text-[10px] text-[#63817b]">可展开</span></summary>{metadataGrid}</details> : metadataGrid}
    {Boolean(sequenceResult.mrna) && <SequenceResultPanel result={sequenceResult} benchmark={Object.keys(sequenceBenchmark).length ? sequenceBenchmark : undefined} reportPath={typeof sequenceReport.output_html === 'string' ? sequenceReport.output_html : undefined} structureId={structureId} onDownload={onDownload} onOpenReport={onOpenReport} />}
    {(Boolean(caddResult.best_hit) || Array.isArray(caddResult.hits) || Array.isArray(caddResult.top_hits)) && <CaddResultPanel result={caddResult} onDownload={onDownload} />}
    <AgentEvidencePanel evidenceMatches={evidenceMatches} evidenceCitations={evidenceCitations} knowledgeMatches={knowledgeMatches} graphMetrics={graphMetrics} provider={evidenceProvider} />
    <EvidenceProvenancePanel provider={evidenceProvider} requestedGeneIds={evidenceRequestedGeneIds} source={evidenceSource} endpoint={evidenceEndpoint} status={evidenceStatus} fallbackReason={evidenceFallbackReason} />
    <MultiOmicsSummaryPanel genomicsMetrics={genomicsMetrics} singleCellMetrics={singleCellMetrics} imageMetrics={imageMetrics} metagenomicsMetrics={metagenomicsMetrics} evidenceMatches={evidenceMatches} knowledgeMatches={knowledgeMatches} graphMetrics={graphMetrics} sequenceResult={sequenceResult} />
    <div data-result-audit className="scroll-mt-6" />
    <AgentExecutionAuditPanel status={typeof manifest.status === 'string' ? manifest.status : undefined} steps={steps} manifestPath={typeof manifest.manifest_path === 'string' ? manifest.manifest_path : undefined} reportPath={typeof sequenceReport.output_html === 'string' ? sequenceReport.output_html : typeof omicsReport.output_md === 'string' ? omicsReport.output_md : typeof report.path === 'string' ? report.path : undefined} />
     {Boolean(fastqQcResult.status) && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">FASTQ 质量控制</div><div className="mt-1 text-sm text-[#b9e6d5]">FastQC {fastqQcSummaries.length ? `完成 ${fastqQcSummaries.length} 个报告` : '报告'} · MultiQC 汇总已生成</div></div>{fastqQcReport && <button onClick={() => onDownload(fastqQcReport)} className="inline-flex items-center gap-2 rounded-lg border border-[#3d5a8c] bg-[#111d32] px-3 py-2 text-xs font-medium text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><Download size={13} />下载 MultiQC 报告</button>}</div><div className="mt-4 grid grid-cols-3 gap-2 sm:max-w-md"><QcStatusMetric label="通过" value={fastqQcCounts.pass} className="status-ok" /><QcStatusMetric label="警告" value={fastqQcCounts.warn} className="status-running" /><QcStatusMetric label="失败" value={fastqQcCounts.fail} className="status-failed" /></div></div>}
     {toolProvenance.length > 0 && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">工具链溯源</div><div className="mt-1 text-sm text-[#b9e6d5]">版本信息来自本次任务的实际执行环境</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />运行环境已验证</span></div><div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{toolProvenance.map((item) => <div key={item.label} className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">{item.label}</div><div className="mt-1 truncate text-[11px] text-[#c9e5dc]" title={item.version}>{item.version}</div></div>)}</div></div>}
     {(alignmentSamples.length > 0 || featureCountsResult.n_genes !== undefined || Boolean(differential.output_csv)) && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">RNA-seq 流程摘要</div><div className="mt-1 text-sm text-[#b9e6d5]">比对、计数和差异分析结果已汇总，可直接查看或继续分析。</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />链路完成</span></div><div className="mt-4 grid grid-cols-2 gap-2 lg:grid-cols-4"><PipelineMetric label="样本数" value={alignmentSamples.length || featureCountsResult.n_samples || '—'} /><PipelineMetric label="平均比对率" value={alignmentRate || '—'} /><PipelineMetric label="计数基因数" value={featureCountsResult.n_genes ?? '—'} /><PipelineMetric label="显著差异基因" value={differential.n_significant ?? '—'} /></div>{alignmentSamples.length > 0 && <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{alignmentSamples.map((sample, index) => <div key={`${String(sample.sample_id || 'sample')}-${index}`} className="flex items-center justify-between rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><span className="font-mono text-[10px] text-[#a9cbc0]">{String(sample.sample_id || `sample-${index + 1}`)}</span><span className="font-mono text-[10px] text-[#8fe5c1]">{String(sample.overall_alignment_rate || '—')}</span></div>)}</div>}</div>}
     {traceSteps.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">工作流追踪 / 工具链</div><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{traceSteps.map((step) => <div key={`${step.index}-${step.id}`} className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-3"><div className="grid size-7 shrink-0 place-items-center rounded-lg border border-[#28524b] bg-[#102b2a] font-mono text-[10px] text-[#8fe5c1]">{String(step.index).padStart(2, '0')}</div><div className="min-w-0 flex-1"><div className="truncate text-xs font-medium text-[#c9e5dc]">{step.id}</div><div className="mt-1 truncate font-mono text-[9px] text-[#66857e]">{step.tool}</div></div><span className={`status-badge ${step.status === 'completed' ? 'status-ok' : step.status === 'failed' ? 'status-failed' : 'status-running'}`}>{step.status === 'completed' ? '已完成' : step.status === 'failed' ? '失败' : '运行中'}</span></div>)}</div></div>}
    {geneIds.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">已注释基因 ID</div><div className="mt-2 flex flex-wrap gap-2">{geneIds.map((geneId) => <span key={geneId} className="rounded-md border border-[#28524b] bg-[#102b2a] px-2 py-1 font-mono text-[10px] text-[#b9e6d5]">{geneId}</span>)}</div></div>}
    <details className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><summary className="cursor-pointer text-xs text-[#8fb2a8]">查看完整结果 JSON</summary><pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-[#061113] p-3 text-[10px] leading-5 text-[#91b8ac]">{JSON.stringify(payload, null, 2)}</pre></details>
  </section>
}

function DomainsView({ plugins }: { plugins: Plugin[] }) {
  return <section className="py-9"><div className="max-w-3xl"><div className="eyebrow">插件目录 / 能力发现</div><h1 className="mt-3 text-4xl font-semibold tracking-[-0.04em] sm:text-5xl">领域是能力，<span className="text-[#8fe5c1]">插件是边界。</span></h1><p className="mt-5 text-sm leading-7 text-[#88a6a0] sm:text-base">每个领域通过统一工具契约接入，状态、版本与能力在运行时可发现。研究代理只编排能力，不把业务逻辑写死在对话层。</p></div><div className="mt-10 grid gap-4 md:grid-cols-2 xl:grid-cols-3">{plugins.map((plugin) => { const Icon = domainIcons[plugin.domain] || Boxes; return <div key={plugin.domain} className="panel group p-5 transition hover:-translate-y-0.5 hover:border-[#3e786a]"><div className="flex items-start justify-between"><div className="grid size-11 place-items-center rounded-xl border border-[#28524b] bg-[#102b2a] text-[#8fe5c1]"><Icon size={20} /></div><span className={`status-badge ${plugin.status === 'available' ? 'status-ok' : 'status-failed'}`}>{plugin.status === 'available' ? '可用' : plugin.status.toUpperCase()}</span></div><h2 className="mt-6 text-lg font-semibold capitalize">{domainLabels[plugin.domain] || plugin.domain}</h2><p className="mt-1 min-h-10 text-xs leading-5 text-[#6e8b85]">{pluginDescriptions[plugin.domain] || plugin.name}</p><div className="mt-5 flex items-end justify-between border-t border-white/[0.07] pt-4"><div><div className="font-mono text-2xl text-[#d7f1e8]">{String(plugin.tool_count).padStart(2, '0')}</div><div className="mt-1 font-mono text-[9px] tracking-[0.15em] text-[#5f7d77]">工具</div></div><div className="text-right font-mono text-[10px] text-[#63837b]">v{plugin.version || 'builtin'}</div></div></div> })}</div></section>
}

export default App
