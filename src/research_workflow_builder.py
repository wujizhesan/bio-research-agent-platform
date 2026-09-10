"""Deterministic workflow construction for the research application."""

try:
    from .research_domain_workflows import (
        _append_cadd_steps,
        _append_imaging_steps,
        _append_knowledge_steps,
        _append_literature_steps,
        _append_sequence_steps,
    )
    from .research_omics_workflow import (
        _is_fastq_qc_task,
        _is_genomics_qc_task,
        _is_metagenomics_task,
        _is_rnaseq_alignment_task,
        _is_rnaseq_analysis_task,
        _is_rnaseq_counting_task,
        _is_single_cell_10x_task,
        _is_single_cell_task,
        _is_variant_calling_task,
        _is_variant_annotation_task,
        _is_variant_normalization_task,
        _is_variant_task,
        append_omics_steps,
    )
except ImportError:
    from research_domain_workflows import (
        _append_cadd_steps,
        _append_imaging_steps,
        _append_knowledge_steps,
        _append_literature_steps,
        _append_sequence_steps,
    )
    from research_omics_workflow import (
        _is_fastq_qc_task,
        _is_genomics_qc_task,
        _is_metagenomics_task,
        _is_rnaseq_alignment_task,
        _is_rnaseq_analysis_task,
        _is_rnaseq_counting_task,
        _is_single_cell_10x_task,
        _is_single_cell_task,
        _is_variant_calling_task,
        _is_variant_annotation_task,
        _is_variant_normalization_task,
        _is_variant_task,
        append_omics_steps,
    )

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
    if 'imaging' in domains:
        required.append({'name': 'image_path', 'description': 'microscopy or scientific image file'})
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
    omics_ready, variant_ready = append_omics_steps(
        task, domains, inputs, output_dir, evidence_provider,
        steps, missing, rationale,
    )
    if evidence_provider == 'gencode' and not (inputs.get('gencode_gtf') or inputs.get('annotation_gtf')):
        missing.append('gencode_gtf')


    _append_literature_steps(
        domains,
        inputs,
        evidence_provider,
        variant_ready,
        omics_ready,
        steps,
        missing,
        rationale,
    )
    _append_imaging_steps(
        domains, inputs, output_dir, steps, missing, rationale
    )
    _append_knowledge_steps(
        task, domains, inputs, output_dir, steps, missing, rationale
    )
    _append_sequence_steps(
        domains, inputs, output_dir, steps, missing, rationale
    )
    _append_cadd_steps(
        domains, inputs, output_dir, steps, missing, rationale
    )

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
