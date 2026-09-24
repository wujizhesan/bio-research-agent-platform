"""Run a trusted built-in tool without rebuilding the discovery registry."""

from importlib import import_module

try:
    from .external_service_policy import ServiceRetryDeferredError
    from .observability import bind_context, log_event
    from .plugin_manager import is_domain_enabled
    from .plugin_security import PluginSecurityError, enforce_plugin_boundary
    from .run_context import (
        bind_run_context,
        build_run_context,
        current_run_context,
        derive_run_context,
    )
except ImportError:
    from external_service_policy import ServiceRetryDeferredError
    from observability import bind_context, log_event
    from plugin_manager import is_domain_enabled
    from plugin_security import PluginSecurityError, enforce_plugin_boundary
    from run_context import (
        bind_run_context,
        build_run_context,
        current_run_context,
        derive_run_context,
    )


_BUILTIN_MODULES = {
    'knowledge': 'knowledge_plugin',
    'literature': 'literature_plugin',
    'omics': 'omics_agent',
}


def resolve_scoped_tool(name, descriptor):
    domain = descriptor.get('domain')
    prefix = f'{domain}_'
    spec = descriptor.get('spec') or {}
    if (
        domain not in _BUILTIN_MODULES
        or not name.startswith(prefix)
        or spec.get('name') != name
        or spec.get('domain') != domain
        or (spec.get('plugin_security') or {}).get('trust') != 'trusted'
    ):
        return None
    module_name = _BUILTIN_MODULES[domain]
    source = import_module(
        f'.{module_name}' if __package__ else module_name,
        package=__package__ or None,
    )
    local_name = name[len(prefix):]
    tool = source.TOOLS.get(local_name)
    return (domain, local_name, tool, spec) if tool is not None else None


def run_scoped_tool(name, arguments, descriptor, resolved=None):
    resolved = resolved or resolve_scoped_tool(name, descriptor)
    if resolved is None or not isinstance(arguments, dict):
        return {'status': 'error', 'error': f'unknown domain tool: {name}'}
    domain, local_name, tool, spec = resolved
    try:
        if not is_domain_enabled(domain):
            return {
                'status': 'error',
                'domain': domain,
                'error': f'plugin domain is disabled: {domain}',
            }
    except ValueError as exc:
        return {'status': 'error', 'domain': domain, 'error': str(exc)}
    parent_context = current_run_context()
    execution_context = (
        derive_run_context(parent_context, name, arguments, spec=spec)
        if parent_context is not None
        else build_run_context(name, arguments, spec=spec)
    )
    with bind_run_context(execution_context), bind_context(tool=name, plugin=domain):
        log_event('tool.execution.started')
        try:
            with enforce_plugin_boundary(
                domain,
                spec['plugin_security'],
                spec['permissions'],
                arguments,
            ):
                result = tool['function'](**arguments)
        except PluginSecurityError as exc:
            return {
                'status': 'error',
                'domain': domain,
                'tool': local_name,
                'error_type': 'plugin_security',
                'error': str(exc),
            }
        except ServiceRetryDeferredError:
            raise
        except Exception as exc:
            return {
                'status': 'error',
                'domain': domain,
                'tool': local_name,
                'error': str(exc),
            }
        return result
