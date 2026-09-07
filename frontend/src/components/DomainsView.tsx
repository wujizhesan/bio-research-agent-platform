import { Activity, Beaker, Boxes, Database, Dna, FlaskConical, Workflow } from 'lucide-react'
import { domainLabels, pluginDescriptions } from '../app/constants'
import type { Plugin } from '../app/types'

const domainIcons: Record<string, typeof Beaker> = {
  cadd: Beaker,
  omics: Activity,
  sequence: Dna,
  literature: FlaskConical,
  knowledge: Database,
  imaging: FlaskConical,
  research: Workflow,
}

export function DomainsView({ plugins }: { plugins: Plugin[] }) {
  return <section className="py-9"><div className="max-w-3xl"><div className="eyebrow">插件目录 / 能力发现</div><h1 className="mt-3 text-4xl font-semibold tracking-[-0.04em] sm:text-5xl">领域是能力，<span className="text-[#8fe5c1]">插件是边界。</span></h1><p className="mt-5 text-sm leading-7 text-[#88a6a0] sm:text-base">每个领域通过统一工具契约接入，状态、版本与能力在运行时可发现。研究代理只编排能力，不把业务逻辑写死在对话层。</p></div><div className="mt-10 grid gap-4 md:grid-cols-2 xl:grid-cols-3">{plugins.map((plugin) => { const Icon = domainIcons[plugin.domain] || Boxes; return <div key={plugin.domain} className="panel group p-5 transition hover:-translate-y-0.5 hover:border-[#3e786a]"><div className="flex items-start justify-between"><div className="grid size-11 place-items-center rounded-xl border border-[#28524b] bg-[#102b2a] text-[#8fe5c1]"><Icon size={20} /></div><span className={`status-badge ${plugin.status === 'available' ? 'status-ok' : 'status-failed'}`}>{plugin.status === 'available' ? '可用' : plugin.status.toUpperCase()}</span></div><h2 className="mt-6 text-lg font-semibold capitalize">{domainLabels[plugin.domain] || plugin.domain}</h2><p className="mt-1 min-h-10 text-xs leading-5 text-[#6e8b85]">{pluginDescriptions[plugin.domain] || plugin.name}</p><div className="mt-5 flex items-end justify-between border-t border-white/[0.07] pt-4"><div><div className="font-mono text-2xl text-[#d7f1e8]">{String(plugin.tool_count).padStart(2, '0')}</div><div className="mt-1 font-mono text-[9px] tracking-[0.15em] text-[#5f7d77]">工具</div></div><div className="text-right font-mono text-[10px] text-[#63837b]">v{plugin.version || 'builtin'}</div></div></div> })}</div></section>
}
