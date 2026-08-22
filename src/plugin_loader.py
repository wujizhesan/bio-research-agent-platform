"""Plugin loading, contracts, metadata, and entry-point discovery."""
import importlib
from dataclasses import dataclass
from importlib.metadata import entry_points


@dataclass(frozen=True)
class PluginContract:
    key: str
    default_module: str
    required: tuple[str, ...]
    api_version: int = 1


CADD_BACKEND_CONTRACTS = (
    PluginContract('receptor_backend', 'prepare_receptor', ('prepare',)),
    PluginContract('library_backend', 'build_library', ('build_library',)),
    PluginContract('docking_backend', 'dock_vina', ('dock_batch',)),
    PluginContract('ml_backend', 'ml_predictor', ('train_model', 'predict_activity')),
    PluginContract('report_backend', 'report', ('generate_report',)),
)


def _target_spec(spec):
    if spec is None or spec == '':
        return None, 'cadd_agent.plugins'
    if isinstance(spec, str):
        return spec, 'cadd_agent.plugins'
    if isinstance(spec, dict):
        target = spec.get('target') or spec.get('module') or spec.get('entry_point')
        if not target:
            raise ValueError('plugin spec requires target, module, or entry_point')
        return target, spec.get('group', 'cadd_agent.plugins')
    raise TypeError('plugin spec must be a string, mapping, or null')


def _load_entry_point(name, group):
    matches = list(entry_points(group=group, name=name))
    if not matches:
        raise ImportError(f'plugin entry point not found: {group}/{name}')
    if len(matches) > 1:
        raise ImportError(f'plugin entry point is ambiguous: {group}/{name}')
    return matches[0].load()


def load_plugin(spec, default_module=None):
    target, group = _target_spec(spec)
    if target is None:
        target = default_module
    if not target:
        raise ValueError('plugin target is required')
    if target.startswith('entrypoint:'):
        return _load_entry_point(target.split(':', 1)[1], group)
    if ':' in target:
        module_name, attribute = target.split(':', 1)
        module = importlib.import_module(module_name)
        return getattr(module, attribute)
    return importlib.import_module(target)


def require_callable(plugin, name):
    function = getattr(plugin, name, None)
    if not callable(function):
        raise TypeError(f'plugin must provide callable {name}()')
    return function


def validate_plugin(plugin, contract):
    declared_api = getattr(plugin, 'PLUGIN_API_VERSION', contract.api_version)
    if int(declared_api) != contract.api_version:
        raise ValueError(
            f'plugin {contract.key} API version {declared_api} '
            f'does not match {contract.api_version}'
        )
    for name in contract.required:
        require_callable(plugin, name)
    return plugin


def load_contract(contract, spec=None):
    plugin = load_plugin(spec, contract.default_module)
    return validate_plugin(plugin, contract)


def plugin_info(plugin, contract, spec=None):
    module_name = getattr(plugin, '__name__', plugin.__class__.__module__)
    capabilities = set(contract.required)
    extra_capabilities = getattr(plugin, 'PLUGIN_CAPABILITIES', ())
    if isinstance(extra_capabilities, str):
        extra_capabilities = (extra_capabilities,)
    capabilities.update(extra_capabilities)
    return {
        'key': contract.key,
        'spec': spec,
        'module': module_name,
        'name': getattr(plugin, 'PLUGIN_NAME', module_name),
        'version': str(getattr(plugin, 'PLUGIN_VERSION', 'builtin')),
        'api_version': int(getattr(plugin, 'PLUGIN_API_VERSION', contract.api_version)),
        'capabilities': sorted(str(item) for item in capabilities),
    }