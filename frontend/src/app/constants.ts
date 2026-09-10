export const luciferaseDemoProtein = 'MEDAKNIKKGPAPFYPLEDGTAGEQLHKAMKRYALVPGTIAFTDAHIEVNITYAEYFEMSVRLAEAMKRYGLNTNHRIVVCSENSLQFFMPVLGALFIGVAVAPANDIYNERELLNSMNISQPTVVFVSKKGLQKILNVQKKLPIIQKIIIMDSKTDYQGFQSMYTFVTSHLPPGFNEYDFVPESFDRDKTIALIMNSSGSTGLPKGVALPHRTACVRFSHARDPIFGNQIIPDTAILSVVPFHHGFGMFTTLGYLICGFRVVLMYRFEEELFLRSLQDYKIQSALLVPTLFSFFAKSTLIDKYDLSNLHEIASGGAPLSKEVGEAVAKRFHLPGIRQGYGLTETTSAILITPEGDDKPGAVGKVVPFFEAKVVDLDTGKTLGVNQRGELCVRGPMIMSGYVNNPEATNALIDKDGWLHSGDIAYWDEDEHFFIVDRLKSLIKYKGYQVAPAELESILLQHPNIFDAGVAGLPDDDAGELPAAVVVLEHGKTMTEKEIVDYVASQVTTAKKLRGGVVFVDEVPKGLTGKLDARKIREILIKAKKGGKSKL'

export const statusLabels: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
}
export const providerLabels: Record<string, string> = {
  local: '本地证据',
  kegg: 'KEGG',
  ncbi_gene: 'NCBI Gene',
  pubmed: 'PubMed',
  uniprot: 'UniProt',
  ucsc: 'UCSC',
  gencode: 'GENCODE',
}

export const domainLabels: Record<string, string> = {
  cadd: 'CADD',
  omics: '组学',
  sequence: 'mRNA / 序列',
  literature: '文献',
  knowledge: '知识库',
  imaging: '成像 / 多模态',
  research: '研究编排',
}

export const pluginDescriptions: Record<string, string> = {
  cadd: '计算机辅助药物设计',
  omics: '组学分析与质量控制',
  research: '生物信息学研究代理',
  literature: '文献与证据检索',
  knowledge: '本地科研知识检索',
  imaging: '显微成像与图像质控',
  sequence: 'mRNA-Forge 序列设计',
}

export const rnaseqFixture = {
  fastqPaths: [
    'examples/omics/rnaseq_fastq_fixture/A1.fastq',
    'examples/omics/rnaseq_fastq_fixture/A2.fastq',
    'examples/omics/rnaseq_fastq_fixture/A3.fastq',
    'examples/omics/rnaseq_fastq_fixture/B1.fastq',
    'examples/omics/rnaseq_fastq_fixture/B2.fastq',
    'examples/omics/rnaseq_fastq_fixture/B3.fastq',
  ],
  fastqR2Paths: [
    'examples/omics/rnaseq_paired_fixture/A1_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/A2_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/A3_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/B1_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/B2_R2.fastq',
    'examples/omics/rnaseq_paired_fixture/B3_R2.fastq',
  ],
  referenceFasta: 'examples/omics/rnaseq_fastq_fixture/reference.fa',
  annotationGtf: 'examples/omics/rnaseq_fastq_fixture/genes.gtf',
  metadataCsv: 'examples/omics/rnaseq_fastq_fixture/metadata.csv',
  geneSetsCsv: 'examples/omics/rnaseq_fastq_fixture/gene_sets.csv',
}
