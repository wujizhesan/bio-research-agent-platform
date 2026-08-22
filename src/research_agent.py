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
        'metagenome',
    ),
    'sequence': (
        'mrna', 'mRNA', 'sequence', 'codon', 'protein sequence',
        'nucleotide', 'translation',
    ),
    'literature': (
        'literature', 'pubmed', 'uniprot', 'paper', 'evidence',
        'citation', '文献', '数据库',
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


def _required_inputs(domains):
    required = []
    if 'omics' in domains:
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
    if 'knowledge' in domains:
        required.append({'name': 'documents_dir', 'description': 'local scientific documents for retrieval'})
    return required


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


def research_plan(task, domains=None):
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task must be a non-empty string')
    selected = _select_domains(task, domains)
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
            'required_inputs': _required_inputs(selected),
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
    return {
        'status': 'planned',
        'application': 'bioinformatics-research-agent',
        'task': task.strip(),
        'selected_domains': selected,
        'capabilities': capabilities,
        'required_inputs': _required_inputs(selected),
        'steps': steps,
        'policy': {
            'llm_may_select_tools': True,
            'llm_may_invent_measurements': False,
            'execution_requires_validated_workflow': True,
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
        'description': 'Build a traceable research plan from a scientific task without inventing measurements.',
        'parameters': _parameters({
            'task': {'type': 'string'},
            'domains': {'type': 'array', 'items': {'type': 'string'}},
        }, ('task',)),
        'function': research_plan,
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
