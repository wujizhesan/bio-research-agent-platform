"""Tool protocol contracts for the omics plugin."""

try:
    from .tool_contracts import bind_tool_contracts, object_parameters
except ImportError:
    from tool_contracts import bind_tool_contracts, object_parameters


def _path_or_paths():
    return {
        'oneOf': [
            {'type': 'string'},
            {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
        ],
    }


def build_omics_tools(functions, statistics_backends, annotation_backends, qc_types):
    parameters = object_parameters
    evidence_providers = ['local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode']
    contracts = {
        'run_analysis': {
            'description': 'Run an end-to-end RNA-seq research analysis with differential expression, pathway enrichment, evidence retrieval and a traceable report.',
            'parameters': parameters({
                'expression_csv': {'type': 'string'},
                'metadata_csv': {'type': 'string'},
                'gene_sets_csv': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'evidence_csv': {'type': 'string'},
                'condition_a': {'type': 'string'},
                'condition_b': {'type': 'string'},
                'evidence_provider': {'type': 'string', 'enum': evidence_providers},
                'evidence_cache_dir': {'type': 'string'},
                'evidence_timeout': {'type': 'number'},
                'statistics_backend': {'type': 'string', 'enum': list(statistics_backends)},
                'genome': {'type': 'string'},
                'gencode_gtf': {'type': 'string'},
            }, ('expression_csv', 'metadata_csv', 'gene_sets_csv', 'output_dir')),
        },
        'run_rnaseq_workbench': {
            'description': 'Run the RNA-seq specialist workbench: FASTQ QC, alignment, feature counts, differential expression and pathway analysis.',
            'parameters': parameters({
                'fastq_paths': _path_or_paths(),
                'fastq_r2_paths': _path_or_paths(),
                'reference_fasta': {'type': 'string'},
                'annotation_gtf': {'type': 'string'},
                'metadata_csv': {'type': 'string'},
                'gene_sets_csv': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'evidence_csv': {'type': 'string'},
                'evidence_provider': {'type': 'string', 'enum': evidence_providers},
                'statistics_backend': {'type': 'string', 'enum': list(statistics_backends)},
                'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
            }, ('fastq_paths', 'output_dir')),
        },
        'run_variant_workbench': {
            'description': 'Run the VCF specialist workbench: variant QC, annotation and gene evidence retrieval.',
            'parameters': parameters({
                'vcf_path': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'annotation_csv': {'type': 'string'},
                'annotation_gtf': {'type': 'string'},
                'annotation_backend': {'type': 'string', 'enum': list(annotation_backends)},
                'evidence_csv': {'type': 'string'},
                'evidence_provider': {'type': 'string', 'enum': evidence_providers},
            }, ('vcf_path', 'output_dir')),
        },
        'run_differential_expression': {
            'description': 'Run a reproducible two-condition RNA-seq differential expression analysis.',
            'parameters': parameters({
                'expression_csv': {'type': 'string'},
                'metadata_csv': {'type': 'string'},
                'output_csv': {'type': 'string'},
                'condition_a': {'type': 'string'},
                'condition_b': {'type': 'string'},
                'statistics_backend': {'type': 'string', 'enum': list(statistics_backends)},
            }, ('expression_csv', 'metadata_csv', 'output_csv')),
        },
        'run_pathway_enrichment': {
            'description': 'Run pathway enrichment against a local gene-set table.',
            'parameters': parameters({
                'de_csv': {'type': 'string'},
                'gene_sets_csv': {'type': 'string'},
                'output_csv': {'type': 'string'},
                'padj_cutoff': {'type': 'number'},
                'abs_log2_fc_cutoff': {'type': 'number'},
            }, ('de_csv', 'gene_sets_csv', 'output_csv')),
        },
        'annotate_variants': {
            'description': 'Annotate VCF variants with VCF ANN records, a local interval table or GENCODE GTF gene coordinates and return traceable gene mappings.',
            'parameters': parameters({
                'vcf_path': {'type': 'string'},
                'output_csv': {'type': 'string'},
                'annotation_csv': {'type': 'string'},
                'annotation_gtf': {'type': 'string'},
                'annotation_backend': {'type': 'string', 'enum': list(annotation_backends)},
            }, ('vcf_path', 'output_csv')),
        },
        'inspect_toolchain': {
            'description': 'Report whether FastQC, MultiQC, HISAT2, SAMtools, bcftools, featureCounts, GATK and VEP are available in the execution environment.',
            'parameters': parameters({}),
        },
        'run_genomics_qc': {
            'description': 'Run reproducible QC for FASTQ, BAM/CRAM or VCF/BCF using a local parser, SAMtools or bcftools.',
            'parameters': parameters({
                'input_path': _path_or_paths(),
                'output_dir': {'type': 'string'},
                'input_type': {'type': 'string', 'enum': list(qc_types)},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
            }, ('input_path', 'output_dir')),
        },
        'run_fastq_qc': {
            'description': 'Run native FastQC on single-end or paired-end FASTQ files and aggregate the reports with MultiQC.',
            'parameters': parameters({
                'fastq_paths': _path_or_paths(),
                'output_dir': {'type': 'string'},
                'fastq_r2_paths': _path_or_paths(),
                'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
            }, ('fastq_paths', 'output_dir')),
        },
        'run_variant_calling': {
            'description': 'Call small variants from an indexed BAM/CRAM against a reference FASTA with SAMtools and bcftools, producing a VCF and provenance manifest.',
            'parameters': parameters({
                'bam_path': {'type': 'string'},
                'reference_fasta': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'output_vcf': {'type': 'string'},
                'region': {'type': 'string'},
                'min_mapping_quality': {'type': 'integer', 'minimum': 0},
                'min_base_quality': {'type': 'integer', 'minimum': 0},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
            }, ('bam_path', 'reference_fasta', 'output_dir')),
        },
        'normalize_variants': {
            'description': 'Normalize a VCF against a reference FASTA with bcftools, left-align indels, split multiallelic records and emit provenance.',
            'parameters': parameters({
                'vcf_path': {'type': 'string'},
                'reference_fasta': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'output_vcf': {'type': 'string'},
                'region': {'type': 'string'},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
            }, ('vcf_path', 'reference_fasta', 'output_dir')),
        },
        'run_rnaseq_alignment': {
            'description': 'Align single-end or paired-end RNA-seq FASTQ files to a reference FASTA with HISAT2 and emit sorted indexed BAM files with alignment provenance.',
            'parameters': parameters({
                'fastq_paths': _path_or_paths(),
                'reference_fasta': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'fastq_r2_paths': _path_or_paths(),
                'output_alignment_paths': _path_or_paths(),
                'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
            }, ('fastq_paths', 'reference_fasta', 'output_dir')),
        },
        'run_feature_counts': {
            'description': 'Generate a gene-by-sample RNA-seq count matrix from BAM/CRAM alignments and a GTF annotation using featureCounts.',
            'parameters': parameters({
                'alignment_paths': _path_or_paths(),
                'annotation_gtf': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'output_csv': {'type': 'string'},
                'feature_type': {'type': 'string'},
                'gene_id_attribute': {'type': 'string'},
                'strand': {'type': 'integer', 'enum': [0, 1, 2]},
                'paired_end': {'type': 'boolean'},
                'threads': {'type': 'integer', 'minimum': 1, 'maximum': 64},
                'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 3600},
            }, ('alignment_paths', 'annotation_gtf', 'output_dir')),
        },
        'run_single_cell_qc': {
            'description': 'Calculate single-cell expression QC metrics and write a filtered cell matrix without requiring Scanpy.',
            'parameters': parameters({
                'matrix_csv': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'cell_id_column': {'type': 'string'},
                'min_genes': {'type': 'integer', 'minimum': 0},
                'max_genes': {'type': 'integer', 'minimum': 0},
                'min_counts': {'type': 'number', 'minimum': 0},
                'max_mito_percent': {'type': 'number', 'minimum': 0, 'maximum': 100},
                'mitochondrial_prefix': {'type': 'string'},
            }, ('matrix_csv', 'output_dir')),
        },
        'run_single_cell_10x_qc': {
            'description': 'Run single-cell QC on 10x Matrix Market, barcodes and features files, preserving sparse output artifacts.',
            'parameters': parameters({
                'matrix_mtx': {'type': 'string'},
                'barcodes_tsv': {'type': 'string'},
                'features_tsv': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'min_genes': {'type': 'integer', 'minimum': 0},
                'max_genes': {'type': 'integer', 'minimum': 0},
                'min_counts': {'type': 'number', 'minimum': 0},
                'max_mito_percent': {'type': 'number', 'minimum': 0, 'maximum': 100},
                'mitochondrial_prefix': {'type': 'string'},
            }, ('matrix_mtx', 'barcodes_tsv', 'features_tsv', 'output_dir')),
        },
        'run_metagenomics_qc': {
            'description': 'Validate a taxon or feature abundance table, calculate relative abundance and sample alpha-diversity metrics.',
            'parameters': parameters({
                'abundance_csv': {'type': 'string'},
                'output_dir': {'type': 'string'},
                'taxon_id_column': {'type': 'string'},
                'min_total_counts': {'type': 'number', 'minimum': 0},
                'min_prevalence': {'type': 'integer', 'minimum': 0},
            }, ('abundance_csv', 'output_dir')),
        },
        'search_gene_evidence': {
            'description': 'Retrieve cited gene evidence from a structured evidence index.',
            'parameters': parameters({
                'gene_ids': {'type': 'array', 'items': {'type': 'string'}},
                'evidence_csv': {'type': 'string'},
                'provider': {'type': 'string', 'enum': evidence_providers},
                'cache_dir': {'type': 'string'},
                'timeout': {'type': 'number'},
                'genome': {'type': 'string'},
                'gencode_gtf': {'type': 'string'},
            }, ('gene_ids',)),
        },
        'generate_omics_report': {
            'description': 'Generate a traceable RNA-seq analysis report.',
            'parameters': parameters({
                'de_csv': {'type': 'string'},
                'pathway_csv': {'type': 'string'},
                'evidence': {'type': 'object'},
                'output_md': {'type': 'string'},
            }, ('de_csv', 'pathway_csv', 'output_md')),
        },
    }
    return bind_tool_contracts(contracts, functions)
