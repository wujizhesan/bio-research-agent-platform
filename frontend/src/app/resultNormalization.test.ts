import { describe, expect, it } from 'vitest'
import type { Job } from './types'
import {
  normalizeCaddHits,
  normalizeJobResult,
  normalizeSequenceBenchmark,
  normalizeSequenceChecks,
  percentMetric,
  sequenceMetricDelta,
} from './resultNormalization'

function completedJob(tool: string, result: Record<string, unknown>): Job {
  return {
    job_id: 'job-test-1',
    tool,
    status: 'completed',
    created_at: '2026-09-07T00:00:00Z',
    finished_at: '2026-09-07T00:01:00Z',
    result,
  }
}

describe('序列与 CADD 结果归一化', () => {
  it('统一序列检查、百分比和嵌套 benchmark 格式', () => {
    const checks = normalizeSequenceChecks([
      ['translation', 'pass', 'restored'],
      { name: 'gc', status: 'fail' },
      'manual-review',
    ])
    const rows = normalizeSequenceBenchmark({
      result: {
        rows: [
          { method: 'naive', metrics: { gc: 0.5, cai: 0.7 } },
          { method: 'greedy', metrics: { GC: 55, cai: 0.91 }, verdict: 'PASS' },
          null,
        ],
      },
    })

    expect(checks).toEqual([
      { name: 'translation', passed: true, detail: 'restored' },
      { name: 'gc', passed: false, detail: undefined },
      { name: 'manual-review', passed: false },
    ])
    expect(rows).toHaveLength(2)
    expect(percentMetric(rows[0].metrics, ['gc'])).toBe(50)
    expect(sequenceMetricDelta(rows[1], rows[0], ['cai'])).toBe('+0.210')
  })

  it('过滤无效 CADD 命中并兼容名称别名', () => {
    expect(normalizeCaddHits({
      top_hits: [
        { name: 'Ligand-A', tag: 'active', affinity: '-9.125' },
        { mol_name: 'invalid', affinity: 'not-a-number' },
        null,
      ],
    })).toEqual([
      { mol_name: 'Ligand-A', tag: 'active', affinity: -9.125 },
    ])
  })
})

describe('normalizeJobResult', () => {
  it('将直接序列任务转换为稳定视图模型', () => {
    const view = normalizeJobResult(completedJob('sequence_pipeline', {
      status: 'completed',
      mrna: 'AUGGCCUAA',
      mrna_len: 9,
      verify: true,
      output_html: 'output/sequence-report.html',
      metrics: { gc: 0.55, cai: 0.91 },
    }))

    expect(view.isSequenceResult).toBe(true)
    expect(view.sequenceResult).toMatchObject({ mrna: 'AUGGCCUAA', verify: true })
    expect(view.sequenceView).toMatchObject({
      mrna: 'AUGGCCUAA',
      mrnaLength: 9,
      cai: 0.91,
      verified: true,
      verdictLabel: '翻译已验证',
    })
    expect(view.sequenceView.gc).toBeCloseTo(55)
    expect(view.sequenceView.metricCards.map((card) => card.value)).toContain('55.0%')
    expect(Object.fromEntries(view.summaryEntries)).toMatchObject({
      status: 'completed',
      mrna_len: 9,
      verify: true,
      report_path: 'output/sequence-report.html',
    })
    expect(view.hasAuditView).toBe(false)
    expect(view.hasEvidenceView).toBe(false)
  })

  it('从工作流步骤聚合 CADD、证据、审计和产物', () => {
    const view = normalizeJobResult(completedJob('research_execute', {
      report: { path: 'output/research-report.html' },
      manifest: {
        status: 'completed',
        manifest_path: 'output/run-manifest.json',
        completed_steps: 2,
        failed_steps: 0,
        steps: [
          {
            id: 'dock',
            tool: 'cadd_run_screening',
            status: 'completed',
            result: {
              result: {
                best_hit: 'Ligand-A',
                best_affinity: -9.125,
                rows: 1,
                hits: [{ mol_name: 'Ligand-A', tag: 'active', affinity: -9.125 }],
              },
            },
          },
          {
            id: 'evidence',
            tool: 'literature_search',
            status: 'completed',
            result: {
              result: {
                provider: 'pubmed',
                requested_gene_ids: ['TP53', 42],
                status: 'fallback',
                fallback_reason: 'remote rate limit',
                matches: [{ title: 'TP53 evidence', source: 'PubMed' }, null],
              },
            },
          },
        ],
      },
    }))

    expect(view.caddResult).toMatchObject({ best_hit: 'Ligand-A', best_affinity: -9.125 })
    expect(view.caddView).toMatchObject({
      bestHit: 'Ligand-A',
      bestAffinity: -9.125,
      rows: 1,
      hasData: true,
    })
    expect(view.evidenceMatches).toEqual([{ title: 'TP53 evidence', source: 'PubMed' }])
    expect(view.evidenceRequestedGeneIds).toEqual(['TP53'])
    expect(view.evidenceFallbackReason).toBe('remote rate limit')
    expect(view.hasEvidenceView).toBe(true)
    expect(view.hasAuditView).toBe(true)
    expect(view.manifestPath).toBe('output/run-manifest.json')
    expect(view.auditReportPath).toBe('output/research-report.html')
    expect(view.traceSteps).toEqual([
      { index: 1, id: 'dock', tool: 'cadd_run_screening', status: 'completed' },
      { index: 2, id: 'evidence', tool: 'literature_search', status: 'completed' },
    ])
  })

  it('聚合 RNA-seq 质控、比对率和工具版本', () => {
    const view = normalizeJobResult(completedJob('research_execute', {
      manifest: {
        steps: [
          {
            tool: 'omics_run_fastq_qc',
            result: {
              status: 'completed',
              reports: ['output/multiqc_report.html'],
              fastqc_summaries: [
                { summary: [{ status: 'pass' }, { status: 'warn' }] },
                { summary: [{ status: 'pass' }, { status: 'fail' }] },
              ],
              provenance: { tools: { fastqc: { version: '0.12.1' }, multiqc: { version: '1.21' } } },
            },
          },
          {
            tool: 'omics_run_rnaseq_alignment',
            result: {
              samples: [
                { sample_id: 'S1', overall_alignment_rate: '90.00%' },
                { sample_id: 'S2', overall_alignment_rate: '85.50%' },
              ],
              provenance: { tools: { hisat2: { version: '2.2.1' } } },
            },
          },
          {
            tool: 'omics_run_feature_counts',
            result: {
              n_genes: 20000,
              n_samples: 2,
              provenance: { tool: { version: '2.0.6' } },
            },
          },
        ],
      },
    }))

    expect(view.hasFastqQc).toBe(true)
    expect(view.fastqQcReport).toBe('output/multiqc_report.html')
    expect(view.fastqQcCounts).toEqual({ pass: 2, warn: 1, fail: 1 })
    expect(view.alignmentRate).toBe('87.75%')
    expect(view.hasRnaseqSummary).toBe(true)
    expect(view.toolProvenance).toEqual([
      { label: 'FastQC', version: '0.12.1' },
      { label: 'MultiQC', version: '1.21' },
      { label: 'HISAT2', version: '2.2.1' },
      { label: 'featureCounts', version: '2.0.6' },
    ])
  })

  it('面对空值和畸形步骤时返回安全默认值', () => {
    const view = normalizeJobResult(completedJob('research_execute', {
      manifest: { steps: [null, 'invalid', { result: [] }] },
      gene_ids: ['TP53', null, 7],
    }))

    expect(view.steps).toEqual([{ result: [] }])
    expect(view.traceSteps).toEqual([{ index: 1, id: 'step-1', tool: 'unknown', status: 'unknown' }])
    expect(view.geneIds).toEqual(['TP53'])
    expect(view.caddResult).toEqual({})
    expect(view.evidenceMatches).toEqual([])
    expect(view.fastqQcCounts).toEqual({ pass: 0, warn: 0, fail: 0 })
  })
})
