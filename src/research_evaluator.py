"""Offline evaluation for deterministic research planning and workflow construction."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from .research_agent import research_build_workflow
    from .workflow_runner import run_workflow
except ImportError:
    from research_agent import research_build_workflow
    from workflow_runner import run_workflow


DEFAULT_SUITE_PATH = Path(__file__).resolve().parents[1] / 'examples' / 'evaluation' / 'research_tasks.json'


def _load_suite(source=None):
    source = source or DEFAULT_SUITE_PATH
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding='utf-8'))
    else:
        payload = source
    tasks = payload.get('tasks') if isinstance(payload, dict) else payload
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('evaluation suite must contain a non-empty tasks array')
    normalized = []
    seen = set()
    for raw in tasks:
        if not isinstance(raw, dict):
            raise ValueError('each evaluation task must be an object')
        task_id = str(raw.get('id', '')).strip()
        task = str(raw.get('task', '')).strip()
        expected = raw.get('expected')
        if not task_id or task_id in seen:
            raise ValueError(f'invalid or duplicate evaluation task id: {task_id}')
        if not task or not isinstance(expected, dict):
            raise ValueError(f'evaluation task requires task text and expected result: {task_id}')
        seen.add(task_id)
        normalized.append({
            'id': task_id,
            'task': task,
            'inputs': dict(raw.get('inputs') or {}),
            'expected': expected,
        })
    return normalized


def _set_metrics(actual, expected):
    actual_set = set(actual)
    expected_set = set(expected)
    overlap = len(actual_set & expected_set)
    precision = overlap / len(actual_set) if actual_set else float(not expected_set)
    recall = overlap / len(expected_set) if expected_set else float(not actual_set)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        'precision': round(precision, 4),
        'recall': round(recall, 4),
        'f1': round(f1, 4),
        'exact': actual_set == expected_set,
    }


def _validate_preview(plan):
    preview = plan.get('workflow_preview')
    if preview is None:
        return {
            'valid': not plan.get('ready') and not plan.get('selected_tools'),
            'status': 'not_planned',
            'steps': 0,
        }
    try:
        manifest = run_workflow(preview, dry_run=True)
    except Exception as exc:
        return {
            'valid': False,
            'status': 'invalid',
            'steps': len(preview.get('steps', [])),
            'error': f'{type(exc).__name__}: {exc}',
        }
    return {
        'valid': manifest.get('status') == 'planned',
        'status': manifest.get('status'),
        'steps': len(manifest.get('steps', [])),
        'failed_steps': manifest.get('failed_steps', 0),
    }


def evaluate_task(case, planner_mode='deterministic'):
    expected = case['expected']
    try:
        plan = research_build_workflow(
            case['task'],
            case['inputs'],
            planner_mode=planner_mode,
        )
        domains = list(plan.get('selected_domains') or [])
        tools = list(plan.get('selected_tools') or [])
        missing = sorted(plan.get('missing_inputs') or [])
        domain_metrics = _set_metrics(domains, expected.get('domains', []))
        tool_metrics = _set_metrics(tools, expected.get('tools', []))
        readiness_match = bool(plan.get('ready')) is bool(expected.get('ready'))
        missing_match = missing == sorted(expected.get('missing_inputs', []))
        workflow = _validate_preview(plan)
        passed = all((
            domain_metrics['exact'],
            tool_metrics['exact'],
            readiness_match,
            missing_match,
            workflow['valid'],
        ))
        return {
            'id': case['id'],
            'task': case['task'],
            'passed': passed,
            'predicted': {
                'domains': domains,
                'tools': tools,
                'ready': bool(plan.get('ready')),
                'missing_inputs': missing,
            },
            'expected': {
                'domains': list(expected.get('domains', [])),
                'tools': list(expected.get('tools', [])),
                'ready': bool(expected.get('ready')),
                'missing_inputs': sorted(expected.get('missing_inputs', [])),
            },
            'metrics': {
                'domains': domain_metrics,
                'tools': tool_metrics,
                'readiness_match': readiness_match,
                'missing_inputs_match': missing_match,
                'workflow_valid': workflow['valid'],
            },
            'workflow': workflow,
        }
    except Exception as exc:
        return {
            'id': case['id'],
            'task': case['task'],
            'passed': False,
            'expected': expected,
            'error': f'{type(exc).__name__}: {exc}',
        }


def evaluate_suite(source=None, planner_mode='deterministic'):
    tasks = _load_suite(source)
    results = [evaluate_task(case, planner_mode=planner_mode) for case in tasks]
    total = len(results)

    def mean(path):
        values = []
        for item in results:
            value = item
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            values.append(float(value or 0))
        return round(sum(values) / total, 4)

    passed = sum(item['passed'] for item in results)
    summary = {
        'tasks': total,
        'passed': passed,
        'failed': total - passed,
        'task_pass_rate': round(passed / total, 4),
        'domain_exact_accuracy': mean(('metrics', 'domains', 'exact')),
        'domain_macro_f1': mean(('metrics', 'domains', 'f1')),
        'tool_exact_accuracy': mean(('metrics', 'tools', 'exact')),
        'tool_macro_f1': mean(('metrics', 'tools', 'f1')),
        'readiness_accuracy': mean(('metrics', 'readiness_match')),
        'missing_inputs_accuracy': mean(('metrics', 'missing_inputs_match')),
        'workflow_valid_rate': mean(('metrics', 'workflow_valid')),
    }
    return {
        'status': 'passed' if passed == total else 'failed',
        'suite_version': 1,
        'planner_mode': planner_mode,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'summary': summary,
        'results': results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Evaluate research Agent planning without executing scientific tools')
    parser.add_argument('--suite', default=str(DEFAULT_SUITE_PATH))
    parser.add_argument('--planner-mode', choices=('deterministic', 'auto', 'llm'), default='deterministic')
    parser.add_argument('--out')
    parser.add_argument('--allow-failures', action='store_true')
    args = parser.parse_args(argv)
    result = evaluate_suite(args.suite, planner_mode=args.planner_mode)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + '\n', encoding='utf-8')
    print(text)
    raise SystemExit(0 if result['status'] == 'passed' or args.allow_failures else 1)


if __name__ == '__main__':
    main()
