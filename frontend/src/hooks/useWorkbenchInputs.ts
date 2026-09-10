import { useCallback, useMemo, useReducer } from 'react'
import { rnaseqFixture } from '../app/constants'
import type {
  PlannerMode,
  ResearchFileSlot,
  ResearchPreset,
  RnaFileSlot,
  RnaInputMode,
  RnaPreflightItem,
  SequenceMethod,
  SequenceMolecule,
  UploadedFile,
} from '../app/types'

export type WorkbenchInputs = {
  researchPreset: ResearchPreset
  plannerMode: PlannerMode
  task: string
  rnaseqTask: string
  variantTask: string
  protein: string
  geneIds: string
  sequenceMolecule: SequenceMolecule
  sequenceMethod: SequenceMethod
  sequenceUseVaxpress: boolean
  sequenceStructureId: string
  evidenceProvider: string
  variantBackend: string
  rnaInputMode: RnaInputMode
  caddExhaustiveness: string
  caddMaxLigands: string
  uploadedFiles: Record<ResearchFileSlot, UploadedFile | null>
  rnaFiles: Record<RnaFileSlot, UploadedFile[]>
}

type WorkbenchInputAction =
  | { type: 'patch'; patch: Partial<WorkbenchInputs> }
  | { type: 'apply_research_preset'; preset: ResearchPreset }
  | { type: 'set_research_file'; slot: ResearchFileSlot; file: UploadedFile | null }
  | { type: 'set_rna_files'; slot: RnaFileSlot; files: UploadedFile[] }
  | { type: 'reset_rna_files' }

function emptyResearchFiles(): Record<ResearchFileSlot, UploadedFile | null> {
  return { expression: null, metadata: null, gene_sets: null, vcf: null, annotation: null, receptor: null, ligand_library: null }
}

function emptyRnaFiles(): Record<RnaFileSlot, UploadedFile[]> {
  return { fastq_r1: [], fastq_r2: [], reference_fasta: [], annotation_gtf: [], metadata: [], gene_sets: [] }
}

export function createInitialWorkbenchInputs(): WorkbenchInputs {
  return {
    researchPreset: 'custom',
    plannerMode: 'auto',
    task: '分析 RNA-seq 差异表达并设计 mRNA 序列',
    rnaseqTask: '运行 FastQC 并比对双端 RNA-seq 读段',
    variantTask: '注释 VCF 变异并检索基因证据',
    protein: 'MKT',
    geneIds: '',
    sequenceMolecule: 'linear',
    sequenceMethod: 'greedy',
    sequenceUseVaxpress: false,
    sequenceStructureId: '',
    evidenceProvider: 'local',
    variantBackend: 'auto',
    rnaInputMode: 'fixture',
    caddExhaustiveness: '4',
    caddMaxLigands: '3',
    uploadedFiles: emptyResearchFiles(),
    rnaFiles: emptyRnaFiles(),
  }
}

export function workbenchInputReducer(state: WorkbenchInputs, action: WorkbenchInputAction): WorkbenchInputs {
  if (action.type === 'patch') return { ...state, ...action.patch }
  if (action.type === 'set_research_file') {
    return { ...state, uploadedFiles: { ...state.uploadedFiles, [action.slot]: action.file } }
  }
  if (action.type === 'set_rna_files') {
    return { ...state, rnaFiles: { ...state.rnaFiles, [action.slot]: action.files } }
  }
  if (action.type === 'reset_rna_files') return { ...state, rnaFiles: emptyRnaFiles() }
  if (action.preset === 'bgi_multiomics') {
    return {
      ...state,
      researchPreset: action.preset,
      task: '运行 BGI 多组学研究流程：基因组质控、10x 单细胞、显微成像、微生物组、证据检索和 mRNA 设计',
      geneIds: 'GeneA, GeneB',
      protein: 'MKT',
      evidenceProvider: 'local',
      plannerMode: 'deterministic',
    }
  }
  if (action.preset === 'online_evidence') {
    return {
      ...state,
      researchPreset: action.preset,
      task: '检索目标基因的在线证据并生成可追溯摘要',
      geneIds: 'TP53, BRCA1',
      evidenceProvider: 'uniprot',
      plannerMode: 'deterministic',
    }
  }
  return {
    ...state,
    researchPreset: action.preset,
    task: '分析 RNA-seq 差异表达并设计 mRNA 序列',
    geneIds: '',
    evidenceProvider: 'local',
    plannerMode: 'auto',
  }
}

export function buildResearchInputs(inputs: WorkbenchInputs) {
  const values: Record<string, unknown> = {
    expression_csv: inputs.uploadedFiles.expression?.path || 'examples/rnaseq/expression.csv',
    metadata_csv: inputs.uploadedFiles.metadata?.path || 'examples/rnaseq/metadata.csv',
    gene_sets_csv: inputs.uploadedFiles.gene_sets?.path || 'examples/rnaseq/gene_sets.csv',
    evidence_csv: inputs.evidenceProvider === 'local' ? 'examples/rnaseq/evidence.csv' : undefined,
    evidence_provider: inputs.evidenceProvider,
    gene_ids: inputs.geneIds.split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean).slice(0, 20),
    protein: inputs.protein,
    output_dir: 'output/frontend_auto_research',
  }
  if (inputs.researchPreset === 'bgi_multiomics') {
    Object.assign(values, {
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
  return values
}

export function researchDomains(preset: ResearchPreset) {
  if (preset === 'bgi_multiomics') return ['omics', 'imaging', 'literature', 'knowledge', 'sequence']
  if (preset === 'online_evidence') return ['literature']
  return undefined
}

export function buildVariantInputs(inputs: WorkbenchInputs) {
  return {
    vcf_path: inputs.uploadedFiles.vcf?.path || 'examples/variants/variants.vcf',
    annotation_csv: inputs.uploadedFiles.annotation?.path || 'examples/variants/gene_annotations.csv',
    annotation_backend: inputs.variantBackend,
    evidence_csv: inputs.evidenceProvider === 'local' ? 'examples/rnaseq/evidence.csv' : undefined,
    evidence_provider: inputs.evidenceProvider,
    output_dir: 'output/frontend_variant_research',
  }
}

export function buildRnaseqInputs(inputs: WorkbenchInputs) {
  const fixtureMode = inputs.rnaInputMode === 'fixture'
  return {
    fastq_paths: fixtureMode ? rnaseqFixture.fastqPaths : inputs.rnaFiles.fastq_r1.length ? inputs.rnaFiles.fastq_r1.map((file) => file.path) : undefined,
    fastq_r2_paths: fixtureMode ? rnaseqFixture.fastqR2Paths : inputs.rnaFiles.fastq_r2.length ? inputs.rnaFiles.fastq_r2.map((file) => file.path) : undefined,
    reference_fasta: fixtureMode ? rnaseqFixture.referenceFasta : inputs.rnaFiles.reference_fasta[0]?.path,
    annotation_gtf: fixtureMode ? rnaseqFixture.annotationGtf : inputs.rnaFiles.annotation_gtf[0]?.path,
    metadata_csv: fixtureMode ? rnaseqFixture.metadataCsv : inputs.rnaFiles.metadata[0]?.path,
    gene_sets_csv: fixtureMode ? rnaseqFixture.geneSetsCsv : inputs.rnaFiles.gene_sets[0]?.path,
    output_dir: 'output/frontend_rnaseq_custom',
    statistics_backend: 'scipy',
  }
}

export function buildCaddInputs(inputs: WorkbenchInputs) {
  return {
    receptor: inputs.uploadedFiles.receptor?.path || 'data/4hjo.pdb',
    ligand_library: inputs.uploadedFiles.ligand_library?.path || 'output/bindingdb_egfr_10000.csv',
    exhaustiveness: Number(inputs.caddExhaustiveness) || 4,
    max_ligands: Number(inputs.caddMaxLigands) || 3,
    output_dir: 'output/frontend_cadd_research',
  }
}

function buildRnaseqPreflight(inputs: WorkbenchInputs) {
  const taskText = inputs.rnaseqTask.toLowerCase()
  const pairedEnd = /paired[- ]?end|双端|双末端/.test(taskText)
  const alignment = /align|hisat|比对|featurecounts|计数|定量/.test(taskText)
  const differential = /differential|deseq|差异表达|差异分析/.test(taskText)
  const enrichment = /enrichment|pathway|gene set|富集|通路|基因集/.test(taskText)
  const fixtureMode = inputs.rnaInputMode === 'fixture'
  const r1Count = fixtureMode ? rnaseqFixture.fastqPaths.length : inputs.rnaFiles.fastq_r1.length
  const r2Count = fixtureMode ? rnaseqFixture.fastqR2Paths.length : inputs.rnaFiles.fastq_r2.length
  const pairMismatch = r2Count > 0 && (r1Count === 0 || r1Count !== r2Count)
  const checks: RnaPreflightItem[] = [
    { label: 'R1 FASTQ', detail: fixtureMode ? `${r1Count} 个仓库样例文件` : r1Count ? `${r1Count} 个文件` : '待上传', ready: r1Count > 0, required: true },
    { label: 'R2 FASTQ', detail: fixtureMode ? `${r2Count} 个仓库样例文件` : r2Count ? `${r2Count} 个文件` : pairedEnd ? '双端任务需要上传' : '未上传，按单端处理', ready: !pairedEnd && r2Count === 0 ? true : r2Count > 0 && !pairMismatch, required: pairedEnd },
    { label: '参考基因组 FASTA', detail: fixtureMode ? '仓库样例已就绪' : inputs.rnaFiles.reference_fasta.length ? '已上传' : alignment ? '比对任务需要上传' : '当前管线可跳过比对', ready: !alignment || fixtureMode || inputs.rnaFiles.reference_fasta.length > 0, required: alignment },
    { label: '基因注释 GTF', detail: fixtureMode ? '仓库样例已就绪' : inputs.rnaFiles.annotation_gtf.length ? '已上传' : differential ? '差异分析前需要计数注释' : 'featureCounts / 差异分析需要', ready: !differential || fixtureMode || inputs.rnaFiles.annotation_gtf.length > 0, required: differential },
    { label: '样本元数据 CSV', detail: fixtureMode ? '仓库样例已就绪' : inputs.rnaFiles.metadata.length ? '已上传' : differential ? '差异分析需要' : '可选', ready: !differential || fixtureMode || inputs.rnaFiles.metadata.length > 0, required: differential },
    { label: '基因集 CSV', detail: fixtureMode ? '仓库样例已就绪' : inputs.rnaFiles.gene_sets.length ? '已上传' : enrichment ? '富集分析需要' : '可选', ready: !enrichment || fixtureMode || inputs.rnaFiles.gene_sets.length > 0, required: enrichment },
  ]
  return { checks, pairMismatch }
}

export function useWorkbenchInputs() {
  const [inputs, dispatch] = useReducer(workbenchInputReducer, undefined, createInitialWorkbenchInputs)
  const updateInputs = useCallback((patch: Partial<WorkbenchInputs>) => dispatch({ type: 'patch', patch }), [])
  const applyResearchPreset = useCallback((preset: ResearchPreset) => dispatch({ type: 'apply_research_preset', preset }), [])
  const setResearchFile = useCallback((slot: ResearchFileSlot, file: UploadedFile | null) => dispatch({ type: 'set_research_file', slot, file }), [])
  const setRnaFiles = useCallback((slot: RnaFileSlot, files: UploadedFile[]) => dispatch({ type: 'set_rna_files', slot, files }), [])
  const resetRnaFiles = useCallback(() => dispatch({ type: 'reset_rna_files' }), [])
  const rnaseqPreflight = useMemo(() => buildRnaseqPreflight(inputs), [inputs])
  return { inputs, updateInputs, applyResearchPreset, setResearchFile, setRnaFiles, resetRnaFiles, rnaseqPreflight }
}
