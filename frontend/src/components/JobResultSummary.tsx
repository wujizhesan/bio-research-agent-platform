import { useState } from 'react'
import { ArrowUpRight, BarChart3, Check, Download, Sparkles } from 'lucide-react'
import { providerLabels } from '../app/constants'
import {
  jobResultArtifactKeys,
  normalizeJobResult,
} from '../app/resultNormalization'
import type { CaddResultViewModel, SequenceResultViewModel } from '../app/resultNormalization'
import type { Job } from '../app/types'
import { PipelineMetric, QcStatusMetric } from './WorkspaceStatus'

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

function SequenceInterpretationPanel({ view }: { view: SequenceResultViewModel }) {
  const { decisionReady, findings } = view
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

function SequenceResultPanel({ view, reportPath, structureId, onDownload, onOpenReport }: { view: SequenceResultViewModel; reportPath?: string; structureId?: string; onDownload: (path: string) => void; onOpenReport: (path: string) => void }) {
  const {
    mrna,
    mrnaLength,
    visibleCodons,
    remainingCodons,
    checks,
    passedChecks,
    expressionPercent,
    benchmarkRows,
    benchmarkStatus,
    moleculeLabel,
    methodLabel,
    metricCards,
    gcWindowSize,
    gcWindows,
    qualityValues,
    verified,
    verdictLabel,
  } = view
  return <section className="mt-5 rounded-2xl border border-[#28524b] bg-[linear-gradient(135deg,rgba(16,43,42,.96),rgba(8,25,29,.96))] p-5 sm:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><div className="eyebrow">序列设计 / 质量概览</div><h3 className="mt-2 text-lg font-semibold text-[#e4f8ef]">mRNA 优化结果</h3><p className="mt-1 text-xs text-[#7fa99e]">{moleculeLabel} · {methodLabel} · 优化 → 评分 → 验证</p></div>
      <div className="flex flex-wrap items-center justify-end gap-2"><span className={`status-badge ${verified ? 'status-ok' : 'status-running'}`}><span className="size-1.5 rounded-full bg-current" />{verdictLabel}</span>{reportPath && <><button onClick={() => onOpenReport(reportPath)} className="inline-flex items-center gap-1.5 rounded-full border border-[#405b96] bg-[#152442] px-2.5 py-1 font-mono text-[10px] text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><ArrowUpRight size={12} />查看报告</button><button onClick={() => onDownload(reportPath)} className="inline-flex items-center gap-1.5 rounded-full border border-[#28524b] bg-[#102b2a] px-2.5 py-1 font-mono text-[10px] text-[#b9e6d5] transition hover:border-[#71cba7] hover:text-white"><Download size={12} />下载 HTML</button></>}</div>
    </div>
    <nav aria-label="mRNA 结果导航" className="mt-4 flex flex-wrap gap-1.5 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-1.5 text-[10px]"><a href="#sequence-core-metrics" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">核心指标</a><a href="#sequence-quality" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">质量画像</a>{benchmarkRows.length > 0 && <a href="#sequence-benchmark" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">基准比较</a>}{structureId && <a href="#sequence-structure" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">结构</a>}<a href="#sequence-interpretation" className="rounded-lg px-2.5 py-1.5 text-[#a9c8be] transition hover:bg-[#123631] hover:text-[#e8fff5]">解读</a></nav>
    <div className="mt-5 rounded-2xl border border-[#32665b] bg-[#061b1d]/80 p-4">
      <div className="flex items-center justify-between gap-3"><div className="field-label mb-0">优化后的 mRNA / {String(mrnaLength)} nt</div><div className="font-mono text-[10px] text-[#6e9d91]">5&apos; → 3&apos;</div></div>
      <div className="mt-3 flex flex-wrap gap-1.5">{visibleCodons.map((codon, index) => <span key={`${codon}-${index}`} className="rounded-md border border-[#2b6457] bg-[#123631] px-2.5 py-2 font-mono text-sm tracking-[0.16em] text-[#d0f7e5]">{codon}</span>)}</div>
      {remainingCodons.length > 0 && <details className="mt-3 rounded-lg border border-white/[0.08] bg-[#071719]/70"><summary className="cursor-pointer px-3 py-2.5 text-xs text-[#9fc4b8]">查看完整序列（剩余 {remainingCodons.length} 个密码子）</summary><div className="border-t border-white/[0.07] p-3"><pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-5 tracking-[0.08em] text-[#b9e6d5]">{mrna}</pre></div></details>}
      {!mrna && <div className="mt-2 text-xs text-[#789791]">结果中没有返回序列文本，请下载完整 JSON 查看。</div>}
    </div>
    <div id="sequence-core-metrics" className="mt-4 scroll-mt-6 grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">{metricCards.map((card) => <div key={card.label} className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">{card.label}</div><div className={`mt-2 font-mono text-xl ${card.tone}`}>{card.value}</div></div>)}</div>
    {expressionPercent !== undefined && <div className="mt-4 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="flex items-center justify-between text-[10px] text-[#86a59e]"><span className="font-mono tracking-[0.12em]">表达评分 · 启发式</span><span className="font-mono text-[#d4f7e6]">{expressionPercent.toFixed(1)}%</span></div><div className="mt-2 h-2 overflow-hidden rounded-full bg-[#17312f]"><div className="h-full rounded-full bg-gradient-to-r from-[#4dba91] to-[#b3f4d4]" style={{ width: `${expressionPercent}%` }} /></div></div>}
    <div id="sequence-quality" className="mt-4 scroll-mt-6 grid gap-4 lg:grid-cols-[0.8fr_1.2fr]">
      <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="field-label mb-0">质量雷达</div><SequenceQualityRadar values={qualityValues} /></div>
      <div className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="flex items-center justify-between gap-3"><div className="field-label mb-0">滑动窗口 GC / {gcWindowSize} nt</div><span className="font-mono text-[10px] text-[#83e3bc]">{gcWindows.length} 个窗口</span></div>{gcWindows.length ? <div className="mt-5 space-y-3">{gcWindows.map((window) => <div key={window.label} className="grid grid-cols-[78px_1fr_48px] items-center gap-3"><span className="font-mono text-[10px] text-[#6f9189]">{window.label}</span><div className="h-2 overflow-hidden rounded-full bg-[#17312f]"><div className={`h-full rounded-full ${window.value >= 30 && window.value <= 80 ? 'bg-[#74d7ad]' : 'bg-[#e6c875]'}`} style={{ width: `${Math.max(2, Math.min(100, window.value))}%` }} /></div><span className="text-right font-mono text-[10px] text-[#b7dace]">{window.value.toFixed(1)}%</span></div>)}</div> : <div className="mt-5 text-xs text-[#6f9189]">暂无序列窗口。</div>}</div>
    </div>
    {checks.length > 0 && <div className="mt-4 rounded-xl border border-white/[0.08] bg-[#071719]/70 p-4"><div className="flex items-center justify-between gap-3"><div className="field-label mb-0">规则检查</div><span className="font-mono text-[10px] text-[#83e3bc]">{passedChecks}/{checks.length} 通过</span></div><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{checks.map((check, index) => <div key={`${check.name}-${index}`} className="flex items-start gap-2 rounded-lg border border-white/[0.06] px-3 py-2"><Check size={13} className={`mt-0.5 shrink-0 ${check.passed ? 'text-[#70e3ad]' : 'text-[#ec9b87]'}`} /><div className="min-w-0"><div className="truncate text-xs text-[#c5e1d7]">{check.name}</div>{check.detail && <div className="mt-1 truncate text-[10px] text-[#6f9189]">{check.detail}</div>}</div></div>)}</div></div>}
    {benchmarkRows.length > 0 && <div className="mt-4 overflow-hidden rounded-xl border border-white/[0.08] bg-[#071719]/70"><div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-4 py-3"><div><div className="field-label mb-0">基准 / 基线比较</div><div className="mt-1 text-xs text-[#6f9189]">与朴素反向翻译基线比较关键序列指标</div></div>{benchmarkStatus && <span className={`status-badge ${benchmarkStatus === 'not_configured' ? 'status-running' : 'status-ok'}`}>{benchmarkStatus === 'not_configured' ? '已记录 VaxPress 回退' : `VaxPress：${benchmarkStatus}`}</span>}</div><div className="overflow-x-auto"><table className="w-full min-w-[620px] text-left text-xs"><thead className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]"><tr><th className="px-4 py-3 font-normal">方法</th><th className="px-4 py-3 font-normal">GC</th><th className="px-4 py-3 font-normal">GC3</th><th className="px-4 py-3 font-normal">CAI</th><th className="px-4 py-3 font-normal">Δ CAI</th><th className="px-4 py-3 font-normal">结论</th></tr></thead><tbody>{benchmarkRows.map((row) => <tr key={row.method} className="border-t border-white/[0.06]"><td className="px-4 py-3 font-medium text-[#c8e6db]">{row.method === 'naive' ? '朴素基线' : row.method === 'greedy' ? '贪心优化' : row.method}</td><td className="px-4 py-3 font-mono text-[#9fe5c5]">{row.gc?.toFixed(1) || '--'}%</td><td className="px-4 py-3 font-mono text-[#aebfff]">{row.gc3?.toFixed(1) || '--'}%</td><td className="px-4 py-3 font-mono text-[#f0d38b]">{row.cai?.toFixed(3) || '--'}</td><td className="px-4 py-3 font-mono text-[#b9e6d5]">{row.caiDelta}</td><td className="px-4 py-3"><span className={`status-badge ${row.verdict === 'PASS' ? 'status-ok' : 'status-running'}`}>{row.verdict === 'PASS' ? '通过' : '复核'}</span></td></tr>)}</tbody></table></div></div>}
    {benchmarkRows.length > 0 && <div id="sequence-benchmark" className="scroll-mt-6" aria-hidden="true" />}
    {structureId && <SequenceStructurePanel structureId={structureId} />}
    <SequenceInterpretationPanel view={view} />
  </section>
}

function CaddResultPanel({ view, onDownload }: { view: CaddResultViewModel; onDownload: (path: string) => void }) {
  const { hits, maxAbsAffinity, scorePlot, topMoleculeImage, bestHit, bestAffinity, rows, maxLigands } = view
  return <section className="mt-5 rounded-2xl border border-[#3d5a8c] bg-[linear-gradient(135deg,rgba(17,29,50,.96),rgba(11,21,38,.96))] p-5 sm:p-6">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="eyebrow text-[#8298d9]">CADD / 虚拟筛选</div><h3 className="mt-2 text-lg font-semibold text-[#eef1ff]">命中排序与结合能</h3><p className="mt-1 text-xs text-[#93a5d4]">数值越负，表示对接受体的预测结合越强</p></div><div className="grid size-10 place-items-center rounded-xl border border-[#405b96] bg-[#152442] text-[#aebfff]"><BarChart3 size={19} /></div></div>
    <div className="mt-5 grid gap-2 sm:grid-cols-3"><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">最佳命中</div><div className="mt-2 truncate text-lg font-semibold text-[#dbe2ff]">{bestHit}</div></div><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">最佳亲和力</div><div className="mt-2 font-mono text-lg text-[#aebfff]">{bestAffinity === undefined ? '--' : `${bestAffinity.toFixed(3)} kcal/mol`}</div></div><div className="rounded-xl border border-white/[0.08] bg-[#0b182d]/80 p-3"><div className="font-mono text-[9px] tracking-[0.12em] text-[#8298c7]">成功对接数</div><div className="mt-2 font-mono text-lg text-[#8fe5c1]">{String(rows)} / {String(maxLigands)}</div></div></div>
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

export function JobResultSummary({ job, structureId, onDownload, onOpenReport }: { job: Job; structureId?: string; onDownload: (path: string) => void; onOpenReport: (path: string) => void }) {
  const view = normalizeJobResult(job)
  const {
    payload,
    manifestStatus,
    manifestPath,
    auditReportPath,
    steps,
    traceSteps,
    summaryEntries,
    isSequenceResult,
    hasEvidenceView,
    hasAuditView,
    hasFastqQc,
    hasRnaseqSummary,
    sequenceResult,
    sequenceView,
    sequenceReportPath,
    caddView,
    evidenceMatches,
    evidenceCitations,
    evidenceProvider,
    evidenceRequestedGeneIds,
    evidenceSource,
    evidenceEndpoint,
    evidenceStatus,
    evidenceFallbackReason,
    knowledgeMatches,
    graphMetrics,
    genomicsMetrics,
    imageMetrics,
    singleCellMetrics,
    metagenomicsMetrics,
    fastqQcSummaries,
    fastqQcReport,
    fastqQcCounts,
    toolProvenance,
    alignmentSamples,
    alignmentRate,
    featureCountsResult,
    differential,
    geneIds,
  } = view
  const jumpTo = (selector: string) => document.querySelector(selector)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  const metadataGrid = <div className="grid gap-3 px-5 py-5 sm:grid-cols-2 lg:grid-cols-4 sm:px-6">{summaryEntries.map(([key, value]) => <div key={key} className="rounded-xl border border-white/[0.08] bg-[#071719]/70 p-3"><div className="font-mono text-[9px] uppercase tracking-[0.12em] text-[#63817b]">{key}</div>{jobResultArtifactKeys.has(key) && typeof value === 'string' ? <div className="mt-2 flex flex-wrap gap-2">{key === 'report_path' && <button onClick={() => onOpenReport(value)} title={`预览 ${value}`} aria-label={`预览 ${key}`} className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[#405b96] bg-[#152442] px-2.5 py-1.5 text-xs text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><ArrowUpRight size={13} /><span className="truncate">查看报告</span></button>}<button onClick={() => onDownload(value)} title={value} aria-label={`下载 ${key}`} className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[#28524b] bg-[#102b2a] px-2.5 py-1.5 text-xs text-[#b9e6d5] transition hover:border-[#71cba7] hover:text-[#ecfff7]"><Download size={13} /><span className="truncate">下载产物</span></button></div> : <div className="mt-2 truncate text-sm text-[#c9e5dc]">{String(value)}</div>}</div>)}</div>
  return <section data-result-overview className="panel mt-5 overflow-hidden" aria-live="polite">
    <nav aria-label="结果分区" className="flex flex-wrap gap-2 border-b border-white/[0.08] px-5 py-3 sm:px-6"><button type="button" onClick={() => jumpTo('[data-result-overview]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">概览</button>{hasEvidenceView && <button type="button" onClick={() => jumpTo('[data-result-evidence]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">证据</button>}{hasAuditView && <button type="button" onClick={() => jumpTo('[data-result-audit]')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">审计</button>}<button type="button" onClick={() => jumpTo('details')} className="rounded-lg border border-white/[0.08] bg-white/[0.035] px-3 py-2 text-[10px] font-medium text-[#b9e6d5] transition hover:border-[#71cba7]">原始 JSON</button></nav>
    <div className="flex items-center justify-between border-b border-white/10 px-5 py-5 sm:px-6"><div><div className="eyebrow">{isSequenceResult ? 'mRNA-Forge / 结果工作区' : '结果 / 溯源'}</div><h2 className="mt-2 text-xl font-semibold">{isSequenceResult ? '序列优化结果' : '结构化结果'}</h2></div><Check size={18} className="text-[#83e3bc]" /></div>
    {isSequenceResult ? <details className="border-b border-white/[0.08] bg-[#061719]/45"><summary className="cursor-pointer list-none px-5 py-3 text-xs text-[#9bb9b0] outline-none focus-visible:ring-2 focus-visible:ring-[#8fe5c1] focus-visible:ring-inset sm:px-6">运行溯源与产物 <span className="ml-2 font-mono text-[10px] text-[#63817b]">可展开</span></summary>{metadataGrid}</details> : metadataGrid}
    {isSequenceResult && <SequenceResultPanel view={sequenceView} reportPath={sequenceReportPath} structureId={structureId} onDownload={onDownload} onOpenReport={onOpenReport} />}
    {caddView.hasData && <CaddResultPanel view={caddView} onDownload={onDownload} />}
    <AgentEvidencePanel evidenceMatches={evidenceMatches} evidenceCitations={evidenceCitations} knowledgeMatches={knowledgeMatches} graphMetrics={graphMetrics} provider={evidenceProvider} />
    <EvidenceProvenancePanel provider={evidenceProvider} requestedGeneIds={evidenceRequestedGeneIds} source={evidenceSource} endpoint={evidenceEndpoint} status={evidenceStatus} fallbackReason={evidenceFallbackReason} />
    <MultiOmicsSummaryPanel genomicsMetrics={genomicsMetrics} singleCellMetrics={singleCellMetrics} imageMetrics={imageMetrics} metagenomicsMetrics={metagenomicsMetrics} evidenceMatches={evidenceMatches} knowledgeMatches={knowledgeMatches} graphMetrics={graphMetrics} sequenceResult={sequenceResult} />
    <div data-result-audit className="scroll-mt-6" />
    <AgentExecutionAuditPanel status={manifestStatus} steps={steps} manifestPath={manifestPath} reportPath={auditReportPath} />
     {hasFastqQc && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">FASTQ 质量控制</div><div className="mt-1 text-sm text-[#b9e6d5]">FastQC {fastqQcSummaries.length ? `完成 ${fastqQcSummaries.length} 个报告` : '报告'} · MultiQC 汇总已生成</div></div>{fastqQcReport && <button onClick={() => onDownload(fastqQcReport)} className="inline-flex items-center gap-2 rounded-lg border border-[#3d5a8c] bg-[#111d32] px-3 py-2 text-xs font-medium text-[#cbd4ff] transition hover:border-[#aebfff] hover:text-white"><Download size={13} />下载 MultiQC 报告</button>}</div><div className="mt-4 grid grid-cols-3 gap-2 sm:max-w-md"><QcStatusMetric label="通过" value={fastqQcCounts.pass} className="status-ok" /><QcStatusMetric label="警告" value={fastqQcCounts.warn} className="status-running" /><QcStatusMetric label="失败" value={fastqQcCounts.fail} className="status-failed" /></div></div>}
     {toolProvenance.length > 0 && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">工具链溯源</div><div className="mt-1 text-sm text-[#b9e6d5]">版本信息来自本次任务的实际执行环境</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />运行环境已验证</span></div><div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{toolProvenance.map((item) => <div key={item.label} className="rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><div className="font-mono text-[9px] tracking-[0.12em] text-[#63817b]">{item.label}</div><div className="mt-1 truncate text-[11px] text-[#c9e5dc]" title={item.version}>{item.version}</div></div>)}</div></div>}
     {hasRnaseqSummary && <div className="border-t border-white/[0.08] px-5 py-5 sm:px-6"><div className="flex flex-wrap items-center justify-between gap-3"><div><div className="field-label">RNA-seq 流程摘要</div><div className="mt-1 text-sm text-[#b9e6d5]">比对、计数和差异分析结果已汇总，可直接查看或继续分析。</div></div><span className="status-badge status-ok"><span className="size-1.5 rounded-full bg-current" />链路完成</span></div><div className="mt-4 grid grid-cols-2 gap-2 lg:grid-cols-4"><PipelineMetric label="样本数" value={alignmentSamples.length || featureCountsResult.n_samples || '—'} /><PipelineMetric label="平均比对率" value={alignmentRate || '—'} /><PipelineMetric label="计数基因数" value={featureCountsResult.n_genes ?? '—'} /><PipelineMetric label="显著差异基因" value={differential.n_significant ?? '—'} /></div>{alignmentSamples.length > 0 && <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{alignmentSamples.map((sample, index) => <div key={`${String(sample.sample_id || 'sample')}-${index}`} className="flex items-center justify-between rounded-lg border border-white/[0.07] bg-[#071719]/70 px-3 py-2.5"><span className="font-mono text-[10px] text-[#a9cbc0]">{String(sample.sample_id || `sample-${index + 1}`)}</span><span className="font-mono text-[10px] text-[#8fe5c1]">{String(sample.overall_alignment_rate || '—')}</span></div>)}</div>}</div>}
     {traceSteps.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">工作流追踪 / 工具链</div><div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{traceSteps.map((step) => <div key={`${step.index}-${step.id}`} className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-[#071719]/70 px-3 py-3"><div className="grid size-7 shrink-0 place-items-center rounded-lg border border-[#28524b] bg-[#102b2a] font-mono text-[10px] text-[#8fe5c1]">{String(step.index).padStart(2, '0')}</div><div className="min-w-0 flex-1"><div className="truncate text-xs font-medium text-[#c9e5dc]">{step.id}</div><div className="mt-1 truncate font-mono text-[9px] text-[#66857e]">{step.tool}</div></div><span className={`status-badge ${step.status === 'completed' ? 'status-ok' : step.status === 'failed' ? 'status-failed' : 'status-running'}`}>{step.status === 'completed' ? '已完成' : step.status === 'failed' ? '失败' : '运行中'}</span></div>)}</div></div>}
    {geneIds.length > 0 && <div className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><div className="field-label">已注释基因 ID</div><div className="mt-2 flex flex-wrap gap-2">{geneIds.map((geneId) => <span key={geneId} className="rounded-md border border-[#28524b] bg-[#102b2a] px-2 py-1 font-mono text-[10px] text-[#b9e6d5]">{geneId}</span>)}</div></div>}
    <details className="border-t border-white/[0.08] px-5 py-4 sm:px-6"><summary className="cursor-pointer text-xs text-[#8fb2a8]">查看完整结果 JSON</summary><pre className="mt-3 max-h-64 overflow-auto rounded-xl bg-[#061113] p-3 text-[10px] leading-5 text-[#91b8ac]">{JSON.stringify(payload, null, 2)}</pre></details>
  </section>
}
