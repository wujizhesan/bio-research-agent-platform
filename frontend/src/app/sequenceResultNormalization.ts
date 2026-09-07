import type { SequenceBenchmarkRow, SequenceCheck } from './types'
import {
  isRecord,
  metricNumber,
  percentMetric,
  recordArray,
  recordValue,
  stringValue,
  type ResultRecord,
} from './resultNormalizationShared'

export type SequenceFinding = {
  title: string
  detail: string
  tone: string
}

export type SequenceBenchmarkViewRow = SequenceBenchmarkRow & {
  gc?: number
  gc3?: number
  cai?: number
  caiDelta: string
}

export type SequenceResultViewModel = {
  raw: ResultRecord
  mrna: string
  mrnaLength: unknown
  visibleCodons: string[]
  remainingCodons: string[]
  checks: SequenceCheck[]
  passedChecks: number
  gc?: number
  gc3?: number
  cai?: number
  expressionPercent?: number
  benchmarkRows: SequenceBenchmarkViewRow[]
  benchmarkStatus: string
  moleculeLabel: string
  methodLabel: string
  metricCards: Array<{ label: string; value: string; tone: string }>
  gcWindowSize: number
  gcWindows: Array<{ label: string; value: number }>
  qualityValues: number[]
  verified: boolean
  verdictLabel: string
  decisionReady: boolean
  findings: SequenceFinding[]
}

export function normalizeSequenceChecks(raw: unknown): SequenceCheck[] {
  if (!Array.isArray(raw)) return []
  return raw.map((value) => {
    if (Array.isArray(value)) {
      return { name: String(value[0] || 'check'), passed: value[1] === 'pass' || value[1] === true, detail: value[2] ? String(value[2]) : undefined }
    }
    const item = recordValue(value)
    if (isRecord(value)) {
      return { name: String(item.name || 'check'), passed: item.passed === true || item.status === 'pass', detail: item.detail ? String(item.detail) : undefined }
    }
    return { name: String(value || 'check'), passed: false }
  })
}

export function normalizeSequenceBenchmark(raw: unknown): SequenceBenchmarkRow[] {
  const envelope = recordValue(raw)
  const payload = isRecord(envelope.result) ? envelope.result : envelope
  return recordArray(payload.rows).map((item) => ({
    method: String(item.method || 'unknown'),
    mrna: stringValue(item.mrna),
    metrics: recordValue(item.metrics),
    verdict: item.verdict ? String(item.verdict) : undefined,
  }))
}

export function sequenceMetricDelta(row: SequenceBenchmarkRow, baseline: SequenceBenchmarkRow | undefined, keys: string[]) {
  if (!baseline) return '--'
  const current = metricNumber(row.metrics, keys)
  const base = metricNumber(baseline.metrics, keys)
  if (current === undefined || base === undefined) return '--'
  const delta = current - base
  return `${delta >= 0 ? '+' : ''}${delta.toFixed(3)}`
}

export function normalizeSequenceResult(result: ResultRecord, benchmark?: ResultRecord): SequenceResultViewModel {
  const metrics = recordValue(result.metrics)
  const mrna = stringValue(result.mrna)?.toUpperCase() ?? ''
  const codons = mrna.match(/.{1,3}/g) ?? []
  const checks = normalizeSequenceChecks(result.checks)
  const gc = percentMetric(metrics, ['gc', 'GC%'])
  const gc3 = percentMetric(metrics, ['gc3', 'GC3%'])
  const cai = metricNumber(metrics, ['cai', 'CAI'])
  const upA = metricNumber(metrics, ['up_a', 'UpA/kb'])
  const upU = metricNumber(metrics, ['up_u', 'UpU/kb'])
  const expression = metricNumber(metrics, ['expression_score'])
  const expressionPercent = expression === undefined ? undefined : Math.min(100, Math.max(0, expression <= 1 ? expression * 100 : expression))
  const passedChecks = checks.filter((check) => check.passed).length
  const benchmarkPayload = benchmark ?? (isRecord(result.benchmark) ? result.benchmark : undefined)
  const normalizedBenchmarkRows = normalizeSequenceBenchmark(benchmarkPayload)
  const baseline = normalizedBenchmarkRows.find((row) => row.method === 'naive')
  const benchmarkRows = normalizedBenchmarkRows.map((row) => ({
    ...row,
    gc: percentMetric(row.metrics, ['gc', 'GC%']),
    gc3: percentMetric(row.metrics, ['gc3', 'GC3%']),
    cai: metricNumber(row.metrics, ['cai', 'CAI']),
    caiDelta: sequenceMetricDelta(row, baseline, ['cai', 'CAI']),
  }))
  const optimized = normalizedBenchmarkRows.find((row) => row.method !== 'naive')
  const baselineCai = baseline ? metricNumber(baseline.metrics, ['cai', 'CAI']) : undefined
  const optimizedCai = optimized ? metricNumber(optimized.metrics, ['cai', 'CAI']) : cai
  const caiDelta = baselineCai !== undefined && optimizedCai !== undefined ? optimizedCai - baselineCai : undefined
  const verified = result.verify === true
  const checksPassed = checks.length > 0 && checks.every((check) => check.passed)
  const gcInRange = gc !== undefined && gc >= 30 && gc <= 80
  const benchmarkStatus = stringValue(benchmarkPayload?.vaxpress) ?? ''
  const molecule = String(result.molecule || 'linear')
  const method = String(result.method || 'greedy')
  const moleculeLabels: Record<string, string> = { linear: '线性 mRNA', circ: '环状 RNA', sa: '自扩增 RNA' }
  const methodLabels: Record<string, string> = { greedy: '确定性贪心', vaxpress: 'VaxPress 适配器' }
  const gcWindowSize = 30
  const gcWindows = Array.from({ length: Math.min(12, Math.max(1, Math.ceil(mrna.length / gcWindowSize))) }, (_, index) => {
    const chunk = mrna.slice(index * gcWindowSize, (index + 1) * gcWindowSize)
    return {
      label: `${index * gcWindowSize + 1}-${Math.min(mrna.length, (index + 1) * gcWindowSize)}`,
      value: chunk ? ((chunk.match(/[GC]/g) ?? []).length / chunk.length) * 100 : 0,
    }
  }).filter((item) => item.label.split('-')[0] !== '1' || mrna.length > 0)
  const findings: SequenceFinding[] = [
    { title: '翻译一致性', detail: verified ? '优化序列可以翻译回目标蛋白，阅读框和起始密码子检查通过。' : '翻译回译未通过，不能直接进入后续实验设计。', tone: verified ? 'status-ok' : 'status-failed' },
    { title: '序列组成', detail: gc === undefined ? '缺少 GC 指标，建议先补充评分结果。' : gcInRange ? `GC ${gc.toFixed(1)}% 位于当前规则窗口 30–80% 内。` : `GC ${gc.toFixed(1)}% 超出当前规则窗口，需要人工复核。`, tone: gcInRange ? 'status-ok' : 'status-running' },
    { title: '密码子策略', detail: caiDelta === undefined ? '暂无可用基线，无法判断优化相对收益。' : `相对朴素基线的 CAI 变化为 ${caiDelta >= 0 ? '+' : ''}${caiDelta.toFixed(3)}，仅代表当前规则评分。`, tone: caiDelta !== undefined && caiDelta >= 0 ? 'status-ok' : 'status-running' },
    { title: '后端边界', detail: benchmarkStatus === 'not_configured' ? 'VaxPress 未配置，当前结果来自确定性后端；没有把回退结果当作模型结果。' : '当前结果已记录后端来源，可继续接入外部 mRNA-Forge。', tone: benchmarkStatus === 'not_configured' ? 'status-running' : 'status-ok' },
  ]

  return {
    raw: result,
    mrna,
    mrnaLength: result.mrna_len || mrna.length,
    visibleCodons: codons.slice(0, 18),
    remainingCodons: codons.slice(18),
    checks,
    passedChecks,
    gc,
    gc3,
    cai,
    expressionPercent,
    benchmarkRows,
    benchmarkStatus,
    moleculeLabel: moleculeLabels[molecule] || molecule,
    methodLabel: methodLabels[method] || method,
    metricCards: [
      { label: 'GC 含量', value: gc === undefined ? '--' : `${gc.toFixed(1)}%`, tone: 'text-[#8fe5c1]' },
      { label: 'GC3', value: gc3 === undefined ? '--' : `${gc3.toFixed(1)}%`, tone: 'text-[#aebfff]' },
      { label: 'CAI', value: cai === undefined ? '--' : cai.toFixed(3), tone: 'text-[#f0d38b]' },
      { label: 'UpA / kb', value: upA === undefined ? '--' : upA.toFixed(2), tone: 'text-[#d1a8ff]' },
      { label: 'UpU / kb', value: upU === undefined ? '--' : upU.toFixed(2), tone: 'text-[#f1a99a]' },
      { label: '表达评分', value: expressionPercent === undefined ? '--' : `${expressionPercent.toFixed(1)}%`, tone: 'text-[#b3f4d4]' },
    ],
    gcWindowSize,
    gcWindows,
    qualityValues: [gc || 0, gc3 || 0, cai === undefined ? 0 : cai * 100, expressionPercent === undefined ? (checks.length ? (passedChecks / checks.length) * 100 : 0) : expressionPercent, verified ? 100 : 0],
    verified,
    verdictLabel: verified ? '翻译已验证' : String(result.verdict || '待复核'),
    decisionReady: verified && checksPassed && gcInRange,
    findings,
  }
}
