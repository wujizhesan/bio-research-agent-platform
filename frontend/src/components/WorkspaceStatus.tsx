import type { ReactNode } from 'react'
import { Boxes, Check, GitBranch, Radio, RefreshCw, Server, Sparkles, Terminal, Upload, Workflow, XCircle } from 'lucide-react'
import { domainLabels, providerLabels, statusLabels } from '../app/constants'
import type { Capabilities, ResearchPlan, RnaPreflightItem, UploadedFile } from '../app/types'

export function CapabilityStrip({ capabilities }: { capabilities: Capabilities | null }) {
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
export function Metric({ label, value, icon }: { label: string; value: string; icon: ReactNode }) {
  return <div className="rounded-2xl border border-white/10 bg-white/[0.035] p-3 sm:p-4"><div className="flex items-center gap-2 text-[#6d9189]">{icon}<span className="font-mono text-[9px] tracking-[0.12em]">{label}</span></div><div className="mt-3 font-mono text-2xl text-[#d9f3eb]">{value}</div></div>
}

export function QcStatusMetric({ label, value, className }: { label: string; value: unknown; className: string }) {
  return <div className={`rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-2 ${className}`}><div className="font-mono text-[9px] tracking-[0.12em]">{label}</div><div className="mt-1 font-mono text-lg text-[#e4f1ed]">{typeof value === 'number' ? value : String(value ?? 0)}</div></div>
}

export function PipelineMetric({ label, value }: { label: string; value: unknown }) {
  return <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#7fa49c]">{label}</div><div className="mt-1 font-mono text-lg text-[#e4f1ed]">{String(value)}</div></div>
}

export function StatusBadge({ status }: { status: string }) {
  const style = status === 'completed' ? 'status-ok' : status === 'failed' || status === 'cancelled' ? 'status-failed' : status === 'running' ? 'status-running' : 'status-queued'
  return <span className={`status-badge ${style}`}><span className="size-1.5 rounded-full bg-current" />{status === 'cancelled' ? '已取消' : statusLabels[status] || status}</span>
}

export function ResearchFileField({ id, label, accept = '.csv,.tsv,text/csv,text/tab-separated-values', file, uploading, onChange }: { id: string; label: string; accept?: string; file: UploadedFile | null; uploading: boolean; onChange: (file?: File) => void }) {
  return <div>
    <div className="field-label">{label}</div>
    <label htmlFor={id} className="flex min-h-[76px] cursor-pointer items-center justify-between gap-3 rounded-xl border border-dashed border-[#315d55] bg-[#071719]/70 px-3 py-3 transition hover:border-[#71cba7] hover:bg-[#102b2a]">
      <input id={id} type="file" accept={accept} className="sr-only" onChange={(event) => { onChange(event.target.files?.[0]); event.currentTarget.value = '' }} />
      <div className="min-w-0"><div className="truncate text-xs font-medium text-[#b8d8ce]">{uploading ? '上传中…' : file?.filename || '选择输入文件'}</div><div className="mt-1 truncate font-mono text-[9px] text-[#668983]">{file ? `${file.size_bytes} 字节 · ${file.sha256.slice(0, 12)}` : '服务端安全存储'}</div></div>
      {uploading ? <RefreshCw size={15} className="shrink-0 animate-spin text-[#8fe5c1]" /> : <Upload size={15} className="shrink-0 text-[#78cdaa]" />}
    </label>
  </div>
}

export function RnaFileField({ id, label, accept, files, fixture, multiple = false, uploading, onChange }: { id: string; label: string; accept?: string; files: UploadedFile[]; fixture?: string; multiple?: boolean; uploading: boolean; onChange: (files: FileList | null) => void }) {
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

export function RnaPreflightCard({ items, pairMismatch }: { items: RnaPreflightItem[]; pairMismatch: boolean }) {
  const requiredCount = items.filter((item) => item.required).length
  const readyRequiredCount = items.filter((item) => item.required && item.ready).length
  const allRequiredReady = !pairMismatch && readyRequiredCount === requiredCount
  return <div className="mt-5 rounded-xl border border-[#244b45] bg-[#0a211f]/75 p-4" role="status" aria-live="polite">
    <div className="flex flex-wrap items-center justify-between gap-2"><div><div className="field-label">运行前检查</div><div className="mt-1 text-xs text-[#9bc3b8]">{readyRequiredCount}/{requiredCount} 个任务必需输入已满足</div></div><span className={`status-badge ${pairMismatch ? 'status-failed' : allRequiredReady ? 'status-ok' : 'status-running'}`}><span className="size-1.5 rounded-full bg-current" />{pairMismatch ? '配对数量不一致' : allRequiredReady ? '输入已就绪' : '待补齐输入'}</span></div>
    <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{items.map((item) => <div key={item.label} className="flex min-w-0 items-start gap-2 rounded-lg border border-white/[0.06] bg-[#071719]/70 px-2.5 py-2"><div className={`mt-0.5 shrink-0 ${item.ready ? 'text-[#70e3ad]' : item.required ? 'text-[#e6c875]' : 'text-[#6d8d86]'}`}>{item.ready ? <Check size={13} /> : <XCircle size={13} />}</div><div className="min-w-0"><div className="truncate text-[11px] font-medium text-[#b8d8ce]">{item.label}{item.required ? <span className="ml-1 text-[#e6c875]">必需</span> : <span className="ml-1 text-[#688983]">可选</span>}</div><div className="mt-0.5 truncate text-[10px] text-[#6f9189]">{item.detail}</div></div></div>)}</div>
  </div>
}

export function ResearchPlanCard({ plan, loading, onExecute }: { plan: ResearchPlan | null; loading: boolean; onExecute: () => void }) {
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

export function EmptyStream() {
  return <div className="flex flex-1 flex-col items-center justify-center text-center"><div className="grid size-14 place-items-center rounded-2xl border border-[#21443f] bg-[#102b2a] text-[#78cdaa]"><Radio size={23} /></div><div className="mt-4 text-sm font-medium text-[#b1cbc4]">等待任务流</div><div className="mt-2 max-w-[220px] text-xs leading-5 text-[#7fa49c]">提交任务后，这里会实时显示状态和可追溯事件。</div></div>
}
