import type { Job } from './types'
import { normalizeCaddResult, type CaddResultViewModel } from './caddResultNormalization'
import { normalizeSequenceResult, type SequenceResultViewModel } from './sequenceResultNormalization'
import {
  isRecord,
  recordArray,
  recordValue,
  stringArray,
  stringValue,
  type ResultRecord,
} from './resultNormalizationShared'

export type TraceStep = {
  index: number
  id: string
  tool: string
  status: string
}

export type JobResultViewModel = {
  payload: ResultRecord
  manifest: ResultRecord
  manifestStatus?: string
  manifestPath?: string
  auditReportPath?: string
  steps: ResultRecord[]
  traceSteps: TraceStep[]
  summaryEntries: Array<[string, unknown]>
  isSequenceResult: boolean
  hasEvidenceView: boolean
  hasAuditView: boolean
  hasFastqQc: boolean
  hasRnaseqSummary: boolean
  sequenceResult: ResultRecord
  sequenceView: SequenceResultViewModel
  sequenceBenchmark: ResultRecord
  sequenceReportPath?: string
  caddResult: ResultRecord
  caddView: CaddResultViewModel
  evidenceMatches: ResultRecord[]
  evidenceCitations: ResultRecord[]
  evidenceProvider?: string
  evidenceRequestedGeneIds: string[]
  evidenceSource?: string
  evidenceEndpoint?: string
  evidenceStatus?: string
  evidenceFallbackReason?: string
  knowledgeMatches: ResultRecord[]
  graphMetrics: ResultRecord
  genomicsMetrics: ResultRecord
  imageMetrics: ResultRecord
  singleCellMetrics: ResultRecord
  metagenomicsMetrics: ResultRecord
  fastqQcSummaries: ResultRecord[]
  fastqQcReport?: string
  fastqQcCounts: Record<'pass' | 'warn' | 'fail', number>
  toolProvenance: Array<{ label: string; version: string }>
  alignmentSamples: ResultRecord[]
  alignmentRate?: string
  featureCountsResult: ResultRecord
  differential: ResultRecord
  geneIds: string[]
}

export const jobResultArtifactKeys = new Set([
  'output_csv',
  'cadd_result_csv',
  'manifest_path',
  'report_path',
  'cadd_report',
  'fastq_manifest',
  'fastq_qc_report',
  'fastq_qc_manifest',
  'feature_counts_csv',
  'feature_counts_summary',
  'differential_expression_csv',
  'pathway_enrichment_csv',
  'omics_report',
  'image_manifest',
  'single_cell_metrics',
  'metagenomics_relative_abundance',
  'metagenomics_sample_metrics',
  'knowledge_index',
  'knowledge_graph',
])

function stepResult(steps: ResultRecord[], tool: string) {
  return recordValue(steps.find((step) => step.tool === tool)?.result)
}

function optionalStepResult(steps: ResultRecord[], tool: string) {
  const result = steps.find((step) => step.tool === tool)?.result
  return isRecord(result) ? result : undefined
}

function unwrappedResult(envelope: ResultRecord) {
  return isRecord(envelope.result) ? envelope.result : envelope
}

export function normalizeJobResult(job: Job): JobResultViewModel {
  const payload = recordValue(job.result)
  const manifest = recordValue(payload.manifest)
  const steps = recordArray(manifest.steps)
  const annotationResult = stepResult(steps, 'omics_annotate_variants')
  const omicsResult = stepResult(steps, 'omics_run_analysis')
  const differential = recordValue(omicsResult.differential_expression)
  const pathway = recordValue(omicsResult.pathway_enrichment)
  const omicsReport = recordValue(omicsResult.report)
  const alignmentResult = stepResult(steps, 'omics_run_rnaseq_alignment')
  const alignmentSamples = recordArray(alignmentResult.samples)
  const alignmentRates = alignmentSamples
    .map((sample) => sample.overall_alignment_rate)
    .filter((value): value is string => typeof value === 'string')
    .map((value) => Number.parseFloat(value))
    .filter((value) => Number.isFinite(value))
  const alignmentRate = alignmentRates.length
    ? `${(alignmentRates.reduce((total, value) => total + value, 0) / alignmentRates.length).toFixed(2)}%`
    : undefined
  const featureCountsResult = stepResult(steps, 'omics_run_feature_counts')

  const caddEnvelope = optionalStepResult(steps, 'cadd_run_screening') ?? (job.tool === 'cadd_run_screening' ? payload : {})
  const caddResult = unwrappedResult(caddEnvelope)

  const fastqQcResult = stepResult(steps, 'omics_run_fastq_qc')
  const fastqQcReports = stringArray(fastqQcResult.reports)
  const fastqQcReport = fastqQcReports.find((value) => value.toLowerCase().includes('multiqc_report.html'))
  const fastqQcSummaries = recordArray(fastqQcResult.fastqc_summaries)
  const fastqQcCounts = fastqQcSummaries.reduce<Record<'pass' | 'warn' | 'fail', number>>((counts, report) => {
    recordArray(report.summary).forEach((module) => {
      const status = module.status
      if (status === 'pass' || status === 'warn' || status === 'fail') counts[status] += 1
    })
    return counts
  }, { pass: 0, warn: 0, fail: 0 })

  const fastqTools = recordValue(recordValue(fastqQcResult.provenance).tools)
  const alignmentTools = recordValue(recordValue(alignmentResult.provenance).tools)
  const featureCountsTool = recordValue(recordValue(featureCountsResult.provenance).tool)
  const toolProvenance = [
    { label: 'FastQC', version: recordValue(fastqTools.fastqc).version },
    { label: 'MultiQC', version: recordValue(fastqTools.multiqc).version },
    { label: 'HISAT2', version: recordValue(alignmentTools.hisat2).version },
    { label: 'HISAT2-build', version: recordValue(alignmentTools['hisat2-build']).version },
    { label: 'SAMtools', version: recordValue(alignmentTools.samtools).version },
    { label: 'featureCounts', version: featureCountsTool.version },
    { label: 'Statistics', version: differential.backend ? `${String(differential.backend)} backend` : undefined },
  ].filter((item): item is { label: string; version: string } => typeof item.version === 'string' && item.version.length > 0)

  const genomicsResult = stepResult(steps, 'omics_run_genomics_qc')
  const genomicsMetrics = recordValue(genomicsResult.metrics)
  const imageResult = stepResult(steps, 'imaging_inspect_image')
  const imageMetrics = recordValue(imageResult.metrics)
  const singleCellResult = stepResult(steps, 'omics_run_single_cell_10x_qc')
  const singleCellMetrics = recordValue(singleCellResult.metrics)
  const singleCellOutputs = recordValue(singleCellResult.outputs)
  const metagenomicsResult = stepResult(steps, 'omics_run_metagenomics_qc')
  const metagenomicsMetrics = recordValue(metagenomicsResult.metrics)
  const metagenomicsOutputs = recordValue(metagenomicsResult.outputs)

  const evidenceEnvelope = optionalStepResult(steps, 'literature_search') ?? (job.tool === 'literature_search' ? payload : {})
  const evidenceResult = recordValue(evidenceEnvelope.result)
  const evidenceSummaryResult = recordValue(stepResult(steps, 'literature_summarize').result)
  const evidenceMatches = recordArray(evidenceResult.matches)
  const evidenceCitations = recordArray(evidenceSummaryResult.citations)
  const evidenceProvider = stringValue(evidenceResult.provider)
  const evidenceRequestedGeneIds = stringArray(evidenceResult.requested_gene_ids)
  const evidenceSource = stringValue(evidenceResult.source_file)
  const evidenceEndpoint = stringValue(evidenceResult.endpoint)
  const evidenceStatus = stringValue(evidenceResult.status)
  const evidenceFallbackReason = stringValue(evidenceResult.fallback_reason)

  const knowledgeIngestResult = recordValue(stepResult(steps, 'knowledge_ingest_directory').result)
  const knowledgeSearchResult = recordValue(stepResult(steps, 'knowledge_search').result)
  const knowledgeMatches = recordArray(knowledgeSearchResult.matches)
  const graphResult = recordValue(stepResult(steps, 'knowledge_build_graph').result)
  const graphMetrics = recordValue(graphResult.metrics)

  const sequenceEnvelope = optionalStepResult(steps, 'sequence_pipeline') ?? payload
  const sequenceResult = unwrappedResult(sequenceEnvelope)
  const sequenceBenchmark = unwrappedResult(stepResult(steps, 'sequence_benchmark'))
  const sequenceReport = unwrappedResult(stepResult(steps, 'sequence_report'))
  const report = recordValue(payload.report)
  const sequenceReportPath = stringValue(sequenceReport.output_html)
  const auditReportPath = sequenceReportPath ?? stringValue(omicsReport.output_md) ?? stringValue(report.path)
  const manifestPath = stringValue(manifest.manifest_path)
  const isSequenceResult = Boolean(sequenceResult.mrna)
  const sequenceView = normalizeSequenceResult(sequenceResult, Object.keys(sequenceBenchmark).length ? sequenceBenchmark : undefined)
  const caddView = normalizeCaddResult(caddResult)

  const summary: ResultRecord = {
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
    fastq_qc_samples: fastqQcSummaries.length || undefined,
    fastq_qc_report: fastqQcReport,
    fastq_qc_manifest: fastqQcResult.manifest_path,
    alignment_samples: alignmentSamples.length || undefined,
    alignment_rate: alignmentRate,
    counted_genes: featureCountsResult.n_genes,
    counted_samples: featureCountsResult.n_samples,
    feature_counts_csv: featureCountsResult.output_csv,
    feature_counts_summary: featureCountsResult.summary_path,
    differential_expression_csv: differential.output_csv,
    pathway_enrichment_csv: pathway.output_csv,
    omics_report: omicsReport.output_md,
    image_format: imageMetrics.format,
    image_dimensions: imageMetrics.width !== undefined && imageMetrics.height !== undefined ? `${imageMetrics.width}x${imageMetrics.height}` : undefined,
    image_channels: imageMetrics.channels,
    image_manifest: imageResult.manifest_path,
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
    report_path: payload.output_md ?? omicsReport.output_md ?? sequenceReport.output_html ?? sequenceResult.output_html ?? report.path,
    cadd_report: caddResult.report,
  }

  const traceSteps = steps.map((step, index) => ({
    index: index + 1,
    id: typeof step.id === 'string' ? step.id : `step-${index + 1}`,
    tool: typeof step.tool === 'string' ? step.tool : 'unknown',
    status: typeof step.status === 'string' ? step.status : 'unknown',
  }))
  const geneIds = stringArray(payload.gene_ids ?? annotationResult.gene_ids)
  const hasEvidenceView = evidenceMatches.length > 0
    || evidenceCitations.length > 0
    || knowledgeMatches.length > 0
    || Object.keys(graphMetrics).length > 0
    || Boolean(evidenceProvider)

  return {
    payload,
    manifest,
    manifestStatus: stringValue(manifest.status),
    manifestPath,
    auditReportPath,
    steps,
    traceSteps,
    summaryEntries: Object.entries(summary).filter(([, value]) => value !== undefined && value !== null),
    isSequenceResult,
    hasEvidenceView,
    hasAuditView: steps.length > 0,
    hasFastqQc: Boolean(fastqQcResult.status),
    hasRnaseqSummary: alignmentSamples.length > 0 || featureCountsResult.n_genes !== undefined || Boolean(differential.output_csv),
    sequenceResult,
    sequenceView,
    sequenceBenchmark,
    sequenceReportPath,
    caddResult,
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
  }
}
