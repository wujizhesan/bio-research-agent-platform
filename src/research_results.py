"""Result serialization for research application tools."""

from pathlib import Path


APPLICATION = 'bioinformatics-research-agent'


def catalog_result(application_version, domains):
    return {
        'status': 'ok',
        'application': APPLICATION,
        'application_version': application_version,
        'domains': domains,
        'policy': 'catalog-only; no computation or external network call',
    }


def presets_result(presets):
    return {
        'status': 'ok',
        'application': APPLICATION,
        'presets': [
            {'id': preset_id, **preset}
            for preset_id, preset in presets.items()
        ],
    }


def plan_result(
    task,
    selected,
    capabilities,
    required_inputs,
    execution,
    steps,
    planner,
):
    return {
        'status': 'planned',
        'application': APPLICATION,
        'task': task.strip(),
        'selected_domains': selected,
        'capabilities': capabilities,
        'required_inputs': required_inputs,
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


def workflow_result(task, selected, execution, planner):
    return {
        'status': 'planned',
        'application': APPLICATION,
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


def write_research_report(manifest, report_path):
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '# Bioinformatics Research Agent Report',
        '',
        f"- Workflow: {manifest.get('workflow', 'unnamed')}",
        f"- Status: {manifest.get('status', 'unknown')}",
        f"- Completed steps: {manifest.get('completed_steps', 0)}",
        f"- Failed steps: {manifest.get('failed_steps', 0)}",
        f"- Resumed steps: {manifest.get('resumed_steps', 0)}",
        f"- Reproducibility fingerprint: {manifest.get('reproducibility', {}).get('run_fingerprint', 'n/a')}",
        f"- Random seed status: {manifest.get('reproducibility', {}).get('seed_status', 'n/a')}",
        f"- Trace ID: {manifest.get('observability', {}).get('trace_id', 'n/a')}",
        f"- Job ID: {manifest.get('observability', {}).get('job_id', 'n/a')}",
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


def execution_result(
    manifest,
    selected,
    report,
    plugin_name,
    plugin_version,
    dry_run,
    resume,
):
    return {
        'status': manifest['status'],
        'application': APPLICATION,
        'selected_domains': selected,
        'manifest': manifest,
        'report': report,
        'manifest_path': manifest.get('manifest_path'),
        'report_path': report.get('path') if isinstance(report, dict) else None,
        'provenance': {
            'application': plugin_name,
            'version': plugin_version,
            'dry_run': dry_run,
            'resume': resume,
            'run_fingerprint': manifest.get('reproducibility', {}).get('run_fingerprint'),
            'seed_status': manifest.get('reproducibility', {}).get('seed_status'),
        },
    }
