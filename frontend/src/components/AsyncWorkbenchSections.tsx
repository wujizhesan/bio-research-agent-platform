import { lazy, Suspense } from 'react'
import { RefreshCw } from 'lucide-react'
import type { FrontendErrorEvent } from '../app/frontendObservability'
import type { Job, Plugin } from '../app/types'
import type { ReportPreview } from '../hooks/useReportPreview'
import { SectionErrorBoundary } from './SectionErrorBoundary'

const LazyDomainsView = lazy(() => import('./DomainsView').then((module) => ({ default: module.DomainsView })))
const LazyJobResultSummary = lazy(() => import('./JobResultSummary').then((module) => ({ default: module.JobResultSummary })))
const LazyReportPreviewModal = lazy(() => import('./ReportPreviewModal').then((module) => ({ default: module.ReportPreviewModal })))

function AsyncSectionFallback({ label, overlay = false }: { label: string; overlay?: boolean }) {
  const status = <div role="status" aria-live="polite" className="flex min-h-24 w-full items-center justify-center gap-3 rounded-2xl border border-white/[0.08] bg-[#0b1b1e] px-5 py-6 text-sm text-[#9bb7b0]"><RefreshCw size={16} className="animate-spin text-[#8fe5c1]" />{label}</div>
  if (overlay) return <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#02090a]/80 p-4 backdrop-blur-sm">{status}</div>
  return <div className="mt-5">{status}</div>
}

export function JobResultSection({ job, pluginId, structureId, onDownload, onOpenReport, onError }: {
  job: Job
  pluginId?: string
  structureId: string
  onDownload: (path: string) => void
  onOpenReport: (path: string) => void
  onError: (event: FrontendErrorEvent) => void
}) {
  return <SectionErrorBoundary boundaryName="job-result" title="结果暂时无法显示" description="当前任务返回了无法渲染的结果；故障已限制在结果区域，其他工作台功能仍可继续使用。" traceId={job.trace_id} jobId={job.job_id} pluginId={pluginId} onError={onError} resetKeys={[job.job_id, job.result]}><Suspense fallback={<AsyncSectionFallback label="正在加载结果渲染器…" />}><LazyJobResultSummary job={job} structureId={structureId} onDownload={onDownload} onOpenReport={onOpenReport} /></Suspense></SectionErrorBoundary>
}

export function ReportPreviewSection({ preview, job, pluginId, onClose, onError }: {
  preview: ReportPreview
  job: Job | null
  pluginId?: string
  onClose: () => void
  onError: (event: FrontendErrorEvent) => void
}) {
  return <SectionErrorBoundary boundaryName="report-preview" title="报告预览无法打开" description="报告内容或预览组件发生异常；关闭预览后可以继续使用工作台。" traceId={job?.trace_id} jobId={job?.job_id} pluginId={pluginId} onError={onError} actionLabel="关闭预览" onReset={onClose} resetKeys={[preview.url]} variant="overlay"><Suspense fallback={<AsyncSectionFallback label="正在加载报告预览…" overlay />}><LazyReportPreviewModal preview={preview} onClose={onClose} /></Suspense></SectionErrorBoundary>
}

export function PluginCatalogSection({ plugins, onRefresh, onError }: {
  plugins: Plugin[]
  onRefresh: () => void
  onError: (event: FrontendErrorEvent) => void
}) {
  return <SectionErrorBoundary boundaryName="plugin-catalog" title="插件页面暂时无法显示" description="某个插件返回了异常元数据；故障已限制在插件页面，不会影响任务工作台。" pluginId="catalog" onError={onError} actionLabel="重新加载插件" onReset={onRefresh} resetKeys={[plugins]}><Suspense fallback={<AsyncSectionFallback label="正在加载插件目录…" />}><LazyDomainsView plugins={plugins} onError={onError} /></Suspense></SectionErrorBoundary>
}
