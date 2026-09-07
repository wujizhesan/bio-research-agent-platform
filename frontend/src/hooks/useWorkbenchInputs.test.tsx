import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { UploadedFile } from '../app/types'
import {
  buildCaddInputs,
  buildResearchInputs,
  buildRnaseqInputs,
  buildVariantInputs,
  createInitialWorkbenchInputs,
  researchDomains,
  useWorkbenchInputs,
  workbenchInputReducer,
} from './useWorkbenchInputs'

function uploaded(fileId: string, path: string): UploadedFile {
  return {
    file_id: fileId,
    filename: path.split('/').pop() || fileId,
    content_type: 'text/plain',
    size_bytes: 10,
    sha256: `${fileId}-sha`,
    path,
    download_url: `/api/v1/files/${fileId}`,
  }
}

describe('workbenchInputReducer', () => {
  it('原子应用研究预设且不覆盖无关模式输入', () => {
    const initial = { ...createInitialWorkbenchInputs(), variantTask: '保留变异任务' }
    const bgi = workbenchInputReducer(initial, { type: 'apply_research_preset', preset: 'bgi_multiomics' })
    const online = workbenchInputReducer(bgi, { type: 'apply_research_preset', preset: 'online_evidence' })

    expect(bgi).toMatchObject({
      researchPreset: 'bgi_multiomics',
      plannerMode: 'deterministic',
      geneIds: 'GeneA, GeneB',
      evidenceProvider: 'local',
      variantTask: '保留变异任务',
    })
    expect(online).toMatchObject({
      researchPreset: 'online_evidence',
      plannerMode: 'deterministic',
      geneIds: 'TP53, BRCA1',
      evidenceProvider: 'uniprot',
      variantTask: '保留变异任务',
    })
  })

  it('按槽位更新上传文件且保持其他槽位引用', () => {
    const initial = createInitialWorkbenchInputs()
    const expression = uploaded('expression', '/uploads/expression.csv')
    const r1 = uploaded('r1', '/uploads/read-1.fastq.gz')
    const withExpression = workbenchInputReducer(initial, { type: 'set_research_file', slot: 'expression', file: expression })
    const withRna = workbenchInputReducer(withExpression, { type: 'set_rna_files', slot: 'fastq_r1', files: [r1] })

    expect(withRna.uploadedFiles.expression).toBe(expression)
    expect(withRna.uploadedFiles.metadata).toBeNull()
    expect(withRna.rnaFiles.fastq_r1).toEqual([r1])
    expect(withRna.rnaFiles.fastq_r2).toEqual([])
  })
})

describe('模式输入构建器', () => {
  it('生成多组学研究参数并限制基因 ID 数量', () => {
    const geneIds = Array.from({ length: 25 }, (_, index) => `GENE${index + 1}`).join(', ')
    const inputs = workbenchInputReducer(createInitialWorkbenchInputs(), {
      type: 'patch',
      patch: { researchPreset: 'bgi_multiomics', geneIds },
    })
    const payload = buildResearchInputs(inputs)

    expect(payload).toMatchObject({
      evidence_provider: 'local',
      multiomics: true,
      output_dir: 'output/frontend_bgi_multiomics',
    })
    expect(payload.gene_ids).toHaveLength(20)
    expect(researchDomains(inputs.researchPreset)).toEqual(['omics', 'imaging', 'literature', 'knowledge', 'sequence'])
  })

  it('为上传模式生成 RNA-seq、变异和 CADD 参数', () => {
    const initial = createInitialWorkbenchInputs()
    const inputs = {
      ...initial,
      rnaInputMode: 'upload' as const,
      variantBackend: 'vcf_ann',
      caddExhaustiveness: '8',
      caddMaxLigands: '12',
      evidenceProvider: 'pubmed',
      uploadedFiles: {
        ...initial.uploadedFiles,
        vcf: uploaded('vcf', '/uploads/input.vcf'),
        receptor: uploaded('receptor', '/uploads/receptor.pdbqt'),
        ligand_library: uploaded('ligands', '/uploads/ligands.csv'),
      },
      rnaFiles: {
        ...initial.rnaFiles,
        fastq_r1: [uploaded('r1', '/uploads/r1.fastq.gz')],
        reference_fasta: [uploaded('reference', '/uploads/reference.fa')],
      },
    }

    expect(buildRnaseqInputs(inputs)).toMatchObject({
      fastq_paths: ['/uploads/r1.fastq.gz'],
      reference_fasta: '/uploads/reference.fa',
      fastq_r2_paths: undefined,
    })
    expect(buildVariantInputs(inputs)).toMatchObject({
      vcf_path: '/uploads/input.vcf',
      annotation_backend: 'vcf_ann',
      evidence_provider: 'pubmed',
      evidence_csv: undefined,
    })
    expect(buildCaddInputs(inputs)).toMatchObject({
      receptor: '/uploads/receptor.pdbqt',
      ligand_library: '/uploads/ligands.csv',
      exhaustiveness: 8,
      max_ligands: 12,
    })
  })
})

describe('useWorkbenchInputs', () => {
  it('根据上传的双端文件计算配对预检状态', () => {
    const { result } = renderHook(() => useWorkbenchInputs())
    act(() => {
      result.current.updateInputs({ rnaInputMode: 'upload', rnaseqTask: '运行双端 RNA-seq 比对' })
      result.current.setRnaFiles('fastq_r1', [uploaded('r1', '/uploads/r1.fastq.gz')])
      result.current.setRnaFiles('fastq_r2', [uploaded('r2a', '/uploads/r2a.fastq.gz'), uploaded('r2b', '/uploads/r2b.fastq.gz')])
    })

    expect(result.current.rnaseqPreflight.pairMismatch).toBe(true)
    expect(result.current.rnaseqPreflight.checks.find((item) => item.label === 'R2 FASTQ')).toMatchObject({
      ready: false,
      required: true,
    })
  })
})
