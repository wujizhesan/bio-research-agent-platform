from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest

from jsonschema import SchemaError, ValidationError, validate

from src.tool_contracts import validate_contract


def outcome(function, instance, schema):
    try:
        function(instance=instance, schema=schema)
    except (ValidationError, SchemaError) as exc:
        return type(exc), exc.message, list(exc.path), list(exc.schema_path)
    return None


class ToolContractTests(unittest.TestCase):
    def test_validation_and_selected_errors_match_jsonschema(self):
        cases = [
            ({'value': 1}, {'type': 'object', 'properties': {'value': {'type': 'integer'}}}),
            ({'value': 'bad', 'extra': 1}, {
                'type': 'object', 'properties': {'value': {'type': 'integer'}},
                'required': ['required'], 'additionalProperties': False,
            }),
            ({'value': 'bad'}, {'anyOf': [
                {'type': 'object', 'properties': {'value': {'type': 'integer'}}},
                {'type': 'array', 'items': {'type': 'string'}},
            ]}),
            (1, {
                '$schema': 'http://json-schema.org/draft-04/schema#',
                'type': 'number', 'minimum': 1, 'exclusiveMinimum': True,
            }),
            (2, {
                '$schema': 'http://json-schema.org/draft-07/schema#',
                'type': 'number', 'exclusiveMinimum': 1,
            }),
            ([1, 'bad'], {
                '$schema': 'https://json-schema.org/draft/2020-12/schema',
                'type': 'array', 'prefixItems': [{'type': 'string'}, {'type': 'integer'}],
            }),
            ({'value': 3}, {
                '$defs': {'value': {'type': 'integer', 'minimum': 5}},
                'properties': {'value': {'$ref': '#/$defs/value'}},
            }),
            (0, True),
            (0, False),
            (0, {'type': 'invalid'}),
            (1, {'enum': (1, 2)}),
            ('\ud800', {'enum': ['\ud800']}),
            ('bad', {'type': 'integer', 'description': 'x' * (65 * 1024)}),
        ]
        for instance, schema in cases:
            with self.subTest(schema=schema if len(str(schema)) < 1024 else 'large schema'):
                self.assertEqual(
                    outcome(validate_contract, instance, schema),
                    outcome(validate, instance, schema),
                )

    def test_in_place_schema_changes_take_effect(self):
        schema = {
            'type': 'object', 'properties': {'value': {'type': 'integer', 'minimum': 1}},
        }
        self.assertIsNone(outcome(validate_contract, {'value': 3}, schema))
        original = deepcopy(schema)
        schema['properties']['value']['minimum'] = 5
        self.assertEqual(
            outcome(validate_contract, {'value': 3}, schema),
            outcome(validate, {'value': 3}, schema),
        )
        self.assertIsNone(outcome(validate_contract, {'value': 3}, original))
        schema['properties']['value']['type'] = 'invalid'
        self.assertEqual(
            outcome(validate_contract, {'value': 3}, schema),
            outcome(validate, {'value': 3}, schema),
        )

    def test_property_order_preserves_selected_error(self):
        schemas = [
            {'properties': {'first': {'type': 'integer'}, 'second': {'type': 'string'}}},
            {'properties': {'second': {'type': 'string'}, 'first': {'type': 'integer'}}},
        ]
        instance = {'first': 'bad', 'second': 1}
        for schema in schemas:
            self.assertEqual(
                outcome(validate_contract, instance, schema),
                outcome(validate, instance, schema),
            )

    def test_concurrent_local_references_do_not_share_validation_state(self):
        schema = {
            '$defs': {'node': {
                'type': 'object', 'properties': {
                    'value': {'type': 'integer'},
                    'children': {'type': 'array', 'items': {'$ref': '#/$defs/node'}},
                },
                'required': ['value'],
            }},
            '$ref': '#/$defs/node',
        }
        instances = [
            {'value': number, 'children': [{'value': 'bad' if number % 2 else number}]}
            for number in range(64)
        ]
        expected = [outcome(validate, instance, schema) for instance in instances]
        with ThreadPoolExecutor(max_workers=8) as executor:
            actual = list(executor.map(
                lambda instance: outcome(validate_contract, instance, schema), instances,
            ))
        self.assertEqual(actual, expected)
