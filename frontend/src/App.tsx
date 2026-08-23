import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  ArrowUpRight,
  Ban,
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

type UploadedFile = {
  file_id: string
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  path: string
  download_url: string
}

type View = 'workspace' | 'domains'
type RunMode = 'research' | 'variant' | 'sequence' | 'cadd'

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
}

const domainLabels: Record<string, string> = {
  cadd: 'CADD',
  omics: 'Omics',
  sequence: 'mRNA / Sequence',
  literature: 'Literature',
  knowledge: 'Knowledge',
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
  const [apiBase] = useState(defaultApiBase)
  const [token, setToken] = useState(() => localStorage.getItem('bio-agent-token') || import.meta.env.VITE_API_TOKEN || 'change-me-in-development')
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [events, setEvents] = useState<EventItem[]>([])
  const [task, setTask] = useState('分析 RNA-seq 差异表达并设计 mRNA 序列')
  const [variantTask, setVariantTask] = useState('Annotate VCF variants and retrieve gene evidence')
  const [protein, setProtein] = useState('MKT')
  const [evidenceProvider, setEvidenceProvider] = useState('local')
  const [variantBackend, setVariantBackend] = useState('auto')
  const [caddExhaustiveness, setCaddExhaustiveness] = useState('4')
  const [caddMaxLigands, setCaddMaxLigands] = useState('3')
  const [uploadedFiles, setUploadedFiles] = useState<Record<ResearchFileSlot, UploadedFile | null>>({ expression: null, metadata: null, gene_sets: null, vcf: null, annotation: null, receptor: null, ligand_library: null })
  const [uploadingFile, setUploadingFile] = useState<ResearchFileSlot | ''>('')
  const [researchPlan, setResearchPlan] = useState<ResearchPlan | null>(null)
  const [loading, setLoading] = useState(false)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  const streamController = useRef<AbortController | null>(null)

  function beginJobStream() {
    streamController.current?.abort()
    const controller = new AbortController()
    streamController.current = controller
    return controller
  }

  function isCurrentStream(controller: AbortController) {
    return streamController.current === controller
  }

  const refresh = useCallback(async () => {
    setError('')
    try {
      const [pluginPayload, jobPayload, capabilityPayload] = await Promise.all([
        apiFetch<{ plugins: Plugin[] }>(apiBase, token, '/api/v1/plugins'),
        apiFetch<{ jobs: Job[] }>(apiBase, token, '/api/v1/jobs?limit=8'),
        apiFetch<Capabilities>(apiBase, token, '/api/v1/capabilities'),
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

  const activeDomains = useMemo(() => plugins.filter((plugin) => plugin.status === 'available').length, [plugins])
  const toolCount = useMemo(() => plugins.reduce((total, plugin) => total + (plugin.tool_count || 0), 0), [plugins])
  const runningJobs = jobs.filter((job) => job.status === 'queued' || job.status === 'running').length

  function saveToken() {
    localStorage.setItem('bio-agent-token', token)
    void refresh()
  }

  function buildResearchInputs() {
    return {
      expression_csv: uploadedFiles.expression?.path || 'examples/rnaseq/expression.csv',
      metadata_csv: uploadedFiles.metadata?.path || 'examples/rnaseq/metadata.csv',
      gene_sets_csv: uploadedFiles.gene_sets?.path || 'examples/rnaseq/gene_sets.csv',
      evidence_csv: evidenceProvider === 'local' ? 'examples/rnaseq/evidence.csv' : undefined,
      evidence_provider: evidenceProvider,
      protein,
      output_dir: 'output/frontend_auto_research',
    }
  }

  function buildVariantInputs() {
    return {
      vcf_path: uploadedFiles.vcf?.path || 'examples/variants/variants.vcf',
      annotation_csv: uploadedFiles.annotation?.path || 'examples/variants/gene_annotations.csv',
      annotation_backend: variantBackend,
      evidence_csv: 'examples/rnaseq/evidence.csv',
      evidence_provider: 'local',
      output_dir: 'output/frontend_variant_research',
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
        { task, inputs: buildResearchInputs(), planner_mode: 'auto' },
        '研究计划已进入执行队列',
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
            molecule: 'linear',
            method: 'greedy',
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
    const outputDir = mode === 'variant'
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

  async function downloadJobArtifact(jobId: string, artifactPath: string) {
    setError('')
    try {
      const response = await fetch(`${apiBase}/api/v1/jobs/${jobId}/artifacts?path=${encodeURIComponent(artifactPath)}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}))
        throw new Error(payload.detail || `产物下载失败: ${response.status}`)
      }
      const blob = await response.blob()
      const disposition = response.headers.get('content-disposition') || ''
      const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || artifactPath.split(/[\\/]/).pop() || 'artifact'
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
            <div className="flex items-center gap-3">
              <div className="hidden items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 font-mono text-[10px] text-[#8aa9a2] sm:flex"><LockKeyhole size={12} />Bearer token</div>
              <input aria-label="API Token" value={token} onChange={(event) => setToken(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && saveToken()} type="password" className="w-32 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-1.5 font-mono text-[10px] text-[#c7ded8] outline-none transition focus:border-[#72dcb4] sm:w-48" placeholder="API token" />
              <button onClick={saveToken} className="rounded-lg bg-[#a8f0d2] px-3 py-1.5 text-xs font-semibold text-[#092521] transition hover:bg-[#c6f8e1]">连接</button>
            </div>
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
              <CapabilityStrip capabilities={capabilities} />
              <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#3f527c] bg-[#111d32]/80 px-5 py-4"><div><div className="font-mono text-[10px] tracking-[0.16em] text-[#9cb9ff]">INTERVIEW DEMO / BGI MULTI-OMICS</div><div className="mt-1 text-sm text-[#b9c8e8]">Genomics QC → 10x single-cell → microbiome → evidence → mRNA</div></div><button aria-label="Run BGI multi-omics demo" onClick={() => void submitBgiMultiomicsDemo()} disabled={loading} className="rounded-lg bg-[#aebfff] px-3 py-2 text-xs font-semibold text-[#111a34] transition hover:bg-[#c4d0ff] disabled:cursor-not-allowed disabled:opacity-50">Run BGI Demo</button></div>

              <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#28524b] bg-[#102b2a]/70 px-5 py-4"><div><div className="font-mono text-[10px] tracking-[0.16em] text-[#8fe5c1]">INTERVIEW DEMO / RNA-SEQ AGENT</div><div className="mt-1 text-sm text-[#b4cdc6]">差异表达 → 通路富集 → 基因证据 → 可追溯报告</div></div><button onClick={() => void submitOmicsDemo()} disabled={loading} className="rounded-lg bg-[#8fe5c1] px-3 py-2 text-xs font-semibold text-[#092521] transition hover:bg-[#b8f4d8] disabled:cursor-not-allowed disabled:opacity-50">运行 RNA-seq Agent</button></div>

              {selectedJob && ['queued', 'running'].includes(selectedJob.status) && <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#5c4930] bg-[#211d16] px-5 py-4"><div className="flex items-center gap-3"><Ban size={16} className="text-[#e6c875]" /><div><div className="text-sm font-medium text-[#f1dfaa]">任务控制</div><div className="mt-1 text-xs text-[#aa9767]">排队中的任务会立即取消，运行中的任务采用协作式取消。</div></div></div><button onClick={() => void cancelSelectedJob()} disabled={selectedJob.cancel_requested} className="rounded-lg border border-[#80643c] px-3 py-2 text-xs font-medium text-[#f1d889] transition hover:bg-[#392d1c] disabled:cursor-not-allowed disabled:opacity-50">{selectedJob.cancel_requested ? '取消请求已发送' : '取消任务'}</button></div>}

              <section className="grid gap-5 xl:grid-cols-[1.08fr_0.92fr]">
                <div className="panel p-5 sm:p-6">
                  <div className="flex items-start justify-between gap-4"><div><div className="eyebrow">01 / START A RUN</div><h2 className="mt-2 text-xl font-semibold">启动一条研究路径</h2></div><div className="rounded-xl border border-[#21443f] bg-[#102b2a] p-2.5 text-[#8fe5c1]"><Play size={17} /></div></div>
                  <div className="mt-7 grid grid-cols-2 gap-1 rounded-xl bg-[#071719] p-1 sm:grid-cols-4"><button onClick={() => setMode('research')} className={`mode-tab ${mode === 'research' ? 'mode-tab-active' : ''}`}><Workflow size={14} />研究规划</button><button onClick={() => setMode('variant')} className={`mode-tab ${mode === 'variant' ? 'mode-tab-active' : ''}`}><GitBranch size={14} />VCF 变异</button><button onClick={() => setMode('sequence')} className={`mode-tab ${mode === 'sequence' ? 'mode-tab-active' : ''}`}><Dna size={14} />mRNA 设计</button><button onClick={() => setMode('cadd')} className={`mode-tab ${mode === 'cadd' ? 'mode-tab-active' : ''}`}><Beaker size={14} />CADD 对接</button></div>
                  {mode === 'research' ? <>
                    <label className="mt-6 block"><span className="field-label">科学问题</span><textarea value={task} onChange={(event) => { setTask(event.target.value); setResearchPlan(null) }} rows={4} className="input-area" placeholder="描述你希望 Agent 协助完成的研究任务" /></label>
                    <div className="mt-5 grid gap-4 sm:grid-cols-[1fr_0.8fr]">
                      <div><label className="field-label" htmlFor="protein-context">蛋白输入上下文</label><input id="protein-context" value={protein} onChange={(event) => { setProtein(event.target.value.toUpperCase()); setResearchPlan(null) }} className="input-control font-mono tracking-[0.18em]" placeholder="例如 MKT" /></div>
                      <div><label className="field-label" htmlFor="evidence-provider">证据源</label><select id="evidence-provider" value={evidenceProvider} onChange={(event) => { setEvidenceProvider(event.target.value); setResearchPlan(null) }} className="input-control"><option value="local">本地证据</option><option value="kegg">KEGG</option><option value="ncbi_gene">NCBI Gene</option><option value="pubmed">PubMed</option><option value="uniprot">UniProt</option></select></div>
                    </div>
                    <div className="mt-5 grid gap-3 sm:grid-cols-3">
                      <ResearchFileField id="expression-file" label="表达矩阵 CSV" file={uploadedFiles.expression} uploading={uploadingFile === 'expression'} onChange={(file) => void handleResearchFileUpload('expression', file)} />
                      <ResearchFileField id="metadata-file" label="样本元数据 CSV" file={uploadedFiles.metadata} uploading={uploadingFile === 'metadata'} onChange={(file) => void handleResearchFileUpload('metadata', file)} />
                      <ResearchFileField id="gene-sets-file" label="基因集 CSV" file={uploadedFiles.gene_sets} uploading={uploadingFile === 'gene_sets'} onChange={(file) => void handleResearchFileUpload('gene_sets', file)} />
                    </div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">上传文件会在服务端校验、计算 SHA-256 并保存到本次研究输入目录；未上传的字段使用仓库示例数据。</p>
                  </> : mode === 'variant' ? <>
                    <label className="mt-6 block"><span className="field-label">Variant research task</span><textarea value={variantTask} onChange={(event) => { setVariantTask(event.target.value); setResearchPlan(null) }} rows={3} className="input-area" placeholder="Describe the VCF annotation and evidence task" /></label>
                    <div className="mt-5 grid gap-3 sm:grid-cols-2">
                      <ResearchFileField id="vcf-file" label="VCF / VCF.GZ input" accept=".vcf,.gz,text/plain" file={uploadedFiles.vcf} uploading={uploadingFile === 'vcf'} onChange={(file) => void handleResearchFileUpload('vcf', file)} />
                      <ResearchFileField id="annotation-file" label="Gene interval CSV" accept=".csv,.tsv,text/csv,text/tab-separated-values" file={uploadedFiles.annotation} uploading={uploadingFile === 'annotation'} onChange={(file) => void handleResearchFileUpload('annotation', file)} />
                    </div>
                    <div className="mt-5"><label className="field-label" htmlFor="variant-backend">Annotation backend</label><select id="variant-backend" value={variantBackend} onChange={(event) => { setVariantBackend(event.target.value); setResearchPlan(null) }} className="input-control"><option value="auto">Auto: VCF ANN → local interval</option><option value="vcf_ann">VCF ANN only</option><option value="local">Local interval table</option></select></div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">The demo uses reproducible fixtures when no files are uploaded. Results retain annotation source and external tool availability.</p>
                  </> : mode === 'sequence' ? <label className="mt-6 block"><span className="field-label">Protein sequence</span><input value={protein} onChange={(event) => { setProtein(event.target.value.toUpperCase()); setResearchPlan(null) }} className="input-control tracking-[0.18em] font-mono" placeholder="例如 MKT" /><span className="mt-2 block text-xs text-[#688983]">内置确定性后端将执行 optimize → score → verify。</span></label> : <>
                    <label className="mt-6 block"><span className="field-label">CADD screening task</span><textarea value="Run a reproducible CADD virtual screening workflow and prioritize docking hits" readOnly rows={3} className="input-area" /></label>
                    <div className="mt-5 grid gap-3 sm:grid-cols-2"><ResearchFileField id="receptor-file" label="受体结构 PDB / PDBQT" accept=".pdb,.pdbqt,text/plain" file={uploadedFiles.receptor} uploading={uploadingFile === 'receptor'} onChange={(file) => void handleResearchFileUpload('receptor', file)} /><ResearchFileField id="ligand-library-file" label="外部分子数据集 CSV" accept=".csv,.tsv,text/csv,text/tab-separated-values" file={uploadedFiles.ligand_library} uploading={uploadingFile === 'ligand_library'} onChange={(file) => void handleResearchFileUpload('ligand_library', file)} /></div>
                    <div className="mt-5 grid gap-4 sm:grid-cols-2"><div><label className="field-label" htmlFor="cadd-max-ligands">演示候选数</label><input id="cadd-max-ligands" type="number" min="1" max="17" value={caddMaxLigands} onChange={(event) => { setCaddMaxLigands(event.target.value); setResearchPlan(null) }} className="input-control font-mono" /><span className="mt-2 block text-xs text-[#688983]">完整筛选可调到 17，演示建议 3。</span></div><div><label className="field-label" htmlFor="cadd-exhaustiveness">Vina exhaustiveness</label><input id="cadd-exhaustiveness" type="number" min="1" max="32" value={caddExhaustiveness} onChange={(event) => { setCaddExhaustiveness(event.target.value); setResearchPlan(null) }} className="input-control font-mono" /><span className="mt-2 block text-xs text-[#688983]">数值越高越稳定，但运行时间更长。</span></div></div>
                    <p className="mt-3 text-xs leading-5 text-[#688983]">CADD 入口会记录受体、数据集、Vina 参数与结果报告。Docker 优先读取本机挂载的 data/4hjo.pdb 和 output/bindingdb_egfr_10000.csv，也支持上传替换。</p>
                  </>}
                  <div className="mt-6 flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2 font-mono text-[10px] text-[#66847e]"><CircleDot size={13} className="text-[#70e3ad]" />ASYNC / TRACEABLE / REPLAYABLE</div><button onClick={submitRun} disabled={loading || (mode === 'research' ? !task.trim() : mode === 'variant' ? !variantTask.trim() : mode === 'sequence' ? !protein.trim() : false)} className="group inline-flex items-center gap-2 rounded-xl bg-[#a8f0d2] px-4 py-2.5 text-sm font-semibold text-[#092521] transition hover:bg-[#c6f8e1] disabled:cursor-not-allowed disabled:opacity-50">{loading ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />}{loading ? '执行中…' : '开始运行'}<ArrowUpRight size={14} className="transition group-hover:translate-x-0.5 group-hover:-translate-y-0.5" /></button></div>
                </div>

                <div className="panel flex min-h-[326px] flex-col p-5 sm:p-6"><div className="flex items-start justify-between"><div><div className="eyebrow">02 / EXECUTION STREAM</div><h2 className="mt-2 text-xl font-semibold">实时执行轨迹</h2></div><div className="flex items-center gap-1.5 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#8fe5c1]"><span className="size-1.5 animate-pulse rounded-full bg-[#70e3ad]" />SSE</div></div>{selectedJob ? <div className="mt-7 flex flex-1 flex-col"><div className="flex items-center justify-between border-b border-white/10 pb-4"><div><div className="font-mono text-[11px] text-[#6f9189]">{formatJobId(selectedJob.job_id)}</div><div className="mt-1 text-sm font-medium">{selectedJob.tool}</div></div><StatusBadge status={selectedJob.status} /></div><div className="mt-5 space-y-3">{events.slice(-4).map((event, index) => <div key={`${event.at}-${index}`} className="flex items-start gap-3 text-xs"><div className="mt-1.5 size-1.5 rounded-full bg-[#83e3bc] shadow-[0_0_12px_#83e3bc]" /><div className="min-w-0 flex-1"><div className="text-[#b2cbc4]">{event.detail}</div><div className="mt-1 font-mono text-[10px] text-[#5f7c76]">{event.at} · {event.status}</div></div></div>)}</div><div className="mt-auto flex items-center gap-2 pt-5 font-mono text-[10px] text-[#64827b]"><Clock3 size={13} />{selectedJob.status === 'completed' ? `完成于 ${formatTime(selectedJob.finished_at)}` : '等待状态更新…'}</div></div> : <EmptyStream />}</div>
              </section>

              {selectedJob?.status === 'completed' && <JobResultSummary job={selectedJob} onDownload={(path) => void downloadJobArtifact(selectedJob.job_id, path)} />}

              {(mode === 'research' || mode === 'variant' || mode === 'sequence' || mode === 'cadd') && selectedJob?.tool !== 'research_execute' && <ResearchPlanCard plan={researchPlan} loading={loading && selectedJob?.tool === 'research_plan'} onExecute={() => void executeResearchPlan()} />}

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
  ]
  return <section className="mb-5" aria-label="Integration surfaces"><div className="mb-2 flex items-center justify-between"><div className="eyebrow">INTEGRATION SURFACES</div><div className="font-mono text-[10px] text-[#66857e]">{capabilities.tool_count} CONTRACTED TOOLS</div></div><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">{cards.map((card) => { const capability = capabilities.interfaces[card.key]; const Icon = card.icon; const available = capability?.status === 'available'; return <div key={card.key} className="rounded-xl border border-white/[0.08] bg-white/[0.035] px-3 py-3"><div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-xs font-medium text-[#c9e5dc]"><Icon size={14} className="text-[#8fe5c1]" />{card.label}</div><span className={`status-badge ${available ? 'status-ok' : 'status-failed'}`}>{available ? 'READY' : capability?.status || 'UNKNOWN'}</span></div><div className="mt-2 truncate font-mono text-[9px] text-[#66857e]" title={card.detail}>{card.detail}</div></div> })}</div></section>
}

function Metric({ label, value, icon }: { label: string; value: string; icon: React.ReactNode }) {
  return <div className="rounded-2xl border border-white/10 bg-white/[0.035] p-3 sm:p-4"><div className="flex items-center gap-2 text-[#6d9189]">{icon}<span className="font-mono text-[9px] tracking-[0.12em]">{label}</span></div><div className="mt-3 font-mono text-2xl text-[#d9f3eb]">{value}</div></div>
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
      </div>
      <div className="grid gap-4 lg:grid-cols-[0.7fr_1.3fr]">
        <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4">
          <div className="field-label">INPUT GATE</div>
          {execution?.ready ? <div className="flex items-center gap-2 text-sm text-[#9be6c5]"><Check size={15} />输入已满足，可执行</div> : <div className="text-sm text-[#efb19f]">缺少必要输入</div>}
          {!execution?.ready && <div className="mt-3 flex flex-wrap gap-1.5">{(execution?.missing_inputs || []).map((item) => <span key={item} className="rounded-md border border-[#70483f] bg-[#2b1b1b] px-2 py-1 font-mono text-[10px] text-[#e9a694]">{item}</span>)}</div>}
          {execution?.rationale?.length ? <div className="mt-4 space-y-2 text-xs leading-5 text-[#789791]">{execution.rationale.map((item) => <div key={item} className="flex gap-2"><span className="mt-2 size-1 rounded-full bg-[#78cdaa]" />{item}</div>)}</div> : null}
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

function JobResultSummary({ job, onDownload }: { job: Job; onDownload: (path: string) => void }) {
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
  const caddStep = steps.find((step) => step.tool === 'cadd_run_screening')
  const caddResult = caddStep?.result && typeof caddStep.result === 'object' && !Array.isArray(caddStep.result) ? caddStep.result as Record<string, unknown> : {}
  const genomicsStep = steps.find((step) => step.tool === 'omics_run_genomics_qc')
  const genomicsResult = genomicsStep?.result && typeof genomicsStep.result === 'object' && !Array.isArray(genomicsStep.result) ? genomicsStep.result as Record<string, unknown> : {}
  const genomicsMetrics = genomicsResult.metrics && typeof genomicsResult.metrics === 'object' && !Array.isArray(genomicsResult.metrics) ? genomicsResult.metrics as Record<string, unknown> : {}
  const singleCellStep = steps.find((step) => step.tool === 'omics_run_single_cell_10x_qc')
  const singleCellResult = singleCellStep?.result && typeof singleCellStep.result === 'object' && !Array.isArray(singleCellStep.result) ? singleCellStep.result as Record<string, unknown> : {}
  const singleCellMetrics = singleCellResult.metrics && typeof singleCellResult.metrics === 'object' && !Array.isArray(singleCellResult.metrics) ? singleCellResult.metrics as Record<string, unknown> : {}
  const singleCellOutputs = singleCellResult.outputs && typeof singleCellResult.outputs === 'object' && !Array.isArray(singleCellResult.outputs) ? singleCellResult.outputs as Record<string, unknown> : {}
  const metagenomicsStep = steps.find((step) => step.tool === 'omics_run_metagenomics_qc')
  const metagenomicsResult = metagenomicsStep?.result && typeof metagenomicsStep.result === 'object' && !Array.isArray(metagenomicsStep.result) ? metagenomicsStep.result as Record<string, unknown> : {}
  const metagenomicsMetrics = metagenomicsResult.metrics && typeof metagenomicsResult.metrics === 'object' && !Array.isArray(metagenomicsResult.metrics) ? metagenomicsResult.metrics as Record<string, unknown> : {}
  const metagenomicsOutputs = metagenomicsResult.outputs && typeof metagenomicsResult.outputs === 'object' && !Array.isArray(metagenomicsResult.outputs) ? metagenomicsResult.outputs as Record<string, unknown> : {}
  const evidenceStep = steps.find((step) => step.tool === 'literature_search')
  const evidenceEnvelope = evidenceStep?.result && typeof evidenceStep.result === 'object' && !Array.isArray(evidenceStep.result) ? evidenceStep.result as Record<string, unknown> : {}
  const evidenceResult = evidenceEnvelope.result && typeof evidenceEnvelope.result === 'object' && !Array.isArray(evidenceEnvelope.result) ? evidenceEnvelope.result as Record<string, unknown> : {}
  const knowledgeIngestStep = steps.find((step) => step.tool === 'knowledge_ingest_directory')
  const knowledgeIngestEnvelope = knowledgeIngestStep?.result && typeof knowledgeIngestStep.result === 'object' && !Array.isArray(knowledgeIngestStep.result) ? knowledgeIngestStep.result as Record<string, unknown> : {}
  const knowledgeIngestResult = knowledgeIngestEnvelope.result && typeof knowledgeIngestEnvelope.result === 'object' && !Array.isArray(knowledgeIngestEnvelope.result) ? knowledgeIngestEnvelope.result as Record<string, unknown> : {}
  const knowledgeSearchStep = steps.find((step) => step.tool === 'knowledge_search')
  const knowledgeSearchEnvelope = knowledgeSearchStep?.result && typeof knowledgeSearchStep.result === 'object' && !Array.isArray(knowledgeSearchStep.result) ? knowledgeSearchStep.result as Record<string, unknown> : {}
  const knowledgeSearchResult = knowledgeSearchEnvelope.result && typeof knowledgeSearchEnvelope.result === 'object' && !Array.isArray(knowledgeSearchEnvelope.result) ? knowledgeSearchEnvelope.result as Record<string, unknown> : {}
  const graphStep = steps.find((step) => step.tool === 'knowledge_build_graph')
  const graphEnvelope = graphStep?.result && typeof graphStep.result === 'object' && !Array.isArray(graphStep.result) ? graphStep.result as Record<string, unknown> : {}
  const graphResult = graphEnvelope.result && typeof graphEnvelope.result === 'object' && !Array.isArray(graphEnvelope.result) ? graphEnvelope.result as Record<string, unknown> : {}
  const graphMetrics = graphResult.metrics && typeof graphResult.metrics === 'object' && !Array.isArray(graphResult.metrics) ? graphResult.metrics as Record<string, unknown> : {}
  const sequenceStep = steps.find((step) => step.tool === 'sequence_pipeline')
  const sequenceEnvelope = sequenceStep?.result && typeof sequenceStep.result === 'object' && !Array.isArray(sequenceStep.result) ? sequenceStep.result as Record<string, unknown> : payload
  const sequenceResult = sequenceEnvelope.result && typeof sequenceEnvelope.result === 'object' && !Array.isArray(sequenceEnvelope.result) ? sequenceEnvelope.result as Record<string, unknown> : sequenceEnvelope
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
    report_path: payload.output_md ?? sequenceResult.output_html ?? report.path,
    cadd_report: caddResult.report,
  }
  const visible = Object.entries(summary).filter(([, value]) => value !== undefined && value !== null)
  const artifactKeys = new Set(['output_csv', 'cadd_result_csv', 'manifest_path', 'report_path', 'cadd_report', 'fastq_manifest', 'single_cell_metrics', 'metagenomics_relative_abundance', 'metagenomics_sample_metrics', 'knowledge_index', 'knowledge_graph'])
  const traceSteps = steps.map((step, index) => ({
    index: index + 1,
    id: typeof step.id === 'string' ? step.id : `step-${index + 1}`,
    tool: typeof step.tool === 'string' ? step.tool : 'unknown',
    status: typeof step.status === 'string' ? step.status : 'unknown',
  }))
  const rawGeneIds = payload.gene_ids ?? annotationResult.gene_ids
  const geneIds = Array.isArray(rawGeneIds) ? rawGeneIds.filter((value): value is string => typeof value === 'string') : []
  return <section className="panel mt-5 overflow-hidden" aria-live="polite">
    <div className="flex items-center justify-between border-b border-white/10 px-5 py-5 sm:px-6"><div><div className="eyebrow">RESULT / PROVENANCE</div><h2 className="mt-2 text-xl font-semibold">结构化结果</h2></div><Check size={18} className="text-[#83e3bc]" /></div>
    <div className="grid gap-3 px-5 py-5 sm:grid-cols-2 lg:grid-cols-4 sm:px-6">{visible.map(([key, value]) => <div key={key} className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] uppercase tracking-[0.12em] text-[#63817b]">{key}</div>{artifactKeys.has(key) && typeof value === 'string' ? <button onClick={() => onDownload(value)} title={value} aria-label={`下载 ${key}`} className="mt-2 inline-flex max-w-full items-center gap-2 rounded-lg border border-[#28524b] bg-[#102b2a] px-2.5 py-1.5 text-xs text-[#b9e6d5] transition hover:border-[#71cba7] hover:text-[#ecfff7]"><Download size={13} /><span className="truncate">下载产物</span></button> : <div className="mt-2 truncate text-sm text-[#c9e5dc]">{String(value)}</div>}</div>)}</div>
    {traceSteps.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">WORKFLOW TRACE / TOOLCHAIN</div><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{traceSteps.map((step) => <div key={`${step.index}-${step.id}`} className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-3"><div className="grid size-7 shrink-0 place-items-center rounded-lg border border-[#28524b] bg-[#102b2a] font-mono text-[10px] text-[#8fe5c1]">{String(step.index).padStart(2, '0')}</div><div className="min-w-0 flex-1"><div className="truncate text-xs font-medium text-[#c9e5dc]">{step.id}</div><div className="mt-1 truncate font-mono text-[9px] text-[#66857e]">{step.tool}</div></div><span className={`status-badge ${step.status === 'completed' ? 'status-ok' : step.status === 'failed' ? 'status-failed' : 'status-running'}`}>{step.status}</span></div>)}</div></div>}
    {geneIds.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">ANNOTATED GENE IDS</div><div className="mt-2 flex flex-wrap gap-2">{geneIds.map((geneId) => <span key={geneId} className="rounded-md border border-[#28524b] bg-[#102b2a] px-2 py-1 font-mono text-[10px] text-[#b9e6d5]">{geneId}</span>)}</div></div>}
    <details className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><summary className="cursor-pointer text-xs text-[#8fb2a8]">查看完整结果 JSON</summary><pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-[#061113] p-3 text-[10px] leading-5 text-[#91b8ac]">{JSON.stringify(payload, null, 2)}</pre></details>
  </section>
}

function DomainsView({ plugins }: { plugins: Plugin[] }) {
  return <section className="py-9"><div className="max-w-3xl"><div className="eyebrow">PLUGIN CATALOG / DISCOVERY</div><h1 className="mt-3 text-4xl font-semibold tracking-[-0.04em] sm:text-5xl">领域是能力，<span className="text-[#8fe5c1]">插件是边界。</span></h1><p className="mt-5 text-sm leading-7 text-[#88a6a0] sm:text-base">每个领域通过统一工具契约接入，状态、版本与能力在运行时可发现。研究 Agent 只编排能力，不把业务逻辑写死在对话层。</p></div><div className="mt-10 grid gap-4 md:grid-cols-2 xl:grid-cols-3">{plugins.map((plugin) => { const Icon = domainIcons[plugin.domain] || Boxes; return <div key={plugin.domain} className="panel group p-5 transition hover:-translate-y-0.5 hover:border-[#3e786a]"><div className="flex items-start justify-between"><div className="grid size-11 place-items-center rounded-xl border border-[#28524b] bg-[#102b2a] text-[#8fe5c1]"><Icon size={20} /></div><span className={`status-badge ${plugin.status === 'available' ? 'status-ok' : 'status-failed'}`}>{plugin.status === 'available' ? 'AVAILABLE' : plugin.status.toUpperCase()}</span></div><h2 className="mt-6 text-lg font-semibold capitalize">{plugin.domain}</h2><p className="mt-1 min-h-10 text-xs leading-5 text-[#6e8b85]">{plugin.name}</p><div className="mt-5 flex items-end justify-between border-t border-white/[0.07] pt-4"><div><div className="font-mono text-2xl text-[#d7f1e8]">{String(plugin.tool_count).padStart(2, '0')}</div><div className="mt-1 font-mono text-[9px] tracking-[0.15em] text-[#5f7d77]">TOOLS</div></div><div className="text-right font-mono text-[10px] text-[#63837b]">v{plugin.version || 'builtin'}</div></div></div> })}</div></section>
}

export default App
