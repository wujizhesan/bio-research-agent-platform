"""Application layer for a traceable bioinformatics research Agent."""
from pathlib import Path


PLUGIN_NAME = 'Bioinformatics Research Agent'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1


def _domain_registry_module():
    try:
        from . import domain_registry
    except ImportError:
        import domain_registry
    return domain_registry


def _workflow_runner_module():
    try:
        from . import workflow_runner
    except ImportError:
        import workflow_runner
    return workflow_runner


def _planner_module():
    try:
        from . import research_planner
    except ImportError:
        import research_planner
    return research_planner


def _project_root():
    try:
        from .config_loader import PROJECT_ROOT
    except ImportError:
        from config_loader import PROJECT_ROOT
    return PROJECT_ROOT


_DOMAIN_KEYWORDS = {
    'cadd': (
        'cadd', 'docking', 'virtual screening', 'ligand', 'molecule',
        'small molecule', 'binding', 'compound',
    ),
    'omics': (
        'omics', 'rna-seq', 'rnaseq', 'transcriptome', 'gene expression',
        'differential expression', 'pathway', 'gene', 'single-cell',
        'metagenome', 'microbiome', '16s', 'taxonomy', '宏基因组', '微生物组', '物种丰度',
        'variant', 'vcf', 'mutation', 'gatk', 'samtools',
        'fastq', 'bam', 'cram', 'quality control', 'quality-control', 'qc',
        'featurecounts', 'feature counts', 'read counting', 'gene counting',
        'hisat2', 'rna-seq alignment', 'rnaseq alignment', 'fastq to bam',
        'rna-seq quantification', 'rna-seq counting', '转录组计数', '基因计数',
        'gene annotation', 'variant annotation', '变异', '突变',
    ),
    'sequence': (
        'mrna', 'mRNA', 'sequence', 'codon', 'protein sequence',
        'nucleotide', 'translation',
    ),
    'literature': (
        'ucsc', 'gencode', 'genome browser', 'gtf',
        'literature', 'pubmed', 'uniprot', 'ncbi', 'kegg', 'paper',
        'evidence', 'citation', 'database', '文献', '数据库',
    ),
    'knowledge': (
        'rag', 'knowledge', 'retrieval', 'full text', '全文',
        'document', '知识库',
    ),
}


_EVIDENCE_PROVIDER_KEYWORDS = (
    ('kegg', ('kegg', 'pathway database', '通路数据库')),
    ('ncbi_gene', ('ncbi', 'gene annotation', '基因注释')),
    ('pubmed', ('pubmed', 'literature', 'paper', 'citation', '文献', '论文')),
    ('uniprot', ('uniprot', 'protein annotation', '蛋白注释')),
)

_EVIDENCE_PROVIDER_KEYWORDS += (
    ('ucsc', ('ucsc', 'genome browser', 'genome coordinate')),
    ('gencode', ('gencode', 'gtf', 'transcript annotation')),
)

_VARIANT_KEYWORDS = (
    'variant', 'vcf', 'mutation', 'gatk', 'samtools', 'gene annotation',
    'variant annotation', 'variant calling', 'variant caller', 'mpileup',
    'normalize variant', 'variant normalization', 'left normalize', 'bcftools norm',
    '变异', '突变', '基因注释', '变异解读', '变异检测', '变异规范化',
)

_GENOMICS_QC_KEYWORDS = (
    'fastq', 'bam', 'cram', 'bcf', 'quality control', 'quality-control',
    'fastqc', 'multiqc', 'fastq quality', 'sequencing quality',
    'sequencing qc', '测序质控', '质量控制',
)

_SINGLE_CELL_KEYWORDS = (
    'single-cell', 'single cell', 'scrna', '10x', '单细胞',
)

_METAGENOMICS_KEYWORDS = (
    'metagenome', 'metagenomics', 'microbiome', '16s', 'amplicon',
    'taxonomy', '宏基因组', '微生物组', '物种丰度',
)


_RNASEQ_COUNTING_KEYWORDS = (
    'featurecounts', 'feature counts', 'read counting', 'gene counting',
    'rna-seq quantification', 'rna-seq counting', '转录组计数', '基因计数',
)

_RNASEQ_ALIGNMENT_KEYWORDS = (
    'rna-seq alignment', 'rnaseq alignment', 'align rna-seq', 'align rnaseq',
    'fastq to bam', 'fastq alignment', 'read alignment', 'hisat2', 'star aligner',
)

_RNASEQ_ANALYSIS_KEYWORDS = (
    'differential expression', 'pathway', 'gene set', 'gene-set',
    'enrichment', 'omics report', '差异表达', '通路', '富集分析',
)


RESEARCH_PRESETS = {
    'bgi_research_demo': {
        'path': 'examples/workflows/bgi_research_demo.yaml',
        'domains': ['omics', 'literature', 'knowledge', 'sequence'],
        'description': 'RNA-seq analysis, local evidence retrieval, knowledge retrieval, report generation and mRNA sequence design.',
    },
    'rnaseq_research_agent': {
        'path': 'examples/workflows/rnaseq_research_agent.yaml',
        'domains': ['omics'],
        'description': 'End-to-end RNA-seq analysis with differential expression, pathway enrichment, evidence retrieval and a traceable report.',
    },
    'bgi_multiomics_demo': {
        'path': 'examples/workflows/bgi_multiomics_demo.yaml',
        'domains': ['omics', 'imaging', 'literature', 'knowledge', 'sequence'],
        'description': 'Reproducible BGI interview demo combining sequencing QC, microscopy image QC, evidence grounding, knowledge graph retrieval and mRNA design.',
    },
    'bgi_variant_demo': {
        'path': 'examples/workflows/bgi_variant_demo.yaml',
        'domains': ['omics', 'literature'],
        'description': 'VCF variant annotation, gene evidence retrieval and a traceable interpretation workflow.',
    },
}


def _parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


def _available_domain_names():
    return set(_domain_registry_module().active_domains())


def _select_domains(task, requested):
    available = _available_domain_names() - {'research'}
    if requested:
        selected = []
        for domain in requested:
            if domain not in available:
                raise ValueError(f'unknown or unavailable research domain: {domain}')
            if domain not in selected:
                selected.append(domain)
        return selected
    text = task.lower()
    scores = {
        domain: sum(keyword.lower() in text for keyword in keywords)
        for domain, keywords in _DOMAIN_KEYWORDS.items()
        if domain in available
    }
    selected = [domain for domain, score in scores.items() if score > 0]
    return selected or sorted(available)


def _resolve_domains(task, requested, inputs=None, planner_mode='deterministic'):
    if requested:
        return _select_domains(task, requested), {
            'backend': 'explicit',
            'mode': planner_mode,
            'domains': list(requested),
        }
    available = _available_domain_names() - {'research'}
    planner = _planner_module().select_domains(task, available, inputs, planner_mode)
    selected = _select_domains(task, planner.get('domains')) if planner.get('domains') else _select_domains(task, None)
    planner['domains'] = selected
    return selected, planner


def _is_variant_task(task, inputs=None):
    inputs = inputs or {}
    if any(key in inputs for key in ('vcf_path', 'vcf', 'variants_vcf')):
        return True
    text = str(task or '').lower()
    return any(keyword.lower() in text for keyword in _VARIANT_KEYWORDS)


def _is_variant_calling_task(task, inputs=None):
    inputs = inputs or {}
    if any(key in inputs for key in ('reference_fasta', 'reference_path', 'reference_genome')):
        return bool(inputs.get('bam_path') or inputs.get('input_bam') or inputs.get('cram_path'))
    text = str(task or '').lower()
    return any(keyword in text for keyword in (
        'variant calling', 'variant caller', 'call variants', 'mpileup', '变异检测',
    ))


def _is_variant_normalization_task(task, inputs=None):
    inputs = inputs or {}
    if inputs.get('normalize_variants'):
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in (
        'normalize variant', 'variant normalization', 'left normalize',
        'bcftools norm', '变异规范化',
    ))


def _is_variant_annotation_task(task, inputs=None):
    inputs = inputs or {}
    if inputs.get('annotation_csv') or inputs.get('annotation_gtf') or inputs.get('gencode_gtf') or inputs.get('annotation_backend'):
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in (
        'variant annotation', 'annotate variant', 'variant interpretation',
        '基因注释', '变异解读',
    ))


def _is_genomics_qc_task(task, inputs=None):
    inputs = inputs or {}
    if any(key in inputs for key in ('input_path', 'fastq_path', 'fastq_paths', 'bam_path', 'cram_path')):
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in _GENOMICS_QC_KEYWORDS)


def _is_fastq_qc_task(task, inputs=None):
    inputs = inputs or {}
    input_type = str(inputs.get('input_type', '')).lower()
    text = str(task or '').lower()
    if any(keyword in text for keyword in (
        'fastqc', 'multiqc', 'fastq quality', 'sequencing quality',
        'quality control', 'quality-control',
    )):
        return True
    return input_type == 'fastq' and not _is_rnaseq_alignment_task(task, inputs)


def _is_single_cell_task(task, inputs=None):
    inputs = inputs or {}
    if any(key in inputs for key in ('matrix_csv', 'single_cell_matrix')):
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in _SINGLE_CELL_KEYWORDS)


def _is_single_cell_10x_task(task, inputs=None):
    inputs = inputs or {}
    if any(key in inputs for key in ('matrix_mtx', 'barcodes_tsv', 'features_tsv')):
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in ('10x', 'matrix.mtx', 'matrix market'))


def _is_metagenomics_task(task, inputs=None):
    inputs = inputs or {}
    if any(key in inputs for key in ('abundance_csv', 'metagenomics_abundance')):
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in _METAGENOMICS_KEYWORDS)


def _is_rnaseq_counting_task(task, inputs=None):
    inputs = inputs or {}
    has_alignment = any(key in inputs for key in (
        'alignment_paths', 'bam_paths', 'bam_path', 'cram_path',
    ))
    has_annotation = bool(inputs.get('annotation_gtf') or inputs.get('gencode_gtf'))
    if has_alignment and has_annotation:
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in _RNASEQ_COUNTING_KEYWORDS)


def _is_rnaseq_alignment_task(task, inputs=None):
    inputs = inputs or {}
    has_fastq = any(key in inputs for key in ('fastq_paths', 'fastq_path', 'fastq'))
    text = str(task or '').lower()
    if has_fastq and any(keyword in text for keyword in _RNASEQ_ALIGNMENT_KEYWORDS):
        return True
    return has_fastq and bool(inputs.get('reference_fasta') or inputs.get('reference_path'))


def _is_rnaseq_analysis_task(task, inputs=None):
    inputs = inputs or {}
    if inputs.get('metadata_csv') and inputs.get('gene_sets_csv'):
        return True
    text = str(task or '').lower()
    return any(keyword in text for keyword in _RNASEQ_ANALYSIS_KEYWORDS)


def _required_inputs(domains, task=None, inputs=None):
    required = []
    if 'omics' in domains:
        if _is_metagenomics_task(task, inputs):
            required.append({'name': 'abundance_csv', 'description': 'taxon or feature abundance table CSV'})
        elif _is_single_cell_task(task, inputs):
            if _is_single_cell_10x_task(task, inputs):
                required.extend([
                    {'name': 'matrix_mtx', 'description': '10x Matrix Market expression matrix'},
                    {'name': 'barcodes_tsv', 'description': '10x cell barcode table'},
                    {'name': 'features_tsv', 'description': '10x feature annotation table'},
                ])
            else:
                required.append({'name': 'matrix_csv', 'description': 'cell-by-gene count matrix CSV'})
        elif _is_rnaseq_alignment_task(task, inputs):
            required.extend([
                {'name': 'fastq_paths', 'description': 'single-end or paired-end RNA-seq R1 FASTQ files'},
                {'name': 'reference_fasta', 'description': 'reference FASTA for HISAT2 alignment'},
            ])
            if _is_rnaseq_counting_task(task, inputs) or _is_rnaseq_analysis_task(task, inputs) or inputs.get('annotation_gtf'):
                required.append({'name': 'annotation_gtf', 'description': 'GTF gene annotation for featureCounts'})
                if _is_rnaseq_analysis_task(task, inputs):
                    required.extend([
                        {'name': 'metadata_csv', 'description': 'sample condition metadata for differential expression'},
                        {'name': 'gene_sets_csv', 'description': 'pathway or gene-set table for enrichment analysis'},
                    ])
        elif _is_rnaseq_counting_task(task, inputs):
            required.extend([
                {'name': 'alignment_paths', 'description': 'aligned BAM/CRAM files for RNA-seq read counting'},
                {'name': 'annotation_gtf', 'description': 'GTF gene annotation for featureCounts'},
            ])
            if _is_rnaseq_analysis_task(task, inputs):
                required.extend([
                    {'name': 'metadata_csv', 'description': 'sample condition metadata for differential expression'},
                    {'name': 'gene_sets_csv', 'description': 'pathway or gene-set table for enrichment analysis'},
                ])
        elif _is_variant_normalization_task(task, inputs):
            required.extend([
                {'name': 'vcf_path', 'description': 'input VCF/BCF variant file'},
                {'name': 'reference_fasta', 'description': 'reference genome FASTA matching VCF alleles'},
            ])
            annotation_backend = (inputs or {}).get('annotation_backend', 'auto')
            if _is_variant_annotation_task(task, inputs) and annotation_backend != 'vcf_ann':
                if annotation_backend == 'gencode_gtf' or (inputs or {}).get('annotation_gtf'):
                    required.append({'name': 'annotation_gtf', 'description': 'GENCODE GTF gene annotation file'})
                else:
                    required.append({'name': 'annotation_csv', 'description': 'genomic interval annotation table'})
        elif _is_variant_calling_task(task, inputs):
            required.extend([
                {'name': 'bam_path', 'description': 'aligned BAM/CRAM file for variant calling'},
                {'name': 'reference_fasta', 'description': 'reference genome FASTA with matching contigs'},
            ])
        elif _is_genomics_qc_task(task, inputs):
            if inputs and inputs.get('fastq_paths'):
                input_name = 'fastq_paths'
            elif inputs and inputs.get('fastq_path'):
                input_name = 'fastq_path'
            else:
                input_name = 'input_path'
            required.append({
                'name': input_name,
                'description': 'single-end or paired-end FASTQ files' if input_name in {'fastq_path', 'fastq_paths'} else 'FASTQ, BAM/CRAM or VCF/BCF input file',
            })
        elif _is_variant_task(task, inputs):
            required.append({'name': 'vcf_path', 'description': 'VCF variant file'})
            annotation_backend = (inputs or {}).get('annotation_backend', 'auto')
            if annotation_backend != 'vcf_ann':
                if annotation_backend == 'gencode_gtf' or (inputs or {}).get('annotation_gtf'):
                    required.append({'name': 'annotation_gtf', 'description': 'GENCODE GTF gene annotation file'})
                else:
                    required.append({'name': 'annotation_csv', 'description': 'genomic interval annotation table'})
        else:
            required.extend([
                {'name': 'expression_csv', 'description': 'gene-by-sample expression matrix'},
                {'name': 'metadata_csv', 'description': 'sample condition metadata'},
                {'name': 'gene_sets_csv', 'description': 'pathway or gene-set table'},
            ])
    if 'sequence' in domains:
        required.append({'name': 'protein', 'description': 'protein sequence or FASTA'})
    if 'cadd' in domains:
        required.extend([
            {'name': 'receptor', 'description': 'target receptor structure'},
            {'name': 'ligand_library', 'description': 'screening ligand library'},
        ])
    if 'literature' in domains:
        required.append({'name': 'gene_ids', 'description': 'gene or protein identifiers'})
        if _select_evidence_provider(task or '', inputs) == 'gencode':
            required.append({'name': 'gencode_gtf', 'description': 'local GENCODE GTF annotation file'})
    if 'knowledge' in domains:
        required.append({'name': 'documents_dir', 'description': 'local scientific documents for retrieval'})
    return required


def _select_evidence_provider(task, inputs=None):
    inputs = inputs or {}
    explicit = inputs.get('evidence_provider')
    providers = {'local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode'}
    if explicit:
        if explicit not in providers:
            raise ValueError(f'unknown evidence provider: {explicit}')
        return explicit
    text = task.lower()
    for provider, keywords in _EVIDENCE_PROVIDER_KEYWORDS:
        if any(keyword.lower() in text for keyword in keywords):
            return provider
    return 'local'


def _build_workflow(task, domains, inputs=None, output_dir='output/research_auto'):
    inputs = dict(inputs or {})
    output_dir = str(inputs.get('output_dir') or output_dir)
    evidence_provider = _select_evidence_provider(task, inputs)
    steps = []
    missing = []
    rationale = []
    omics_ready = False
    variant_ready = False
    if evidence_provider == 'gencode' and not (inputs.get('gencode_gtf') or inputs.get('annotation_gtf')):
        missing.append('gencode_gtf')

    variant_task = _is_variant_task(task, inputs)
    metagenomics_task = _is_metagenomics_task(task, inputs)
    single_cell_task = _is_single_cell_task(task, inputs)
    single_cell_10x_task = _is_single_cell_10x_task(task, inputs)
    rnaseq_alignment_task = _is_rnaseq_alignment_task(task, inputs)
    rnaseq_counting_task = _is_rnaseq_counting_task(task, inputs)
    rnaseq_analysis_task = _is_rnaseq_analysis_task(task, inputs)
    variant_normalization_task = _is_variant_normalization_task(task, inputs)
    variant_calling_task = _is_variant_calling_task(task, inputs)
    qc_task = _is_genomics_qc_task(task, inputs)
    fastq_qc_task = _is_fastq_qc_task(task, inputs)
    if 'omics' in domains and metagenomics_task:
        abundance_csv = inputs.get('abundance_csv') or inputs.get('metagenomics_abundance')
        if not abundance_csv:
            missing.append('abundance_csv')
        else:
            metagenomics_args = {
                'abundance_csv': str(abundance_csv),
                'output_dir': output_dir,
            }
            for key in ('taxon_id_column', 'min_total_counts', 'min_prevalence'):
                if inputs.get(key) is not None:
                    metagenomics_args[key] = inputs[key]
            steps.append({
                'id': 'metagenomics_qc',
                'tool': 'omics_run_metagenomics_qc',
                'args': metagenomics_args,
            })
            rationale.append('metagenomics QC normalizes abundance and calculates observed taxa and Shannon diversity')
    elif 'omics' in domains and single_cell_task:
        if single_cell_10x_task:
            ten_x_inputs = ('matrix_mtx', 'barcodes_tsv', 'features_tsv')
            missing.extend(key for key in ten_x_inputs if not inputs.get(key))
            if not any(key in missing for key in ten_x_inputs):
                ten_x_args = {
                    key: str(inputs[key]) for key in ten_x_inputs
                }
                ten_x_args['output_dir'] = output_dir
                for key in (
                    'min_genes', 'max_genes', 'min_counts',
                    'max_mito_percent', 'mitochondrial_prefix',
                ):
                    if inputs.get(key) is not None:
                        ten_x_args[key] = inputs[key]
                steps.append({
                    'id': 'single_cell_10x_qc',
                    'tool': 'omics_run_single_cell_10x_qc',
                    'args': ten_x_args,
                })
                rationale.append('10x single-cell QC preserves sparse Matrix Market artifacts and filters cells by core QC metrics')
        else:
            matrix_csv = inputs.get('matrix_csv') or inputs.get('single_cell_matrix')
            if not matrix_csv:
                missing.append('matrix_csv')
            else:
                single_cell_args = {
                    'matrix_csv': str(matrix_csv),
                    'output_dir': output_dir,
                }
                for key in (
                    'cell_id_column', 'min_genes', 'max_genes', 'min_counts',
                    'max_mito_percent', 'mitochondrial_prefix',
                ):
                    if inputs.get(key) is not None:
                        single_cell_args[key] = inputs[key]
                steps.append({
                    'id': 'single_cell_qc',
                    'tool': 'omics_run_single_cell_qc',
                    'args': single_cell_args,
                })
                rationale.append('single-cell QC calculates genes-per-cell, total counts and mitochondrial fraction')
    elif 'omics' in domains and rnaseq_alignment_task:
        fastq_paths = inputs.get('fastq_paths') or inputs.get('fastq_path') or inputs.get('fastq')
        reference_fasta = inputs.get('reference_fasta') or inputs.get('reference_path')
        annotation_gtf = inputs.get('annotation_gtf') or inputs.get('gencode_gtf')
        if not fastq_paths:
            missing.append('fastq_paths')
        if not reference_fasta:
            missing.append('reference_fasta')
        if rnaseq_counting_task or rnaseq_analysis_task or annotation_gtf:
            if not annotation_gtf:
                missing.append('annotation_gtf')
        if fastq_paths and reference_fasta:
            alignment_dependencies = []
            alignment_args = {
                'fastq_paths': [str(path) for path in fastq_paths] if isinstance(fastq_paths, (list, tuple)) else str(fastq_paths),
                'reference_fasta': str(reference_fasta),
                'output_dir': output_dir,
            }
            for key in ('output_alignment_paths', 'threads', 'timeout'):
                if inputs.get(key) is not None:
                    alignment_args[key] = inputs[key]
            if inputs.get('fastq_r2_paths') is not None:
                alignment_args['fastq_r2_paths'] = inputs['fastq_r2_paths']
            if fastq_qc_task:
                qc_args = {
                    'fastq_paths': alignment_args['fastq_paths'],
                    'output_dir': str(Path(output_dir) / 'fastq_qc'),
                }
                if inputs.get('fastq_r2_paths') is not None:
                    qc_args['fastq_r2_paths'] = inputs['fastq_r2_paths']
                for key in ('qc_threads', 'qc_timeout'):
                    if inputs.get(key) is not None:
                        qc_args[key.removeprefix('qc_')] = inputs[key]
                steps.append({
                    'id': 'fastq_qc',
                    'tool': 'omics_run_fastq_qc',
                    'args': qc_args,
                })
                alignment_dependencies.append('fastq_qc')
                rationale.append('FastQC evaluates per-file sequencing quality and MultiQC aggregates a reviewable multi-sample report before alignment')
            steps.append({
                'id': 'rnaseq_alignment',
                'tool': 'omics_run_rnaseq_alignment',
                **({'depends_on': alignment_dependencies} if alignment_dependencies else {}),
                'args': alignment_args,
            })
            rationale.append('HISAT2 aligns RNA-seq FASTQ reads to a reference and emits sorted indexed BAM files with alignment provenance')
            if annotation_gtf:
                counting_args = {
                    'alignment_paths': '${rnaseq_alignment.alignment_paths}',
                    'annotation_gtf': str(annotation_gtf),
                    'output_dir': output_dir,
                }
                paired_end = inputs.get('paired_end')
                if paired_end is None and inputs.get('fastq_r2_paths') is not None:
                    paired_end = True
                if paired_end is not None:
                    counting_args['paired_end'] = bool(paired_end)
                if inputs.get('output_csv') or inputs.get('counts_csv'):
                    counting_args['output_csv'] = str(inputs.get('output_csv') or inputs['counts_csv'])
                for key in (
                    'feature_type', 'gene_id_attribute', 'strand',
                    'threads', 'timeout',
                ):
                    if inputs.get(key) is not None:
                        counting_args[key] = inputs[key]
                steps.append({
                    'id': 'rnaseq_feature_counts',
                    'tool': 'omics_run_feature_counts',
                    'depends_on': ['rnaseq_alignment'],
                    'args': counting_args,
                })
                rationale.append('featureCounts converts HISAT2 BAM outputs plus GTF exon annotations into a gene-by-sample count matrix with provenance')
                if rnaseq_analysis_task:
                    metadata_csv = inputs.get('metadata_csv')
                    gene_sets_csv = inputs.get('gene_sets_csv')
                    if not metadata_csv:
                        missing.append('metadata_csv')
                    if not gene_sets_csv:
                        missing.append('gene_sets_csv')
                    if metadata_csv and gene_sets_csv:
                        analysis_args = {
                            'expression_csv': '${rnaseq_feature_counts.output_csv}',
                            'metadata_csv': str(metadata_csv),
                            'gene_sets_csv': str(gene_sets_csv),
                            'output_dir': output_dir,
                            'evidence_provider': evidence_provider,
                        }
                        for key in (
                            'evidence_csv', 'condition_a', 'condition_b', 'evidence_timeout',
                            'statistics_backend', 'genome',
                        ):
                            if inputs.get(key) is not None:
                                analysis_args[key] = inputs[key]
                        if inputs.get('evidence_cache_dir'):
                            analysis_args['evidence_cache_dir'] = str(inputs['evidence_cache_dir'])
                        elif evidence_provider != 'local':
                            analysis_args['evidence_cache_dir'] = str(Path(output_dir) / 'evidence_cache')
                        if evidence_provider == 'gencode':
                            analysis_args['gencode_gtf'] = str(inputs.get('gencode_gtf') or annotation_gtf)
                        steps.append({
                            'id': 'omics_analysis',
                            'tool': 'omics_run_analysis',
                            'depends_on': ['rnaseq_feature_counts'],
                            'args': analysis_args,
                        })
                        omics_ready = True
                        rationale.append(f'count matrix is forwarded to {evidence_provider} differential expression, pathway enrichment and report generation')
    elif 'omics' in domains and rnaseq_counting_task:
        alignment_paths = (
            inputs.get('alignment_paths') or inputs.get('bam_paths')
            or inputs.get('bam_path') or inputs.get('cram_path')
        )
        annotation_gtf = inputs.get('annotation_gtf') or inputs.get('gencode_gtf')
        if not alignment_paths:
            missing.append('alignment_paths')
        if not annotation_gtf:
            missing.append('annotation_gtf')
        if alignment_paths and annotation_gtf:
            if isinstance(alignment_paths, (list, tuple)):
                serialized_alignments = [str(path) for path in alignment_paths]
            else:
                serialized_alignments = str(alignment_paths)
            counting_args = {
                'alignment_paths': serialized_alignments,
                'annotation_gtf': str(annotation_gtf),
                'output_dir': output_dir,
            }
            if inputs.get('output_csv') or inputs.get('counts_csv'):
                counting_args['output_csv'] = str(inputs.get('output_csv') or inputs['counts_csv'])
            for key in (
                'feature_type', 'gene_id_attribute', 'strand', 'paired_end',
                'threads', 'timeout',
            ):
                if inputs.get(key) is not None:
                    counting_args[key] = inputs[key]
            steps.append({
                'id': 'rnaseq_feature_counts',
                'tool': 'omics_run_feature_counts',
                'args': counting_args,
            })
            rationale.append('featureCounts converts aligned RNA-seq BAM/CRAM files plus GTF exon annotations into a gene-by-sample count matrix with provenance')
            if rnaseq_analysis_task:
                metadata_csv = inputs.get('metadata_csv')
                gene_sets_csv = inputs.get('gene_sets_csv')
                if not metadata_csv:
                    missing.append('metadata_csv')
                if not gene_sets_csv:
                    missing.append('gene_sets_csv')
                if metadata_csv and gene_sets_csv:
                    analysis_args = {
                        'expression_csv': '${rnaseq_feature_counts.output_csv}',
                        'metadata_csv': str(metadata_csv),
                        'gene_sets_csv': str(gene_sets_csv),
                        'output_dir': output_dir,
                        'evidence_provider': evidence_provider,
                    }
                    for key in (
                        'evidence_csv', 'condition_a', 'condition_b', 'evidence_timeout',
                        'statistics_backend', 'genome',
                    ):
                        if inputs.get(key) is not None:
                            analysis_args[key] = inputs[key]
                    if inputs.get('evidence_cache_dir'):
                        analysis_args['evidence_cache_dir'] = str(inputs['evidence_cache_dir'])
                    elif evidence_provider != 'local':
                        analysis_args['evidence_cache_dir'] = str(Path(output_dir) / 'evidence_cache')
                    if evidence_provider == 'gencode':
                        analysis_args['gencode_gtf'] = str(inputs.get('gencode_gtf') or annotation_gtf)
                    steps.append({
                        'id': 'omics_analysis',
                        'tool': 'omics_run_analysis',
                        'depends_on': ['rnaseq_feature_counts'],
                        'args': analysis_args,
                    })
                    omics_ready = True
                    rationale.append(f'count matrix is forwarded to {evidence_provider} differential expression, pathway enrichment and report generation')
    elif 'omics' in domains and variant_normalization_task:
        vcf_path = inputs.get('vcf_path') or inputs.get('vcf') or inputs.get('variants_vcf')
        reference_fasta = inputs.get('reference_fasta') or inputs.get('reference_path') or inputs.get('reference_genome')
        if not vcf_path:
            missing.append('vcf_path')
        if not reference_fasta:
            missing.append('reference_fasta')
        if vcf_path and reference_fasta:
            normalized_vcf = str(inputs.get('normalized_vcf') or Path(output_dir) / 'normalized.vcf')
            normalization_args = {
                'vcf_path': str(vcf_path),
                'reference_fasta': str(reference_fasta),
                'output_dir': output_dir,
                'output_vcf': normalized_vcf,
            }
            for key in ('region', 'timeout'):
                if inputs.get(key) is not None:
                    normalization_args[key] = inputs[key]
            steps.append({
                'id': 'variant_normalization',
                'tool': 'omics_normalize_variants',
                'args': normalization_args,
            })
            rationale.append('VCF normalization uses reference-aware bcftools norm to left-align indels and split multiallelic records')
            if _is_variant_annotation_task(task, inputs):
                annotation_backend = str(inputs.get('annotation_backend', 'auto'))
                annotation_csv = inputs.get('annotation_csv')
                annotation_gtf = inputs.get('annotation_gtf') or inputs.get('gencode_gtf')
                if annotation_backend == 'gencode_gtf' and not annotation_gtf:
                    missing.append('annotation_gtf')
                elif annotation_backend not in {'vcf_ann', 'gencode_gtf'} and not annotation_csv and not annotation_gtf:
                    missing.append('annotation_csv')
                if annotation_backend == 'vcf_ann' or annotation_csv or annotation_gtf:
                    annotation_args = {
                        'vcf_path': '${variant_normalization.output_vcf}',
                        'output_csv': str(inputs.get(
                            'variant_output_csv', Path(output_dir) / 'variant_annotation.csv'
                        )),
                        'annotation_backend': annotation_backend,
                    }
                    if annotation_csv:
                        annotation_args['annotation_csv'] = str(annotation_csv)
                    if annotation_gtf:
                        annotation_args['annotation_gtf'] = str(annotation_gtf)
                    steps.append({
                        'id': 'variant_annotation',
                        'tool': 'omics_annotate_variants',
                        'depends_on': ['variant_normalization'],
                        'args': annotation_args,
                    })
                    variant_ready = True
            rationale.append('normalized variants are forwarded to the existing ANN, GENCODE GTF or local genomic interval annotator')
    elif 'omics' in domains and variant_calling_task:
        bam_path = inputs.get('bam_path') or inputs.get('input_bam') or inputs.get('cram_path')
        reference_fasta = inputs.get('reference_fasta') or inputs.get('reference_path') or inputs.get('reference_genome')
        if not bam_path:
            missing.append('bam_path')
        if not reference_fasta:
            missing.append('reference_fasta')
        if bam_path and reference_fasta:
            variant_calling_args = {
                'bam_path': str(bam_path),
                'reference_fasta': str(reference_fasta),
                'output_dir': output_dir,
            }
            if inputs.get('output_vcf'):
                variant_calling_args['output_vcf'] = str(inputs['output_vcf'])
            for key in ('region', 'min_mapping_quality', 'min_base_quality', 'timeout'):
                if inputs.get(key) is not None:
                    variant_calling_args[key] = inputs[key]
            steps.append({
                'id': 'variant_calling',
                'tool': 'omics_run_variant_calling',
                'args': variant_calling_args,
            })
            rationale.append('variant calling uses indexed BAM/CRAM, reference FASTA and fixed bcftools mpileup/call commands with provenance')
    elif 'omics' in domains and qc_task:
        qc_input = (
            inputs.get('input_path')
            or inputs.get('fastq_path')
            or inputs.get('fastq_paths')
            or inputs.get('bam_path')
            or inputs.get('cram_path')
            or inputs.get('vcf_path')
            or inputs.get('bcf_path')
        )
        if not qc_input:
            missing.append('input_path')
        else:
            if isinstance(qc_input, (list, tuple)):
                serialized_input = [str(path) for path in qc_input]
            else:
                serialized_input = str(qc_input)
            if fastq_qc_task:
                qc_args = {
                    'fastq_paths': serialized_input,
                    'output_dir': output_dir,
                    'timeout': int(inputs.get('qc_timeout', 900)),
                }
                if inputs.get('fastq_r2_paths') is not None:
                    qc_args['fastq_r2_paths'] = inputs['fastq_r2_paths']
                if inputs.get('qc_threads') is not None:
                    qc_args['threads'] = int(inputs['qc_threads'])
                steps.append({
                    'id': 'fastq_qc',
                    'tool': 'omics_run_fastq_qc',
                    'args': qc_args,
                })
                rationale.append('native FastQC evaluates sequencing quality and MultiQC aggregates a reviewable report')
            else:
                steps.append({
                    'id': 'genomics_qc',
                    'tool': 'omics_run_genomics_qc',
                    'args': {
                        'input_path': serialized_input,
                        'output_dir': output_dir,
                        'input_type': str(inputs.get('input_type', 'auto')),
                        'timeout': int(inputs.get('qc_timeout', 300)),
                    },
                })
                rationale.append('genomics QC uses a reproducible FASTQ parser or fixed SAMtools/bcftools commands')
    elif 'omics' in domains and variant_task:
        vcf_path = inputs.get('vcf_path') or inputs.get('vcf') or inputs.get('variants_vcf')
        annotation_backend = str(inputs.get('annotation_backend', 'auto'))
        annotation_csv = inputs.get('annotation_csv')
        annotation_gtf = inputs.get('annotation_gtf') or inputs.get('gencode_gtf')
        if not vcf_path:
            missing.append('vcf_path')
        if annotation_backend == 'gencode_gtf' and not annotation_gtf:
            missing.append('annotation_gtf')
        elif annotation_backend not in {'vcf_ann', 'gencode_gtf'} and not annotation_csv and not annotation_gtf:
            missing.append('annotation_csv')
        if vcf_path and (annotation_backend == 'vcf_ann' or annotation_csv or annotation_gtf):
            args = {
                'vcf_path': str(vcf_path),
                'output_csv': str(inputs.get(
                    'variant_output_csv', Path(output_dir) / 'variant_annotation.csv'
                )),
                'annotation_backend': annotation_backend,
            }
            if annotation_csv:
                args['annotation_csv'] = str(annotation_csv)
            if annotation_gtf:
                args['annotation_gtf'] = str(annotation_gtf)
            steps.append({
                'id': 'variant_annotation',
                'tool': 'omics_annotate_variants',
                'args': args,
            })
            variant_ready = True
            rationale.append('variant annotation uses VCF ANN records, GENCODE GTF coordinates or a local genomic interval table')
    elif 'omics' in domains:
        omics_required = ('expression_csv', 'metadata_csv', 'gene_sets_csv')
        missing.extend(key for key in omics_required if not inputs.get(key))
        if not any(key in missing for key in omics_required):
            args = {key: str(inputs[key]) for key in omics_required}
            args['output_dir'] = output_dir
            args['evidence_provider'] = evidence_provider
            for key in (
                'evidence_csv', 'condition_a', 'condition_b', 'evidence_timeout',
                'statistics_backend', 'genome', 'gencode_gtf',
            ):
                if inputs.get(key) is not None:
                    args[key] = inputs[key]
            if inputs.get('evidence_cache_dir'):
                args['evidence_cache_dir'] = str(inputs['evidence_cache_dir'])
            elif evidence_provider != 'local':
                args['evidence_cache_dir'] = str(Path(output_dir) / 'evidence_cache')
            steps.append({
                'id': 'omics_analysis',
                'tool': 'omics_run_analysis',
                'args': args,
            })
            omics_ready = True
            rationale.append(f'omics analysis uses {evidence_provider} evidence')

    direct_literature = bool(inputs.get('gene_ids'))
    reuse_omics_evidence = omics_ready and evidence_provider != 'local' and not direct_literature
    if 'literature' in domains and variant_ready and evidence_provider == 'local' and not inputs.get('evidence_csv'):
        missing.append('evidence_csv')
    if direct_literature and evidence_provider == 'local' and not inputs.get('evidence_csv'):
        missing.append('evidence_csv')
    if 'literature' in domains and variant_ready and not direct_literature:
        search_args = {
            'gene_ids': '${variant_annotation.gene_ids}',
            'provider': evidence_provider,
        }
        if inputs.get('evidence_csv'):
            search_args['evidence_csv'] = str(inputs['evidence_csv'])
        if inputs.get('evidence_cache_dir'):
            search_args['cache_dir'] = str(inputs['evidence_cache_dir'])
        if inputs.get('genome'):
            search_args['genome'] = str(inputs['genome'])
        if inputs.get('gencode_gtf') or inputs.get('annotation_gtf'):
            search_args['gencode_gtf'] = str(inputs.get('gencode_gtf') or inputs['annotation_gtf'])
        steps.extend([
            {
                'id': 'variant_evidence_search',
                'tool': 'literature_search',
                'depends_on': ['variant_annotation'],
                'args': search_args,
            },
            {
                'id': 'variant_evidence_summary',
                'tool': 'literature_summarize',
                'depends_on': ['variant_evidence_search'],
                'args': {'evidence': '${variant_evidence_search.result}'},
            },
        ])
        rationale.append(f'variant genes are forwarded to {evidence_provider} evidence retrieval')
    elif 'literature' in domains and not reuse_omics_evidence:
        if not direct_literature:
            missing.append('gene_ids')
        else:
            search_args = {
                'gene_ids': [str(gene_id) for gene_id in inputs['gene_ids']],
                'provider': evidence_provider,
            }
            if inputs.get('evidence_csv'):
                search_args['evidence_csv'] = str(inputs['evidence_csv'])
            if inputs.get('evidence_cache_dir'):
                search_args['cache_dir'] = str(inputs['evidence_cache_dir'])
            if inputs.get('genome'):
                search_args['genome'] = str(inputs['genome'])
            if inputs.get('gencode_gtf') or inputs.get('annotation_gtf'):
                search_args['gencode_gtf'] = str(inputs.get('gencode_gtf') or inputs['annotation_gtf'])
            steps.extend([
                {
                    'id': 'literature_search',
                    'tool': 'literature_search',
                    'args': search_args,
                },
                {
                    'id': 'literature_summary',
                    'tool': 'literature_summarize',
                    'depends_on': ['literature_search'],
                    'args': {'evidence': '${literature_search.result}'},
                },
            ])
            rationale.append(f'literature search uses {evidence_provider} evidence')
    elif reuse_omics_evidence:
        rationale.append('significant genes from omics analysis are forwarded to the selected evidence provider')

    if 'knowledge' in domains:
        index_path = inputs.get('knowledge_index_path')
        if not index_path and inputs.get('documents_dir'):
            index_path = str(Path(output_dir) / 'knowledge_index.json')
            steps.append({
                'id': 'knowledge_ingest',
                'tool': 'knowledge_ingest_directory',
                'args': {
                    'input_dir': str(inputs['documents_dir']),
                    'output_path': index_path,
                },
            })
        if index_path:
            search_step = {
                'id': 'knowledge_search',
                'tool': 'knowledge_search',
                'args': {
                    'query': task.strip(),
                    'index_path': index_path,
                    'top_k': int(inputs.get('top_k', 5)),
                },
            }
            if any(step['id'] == 'knowledge_ingest' for step in steps):
                search_step['depends_on'] = ['knowledge_ingest']
                search_step['args']['index_path'] = '${knowledge_ingest.result.output_path}'
            steps.append(search_step)
            rationale.append('knowledge retrieval grounds the research context')
        else:
            missing.append('documents_dir')

    if 'sequence' in domains:
        if not inputs.get('protein'):
            missing.append('protein')
        else:
            steps.extend([
                {
                    'id': 'sequence_design',
                    'tool': 'sequence_pipeline',
                    'args': {
                        'protein': str(inputs['protein']),
                        'molecule': inputs.get('molecule', 'linear'),
                        'method': inputs.get('method', 'greedy'),
                    },
                },
                {
                    'id': 'sequence_report',
                    'tool': 'sequence_report',
                    'depends_on': ['sequence_design'],
                    'args': {
                        'result': '${sequence_design.result}',
                        'output_path': str(inputs.get(
                            'sequence_report_path',
                            Path(output_dir) / 'sequence_report.html',
                        )),
                    },
                },
            ])
            rationale.append('sequence pipeline includes optimization, scoring and translation verification')

    if 'cadd' in domains:
        ligand_library = inputs.get('ligand_library') or inputs.get('external_dataset')
        if not inputs.get('receptor'):
            missing.append('receptor')
        elif not Path(str(inputs['receptor'])).exists():
            missing.append('receptor')
        if not ligand_library:
            missing.append('ligand_library')
        elif not Path(str(ligand_library)).exists():
            missing.append('ligand_library')
        if inputs.get('receptor') and ligand_library and not missing:
            steps.append({
                'id': 'cadd_screening',
                'tool': 'cadd_run_screening',
                'args': {
                    'receptor': str(inputs['receptor']),
                    'out': str(Path(output_dir) / 'cadd'),
                    'external_dataset': str(ligand_library),
                    'exhaustiveness': int(inputs.get('exhaustiveness', 4)),
                    'max_ligands': int(inputs.get('max_ligands', 3)),
                },
            })
            rationale.append('CADD screening is isolated as a reproducible execution step')

    missing = sorted(set(missing))
    workflow = {'name': 'auto-research-workflow', 'steps': steps} if steps else None
    ready = bool(workflow and not missing)
    return {
        'ready': ready,
        'missing_inputs': missing,
        'evidence_provider': evidence_provider,
        'selected_tools': [step['tool'] for step in steps],
        'rationale': rationale,
        'workflow': workflow if ready else None,
        'workflow_preview': workflow,
    }


def research_catalog():
    domain_catalog = _domain_registry_module().active_domain_catalog
    return {
        'status': 'ok',
        'application': 'bioinformatics-research-agent',
        'application_version': PLUGIN_VERSION,
        'domains': domain_catalog(),
        'policy': 'catalog-only; no computation or external network call',
    }


def research_presets():
    return {
        'status': 'ok',
        'application': 'bioinformatics-research-agent',
        'presets': [
            {'id': preset_id, **preset}
            for preset_id, preset in RESEARCH_PRESETS.items()
        ],
    }


def research_run_preset(preset, output_path='output/research_manifest.json',
                        report_path='output/research_report.md', dry_run=True,
                        continue_on_error=False):
    if preset not in RESEARCH_PRESETS:
        raise ValueError(f'unknown research preset: {preset}')
    preset_config = RESEARCH_PRESETS[preset]
    workflow_path = _project_root() / preset_config['path']
    workflow = _workflow_runner_module().load_workflow(workflow_path)
    return research_execute(
        workflow,
        domains=preset_config['domains'],
        output_path=output_path,
        report_path=report_path,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
    )


def research_plan(task, domains=None, inputs=None, output_dir='output/research_auto', planner_mode='deterministic'):
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task must be a non-empty string')
    if inputs is not None and not isinstance(inputs, dict):
        raise ValueError('inputs must be an object')
    selected, planner = _resolve_domains(task, domains, inputs, planner_mode)
    tool_specs = _domain_registry_module().active_tool_specs
    capabilities = [
        spec['name']
        for spec in tool_specs()
        if spec['domain'] in selected
    ]
    steps = [
        {
            'id': 'capability_check',
            'type': 'platform',
            'status': 'ready',
            'description': 'Inspect available domain plugins and tool contracts.',
        },
        {
            'id': 'input_validation',
            'type': 'application',
            'status': 'required',
            'required_inputs': _required_inputs(selected, task, inputs),
        },
        {
            'id': 'validated_workflow',
            'type': 'execution',
            'status': 'ready',
            'allowed_domains': selected,
            'allowed_tools': capabilities,
        },
        {
            'id': 'traceable_report',
            'type': 'report',
            'status': 'ready',
            'description': 'Persist step results, provenance and quality checks.',
        },
    ]
    execution = _build_workflow(task, selected, inputs, output_dir)
    return {
        'status': 'planned',
        'application': 'bioinformatics-research-agent',
        'task': task.strip(),
        'selected_domains': selected,
        'capabilities': capabilities,
        'required_inputs': _required_inputs(selected, task, inputs),
        'execution': execution,
        'evidence_provider': execution['evidence_provider'],
        'steps': steps,
        'planner': planner,
        'provenance': {
            'planner': planner['backend'],
            'planner_mode': planner['mode'],
            'planner_model': planner.get('model'),
            'fallback_reason': planner.get('fallback_reason'),
        },
        'policy': {
            'llm_may_select_tools': False,
            'llm_may_select_domains': planner['backend'] == 'llm',
            'llm_may_invent_measurements': False,
            'execution_requires_validated_workflow': True,
        },
    }


def research_build_workflow(task, inputs, domains=None, output_dir='output/research_auto', planner_mode='deterministic'):
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task must be a non-empty string')
    if not isinstance(inputs, dict):
        raise ValueError('inputs must be an object')
    selected, planner = _resolve_domains(task, domains, inputs, planner_mode)
    execution = _build_workflow(task, selected, inputs, output_dir)
    return {
        'status': 'planned',
        'application': 'bioinformatics-research-agent',
        'task': task.strip(),
        'selected_domains': selected,
        **execution,
        'provenance': {
            'planner': planner['backend'],
            'planner_mode': planner['mode'],
            'planner_model': planner.get('model'),
            'fallback_reason': planner.get('fallback_reason'),
            'workflow_validation': 'delegated to research_execute',
        },
    }


def _write_research_report(manifest, report_path):
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '# Bioinformatics Research Agent Report',
        '',
        f"- Workflow: {manifest.get('workflow', 'unnamed')}",
        f"- Status: {manifest.get('status', 'unknown')}",
        f"- Completed steps: {manifest.get('completed_steps', 0)}",
        f"- Failed steps: {manifest.get('failed_steps', 0)}",
        '',
        '## Steps',
        '',
        '| Step | Tool | Status |',
        '|---|---|---|',
    ]
    for step in manifest.get('steps', []):
        lines.append(
            f"| {step.get('id', '')} | {step.get('tool', '')} | {step.get('status', '')} |"
        )
    lines.extend(['', '## Evidence and outputs', ''])
    for step in manifest.get('steps', []):
        result = step.get('result', {})
        if not isinstance(result, dict):
            continue
        payload = result.get('result', result)
        if not isinstance(payload, dict):
            continue
        if result.get('plugin') == 'literature':
            lines.append(
                f"- {step.get('id')}: literature matches={payload.get('n_matches', 0)}"
            )
        if result.get('plugin') == 'knowledge':
            matches = payload.get('matches', [])
            lines.append(
                f"- {step.get('id')}: retrieved knowledge matches={payload.get('n_matches', 0)}"
            )
            for match in matches[:3]:
                lines.append(
                    f"  - {match.get('title', match.get('document_id', 'document'))} "
                    f"(score={match.get('score', 0)})"
                )
        if result.get('plugin') == 'sequence':
            metrics = payload.get('metrics') or payload.get('result', {}).get('metrics', {})
            lines.append(
                f"- {step.get('id')}: sequence verdict={payload.get('verdict', 'n/a')}, "
                f"verified={payload.get('verify', 'n/a')}, metrics={metrics}"
            )
        for key in ('output_csv', 'output_md', 'output_html'):
            if payload.get(key):
                lines.append(f"- {step.get('id')}: {key} = {payload[key]}")
    report_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'status': 'ok', 'path': str(report_path)}


def research_execute(workflow, domains=None, output_path='output/research_manifest.json',
                     report_path='output/research_report.md', dry_run=True,
                     continue_on_error=False):
    if not isinstance(workflow, dict):
        raise ValueError('workflow must be an object')
    registry = _domain_registry_module()
    tool_specs = registry.active_tool_specs
    run_workflow = _workflow_runner_module().run_workflow
    requested_domains = domains
    if requested_domains is None:
        referenced_domains = []
        for step in workflow.get('steps', []):
            tool = step.get('tool') if isinstance(step, dict) else None
            domain = tool.split('_', 1)[0] if isinstance(tool, str) and '_' in tool else None
            if domain and domain not in referenced_domains:
                referenced_domains.append(domain)
        requested_domains = referenced_domains or None
    selected = _select_domains(workflow.get('name', 'research workflow'), requested_domains)
    allowed_tools = [
        spec['name']
        for spec in tool_specs()
        if spec['domain'] in selected
    ]
    manifest_path = Path(output_path)
    if not manifest_path.is_absolute():
        manifest_path = _project_root() / manifest_path
    manifest = run_workflow(
        workflow,
        output_path=manifest_path,
        dry_run=dry_run,
        allowed_tools=allowed_tools,
        continue_on_error=continue_on_error,
    )
    report = (
        _write_research_report(
            manifest,
            _project_root() / report_path
            if report_path and not Path(report_path).is_absolute()
            else report_path,
        )
        if report_path else None
    )
    return {
        'status': manifest['status'],
        'application': 'bioinformatics-research-agent',
        'selected_domains': selected,
        'manifest': manifest,
        'report': report,
        'provenance': {
            'application': PLUGIN_NAME,
            'version': PLUGIN_VERSION,
            'dry_run': dry_run,
        },
    }


TOOLS = {
    'catalog': {
        'description': 'List available bioinformatics domains, plugins, versions and health status.',
        'parameters': _parameters({}),
        'function': research_catalog,
    },
    'presets': {
        'description': 'List reproducible research application presets.',
        'parameters': _parameters({}),
        'function': research_presets,
    },
    'run_preset': {
        'description': 'Run a named research preset in dry-run or execution mode.',
        'parameters': _parameters({
            'preset': {'type': 'string', 'enum': list(RESEARCH_PRESETS)},
            'output_path': {'type': 'string'},
            'report_path': {'type': 'string'},
            'dry_run': {'type': 'boolean'},
            'continue_on_error': {'type': 'boolean'},
        }, ('preset',)),
        'function': research_run_preset,
    },
    'plan': {
        'description': 'Build a traceable research plan and infer evidence sources without inventing measurements.',
        'parameters': _parameters({
            'task': {'type': 'string'},
            'domains': {'type': 'array', 'items': {'type': 'string'}},
            'inputs': {'type': 'object'},
            'output_dir': {'type': 'string'},
            'planner_mode': {'type': 'string', 'enum': ['deterministic', 'auto', 'llm']},
        }, ('task',)),
        'function': research_plan,
    },
    'build_workflow': {
        'description': 'Build an executable cross-domain workflow from a research task and validated inputs.',
        'parameters': _parameters({
            'task': {'type': 'string'},
            'domains': {'type': 'array', 'items': {'type': 'string'}},
            'inputs': {'type': 'object'},
            'output_dir': {'type': 'string'},
            'planner_mode': {'type': 'string', 'enum': ['deterministic', 'auto', 'llm']},
        }, ('task', 'inputs')),
        'function': research_build_workflow,
    },
    'execute': {
        'description': 'Execute or dry-run a validated cross-domain research workflow with an audit manifest.',
        'parameters': _parameters({
            'workflow': {'type': 'object'},
            'domains': {'type': 'array', 'items': {'type': 'string'}},
            'output_path': {'type': 'string'},
            'report_path': {'type': 'string'},
            'dry_run': {'type': 'boolean'},
            'continue_on_error': {'type': 'boolean'},
        }, ('workflow',)),
        'function': research_execute,
    },
}
