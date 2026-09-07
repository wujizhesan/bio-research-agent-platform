"""Traceable workflow orchestration over the unified bioinformatics tool registry."""
import argparse
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError as exc:
    raise SystemExit('workflow runner requires PyYAML and jsonschema') from exc

try:
    from .domain_registry import run_tool, active_tool_specs
    from .workflow_checkpoint import (
        CHECKPOINT_VERSION,
        CheckpointStore,
        artifacts_valid,
        collect_artifacts,
        digest_value,
        step_fingerprint,
        workflow_fingerprint,
    )
    from .reproducibility import (
        REPRODUCIBILITY_VERSION,
        finalize_manifest,
        runtime_snapshot,
        step_provenance,
    )
    from .observability import (
        WORKFLOW_ACTIVE,
        WORKFLOW_DURATION,
        WORKFLOW_RUNS,
        WORKFLOW_STEP_DURATION,
        WORKFLOW_STEPS,
        bind_context,
        current_context,
        log_event,
    )
except ImportError:
    from domain_registry import run_tool, active_tool_specs
    from workflow_checkpoint import (
        CHECKPOINT_VERSION,
        CheckpointStore,
        artifacts_valid,
        collect_artifacts,
        digest_value,
        step_fingerprint,
        workflow_fingerprint,
    )
    from reproducibility import (
        REPRODUCIBILITY_VERSION,
        finalize_manifest,
        runtime_snapshot,
        step_provenance,
    )
    from observability import (
        WORKFLOW_ACTIVE,
        WORKFLOW_DURATION,
        WORKFLOW_RUNS,
        WORKFLOW_STEP_DURATION,
        WORKFLOW_STEPS,
        bind_context,
        current_context,
        log_event,
    )


_REFERENCE = re.compile(r'\$\{([^}]+)\}')
_FAILURE_STATUSES = {'error', 'missing', 'not_found'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def load_workflow(source):
    if isinstance(source, (str, Path)):
        path = Path(source)
        text = path.read_text(encoding='utf-8')
        data = yaml.safe_load(text) if path.suffix.lower() in {'.yaml', '.yml'} else json.loads(text)
    else:
        data = copy.deepcopy(source)
    if not isinstance(data, dict):
        raise ValueError('workflow must be an object')
    steps = data.get('steps')
    if not isinstance(steps, list) or not steps:
        raise ValueError('workflow steps must be a non-empty list')
    return data


def _lookup(context, expression):
    parts = expression.split('.')
    if len(parts) < 2 or parts[0] not in context:
        raise ValueError(f'unknown workflow reference: {expression}')
    value = context[parts[0]]
    for part in parts[1:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError(f'unknown workflow reference: {expression}')
    return value


def _resolve(value, context):
    if isinstance(value, dict):
        return {key: _resolve(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, context) for item in value]
    if not isinstance(value, str):
        return value
    match = _REFERENCE.fullmatch(value)
    if match:
        return _lookup(context, match.group(1))
    return _REFERENCE.sub(lambda item: str(_lookup(context, item.group(1))), value)


def _validate_step(step, specs, seen_ids, allowed_tools):
    if not isinstance(step, dict):
        raise ValueError('each workflow step must be an object')
    step_id = step.get('id')
    tool = step.get('tool')
    args = step.get('args', {})
    if not isinstance(step_id, str) or not step_id:
        raise ValueError('workflow step id must be a non-empty string')
    if step_id in seen_ids:
        raise ValueError(f'duplicate workflow step id: {step_id}')
    if not isinstance(tool, str) or tool not in specs:
        raise ValueError(f'unknown workflow tool: {tool}')
    if allowed_tools is not None and tool not in allowed_tools:
        raise ValueError(f'workflow tool is not allowed: {tool}')
    if not isinstance(args, dict):
        raise ValueError(f'workflow args must be an object: {step_id}')
    dependencies = step.get('depends_on', [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise ValueError(f'depends_on must be a list of strings: {step_id}')
    return step_id, tool, args, dependencies


def _validate_args(tool, args, specs):
    errors = sorted(
        Draft202012Validator(specs[tool]['parameters']).iter_errors(args),
        key=lambda error: list(error.path),
    )
    if errors:
        details = '; '.join(error.message for error in errors[:3])
        raise ValueError(f'invalid arguments for {tool}: {details}')


def _context_value(result, arguments, dry_run):
    if not dry_run:
        return result
    return {**result, 'result': {**arguments, 'status': 'planned'}, **arguments}


def _reuse_reason(previous, fingerprint):
    if previous is None:
        return None
    if previous.get('status') != 'completed':
        return 'previous step did not complete'
    if previous.get('fingerprint') != fingerprint:
        return 'step inputs or plugin contract changed'
    if not artifacts_valid(previous.get('artifacts')):
        return 'checkpoint artifacts are missing or changed'
    if 'result' not in previous:
        return 'checkpoint result is missing'
    return 'reusable'


def _run_workflow(workflow, output_path=None, dry_run=False, max_steps=32,
                  allowed_tools=None, continue_on_error=False, resume=False,
                  checkpoint=None):
    workflow = load_workflow(workflow)
    specs = {spec['name']: spec for spec in active_tool_specs()}
    steps = workflow['steps']
    if len(steps) > max_steps:
        raise ValueError(f'workflow exceeds max_steps={max_steps}')
    allowed = set(allowed_tools) if allowed_tools is not None else None
    seen_ids = set()
    normalized_steps = []
    for step in steps:
        step_id, tool, args, dependencies = _validate_step(step, specs, seen_ids, allowed)
        normalized_steps.append((step_id, tool, args, dependencies))
        seen_ids.add(step_id)

    checkpoint = checkpoint or CheckpointStore(output_path)
    previous_manifest = checkpoint.load() if resume else None
    previous_steps = {
        step.get('id'): step
        for step in (previous_manifest or {}).get('steps', [])
        if isinstance(step, dict) and isinstance(step.get('id'), str)
    }
    definition_fingerprint = workflow_fingerprint(
        workflow,
        dry_run=dry_run,
        allowed_tools=allowed,
    )
    environment = runtime_snapshot()
    recipe = {
        'workflow': copy.deepcopy(workflow),
        'dry_run': bool(dry_run),
        'allowed_tools': sorted(allowed) if allowed is not None else None,
    }
    manifest = {
        'checkpoint_version': CHECKPOINT_VERSION,
        'run_id': (
            previous_manifest.get('run_id')
            if previous_manifest and previous_manifest.get('run_id')
            else uuid4().hex
        ),
        'workflow': workflow.get('name', 'unnamed'),
        'workflow_fingerprint': definition_fingerprint,
        'status': 'running',
        'created_at': (
            previous_manifest.get('created_at')
            if previous_manifest and previous_manifest.get('created_at')
            else _now()
        ),
        'updated_at': _now(),
        'dry_run': bool(dry_run),
        'resume_requested': bool(resume),
        'resume_count': int((previous_manifest or {}).get('resume_count', 0)) + bool(previous_manifest),
        'resumed_steps': 0,
        'executed_steps': 0,
        'invalidated_steps': [],
        'steps': [],
        'observability': current_context(),
        'reproducibility': {
            'version': REPRODUCIBILITY_VERSION,
            'hash_algorithm': 'sha256',
            'canonicalization': 'json-sort-keys',
            'environment': environment,
            'recipe': recipe,
            'recipe_fingerprint': definition_fingerprint,
        },
    }
    if previous_manifest:
        manifest['resumed_from'] = str(checkpoint.path)
    if output_path:
        manifest['manifest_path'] = str(Path(output_path))
    checkpoint.write(manifest)
    log_event(
        'workflow.started',
        run_id=manifest['run_id'],
        workflow=manifest['workflow'],
        dry_run=bool(dry_run),
    )
    context = {}
    fingerprints = {}
    input_file_cache = {}
    for step_id, tool, raw_args, dependencies in normalized_steps:
        step_started = perf_counter()
        trace = {
            'id': step_id,
            'tool': tool,
            'depends_on': dependencies,
            'status': 'running',
            'reused': False,
            'started_at': _now(),
        }
        try:
            for dependency in dependencies:
                if dependency not in context:
                    raise ValueError(f'dependency has not completed: {dependency}')
            args = _resolve(raw_args, context)
            _validate_args(tool, args, specs)
            trace['resolved_args'] = args
            provenance = step_provenance(
                args,
                specs[tool],
                environment['fingerprint'],
                file_cache=input_file_cache,
            )
            trace['reproducibility'] = provenance
            fingerprint = step_fingerprint(
                tool,
                args,
                specs[tool],
                {dependency: fingerprints[dependency] for dependency in dependencies},
                dry_run=dry_run,
                file_cache=input_file_cache,
                environment_fingerprint=environment['fingerprint'],
                implementation_fingerprint=provenance['plugin']['fingerprint'],
            )
            trace['fingerprint'] = fingerprint
            previous = previous_steps.get(step_id)
            reason = _reuse_reason(previous, fingerprint) if resume else None
            if reason == 'reusable':
                trace = copy.deepcopy(previous)
                trace.update({
                    'reused': True,
                    'resumed_at': _now(),
                })
                manifest['steps'].append(trace)
                manifest['resumed_steps'] += 1
                context[step_id] = _context_value(
                    trace['result'],
                    trace.get('resolved_args', args),
                    dry_run,
                )
                fingerprints[step_id] = fingerprint
                manifest['updated_at'] = _now()
                checkpoint.write(manifest)
                elapsed = perf_counter() - step_started
                WORKFLOW_STEPS.labels(tool, 'reused').inc()
                WORKFLOW_STEP_DURATION.labels(tool).observe(elapsed)
                log_event(
                    'workflow.step.completed',
                    run_id=manifest['run_id'],
                    step_id=step_id,
                    tool=tool,
                    status='reused',
                    duration_seconds=elapsed,
                )
                continue
            if previous is not None:
                manifest['invalidated_steps'].append({
                    'id': step_id,
                    'reason': reason,
                })
            trace['attempt'] = int((previous or {}).get('attempt', 0)) + 1
            manifest['steps'].append(trace)
            manifest['executed_steps'] += 1
            manifest['updated_at'] = _now()
            checkpoint.write(manifest)
            with bind_context(
                run_id=manifest['run_id'],
                step_id=step_id,
                tool=tool,
            ):
                log_event('workflow.step.started')
                result = {'status': 'planned'} if dry_run else run_tool(tool, args)
            trace['result'] = result
            failed = isinstance(result, dict) and result.get('status') in _FAILURE_STATUSES
            trace['status'] = 'failed' if failed else 'completed'
            trace['finished_at'] = _now()
            trace['artifacts'] = collect_artifacts(result)
            trace['reproducibility']['result_digest'] = digest_value(result)
            context[step_id] = _context_value(result, args, dry_run)
            fingerprints[step_id] = fingerprint
            manifest['updated_at'] = _now()
            checkpoint.write(manifest)
            elapsed = perf_counter() - step_started
            outcome = trace['status'] if not dry_run else 'planned'
            WORKFLOW_STEPS.labels(tool, outcome).inc()
            WORKFLOW_STEP_DURATION.labels(tool).observe(elapsed)
            log_event(
                'workflow.step.completed',
                run_id=manifest['run_id'],
                step_id=step_id,
                tool=tool,
                status=outcome,
                duration_seconds=elapsed,
            )
            if failed and not continue_on_error:
                manifest['status'] = 'failed'
                break
        except Exception as exc:
            trace.update({'status': 'failed', 'error': str(exc), 'finished_at': _now()})
            if trace not in manifest['steps']:
                trace['attempt'] = int((previous_steps.get(step_id) or {}).get('attempt', 0)) + 1
                manifest['steps'].append(trace)
                manifest['executed_steps'] += 1
            manifest['updated_at'] = _now()
            checkpoint.write(manifest)
            elapsed = perf_counter() - step_started
            WORKFLOW_STEPS.labels(tool, 'failed').inc()
            WORKFLOW_STEP_DURATION.labels(tool).observe(elapsed)
            log_event(
                'workflow.step.failed',
                run_id=manifest['run_id'],
                step_id=step_id,
                tool=tool,
                error_type=type(exc).__name__,
                duration_seconds=elapsed,
            )
            if not continue_on_error:
                manifest['status'] = 'failed'
                break
    else:
        manifest['status'] = 'planned' if dry_run else 'completed'
    manifest['completed_steps'] = sum(step['status'] == 'completed' for step in manifest['steps'])
    manifest['failed_steps'] = sum(step['status'] == 'failed' for step in manifest['steps'])
    manifest['updated_at'] = _now()
    finalize_manifest(manifest)
    checkpoint.write(manifest)
    return manifest


def run_workflow(workflow, output_path=None, dry_run=False, max_steps=32,
                 allowed_tools=None, continue_on_error=False, resume=False):
    checkpoint = CheckpointStore(output_path)
    started = perf_counter()
    mode = str(bool(dry_run)).lower()
    result = None
    WORKFLOW_ACTIVE.inc()
    try:
        with checkpoint.lock():
            result = _run_workflow(
                workflow,
                output_path=output_path,
                dry_run=dry_run,
                max_steps=max_steps,
                allowed_tools=allowed_tools,
                continue_on_error=continue_on_error,
                resume=resume,
                checkpoint=checkpoint,
            )
        return result
    except Exception as exc:
        WORKFLOW_RUNS.labels('exception', mode).inc()
        log_event(
            'workflow.failed',
            error_type=type(exc).__name__,
            duration_seconds=perf_counter() - started,
        )
        raise
    finally:
        elapsed = perf_counter() - started
        WORKFLOW_ACTIVE.dec()
        WORKFLOW_DURATION.labels(mode).observe(elapsed)
        if result is not None:
            WORKFLOW_RUNS.labels(result['status'], mode).inc()
            log_event(
                'workflow.completed',
                run_id=result['run_id'],
                workflow=result['workflow'],
                status=result['status'],
                duration_seconds=elapsed,
                completed_steps=result['completed_steps'],
                failed_steps=result['failed_steps'],
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a traceable CADD/omics workflow')
    parser.add_argument('--workflow', required=True)
    parser.add_argument('--out', help='workflow manifest JSON path')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-steps', type=int, default=32)
    parser.add_argument('--allow-tool', action='append')
    parser.add_argument('--continue-on-error', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    result = run_workflow(
        args.workflow,
        output_path=args.out,
        dry_run=args.dry_run,
        max_steps=args.max_steps,
        allowed_tools=args.allow_tool,
        continue_on_error=args.continue_on_error,
        resume=args.resume,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if result['status'] in {'completed', 'planned'} else 1)


if __name__ == '__main__':
    main()
