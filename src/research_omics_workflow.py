"""Omics workflow step construction for research planning."""

from pathlib import Path

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


def _append_multiomics_steps(inputs, output_dir, steps, missing, rationale):
    omics_ready = False
    variant_ready = False
    fastq_path = inputs.get('fastq_path') or inputs.get('fastq_paths')
    if not fastq_path:
        missing.append('fastq_paths')
    else:
        steps.append({
            'id': 'genomics_qc',
            'tool': 'omics_run_genomics_qc',
            'args': {
                'input_path': str(fastq_path[0] if isinstance(fastq_path, (list, tuple)) else fastq_path),
                'input_type': str(inputs.get('input_type', 'fastq')),
                'output_dir': str(Path(output_dir) / 'genomics_qc'),
            },
        })
        rationale.append('genomics QC records sequencing read counts and quality metrics')
    ten_x_inputs = ('matrix_mtx', 'barcodes_tsv', 'features_tsv')
    missing.extend(key for key in ten_x_inputs if not inputs.get(key))
    if not any(key in missing for key in ten_x_inputs):
        steps.append({
            'id': 'single_cell_10x_qc',
            'tool': 'omics_run_single_cell_10x_qc',
            'args': {
                **{key: str(inputs[key]) for key in ten_x_inputs},
                'output_dir': str(Path(output_dir) / 'single_cell_10x_qc'),
            },
        })
        rationale.append('10x single-cell QC preserves sparse Matrix Market artifacts and filters cells by core QC metrics')
    abundance_csv = inputs.get('abundance_csv') or inputs.get('metagenomics_abundance')
    if not abundance_csv:
        missing.append('abundance_csv')
    else:
        steps.append({
            'id': 'metagenomics_qc',
            'tool': 'omics_run_metagenomics_qc',
            'args': {
                'abundance_csv': str(abundance_csv),
                'output_dir': str(Path(output_dir) / 'metagenomics_qc'),
                'min_prevalence': int(inputs.get('min_prevalence', 1)),
            },
        })
        rationale.append('metagenomics QC normalizes abundance and calculates observed taxa and Shannon diversity')
    return omics_ready, variant_ready


def _append_metagenomics_steps(inputs, output_dir, steps, missing, rationale):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_single_cell_steps(
        inputs, output_dir, steps, missing, rationale, single_cell_10x_task):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_rnaseq_alignment_steps(
        inputs, output_dir, evidence_provider, steps, missing, rationale,
        fastq_qc_task, rnaseq_counting_task, rnaseq_analysis_task):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_rnaseq_counting_steps(
        inputs, output_dir, evidence_provider, steps, missing, rationale,
        rnaseq_analysis_task):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_variant_normalization_steps(
        task, inputs, output_dir, steps, missing, rationale):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_variant_calling_steps(inputs, output_dir, steps, missing, rationale):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_genomics_qc_steps(
        inputs, output_dir, steps, missing, rationale, fastq_qc_task):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_variant_annotation_steps(inputs, output_dir, steps, missing, rationale):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def _append_default_omics_steps(
        inputs, output_dir, evidence_provider, steps, missing, rationale):
    omics_ready = False
    variant_ready = False
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
    return omics_ready, variant_ready


def append_omics_steps(task, domains, inputs, output_dir, evidence_provider, steps, missing, rationale):
    if 'omics' not in domains:
        return False, False
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
    multiomics_task = bool(inputs.get('multiomics'))
    if multiomics_task:
        return _append_multiomics_steps(inputs, output_dir, steps, missing, rationale)
    if metagenomics_task:
        return _append_metagenomics_steps(inputs, output_dir, steps, missing, rationale)
    if single_cell_task:
        return _append_single_cell_steps(
            inputs, output_dir, steps, missing, rationale, single_cell_10x_task,
        )
    if rnaseq_alignment_task:
        return _append_rnaseq_alignment_steps(
            inputs, output_dir, evidence_provider, steps, missing, rationale,
            fastq_qc_task, rnaseq_counting_task, rnaseq_analysis_task,
        )
    if rnaseq_counting_task:
        return _append_rnaseq_counting_steps(
            inputs, output_dir, evidence_provider, steps, missing, rationale,
            rnaseq_analysis_task,
        )
    if variant_normalization_task:
        return _append_variant_normalization_steps(
            task, inputs, output_dir, steps, missing, rationale,
        )
    if variant_calling_task:
        return _append_variant_calling_steps(inputs, output_dir, steps, missing, rationale)
    if qc_task:
        return _append_genomics_qc_steps(
            inputs, output_dir, steps, missing, rationale, fastq_qc_task,
        )
    if variant_task:
        return _append_variant_annotation_steps(inputs, output_dir, steps, missing, rationale)
    return _append_default_omics_steps(
        inputs, output_dir, evidence_provider, steps, missing, rationale,
    )
