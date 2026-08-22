"""Standard manifest and validation helpers for discoverable domain plugins."""
import re


MANIFEST_VERSION = 1
SUPPORTED_API_VERSION = 1
VALID_KINDS = {
    'builtin',
    'external',
    'application',
    'builtin_adapter',
    'entry_point',
}
VALID_STATUSES = {'available', 'unavailable', 'disabled', 'error'}


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
    })
    if health:
        manifest['health'] = dict(health)
    return validate_manifest(manifest)
