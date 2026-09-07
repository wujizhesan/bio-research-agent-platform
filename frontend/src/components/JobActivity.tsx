import { Ban, ChevronRight, Clock3, RefreshCw } from 'lucide-react'
import type { EventItem, Job } from '../app/types'
import { formatJobId, formatTime } from '../hooks/useJobRunner'
import { EmptyStream, StatusBadge } from './WorkspaceStatus'

export function JobControl({ job, loading, onCancel, onRetry }: {
  job: Job | null
  loading: boolean
  onCancel: () => void
  onRetry: (job: Job) => void
}) {
  if (!job) return null
  if (job.status === 'queued' || job.status === 'running') {
    return <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#5c4930] bg-[#211d16] px-5 py-4"><div className="flex items-center gap-3"><Ban size={16} className="text-[#e6c875]" /><div><div className="text-sm font-medium text-[#f1dfaa]">任务控制</div><div className="mt-1 text-xs text-[#aa9767]">排队中的任务会立即取消，运行中的任务采用协作式取消。</div></div></div><button onClick={onCancel} disabled={job.cancel_requested} className="rounded-lg border border-[#80643c] px-3 py-2 text-xs font-medium text-[#f1d889] transition hover:bg-[#392d1c] disabled:cursor-not-allowed disabled:opacity-50">{job.cancel_requested ? '取消请求已发送' : '取消任务'}</button></div>
  }
  if (job.status === 'failed' || job.status === 'cancelled') {
    return <div className="mt-5 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-[#3f527c] bg-[#111d32] px-5 py-4"><div className="flex items-center gap-3"><RefreshCw size={16} className="text-[#aebfff]" /><div><div className="text-sm font-medium text-[#d7ddff]">任务恢复</div><div className="mt-1 text-xs text-[#99a6cf]">保留原任务记录，复制原始参数重新提交。</div></div></div><button aria-label="Retry selected task" onClick={() => onRetry(job)} disabled={loading} className="rounded-lg bg-[#aebfff] px-3 py-2 text-xs font-semibold text-[#111a34] transition hover:bg-[#c4d0ff] disabled:cursor-not-allowed disabled:opacity-50">重试任务</button></div>
  }
  return null
}

export function ExecutionStream({ job, events }: { job: Job | null; events: EventItem[] }) {
  return <div className="panel flex min-h-[326px] flex-col p-5 sm:p-6"><div className="flex items-start justify-between"><div><div className="eyebrow">02 / 执行流</div><h2 className="mt-2 text-xl font-semibold">实时执行轨迹</h2></div><div className="flex items-center gap-1.5 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#8fe5c1]"><span className="size-1.5 animate-pulse rounded-full bg-[#70e3ad]" />SSE</div></div>{job ? <div className="mt-7 flex flex-1 flex-col"><div className="flex items-center justify-between border-b border-white/10 pb-4"><div><div className="font-mono text-[11px] text-[#6f9189]">{formatJobId(job.job_id)}</div><div className="mt-1 text-sm font-medium">{job.tool}</div></div><StatusBadge status={job.status} /></div><div className="mt-5 space-y-3">{events.slice(-4).map((event, index) => <div key={`${event.at}-${index}`} className="flex items-start gap-3 text-xs"><div className="mt-1.5 size-1.5 rounded-full bg-[#83e3bc] shadow-[0_0_12px_#83e3bc]" /><div className="min-w-0 flex-1"><div className="text-[#b2cbc4]">{event.detail}</div><div className="mt-1 font-mono text-[10px] text-[#7fa49c]">{event.at} · {event.status}</div></div></div>)}</div><div className="mt-auto flex items-center gap-2 pt-5 font-mono text-[10px] text-[#7fa49c]"><Clock3 size={13} />{job.status === 'completed' ? `完成于 ${formatTime(job.finished_at)}` : '等待状态更新…'}</div></div> : <EmptyStream />}</div>
}

export function RecentJobs({ jobs, onRefresh, onSelect }: {
  jobs: Job[]
  onRefresh: () => void
  onSelect: (job: Job) => void
}) {
  return <section className="panel mt-5 overflow-hidden"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-5 py-5 sm:px-6"><div><div className="eyebrow">03 / 最近任务</div><h2 className="mt-2 text-xl font-semibold">最近任务</h2></div><button onClick={onRefresh} className="inline-flex items-center gap-2 rounded-lg border border-white/10 px-3 py-2 text-xs text-[#9bb7b0] transition hover:border-[#4f8c7d] hover:text-[#d6eee7]"><RefreshCw size={13} />刷新</button></div>{jobs.length ? <div className="overflow-x-auto"><table className="w-full min-w-[680px] text-left text-sm"><thead className="bg-white/[0.025] font-mono text-[10px] tracking-[0.12em] text-[#7fa49c]"><tr><th className="px-5 py-3 font-normal sm:px-6">任务 ID</th><th className="px-5 py-3 font-normal">工具</th><th className="px-5 py-3 font-normal">状态</th><th className="px-5 py-3 font-normal">创建时间</th><th className="px-5 py-3 font-normal"><span className="sr-only">操作</span></th></tr></thead><tbody>{jobs.map((job) => <tr key={job.job_id} onClick={() => onSelect(job)} className="cursor-pointer border-t border-white/[0.06] transition hover:bg-white/[0.035]"><td className="px-5 py-4 font-mono text-xs text-[#81aaa1] sm:px-6">{formatJobId(job.job_id)}</td><td className="px-5 py-4 font-medium text-[#c7ddd7]">{job.tool}</td><td className="px-5 py-4"><StatusBadge status={job.status} /></td><td className="px-5 py-4 font-mono text-xs text-[#7fa49c]">{formatTime(job.created_at)}</td><td className="px-5 py-4 text-right text-[#6b8f87]"><button type="button" aria-label={`查看任务 ${job.job_id}`} onClick={(event) => { event.stopPropagation(); onSelect(job) }} className="inline-flex rounded-lg p-2 transition hover:bg-white/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#8fe5c1]"><ChevronRight size={15} /></button></td></tr>)}</tbody></table></div> : <div className="px-6 py-12 text-center text-sm text-[#7fa49c]">还没有运行记录，先启动一条研究路径。</div>}</section>
}
