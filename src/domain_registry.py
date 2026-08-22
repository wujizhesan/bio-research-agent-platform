"""Unified and discoverable tool registry for the CADD and RNA-seq domain adapters."""
import argparse
import json
from importlib.metadata import entry_points
from types import SimpleNamespace

try:
    from . import agent as CADD_PLUGIN
    from . import omics_agent as OMICS_PLUGIN
    from . import research_agent as RESEARCH_PLUGIN
    from . import literature_plugin as LITERATURE_PLUGIN
    from . import knowledge_plugin as KNOWLEDGE_PLUGIN
except ImportError:
    import agent as CADD_PLUGIN
    import omics_agent as OMICS_PLUGIN
    import research_agent as RESEARCH_PLUGIN
    import literature_plugin as LITERATURE_PLUGIN
    import knowledge_plugin as KNOWLEDGE_PLUGIN

try:
    from .plugin_manifest import build_manifest
except ImportError:
    from plugin_manifest import build_manifest


def validate_tool_map(domain, tools):
    if not isinstance(tools, dict) or not tools:
        raise ValueError(f'domain {domain} must expose a non-empty TOOLS mapping')
    for name, spec in tools.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f'domain {domain} has an invalid tool name')
        if not isinstance(spec, dict):
            raise ValueError(f'domain {domain} tool {name} must be a mapping')
        if not isinstance(spec.get('description'), str):
            raise ValueError(f'domain {domain} tool {name} needs a description')
        if not isinstance(spec.get('parameters'), dict):
            raise ValueError(f'domain {domain} tool {name} needs parameters')
        if not callable(spec.get('function')):
            raise ValueError(f'domain {domain} tool {name} needs a callable function')
    return tools


def _discover_external_domains(group='cadd_agent.domains'):
    discovered = {}
    sources = {}
    errors = {}
    for entry_point in entry_points(group=group):
        name = entry_point.name
        try:
            loaded = entry_point.load()
            tools = getattr(loaded, 'TOOLS', loaded)
            validate_tool_map(name, tools)
            if name in DOMAIN_TOOLS or name in discovered:
                raise ValueError('duplicate domain entry point: ' + name)
            discovered[name] = tools
            sources[name] = loaded
        except Exception as exc:
            errors.setdefault(name, {
                'name': name,
                'kind': 'entry_point',
                'version': 'unknown',
                'status': 'error',
                'reason': type(exc).__name__ + ': ' + str(exc),
                'entrypoint': str(getattr(entry_point, 'value', name)),
            })
    return discovered, sources, errors

DOMAIN_TOOLS = {
    'cadd': validate_tool_map('cadd', CADD_PLUGIN.TOOLS),
    'omics': validate_tool_map('omics', OMICS_PLUGIN.TOOLS),
    'research': validate_tool_map('research', RESEARCH_PLUGIN.TOOLS),
    'literature': validate_tool_map('literature', LITERATURE_PLUGIN.TOOLS),
    'knowledge': validate_tool_map('knowledge', KNOWLEDGE_PLUGIN.TOOLS),
}
try:
    from . import sequence_plugin as SEQUENCE_PLUGIN
except ImportError:
    import sequence_plugin as SEQUENCE_PLUGIN

SEQUENCE_PLUGIN_NAME = SEQUENCE_PLUGIN.PLUGIN_NAME
SEQUENCE_PLUGIN_VERSION = SEQUENCE_PLUGIN.PLUGIN_VERSION
SEQUENCE_STATUS = SEQUENCE_PLUGIN.plugin_status()
SEQUENCE_TOOLS = SEQUENCE_PLUGIN.load_tools()
if SEQUENCE_TOOLS:
    DOMAIN_TOOLS['sequence'] = validate_tool_map('sequence', SEQUENCE_TOOLS)
EXTERNAL_DOMAIN_TOOLS, EXTERNAL_DOMAIN_SOURCES, EXTERNAL_DOMAIN_ERRORS = _discover_external_domains()
DOMAIN_TOOLS.update(EXTERNAL_DOMAIN_TOOLS)

DOMAIN_SOURCES = {
    'cadd': CADD_PLUGIN,
    'omics': OMICS_PLUGIN,
    'research': RESEARCH_PLUGIN,
    'literature': LITERATURE_PLUGIN,
    'knowledge': KNOWLEDGE_PLUGIN,
    'sequence': SEQUENCE_PLUGIN,
    **EXTERNAL_DOMAIN_SOURCES,
}

DOMAIN_METADATA = {
    'cadd': {
        'name': 'CADD',
        'kind': 'builtin',
        'version': 'builtin',
        'status': 'available',
    },
    'omics': {
        'name': 'Omics',
        'kind': 'builtin',
        'version': 'builtin',
        'status': 'available',
    },
    'sequence': {
        'name': SEQUENCE_PLUGIN_NAME,
        'kind': 'external',
        'version': SEQUENCE_PLUGIN_VERSION,
        'status': 'available' if SEQUENCE_TOOLS else 'unavailable',
        **SEQUENCE_STATUS,
    },
    'research': {
        'name': 'Bioinformatics Research Agent',
        'kind': 'application',
        'version': '0.1.0',
        'status': 'available',
    },
    'literature': {
        'name': 'Literature and evidence',
        'kind': 'builtin_adapter',
        'version': '0.1.0',
        'status': 'available',
    },
    'knowledge': {
        'name': 'Local scientific knowledge retrieval',
        'kind': 'builtin_adapter',
        'version': '0.1.0',
        'status': 'available',
    },
}
for domain in DOMAIN_TOOLS:
    DOMAIN_METADATA.setdefault(domain, {
        'name': domain,
        'kind': 'entry_point',
        'version': 'unknown',
        'status': 'available',
    })

DOMAIN_MANIFESTS = {}
for domain, tools in DOMAIN_TOOLS.items():
    metadata = DOMAIN_METADATA[domain]
    DOMAIN_MANIFESTS[domain] = build_manifest(
        domain,
        DOMAIN_SOURCES[domain],
        tools,
        kind=metadata.get('kind', 'entry_point'),
        status=metadata.get('status', 'available'),
        domains=[domain],
        health={key: value for key, value in metadata.items()
                if key in {'available', 'root', 'reason', 'missing'}},
    )
if 'sequence' not in DOMAIN_MANIFESTS:
    DOMAIN_MANIFESTS['sequence'] = build_manifest(
        'sequence',
        SEQUENCE_PLUGIN,
        SEQUENCE_TOOLS or {},
        kind='external',
        status='unavailable',
        domains=['sequence'],
        health=SEQUENCE_STATUS,
    )


for domain, failure in EXTERNAL_DOMAIN_ERRORS.items():
    if domain in DOMAIN_MANIFESTS:
        continue
    source = SimpleNamespace(
        __name__=failure['entrypoint'],
        PLUGIN_NAME=domain,
        PLUGIN_VERSION='unknown',
        PLUGIN_API_VERSION=1,
        PLUGIN_CAPABILITIES=(),
    )
    DOMAIN_MANIFESTS[domain] = build_manifest(
        domain, source, {}, kind='entry_point', status='error',
        domains=[domain], health={'reason': failure['reason']},
    )

def _qualified_name(domain, name):
    return f'{domain}_{name}'


def _split_name(name):
    for domain in DOMAIN_TOOLS:
        prefix = f'{domain}_'
        if name.startswith(prefix):
            return domain, name[len(prefix):]
    return None, None


def tool_specs(domain=None):
    domains = [domain] if domain else list(DOMAIN_TOOLS)
    if any(item not in DOMAIN_TOOLS for item in domains):
        raise ValueError(f'unknown domain: {domain}')
    specs = []
    for selected_domain in domains:
        for name, spec in DOMAIN_TOOLS[selected_domain].items():
            specs.append({
                'name': _qualified_name(selected_domain, name),
                'domain': selected_domain,
                'description': spec['description'],
                'parameters': spec['parameters'],
                'function': spec['function'],
            })
    return specs


def _plugin_enabled(domain):
    try:
        from .plugin_manager import is_domain_enabled
    except ImportError:
        from plugin_manager import is_domain_enabled
    return is_domain_enabled(domain)


def active_tool_specs(domain=None):
    return [
        spec for spec in tool_specs(domain)
        if _plugin_enabled(spec['domain'])
    ]


def active_domains():
    return tuple(domain for domain in DOMAIN_TOOLS if _plugin_enabled(domain))


def active_domain_catalog():
    try:
        from .plugin_manager import PluginManager
    except ImportError:
        from plugin_manager import PluginManager
    return PluginManager().list()


def available_domains():
    return tuple(DOMAIN_TOOLS)


def domain_catalog():
    catalog = []
    for domain, tools in DOMAIN_TOOLS.items():
        metadata = dict(DOMAIN_METADATA.get(domain, {}))
        metadata.update({
            'domain': domain,
            'status': DOMAIN_MANIFESTS[domain]['status'],
            'tool_count': len(tools),
            'tools': sorted(tools),
            'manifest': dict(DOMAIN_MANIFESTS[domain]),
        })
        catalog.append(metadata)
    if 'sequence' not in DOMAIN_TOOLS and SEQUENCE_STATUS:
        metadata = dict(DOMAIN_METADATA['sequence'])
        metadata.update({
            'domain': 'sequence',
            'status': DOMAIN_MANIFESTS['sequence']['status'],
            'tool_count': 0,
            'tools': [],
            'manifest': dict(DOMAIN_MANIFESTS['sequence']),
        })
        catalog.append(metadata)
    for domain, failure in EXTERNAL_DOMAIN_ERRORS.items():
        if domain in DOMAIN_TOOLS or domain not in DOMAIN_MANIFESTS:
            continue
        metadata = dict(failure)
        metadata.update({
            'domain': domain,
            'tool_count': 0,
            'tools': [],
            'manifest': dict(DOMAIN_MANIFESTS[domain]),
        })
        catalog.append(metadata)
    return catalog

def run_tool(name, args=None):
    domain, local_name = _split_name(name)
    if domain is None or local_name not in DOMAIN_TOOLS[domain]:
        return {'status': 'error', 'error': f'unknown domain tool: {name}'}
    try:
        try:
            from .plugin_manager import is_domain_enabled
        except ImportError:
            from plugin_manager import is_domain_enabled
        if not is_domain_enabled(domain):
            return {
                'status': 'error',
                'domain': domain,
                'error': f'plugin domain is disabled: {domain}',
            }
    except ValueError as exc:
        return {'status': 'error', 'domain': domain, 'error': str(exc)}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else {}
        except json.JSONDecodeError as exc:
            return {'status': 'error', 'error': f'invalid tool arguments: {exc}'}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {'status': 'error', 'error': 'tool arguments must be an object'}
    try:
        return DOMAIN_TOOLS[domain][local_name]['function'](**args)
    except Exception as exc:
        return {'status': 'error', 'domain': domain, 'tool': local_name, 'error': str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='List unified bioinformatics Agent tools')
    parser.add_argument('--domain', default='all')
    parser.add_argument('--catalog', action='store_true')
    args = parser.parse_args(argv)
    if args.catalog:
        print(json.dumps(active_domain_catalog(), ensure_ascii=False, indent=2))
        return
    selected = None if args.domain == 'all' else args.domain
    output = [
        {key: value for key, value in spec.items() if key != 'function'}
        for spec in active_tool_specs(selected)
    ]
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()