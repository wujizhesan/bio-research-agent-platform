import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import domain_registry
from src.plugin_registry import DomainRegistry, validate_tool_map


def plugin(name):
    return SimpleNamespace(
        PLUGIN_NAME=name,
        PLUGIN_VERSION="1.0.0",
        PLUGIN_API_VERSION=1,
        PLUGIN_CAPABILITIES=(f"{name}.run",),
    )


def tool(result):
    return {
        "description": "Run plugin",
        "parameters": {"type": "object"},
        "function": lambda: result,
    }


class DomainRegistryTests(unittest.TestCase):
    def test_registration_builds_catalog_and_exact_tool_index(self):
        registry = DomainRegistry()
        registry.register("demo", plugin("demo"), {"run": tool("demo")}, kind="external")
        registry.register(
            "demo_extended",
            plugin("demo_extended"),
            {"run": tool("extended")},
            kind="external",
        )

        self.assertEqual(registry.resolve("demo_run")[2]["function"](), "demo")
        self.assertEqual(
            registry.resolve("demo_extended_run")[2]["function"](),
            "extended",
        )
        self.assertEqual(registry.available_domains(), ("demo", "demo_extended"))
        self.assertEqual(registry.catalog()[0]["manifest"]["tool_count"], 1)

    def test_duplicate_domain_is_rejected(self):
        registry = DomainRegistry()
        registry.register("demo", plugin("demo"), {"run": tool("ok")}, kind="external")
        with self.assertRaisesRegex(ValueError, "duplicate domain plugin"):
            registry.register(
                "demo",
                plugin("duplicate"),
                {"run": tool("duplicate")},
                kind="external",
            )

    def test_qualified_tool_name_collision_is_rejected_atomically(self):
        registry = DomainRegistry()
        registry.register(
            "demo",
            plugin("demo"),
            {"extended_run": tool("first")},
            kind="external",
        )
        with self.assertRaisesRegex(ValueError, "duplicate qualified tool name"):
            registry.register(
                "demo_extended",
                plugin("demo_extended"),
                {"run": tool("second")},
                kind="external",
            )

        self.assertEqual(registry.available_domains(), ("demo",))
        self.assertEqual(registry.resolve("demo_extended_run")[2]["function"](), "first")

    def test_unavailable_domain_can_expose_health_without_tools(self):
        registry = DomainRegistry()
        registry.register(
            "optional",
            plugin("optional"),
            {},
            kind="external",
            status="unavailable",
            health={"reason": "dependency missing"},
        )

        self.assertEqual(registry.available_domains(), ())
        self.assertEqual(registry.catalog()[0]["status"], "unavailable")
        self.assertEqual(
            registry.catalog()[0]["manifest"]["health"]["reason"],
            "dependency missing",
        )

    def test_invalid_output_schema_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'output schema'):
            validate_tool_map('demo', {
                'run': {
                    'description': 'run',
                    'parameters': {},
                    'returns': {'type': 'not-a-json-schema-type'},
                    'function': lambda: None,
                },
            })

    def test_runtime_enforces_input_and_output_contracts(self):
        registry = DomainRegistry()
        registry.register(
            'demo',
            plugin('demo'),
            {
                'run': {
                    'description': 'run',
                    'parameters': {
                        'type': 'object',
                        'properties': {'value': {'type': 'integer'}},
                        'required': ['value'],
                    },
                    'returns': {
                        'type': 'object',
                        'required': ['status'],
                    },
                    'function': lambda value: {'value': value},
                },
            },
            kind='external',
        )
        with patch.object(domain_registry, 'REGISTRY', registry):
            invalid_input = domain_registry.run_tool('demo_run', {'value': 'bad'})
            with patch('src.plugin_manager.PluginManager.record_contract_failure') as failure:
                invalid_output = domain_registry.run_tool('demo_run', {'value': 1})
        self.assertEqual(invalid_input['error_type'], 'input_contract')
        self.assertEqual(invalid_output['error_type'], 'output_contract')
        failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
