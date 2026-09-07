"""Standard manifest and validation helpers for discoverable domain plugins."""
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import re

from jsonschema.validators import validator_for
from packaging.requirements import InvalidRequirement, Requirement

try:
    from .resource_scheduling import ResourceRequest
    from .plugin_security import (
        normalize_permissions,
        permission_grant_report,
        security_profile,
        validate_security_profile,
    )
except ImportError:
    from resource_scheduling import ResourceRequest
    from plugin_security import (
        normalize_permissions,
        permission_grant_report,
        security_profile,
        validate_security_profile,
    )


MANIFEST_VERSION = 2
SUPPORTED_API_VERSION = 1
VALID_KINDS = {
    'builtin',
    'external',
    'application',
    'builtin_adapter',
    'entry_point',
}
VALID_STATUSES = {'available', 'unavailable', 'disabled', 'error'}
SEMVER_PATTERN = re.compile(
    r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
    r'(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$'
)
ENTRYPOINT_PATTERN = re.compile(
    r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*'
    r'(?::[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?$'
)


def validate_json_schema(schema, label):
    if not isinstance(schema, dict):
        raise ValueError(f'{label} must be a JSON Schema mapping')
    try:
        validator_for(schema).check_schema(schema)
    except Exception as exc:
        raise ValueError(f'invalid {label}: {exc}') from exc
    return schema


def _contract_digest(manifest):
    contract = {
        'manifest_version': manifest['manifest_version'],
        'key': manifest['key'],
        'version': manifest['version'],
        'api_version': manifest['api_version'],
        'requirements': manifest['requirements'],
        'security': manifest['security'],
        'tool_contracts': manifest['tool_contracts'],
    }
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def check_requirements(requirements):
    missing = []
    incompatible = []
    installed = {}
    for raw in requirements:
        requirement = Requirement(raw)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            current = package_version(requirement.name)
        except PackageNotFoundError:
            missing.append(str(requirement))
            continue
        installed[requirement.name] = current
        if requirement.specifier and current not in requirement.specifier:
            incompatible.append({
                'requirement': str(requirement),
                'installed': current,
            })
    return {
        'compatible': not missing and not incompatible,
        'missing': missing,
        'incompatible': incompatible,
        'installed': installed,
    }


def validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError('plugin manifest must be a mapping')
    if manifest.get('manifest_version') != MANIFEST_VERSION:
        raise ValueError(
            f"unsupported plugin manifest version: {manifest.get('manifest_version')}"
        )
    key = manifest.get('key')
    if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]*', key):
        raise ValueError('plugin manifest key must be a lowercase identifier')
    for field in ('name', 'version'):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ValueError(f'plugin manifest needs a non-empty {field}')
    if manifest.get('status') == 'available' and not SEMVER_PATTERN.fullmatch(manifest['version']):
        raise ValueError('available plugin version must use semantic versioning')
    api_version = manifest.get('api_version')
    if not isinstance(api_version, int) or isinstance(api_version, bool):
        raise ValueError('plugin manifest api_version must be an integer')
    if api_version != SUPPORTED_API_VERSION:
        raise ValueError(
            f'unsupported plugin API version: {api_version}; '
            f'expected {SUPPORTED_API_VERSION}'
        )
    if manifest.get('kind') not in VALID_KINDS:
        raise ValueError(f"unsupported plugin kind: {manifest.get('kind')}")
    if manifest.get('status') not in VALID_STATUSES:
        raise ValueError(f"unsupported plugin status: {manifest.get('status')}")
    for field in ('domains', 'capabilities', 'tools'):
        values = manifest.get(field)
        if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
            raise ValueError(f'plugin manifest {field} must be a list of strings')
        if len(values) != len(set(values)):
            raise ValueError(f'plugin manifest {field} must not contain duplicates')
    if not isinstance(manifest.get('tool_count'), int) or manifest['tool_count'] != len(manifest['tools']):
        raise ValueError('plugin manifest tool_count does not match tools')
    if not isinstance(manifest.get('entrypoint'), str) or not manifest['entrypoint'].strip():
        raise ValueError('plugin manifest needs an entrypoint')
    if not ENTRYPOINT_PATTERN.fullmatch(manifest['entrypoint']):
        raise ValueError('plugin manifest entrypoint must be a Python module path')
    requirements = manifest.get('requirements')
    if not isinstance(requirements, list) or any(not isinstance(item, str) or not item.strip() for item in requirements):
        raise ValueError('plugin manifest requirements must be a list of strings')
    try:
        for requirement in requirements:
            parsed = Requirement(requirement)
            if parsed.url:
                raise ValueError('plugin requirements must not use direct URLs or file references')
    except InvalidRequirement as exc:
        raise ValueError(f'invalid plugin requirement: {exc}') from exc
    validate_security_profile(manifest.get('security'), manifest.get('kind'))
    contracts = manifest.get('tool_contracts')
    if not isinstance(contracts, dict) or set(contracts) != set(manifest['tools']):
        raise ValueError('plugin manifest tool_contracts must match tools')
    for name, contract in contracts.items():
        if not isinstance(contract, dict):
            raise ValueError(f'plugin tool contract must be a mapping: {name}')
        validate_json_schema(contract.get('input'), f'{name} input schema')
        validate_json_schema(contract.get('output'), f'{name} output schema')
        ResourceRequest.from_mapping(contract.get('resources'))
        normalize_permissions(contract.get('permissions'), contract.get('input'))
    digest = manifest.get('contract_digest')
    if not isinstance(digest, str) or digest != _contract_digest(manifest):
        raise ValueError('plugin manifest contract_digest is invalid')
    return manifest


def build_manifest(key, plugin, tools, kind, status='available', domains=None,
                   health=None):
    if not isinstance(tools, dict) or (not tools and status not in {'unavailable', 'error'}):
        raise ValueError(f'plugin {key} must expose a non-empty TOOLS mapping')
    declared = getattr(plugin, 'PLUGIN_MANIFEST', {}) or {}
    if not isinstance(declared, dict):
        raise ValueError(f'plugin {key} PLUGIN_MANIFEST must be a mapping')
    module_name = getattr(plugin, '__name__', plugin.__class__.__module__)
    capabilities = getattr(plugin, 'PLUGIN_CAPABILITIES', ())
    if isinstance(capabilities, str):
        capabilities = (capabilities,)
    requirements = declared.get('requirements', getattr(plugin, 'PLUGIN_REQUIREMENTS', ()))
    if isinstance(requirements, str):
        requirements = (requirements,)
    tool_contracts = {
        name: {
            'input': dict(spec['parameters']),
            'output': dict(spec.get('returns') or spec.get('result_schema') or {}),
            'resources': ResourceRequest.from_mapping(spec.get('resources')).as_dict(),
            'permissions': normalize_permissions(
                spec.get('permissions'),
                spec.get('parameters'),
            ),
        }
        for name, spec in tools.items()
    }
    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'key': key,
        'name': str(getattr(plugin, 'PLUGIN_NAME', key)),
        'version': str(getattr(plugin, 'PLUGIN_VERSION', '0.0.0')),
        'api_version': int(getattr(plugin, 'PLUGIN_API_VERSION', SUPPORTED_API_VERSION)),
        'kind': kind,
        'status': status,
        'entrypoint': module_name,
        'domains': list(domains or [key]),
        'capabilities': sorted({str(item) for item in capabilities}),
        'tools': sorted(tools),
        'tool_count': len(tools),
        'requirements': sorted({str(item) for item in requirements}),
        'security': security_profile(kind),
        'tool_contracts': tool_contracts,
    }
    manifest.update(declared)
    manifest.update({
        'key': key,
        'kind': kind,
        'status': status,
        'entrypoint': str(declared.get('entrypoint', module_name)),
        'domains': list(declared.get('domains', domains or [key])),
        'capabilities': sorted({str(item) for item in declared.get('capabilities', capabilities)}),
        'tools': sorted(tools),
        'tool_count': len(tools),
        'requirements': sorted({str(item) for item in declared.get('requirements', requirements)}),
        'security': security_profile(kind),
        'tool_contracts': tool_contracts,
    })
    if health:
        manifest['health'] = dict(health)
    manifest['contract_digest'] = _contract_digest(manifest)
    return validate_manifest(manifest)


def validate_install_candidate(manifest):
    try:
        validate_manifest(manifest)
        requirements = check_requirements(manifest['requirements'])
        permissions = permission_grant_report(manifest)
    except (TypeError, ValueError) as exc:
        return {
            'compatible': False,
            'errors': [str(exc)],
            'requirements': None,
            'permissions': None,
        }
    errors = []
    if requirements['missing']:
        errors.append('missing requirements: ' + ', '.join(requirements['missing']))
    if requirements['incompatible']:
        errors.append('incompatible requirements: ' + ', '.join(
            item['requirement'] for item in requirements['incompatible']
        ))
    for capability, denied in permissions['denied'].items():
        if denied:
            errors.append(
                f'unapproved {capability} permissions: ' + ', '.join(denied)
            )
    return {
        'compatible': not errors,
        'errors': errors,
        'requirements': requirements,
        'permissions': permissions,
        'contract_digest': manifest['contract_digest'],
    }
