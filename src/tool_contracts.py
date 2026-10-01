"""Shared helpers for plugin tool protocol contracts."""

from functools import lru_cache
import json


def _json_schema_value(value):
    if type(value) is dict:
        return all(
            type(key) is str and _json_schema_value(item)
            for key, item in value.items()
        )
    if type(value) is list:
        return all(_json_schema_value(item) for item in value)
    return type(value) in (str, int, float, bool, type(None))


@lru_cache(maxsize=128)
def _cached_validator(encoded, cls):
    schema = json.loads(encoded)
    cls.check_schema(schema)
    return cls(schema)


def contract_validator(schema, *, cls=None):
    from jsonschema.validators import validator_for

    cls = cls or validator_for(schema)
    try:
        encoded = (
            json.dumps(schema, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            if _json_schema_value(schema) else None
        )
        within_limit = encoded is not None and len(encoded.encode('utf-8')) <= 64 * 1024
    except (TypeError, ValueError, RecursionError):
        within_limit = False
    if within_limit:
        return _cached_validator(encoded, cls)
    cls.check_schema(schema)
    return cls(schema)


def validate_contract(instance, schema):
    from jsonschema.exceptions import best_match

    error = best_match(contract_validator(schema).iter_errors(instance))
    if error is not None:
        raise error


def object_parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


def bind_tool_contracts(contracts, functions):
    bound = {}
    for name, contract in contracts.items():
        function = functions.get(name)
        if function is None:
            raise ValueError(f'missing implementation for tool contract: {name}')
        bound[name] = {**contract, 'function': function}
    return bound
