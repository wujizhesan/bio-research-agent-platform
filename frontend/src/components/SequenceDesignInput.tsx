import { Check, Sparkles } from 'lucide-react'
import { luciferaseDemoProtein } from '../app/constants'
import type { SequenceMethod, SequenceMolecule } from '../app/types'

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
export function SequenceDesignInput({ protein, molecule, method, useVaxpress, structureId, onProteinChange, onMoleculeChange, onMethodChange, onUseVaxpressChange, onStructureChange }: SequenceDesignInputProps) {
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
