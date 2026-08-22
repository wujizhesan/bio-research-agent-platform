"""Traceable workflow orchestration over the unified bioinformatics tool registry."""
import argparse
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError as exc:
    raise SystemExit('workflow runner requires PyYAML and jsonschema') from exc

try:
    from .domain_registry import run_tool, active_tool_specs
except ImportError:
    from domain_registry import run_tool, active_tool_specs


_REFERENCE = re.compile(r'\$\{([^}]+)\}')
_FAILURE_STATUSES = {'error', 'missing', 'not_found'}


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


def run_workflow(workflow, output_path=None, dry_run=False, max_steps=32,
                 allowed_tools=None, continue_on_error=False):
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

    manifest = {
        'workflow': workflow.get('name', 'unnamed'),
        'status': 'running',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'dry_run': bool(dry_run),
        'steps': [],
    }
    context = {}
    for step_id, tool, raw_args, dependencies in normalized_steps:
        trace = {
            'id': step_id,
            'tool': tool,
            'depends_on': dependencies,
            'status': 'running',
        }
        try:
            for dependency in dependencies:
                if dependency not in context:
                    raise ValueError(f'dependency has not completed: {dependency}')
            args = _resolve(raw_args, context)
            _validate_args(tool, args, specs)
            trace['resolved_args'] = args
            result = {'status': 'planned'} if dry_run else run_tool(tool, args)
            trace['result'] = result
            failed = isinstance(result, dict) and result.get('status') in _FAILURE_STATUSES
            trace['status'] = 'failed' if failed else 'completed'
            context[step_id] = ({**result, 'result': {**args, 'status': 'planned'}, **args} if dry_run else result)
            manifest['steps'].append(trace)
            if failed and not continue_on_error:
                manifest['status'] = 'failed'
                break
        except Exception as exc:
            trace.update({'status': 'failed', 'error': str(exc)})
            manifest['steps'].append(trace)
            if not continue_on_error:
                manifest['status'] = 'failed'
                break
    else:
        manifest['status'] = 'planned' if dry_run else 'completed'
    manifest['completed_steps'] = sum(step['status'] == 'completed' for step in manifest['steps'])
    manifest['failed_steps'] = sum(step['status'] == 'failed' for step in manifest['steps'])
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        manifest['manifest_path'] = str(output_path)
        output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a traceable CADD/omics workflow')
    parser.add_argument('--workflow', required=True)
    parser.add_argument('--out', help='workflow manifest JSON path')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-steps', type=int, default=32)
    parser.add_argument('--allow-tool', action='append')
    parser.add_argument('--continue-on-error', action='store_true')
    args = parser.parse_args(argv)
    result = run_workflow(
        args.workflow,
        output_path=args.out,
        dry_run=args.dry_run,
        max_steps=args.max_steps,
        allowed_tools=args.allow_tool,
        continue_on_error=args.continue_on_error,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if result['status'] in {'completed', 'planned'} else 1)


if __name__ == '__main__':
    main()
