"""Non-omics domain step construction for research planning."""

from pathlib import Path

def _append_literature_steps(
    domains,
    inputs,
    evidence_provider,
    variant_ready,
    omics_ready,
    steps,
    missing,
    rationale,
):
    direct_literature = bool(inputs.get('gene_ids'))
    reuse_omics_evidence = (
        omics_ready and evidence_provider != 'local' and not direct_literature
    )
    if (
        'literature' in domains
        and variant_ready
        and evidence_provider == 'local'
        and not inputs.get('evidence_csv')
    ):
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
            search_args['gencode_gtf'] = str(
                inputs.get('gencode_gtf') or inputs['annotation_gtf']
            )
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
        rationale.append(
            f'variant genes are forwarded to {evidence_provider} evidence retrieval'
        )
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
                search_args['gencode_gtf'] = str(
                    inputs.get('gencode_gtf') or inputs['annotation_gtf']
                )
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
        rationale.append(
            'significant genes from omics analysis are forwarded to the selected evidence provider'
        )


def _append_imaging_steps(domains, inputs, output_dir, steps, missing, rationale):
    if 'imaging' not in domains:
        return
    image_path = inputs.get('image_path')
    if not image_path:
        missing.append('image_path')
        return
    steps.append({
        'id': 'image_qc',
        'tool': 'imaging_inspect_image',
        'args': {
            'image_path': str(image_path),
            'output_dir': str(
                inputs.get('image_output_dir') or Path(output_dir) / 'image_qc'
            ),
            'modality': str(inputs.get('image_modality', 'microscopy')),
        },
    })
    rationale.append(
        'image QC records deterministic dimensions, channels and SHA-256 provenance'
    )


def _append_knowledge_steps(
    task,
    domains,
    inputs,
    output_dir,
    steps,
    missing,
    rationale,
):
    if 'knowledge' not in domains:
        return
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
    if not index_path:
        missing.append('documents_dir')
        return
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


def _append_sequence_steps(domains, inputs, output_dir, steps, missing, rationale):
    if 'sequence' not in domains:
        return
    if not inputs.get('protein'):
        missing.append('protein')
        return
    steps.append({
        'id': 'sequence_design',
        'tool': 'sequence_pipeline',
        'args': {
            'protein': str(inputs['protein']),
            'molecule': inputs.get('molecule', 'linear'),
            'method': inputs.get('method', 'greedy'),
        },
    })
    report_dependencies = ['sequence_design']
    if inputs.get('include_benchmark', False):
        steps.append({
            'id': 'sequence_benchmark',
            'tool': 'sequence_benchmark',
            'depends_on': ['sequence_design'],
            'args': {
                'protein': str(inputs['protein']),
                'molecule': inputs.get('molecule', 'linear'),
                'use_vaxpress': bool(inputs.get('use_vaxpress', False)),
            },
        })
        report_dependencies.append('sequence_benchmark')
    steps.append({
        'id': 'sequence_report',
        'tool': 'sequence_report',
        'depends_on': report_dependencies,
        'args': {
            'result': '${sequence_design.result}',
            'output_path': str(
                inputs.get(
                    'sequence_report_path',
                    Path(output_dir) / 'sequence_report.html',
                )
            ),
        },
    })
    rationale.append(
        'sequence pipeline includes optimization, scoring and translation verification'
    )
    if inputs.get('include_benchmark', False):
        rationale.append(
            'benchmark compares naive and greedy codon strategies and records optional VaxPress fallback'
        )


def _append_cadd_steps(domains, inputs, output_dir, steps, missing, rationale):
    if 'cadd' not in domains:
        return
    ligand_library = inputs.get('ligand_library') or inputs.get('external_dataset')
    if not inputs.get('receptor') or not Path(str(inputs['receptor'])).exists():
        missing.append('receptor')
    if not ligand_library or not Path(str(ligand_library)).exists():
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
