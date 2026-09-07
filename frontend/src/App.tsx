import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  ArrowUpRight,
  Ban,
  Beaker,
  Boxes,
  ChevronRight,
  CircleDot,
  Clock3,
  Dna,
  GitBranch,
  LayoutDashboard,
  LockKeyhole,
  Play,
  Radio,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Terminal,
  Workflow,
  XCircle,
} from 'lucide-react'
import { apiFetch, followJob, uploadFile } from './app/api'
import { luciferaseDemoProtein, rnaseqFixture, statusLabels } from './app/constants'
import type {
  Capabilities,
  EventItem,
  Job,
  PlannerMode,
  Plugin,
  Project,
  ResearchFileSlot,
  ResearchPlan,
  ResearchPreset,
  RnaFileSlot,
  RnaInputMode,
  RnaPreflightItem,
  RunMode,
  SequenceMethod,
  SequenceMolecule,
  UploadedFile,
  View,
} from './app/types'
import { DomainsView } from './components/DomainsView'
import { JobResultSummary, ReportPreviewModal } from './components/JobResultSummary'
import { SequenceDesignInput } from './components/SequenceDesignInput'
import {
  CapabilityStrip,
  EmptyStream,
  Metric,
  ResearchFileField,
  ResearchPlanCard,
  RnaFileField,
  RnaPreflightCard,
  StatusBadge,
} from './components/WorkspaceStatus'
import { useManagedJobStream } from './hooks/useManagedJobStream'
import { useReportPreview } from './hooks/useReportPreview'

const runtimeApiBase = new URLSearchParams(window.location.search).get('api') || ''
const defaultApiBase = runtimeApiBase || import.meta.env.VITE_API_BASE_URL || (
  window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
    ? 'http://127.0.0.1:8000'
    : ''
)
const localDevelopmentToken = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  ? 'change-me-in-development'
  : ''

const terminalJobStatuses = new Set<Job['status']>(['completed', 'failed', 'cancelled'])

function mergeJobState(previous: Job | undefined, next: Job) {
  if (previous && terminalJobStatuses.has(previous.status) && !terminalJobStatuses.has(next.status)) return previous
  return next
}

function mergeJobList(current: Job[], incoming: Job[]) {
  const currentById = new Map(current.map((job) => [job.job_id, job]))
  return incoming.map((job) => mergeJobState(currentById.get(job.job_id), job))
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
  const [token, setToken] = useState(() => localStorage.getItem('bio-agent-token') || import.meta.env.VITE_API_TOKEN || localDevelopmentToken)
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
  const sequenceDemoStarted = useRef(false)
  const { beginJobStream, isCurrentStream, finishJobStream } = useManagedJobStream()
  const { reportPreview, showReportPreview, closeReportPreview } = useReportPreview()

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
        finishJobStream(controller)
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





export default App
