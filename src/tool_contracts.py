"""Shared helpers for plugin tool protocol contracts."""


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
