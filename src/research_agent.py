"""Application layer for a traceable bioinformatics research Agent."""
from pathlib import Path

try:
    from .research_results import (
        catalog_result,
        execution_result,
        plan_result,
        presets_result,
        workflow_result,
        write_research_report as _write_research_report,
    )
    from .research_validation import (
        select_domains,
        validate_planning_request,
        validate_preset,
        validate_workflow,
    )
    from .research_protocol import build_research_tools
    from .research_workflow_builder import _build_workflow, _required_inputs
except ImportError:
    from research_results import (
        catalog_result,
        execution_result,
        plan_result,
        presets_result,
        workflow_result,
        write_research_report as _write_research_report,
    )
    from research_validation import (
        select_domains,
        validate_planning_request,
        validate_preset,
        validate_workflow,
    )
    from research_protocol import build_research_tools
    from research_workflow_builder import _build_workflow, _required_inputs


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
    'imaging': (
        'imaging', 'image qc', 'image quality', 'microscopy', 'microscope',
        'scientific image', 'cell image', '显微图像', '图像质控',
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


def _available_domain_names():
    return set(_domain_registry_module().active_domains())


def _select_domains(task, requested):
    return select_domains(
        task,
        requested,
        _available_domain_names(),
        _DOMAIN_KEYWORDS,
    )


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




def research_catalog():
    domain_catalog = _domain_registry_module().active_domain_catalog
    return catalog_result(PLUGIN_VERSION, domain_catalog())


def research_presets():
    return presets_result(RESEARCH_PRESETS)


def research_run_preset(preset, output_path='output/research_manifest.json',
                        report_path='output/research_report.md', dry_run=True,
                        continue_on_error=False, resume=False):
    preset_config = validate_preset(preset, RESEARCH_PRESETS)
    workflow_path = _project_root() / preset_config['path']
    workflow = _workflow_runner_module().load_workflow(workflow_path)
    return research_execute(
        workflow,
        domains=preset_config['domains'],
        output_path=output_path,
        report_path=report_path,
        dry_run=dry_run,
        continue_on_error=continue_on_error,
        resume=resume,
    )


def research_plan(task, domains=None, inputs=None, output_dir='output/research_auto', planner_mode='deterministic'):
    task, inputs = validate_planning_request(task, inputs)
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
    return plan_result(
        task,
        selected,
        capabilities,
        _required_inputs(selected, task, inputs),
        execution,
        steps,
        planner,
    )


def research_build_workflow(task, inputs, domains=None, output_dir='output/research_auto', planner_mode='deterministic'):
    task, inputs = validate_planning_request(task, inputs, require_inputs=True)
    selected, planner = _resolve_domains(task, domains, inputs, planner_mode)
    execution = _build_workflow(task, selected, inputs, output_dir)
    return workflow_result(task, selected, execution, planner)


def research_execute(workflow, domains=None, output_path='output/research_manifest.json',
                     report_path='output/research_report.md', dry_run=True,
                     continue_on_error=False, resume=False):
    workflow = validate_workflow(workflow)
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
        resume=resume,
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
    return execution_result(
        manifest,
        selected,
        report,
        PLUGIN_NAME,
        PLUGIN_VERSION,
        dry_run,
        resume,
    )


def research_verify_reproducibility(manifest_path, check_environment=True):
    path = Path(manifest_path)
    if not path.is_absolute():
        path = _project_root() / path
    try:
        from .reproducibility import verify_manifest
    except ImportError:
        from reproducibility import verify_manifest
    return verify_manifest(
        path,
        check_environment=check_environment,
        tool_specs=_domain_registry_module().active_tool_specs(),
    )


TOOLS = build_research_tools({
    'catalog': research_catalog,
    'presets': research_presets,
    'run_preset': research_run_preset,
    'plan': research_plan,
    'build_workflow': research_build_workflow,
    'execute': research_execute,
    'verify_reproducibility': research_verify_reproducibility,
}, RESEARCH_PRESETS)
