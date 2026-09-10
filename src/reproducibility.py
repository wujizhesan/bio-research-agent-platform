"""Scientific workflow provenance capture and reproducibility verification."""

import inspect
import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    from .workflow_checkpoint import digest_value, file_sha256, fingerprint_inputs
except ImportError:
    from workflow_checkpoint import digest_value, file_sha256, fingerprint_inputs


REPRODUCIBILITY_VERSION = 1
SCIENTIFIC_DISTRIBUTIONS = (
    'numpy', 'pandas', 'scipy', 'scikit-learn', 'rdkit', 'meeko',
    'biopython', 'gemmi', 'matplotlib', 'PyYAML', 'jsonschema',
)
REPRODUCIBILITY_ENVIRONMENT = (
    'PYTHONHASHSEED', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
    'OPENBLAS_NUM_THREADS', 'CUDA_VISIBLE_DEVICES',
    'CUBLAS_WORKSPACE_CONFIG',
)
SEED_KEYS = {'seed', 'seeds', 'random_seed', 'random_seeds', 'random_state'}


def _runtime_fingerprint(snapshot):
    python = dict(snapshot.get('python', {}))
    python.pop('executable', None)
    return digest_value({
        'python': python,
        'system': snapshot.get('system', {}),
        'packages': snapshot.get('packages', {}),
        'environment': snapshot.get('environment', {}),
    })


def runtime_snapshot():
    packages = {}
    for distribution in SCIENTIFIC_DISTRIBUTIONS:
        try:
            packages[distribution] = version(distribution)
        except PackageNotFoundError:
            packages[distribution] = None
    environment = {
        key: os.environ[key]
        for key in REPRODUCIBILITY_ENVIRONMENT
        if key in os.environ
    }
    snapshot = {
        'python': {
            'version': platform.python_version(),
            'implementation': platform.python_implementation(),
            'executable': str(Path(sys.executable).resolve()),
        },
        'system': {
            'platform': platform.platform(),
            'machine': platform.machine(),
        },
        'packages': packages,
        'environment': environment,
    }
    snapshot['fingerprint'] = _runtime_fingerprint(snapshot)
    return snapshot


def implementation_snapshot(spec):
    function = spec.get('function')
    source_path = None
    try:
        source_path = inspect.getsourcefile(function) or inspect.getfile(function)
    except (TypeError, OSError):
        pass
    implementation = {
        'module': getattr(function, '__module__', None),
        'qualname': getattr(function, '__qualname__', None),
        'source_path': None,
        'source_sha256': None,
    }
    if source_path:
        path = Path(source_path).resolve()
        implementation['source_path'] = str(path)
        if path.is_file():
            implementation['source_sha256'] = file_sha256(path)
    return implementation


def plugin_snapshot(spec):
    plugin = {
        'domain': spec.get('domain'),
        'version': spec.get('plugin_version'),
        'api_version': spec.get('plugin_api_version'),
        'contract_digest': spec.get('plugin_contract_digest'),
        'implementation': implementation_snapshot(spec),
    }
    implementation = plugin['implementation']
    plugin['fingerprint'] = digest_value({
        'domain': plugin['domain'],
        'version': plugin['version'],
        'api_version': plugin['api_version'],
        'contract_digest': plugin['contract_digest'],
        'implementation': {
            'module': implementation['module'],
            'qualname': implementation['qualname'],
            'source_sha256': implementation['source_sha256'],
        },
    })
    return plugin


def _collect_seed_schema(schema, prefix=''):
    found = []
    if not isinstance(schema, dict):
        return found
    properties = schema.get('properties', {})
    if isinstance(properties, dict):
        for key, child in properties.items():
            path = f'{prefix}.{key}' if prefix else key
            if key.lower() in SEED_KEYS:
                found.append(path)
            found.extend(_collect_seed_schema(child, path))
    items = schema.get('items')
    if isinstance(items, dict):
        found.extend(_collect_seed_schema(items, f'{prefix}[]' if prefix else '[]'))
    return found


def _collect_seed_values(value, prefix=''):
    found = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f'{prefix}.{key}' if prefix else key
            if key.lower() in SEED_KEYS:
                found[path] = child
            found.update(_collect_seed_values(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f'{prefix}[{index}]'
            found.update(_collect_seed_values(child, path))
    return found


def seed_snapshot(arguments, parameter_schema):
    declared = _collect_seed_values(arguments)
    expected = sorted(set(_collect_seed_schema(parameter_schema)))
    missing = [path for path in expected if path not in declared]
    if declared:
        status = 'declared' if not missing else 'partially_declared'
    elif expected:
        status = 'unspecified'
    else:
        status = 'not_applicable'
    return {
        'status': status,
        'values': declared,
        'expected': expected,
        'missing': missing,
    }


def _file_records(value):
    records = []
    if isinstance(value, dict):
        if {'path', 'size', 'sha256'} <= value.keys():
            records.append({key: value[key] for key in ('path', 'size', 'sha256')})
        else:
            for child in value.values():
                records.extend(_file_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(_file_records(child))
    unique = {record['path']: record for record in records}
    return [unique[path] for path in sorted(unique)]


def step_provenance(arguments, spec, environment_fingerprint, *, file_cache=None):
    inputs = fingerprint_inputs(arguments, file_cache=file_cache)
    return {
        'inputs': inputs,
        'input_files': _file_records(inputs),
        'randomness': seed_snapshot(arguments, spec.get('parameters', {})),
        'plugin': plugin_snapshot(spec),
        'environment_fingerprint': environment_fingerprint,
    }


def run_fingerprint(manifest):
    return digest_value({
        'workflow_fingerprint': manifest.get('workflow_fingerprint'),
        'recipe_fingerprint': manifest.get('reproducibility', {}).get(
            'recipe_fingerprint'
        ),
        'environment_fingerprint': manifest.get('reproducibility', {}).get(
            'environment', {}
        ).get('fingerprint'),
        'steps': [
            {
                'id': step.get('id'),
                'fingerprint': step.get('fingerprint'),
                'inputs': step.get('reproducibility', {}).get('inputs'),
                'randomness': step.get('reproducibility', {}).get('randomness'),
                'plugin_fingerprint': step.get('reproducibility', {}).get(
                    'plugin', {}
                ).get('fingerprint'),
                'result_digest': step.get('reproducibility', {}).get('result_digest'),
                'artifacts': step.get('artifacts', []),
            }
            for step in manifest.get('steps', [])
        ],
    })


def finalize_manifest(manifest):
    reproducibility = manifest.setdefault('reproducibility', {})
    reproducibility['run_fingerprint'] = run_fingerprint(manifest)
    randomness = [
        step.get('reproducibility', {}).get('randomness', {})
        for step in manifest.get('steps', [])
    ]
    statuses = {item.get('status') for item in randomness if item}
    if not statuses:
        reproducibility['seed_status'] = 'unknown'
    elif statuses & {'unspecified', 'partially_declared'}:
        reproducibility['seed_status'] = 'unspecified'
    elif 'declared' in statuses:
        reproducibility['seed_status'] = 'recorded'
    else:
        reproducibility['seed_status'] = 'not_required'
    unspecified_steps = [
        step.get('id')
        for step in manifest.get('steps', [])
        if step.get('reproducibility', {}).get('randomness', {}).get('status')
        in {'unspecified', 'partially_declared'}
    ]
    reproducibility['warnings'] = (
        [{
            'code': 'random_seed_unspecified',
            'steps': unspecified_steps,
        }]
        if unspecified_steps else []
    )
    return manifest


def _verify_files(records, category):
    issues = []
    for record in records or []:
        path = Path(record.get('path', ''))
        if not path.is_file():
            issues.append({'category': category, 'path': str(path), 'reason': 'missing'})
            continue
        size = path.stat().st_size
        if size != record.get('size'):
            issues.append({
                'category': category,
                'path': str(path),
                'reason': 'size_changed',
                'expected': record.get('size'),
                'actual': size,
            })
            continue
        digest = file_sha256(path)
        if digest != record.get('sha256'):
            issues.append({
                'category': category,
                'path': str(path),
                'reason': 'sha256_changed',
                'expected': record.get('sha256'),
                'actual': digest,
            })
    return issues


def verify_manifest(source, *, check_environment=True, tool_specs=None):
    if isinstance(source, (str, Path)):
        import json
        path = Path(source)
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest_path = str(path.resolve())
    else:
        manifest = source
        manifest_path = None
    if not isinstance(manifest, dict):
        raise ValueError('reproducibility manifest must be an object')
    reproducibility = manifest.get('reproducibility', {})
    issues = []
    environment = reproducibility.get('environment', {})
    recorded_environment = environment.get('fingerprint')
    if recorded_environment != _runtime_fingerprint(environment):
        issues.append({
            'category': 'manifest',
            'reason': 'environment_snapshot_changed',
        })
    recipe = reproducibility.get('recipe')
    recipe_fingerprint = reproducibility.get('recipe_fingerprint')
    if recipe_fingerprint != digest_value(recipe):
        issues.append({
            'category': 'manifest',
            'reason': 'recipe_fingerprint_changed',
        })
    if recipe_fingerprint != manifest.get('workflow_fingerprint'):
        issues.append({
            'category': 'manifest',
            'reason': 'workflow_recipe_mismatch',
        })
    for step in manifest.get('steps', []):
        provenance = step.get('reproducibility', {})
        issues.extend(_verify_files(provenance.get('input_files'), 'input'))
        issues.extend(_verify_files(step.get('artifacts'), 'output'))
        if 'result' in step and provenance.get('result_digest') != digest_value(step['result']):
            issues.append({
                'category': 'manifest',
                'step': step.get('id'),
                'reason': 'result_digest_changed',
            })
    if check_environment:
        expected = recorded_environment
        actual = runtime_snapshot()['fingerprint']
        if expected != actual:
            issues.append({
                'category': 'environment',
                'reason': 'fingerprint_changed',
                'expected': expected,
                'actual': actual,
            })
    if tool_specs is not None:
        current = {spec.get('name'): plugin_snapshot(spec) for spec in tool_specs}
        for step in manifest.get('steps', []):
            expected = step.get('reproducibility', {}).get('plugin', {}).get('fingerprint')
            actual = current.get(step.get('tool'), {}).get('fingerprint')
            if expected != actual:
                issues.append({
                    'category': 'plugin',
                    'tool': step.get('tool'),
                    'reason': 'fingerprint_changed',
                    'expected': expected,
                    'actual': actual,
                })
    expected_run = reproducibility.get('run_fingerprint')
    actual_run = run_fingerprint(manifest)
    if expected_run != actual_run:
        issues.append({
            'category': 'manifest',
            'reason': 'run_fingerprint_changed',
            'expected': expected_run,
            'actual': actual_run,
        })
    return {
        'status': 'verified' if not issues else 'drift_detected',
        'reproducible': not issues,
        'manifest_path': manifest_path,
        'run_fingerprint': expected_run,
        'issues': issues,
    }


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(description='Verify a scientific workflow manifest')
    parser.add_argument('manifest')
    parser.add_argument('--skip-environment', action='store_true')
    parser.add_argument('--skip-plugins', action='store_true')
    args = parser.parse_args(argv)
    specs = None
    if not args.skip_plugins:
        try:
            from .domain_registry import active_tool_specs
        except ImportError:
            from domain_registry import active_tool_specs
        specs = active_tool_specs()
    result = verify_manifest(
        args.manifest,
        check_environment=not args.skip_environment,
        tool_specs=specs,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['reproducible'] else 1)


if __name__ == '__main__':
    main()
