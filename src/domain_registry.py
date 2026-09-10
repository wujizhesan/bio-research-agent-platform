"""Unified, discoverable registry for scientific domain tools."""

import argparse
import json
import os
from importlib.metadata import entry_points
from time import perf_counter
from types import SimpleNamespace

from jsonschema import ValidationError, validate

try:
    from . import agent as CADD_PLUGIN
    from . import imaging_plugin as IMAGING_PLUGIN
    from . import knowledge_plugin as KNOWLEDGE_PLUGIN
    from . import literature_plugin as LITERATURE_PLUGIN
    from . import omics_agent as OMICS_PLUGIN
    from . import research_agent as RESEARCH_PLUGIN
    from . import sequence_plugin as SEQUENCE_PLUGIN
    from .plugin_registry import DomainRegistry, validate_tool_map
    from .plugin_manifest import build_manifest, validate_install_candidate
    from .observability import (
        TOOL_ACTIVE,
        TOOL_DURATION,
        TOOL_EXECUTIONS,
        bind_context,
        log_event,
    )
    from .plugin_security import (
        PluginSecurityError,
        enforce_plugin_boundary,
        enforce_plugin_import_boundary,
    )
    from .run_context import (
        bind_run_context,
        build_run_context,
        current_run_context,
        derive_run_context,
    )
except ImportError:
    import agent as CADD_PLUGIN
    import imaging_plugin as IMAGING_PLUGIN
    import knowledge_plugin as KNOWLEDGE_PLUGIN
    import literature_plugin as LITERATURE_PLUGIN
    import omics_agent as OMICS_PLUGIN
    import research_agent as RESEARCH_PLUGIN
    import sequence_plugin as SEQUENCE_PLUGIN
    from plugin_registry import DomainRegistry, validate_tool_map
    from plugin_manifest import build_manifest, validate_install_candidate
    from observability import (
        TOOL_ACTIVE,
        TOOL_DURATION,
        TOOL_EXECUTIONS,
        bind_context,
        log_event,
    )
    from plugin_security import (
        PluginSecurityError,
        enforce_plugin_boundary,
        enforce_plugin_import_boundary,
    )
    from run_context import (
        bind_run_context,
        build_run_context,
        current_run_context,
        derive_run_context,
    )


ENTRY_POINT_GROUP = "cadd_agent.domains"


class PluginDependencyError(ValueError):
    pass

BUILTIN_DOMAINS = (
    (
        "cadd",
        CADD_PLUGIN,
        {"name": "CADD", "kind": "builtin", "version": "builtin"},
    ),
    (
        "omics",
        OMICS_PLUGIN,
        {"name": "Omics", "kind": "builtin", "version": "builtin"},
    ),
    (
        "research",
        RESEARCH_PLUGIN,
        {
            "name": "Bioinformatics Research Agent",
            "kind": "application",
            "version": "0.1.0",
        },
    ),
    (
        "literature",
        LITERATURE_PLUGIN,
        {
            "name": "Literature and evidence",
            "kind": "builtin_adapter",
            "version": "0.1.0",
        },
    ),
    (
        "knowledge",
        KNOWLEDGE_PLUGIN,
        {
            "name": "Local scientific knowledge retrieval",
            "kind": "builtin_adapter",
            "version": "0.1.0",
        },
    ),
    (
        "imaging",
        IMAGING_PLUGIN,
        {
            "name": "Microscopy and image QC",
            "kind": "builtin_adapter",
            "version": "0.1.0",
        },
    ),
)

BUILTIN_DOMAIN_NAMES = frozenset(
    [key for key, _, _ in BUILTIN_DOMAINS] + ["sequence"]
)


def _discover_external_domains(group=ENTRY_POINT_GROUP, reserved_domains=None):
    reserved = set(BUILTIN_DOMAIN_NAMES if reserved_domains is None else reserved_domains)
    discovered = {}
    sources = {}
    errors = {}
    sandbox_domain = os.environ.get('PLUGIN_SANDBOX_DOMAIN')
    for entry_point in entry_points(group=group):
        name = entry_point.name
        if sandbox_domain and name != sandbox_domain:
            continue
        try:
            distribution = getattr(entry_point, 'dist', None)
            locate_file = getattr(distribution, 'locate_file', None)
            package_roots = [locate_file('')] if callable(locate_file) else []
            with enforce_plugin_import_boundary(name, package_roots):
                loaded = entry_point.load()
            tools = getattr(loaded, "TOOLS", loaded)
            validate_tool_map(name, tools)
            candidate = build_manifest(name, loaded, tools, kind="entry_point")
            compatibility = validate_install_candidate(candidate)
            if not compatibility["compatible"]:
                raise PluginDependencyError("; ".join(compatibility["errors"]))
            if name in reserved or name in discovered:
                raise ValueError("duplicate domain entry point: " + name)
            discovered[name] = tools
            sources[name] = loaded
        except Exception as exc:
            errors.setdefault(
                name,
                {
                    "name": name,
                    "kind": "entry_point",
                    "version": "unknown",
                    "status": (
                        "unavailable"
                        if isinstance(exc, PluginDependencyError)
                        else "error"
                    ),
                    "reason": f"{type(exc).__name__}: {exc}",
                    "entrypoint": str(getattr(entry_point, "value", name)),
                },
            )
    return discovered, sources, errors


def _build_registry():
    registry = DomainRegistry()
    for domain, source, metadata in BUILTIN_DOMAINS:
        registry.register(
            domain,
            source,
            source.TOOLS,
            kind=metadata["kind"],
            metadata=metadata,
        )

    sequence_status = SEQUENCE_PLUGIN.plugin_status()
    sequence_tools = SEQUENCE_PLUGIN.load_tools()
    sequence_available = bool(sequence_tools)
    registry.register(
        "sequence",
        SEQUENCE_PLUGIN,
        sequence_tools or {},
        kind="external",
        status="available" if sequence_available else "unavailable",
        health=sequence_status,
        metadata={
            "name": SEQUENCE_PLUGIN.PLUGIN_NAME,
            "kind": "external",
            "version": SEQUENCE_PLUGIN.PLUGIN_VERSION,
            **sequence_status,
        },
    )

    discovered, sources, errors = _discover_external_domains(
        reserved_domains=registry.domains
    )
    for domain, tools in discovered.items():
        registry.register(
            domain,
            sources[domain],
            tools,
            kind="entry_point",
            metadata={
                "name": domain,
                "kind": "entry_point",
                "version": "unknown",
            },
        )
    for domain, failure in errors.items():
        source = SimpleNamespace(
            __name__=failure["entrypoint"],
            PLUGIN_NAME=domain,
            PLUGIN_VERSION="unknown",
            PLUGIN_API_VERSION=1,
            PLUGIN_CAPABILITIES=(),
        )
        registry.register(
            domain,
            source,
            {},
            kind="entry_point",
            status=failure["status"],
            health={"reason": failure["reason"]},
            metadata=failure,
        )
    return registry, sequence_status, sequence_tools, discovered, sources, errors


(
    REGISTRY,
    SEQUENCE_STATUS,
    SEQUENCE_TOOLS,
    EXTERNAL_DOMAIN_TOOLS,
    EXTERNAL_DOMAIN_SOURCES,
    EXTERNAL_DOMAIN_ERRORS,
) = _build_registry()

SEQUENCE_PLUGIN_NAME = SEQUENCE_PLUGIN.PLUGIN_NAME
SEQUENCE_PLUGIN_VERSION = SEQUENCE_PLUGIN.PLUGIN_VERSION
DOMAIN_TOOLS = REGISTRY.tool_maps
DOMAIN_SOURCES = REGISTRY.sources
DOMAIN_METADATA = REGISTRY.metadata
DOMAIN_MANIFESTS = REGISTRY.manifests


def _qualified_name(domain, name):
    return REGISTRY.qualify(domain, name)


def _split_name(name):
    resolved = REGISTRY.resolve(name)
    return resolved[:2] if resolved else (None, None)


def tool_specs(domain=None):
    return REGISTRY.tool_specs(domain)


def _plugin_enabled(domain):
    try:
        from .plugin_manager import is_domain_enabled
    except ImportError:
        from plugin_manager import is_domain_enabled
    return is_domain_enabled(domain)


def active_tool_specs(domain=None):
    return [spec for spec in tool_specs(domain) if _plugin_enabled(spec["domain"])]


def active_domains():
    return tuple(domain for domain in available_domains() if _plugin_enabled(domain))


def active_domain_catalog():
    try:
        from .plugin_manager import PluginManager
    except ImportError:
        from plugin_manager import PluginManager
    return PluginManager().list()


def available_domains():
    return REGISTRY.available_domains()


def domain_catalog():
    return REGISTRY.catalog()


def _parse_tool_arguments(args):
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else {}
        except json.JSONDecodeError as exc:
            return None, f"invalid tool arguments: {exc}"
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, "tool arguments must be an object"
    return args, None


def _run_tool(name, args=None):
    resolved = REGISTRY.resolve(name)
    if resolved is None:
        return {"status": "error", "error": f"unknown domain tool: {name}"}
    domain, local_name, spec = resolved
    try:
        if not _plugin_enabled(domain):
            return {
                "status": "error",
                "domain": domain,
                "error": f"plugin domain is disabled: {domain}",
            }
    except ValueError as exc:
        return {"status": "error", "domain": domain, "error": str(exc)}
    arguments, error = _parse_tool_arguments(args)
    if error:
        return {"status": "error", "error": error}
    try:
        validate(instance=arguments, schema=spec["parameters"])
    except ValidationError as exc:
        return {
            "status": "error",
            "domain": domain,
            "tool": local_name,
            "error_type": "input_contract",
            "error": exc.message,
        }
    try:
        registered = REGISTRY.domains[domain]
        contract = registered.manifest['tool_contracts'][local_name]
        with enforce_plugin_boundary(
            domain,
            registered.manifest['security'],
            contract['permissions'],
            arguments,
        ):
            result = spec["function"](**arguments)
    except PluginSecurityError as exc:
        return {
            "status": "error",
            "domain": domain,
            "tool": local_name,
            "error_type": "plugin_security",
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "status": "error",
            "domain": domain,
            "tool": local_name,
            "error": str(exc),
        }
    output_schema = spec.get("returns") or spec.get("result_schema") or {}
    try:
        validate(instance=result, schema=output_schema)
    except ValidationError as exc:
        reason = f"output contract violation for {name}: {exc.message}"
        try:
            from .plugin_manager import PluginManager
        except ImportError:
            from plugin_manager import PluginManager
        try:
            PluginManager().record_contract_failure(domain, reason)
        except Exception:
            pass
        return {
            "status": "error",
            "domain": domain,
            "tool": local_name,
            "error_type": "output_contract",
            "error": reason,
        }
    return result


def run_tool(name, args=None):
    resolved = REGISTRY.resolve(name)
    domain = resolved[0] if resolved is not None else 'unknown'
    metric_tool = name if resolved is not None else 'unknown'
    arguments, _ = _parse_tool_arguments(args)
    arguments = arguments or {}
    spec = (
        next(
            item for item in REGISTRY.tool_specs(domain)
            if item['name'] == metric_tool
        )
        if resolved is not None else {'domain': domain}
    )
    parent_context = current_run_context()
    execution_context = (
        derive_run_context(parent_context, metric_tool, arguments, spec=spec)
        if parent_context is not None
        else build_run_context(metric_tool, arguments, spec=spec)
    )
    started = perf_counter()
    TOOL_ACTIVE.labels(domain, metric_tool).inc()
    with bind_run_context(execution_context), bind_context(tool=metric_tool, plugin=domain):
        log_event('tool.execution.started')
        try:
            result = _run_tool(name, args)
        except BaseException as exc:
            TOOL_EXECUTIONS.labels(domain, metric_tool, 'exception').inc()
            log_event('tool.execution.failed', error_type=type(exc).__name__)
            raise
        finally:
            TOOL_ACTIVE.labels(domain, metric_tool).dec()
            TOOL_DURATION.labels(domain, metric_tool).observe(perf_counter() - started)
        outcome = (
            'error'
            if isinstance(result, dict)
            and result.get('status') in {'error', 'failed', 'missing', 'not_found'}
            else 'success'
        )
        TOOL_EXECUTIONS.labels(domain, metric_tool, outcome).inc()
        log_event(
            'tool.execution.completed',
            status=outcome,
            error_type=(
                result.get('error_type')
                if isinstance(result, dict) and outcome == 'error'
                else None
            ),
            duration_seconds=perf_counter() - started,
        )
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="List unified bioinformatics Agent tools")
    parser.add_argument("--domain", default="all")
    parser.add_argument("--catalog", action="store_true")
    args = parser.parse_args(argv)
    if args.catalog:
        print(json.dumps(active_domain_catalog(), ensure_ascii=False, indent=2))
        return
    selected = None if args.domain == "all" else args.domain
    output = [
        {key: value for key, value in spec.items() if key != "function"}
        for spec in active_tool_specs(selected)
    ]
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
