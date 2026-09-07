"""In-memory registry for scientific domain plugins and their tools."""

from dataclasses import dataclass
from typing import Any

try:
    from .plugin_manifest import build_manifest, validate_json_schema
    from .resource_scheduling import ResourceRequest
    from .plugin_security import normalize_permissions
except ImportError:
    from plugin_manifest import build_manifest, validate_json_schema
    from resource_scheduling import ResourceRequest
    from plugin_security import normalize_permissions


ToolDefinition = dict[str, Any]
ToolMap = dict[str, ToolDefinition]


def validate_tool_map(domain: str, tools: object, *, allow_empty: bool = False) -> ToolMap:
    if not isinstance(tools, dict) or (not tools and not allow_empty):
        requirement = "a tools mapping" if allow_empty else "a non-empty TOOLS mapping"
        raise ValueError(f"domain {domain} must expose {requirement}")
    for name, spec in tools.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"domain {domain} has an invalid tool name")
        if not isinstance(spec, dict):
            raise ValueError(f"domain {domain} tool {name} must be a mapping")
        if not isinstance(spec.get("description"), str):
            raise ValueError(f"domain {domain} tool {name} needs a description")
        if not isinstance(spec.get("parameters"), dict):
            raise ValueError(f"domain {domain} tool {name} needs parameters")
        validate_json_schema(spec["parameters"], f"{domain}.{name} input schema")
        output_schema = spec.get("returns") or spec.get("result_schema") or {}
        validate_json_schema(output_schema, f"{domain}.{name} output schema")
        ResourceRequest.from_mapping(spec.get("resources"))
        normalize_permissions(spec.get("permissions"), spec["parameters"])
        if not callable(spec.get("function")):
            raise ValueError(f"domain {domain} tool {name} needs a callable function")
    return tools


@dataclass(frozen=True)
class RegisteredDomain:
    key: str
    source: object
    tools: ToolMap
    metadata: dict[str, Any]
    manifest: dict[str, Any]

    def catalog_entry(self) -> dict[str, Any]:
        return {
            **self.metadata,
            "domain": self.key,
            "status": self.manifest["status"],
            "tool_count": len(self.tools),
            "tools": sorted(self.tools),
            "manifest": dict(self.manifest),
        }


class DomainRegistry:
    def __init__(self) -> None:
        self._domains: dict[str, RegisteredDomain] = {}
        self._qualified_tools: dict[str, tuple[str, str, ToolDefinition]] = {}

    @property
    def domains(self) -> dict[str, RegisteredDomain]:
        return dict(self._domains)

    @property
    def tool_maps(self) -> dict[str, ToolMap]:
        return {
            key: plugin.tools
            for key, plugin in self._domains.items()
            if plugin.tools
        }

    @property
    def sources(self) -> dict[str, object]:
        return {key: plugin.source for key, plugin in self._domains.items()}

    @property
    def metadata(self) -> dict[str, dict[str, Any]]:
        return {key: dict(plugin.metadata) for key, plugin in self._domains.items()}

    @property
    def manifests(self) -> dict[str, dict[str, Any]]:
        return {key: dict(plugin.manifest) for key, plugin in self._domains.items()}

    def register(
        self,
        key: str,
        source: object,
        tools: ToolMap,
        *,
        kind: str,
        status: str = "available",
        domains: list[str] | None = None,
        health: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RegisteredDomain:
        if key in self._domains:
            raise ValueError(f"duplicate domain plugin: {key}")
        validated_tools = validate_tool_map(
            key,
            tools,
            allow_empty=status in {"unavailable", "error"},
        )
        manifest = build_manifest(
            key,
            source,
            validated_tools,
            kind=kind,
            status=status,
            domains=domains or [key],
            health=health,
        )
        domain_metadata = dict(metadata or {})
        domain_metadata.setdefault("name", manifest["name"])
        domain_metadata.setdefault("kind", kind)
        domain_metadata.setdefault("version", manifest["version"])
        domain_metadata["status"] = status
        plugin = RegisteredDomain(
            key=key,
            source=source,
            tools=validated_tools,
            metadata=domain_metadata,
            manifest=manifest,
        )
        qualified_tools = {
            self.qualify(key, tool_name): (key, tool_name, definition)
            for tool_name, definition in validated_tools.items()
        }
        collisions = self._qualified_tools.keys() & qualified_tools.keys()
        if collisions:
            name = sorted(collisions)[0]
            raise ValueError(f"duplicate qualified tool name: {name}")
        self._qualified_tools.update(qualified_tools)
        self._domains[key] = plugin
        return plugin

    @staticmethod
    def qualify(domain: str, name: str) -> str:
        return f"{domain}_{name}"

    def resolve(self, qualified_name: str) -> tuple[str, str, ToolDefinition] | None:
        return self._qualified_tools.get(qualified_name)

    def available_domains(self) -> tuple[str, ...]:
        return tuple(key for key, plugin in self._domains.items() if plugin.tools)

    def tool_specs(self, domain: str | None = None) -> list[dict[str, Any]]:
        if domain is not None and (
            domain not in self._domains or not self._domains[domain].tools
        ):
            raise ValueError(f"unknown domain: {domain}")
        selected = (domain,) if domain else self.available_domains()
        return [
            {
                "name": self.qualify(domain_key, name),
                "domain": domain_key,
                "description": spec["description"],
                "parameters": spec["parameters"],
                "returns": spec.get("returns") or spec.get("result_schema") or {},
                "resources": ResourceRequest.from_mapping(
                    spec.get("resources")
                ).as_dict(),
                "plugin_version": self._domains[domain_key].manifest["version"],
                "plugin_api_version": self._domains[domain_key].manifest["api_version"],
                "plugin_contract_digest": self._domains[domain_key].manifest["contract_digest"],
                "plugin_security": dict(
                    self._domains[domain_key].manifest["security"]
                ),
                "permissions": dict(
                    self._domains[domain_key].manifest["tool_contracts"][name]["permissions"]
                ),
                "function": spec["function"],
            }
            for domain_key in selected
            for name, spec in self._domains[domain_key].tools.items()
        ]

    def catalog(self) -> list[dict[str, Any]]:
        return [plugin.catalog_entry() for plugin in self._domains.values()]
