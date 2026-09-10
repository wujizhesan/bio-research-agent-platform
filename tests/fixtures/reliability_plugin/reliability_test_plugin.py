import time
from pathlib import Path


PLUGIN_NAME = 'CI reliability test plugin'
PLUGIN_VERSION = '1.0.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = ('test.reliability',)


def hold(duration_seconds):
    duration = max(float(duration_seconds), 0.1)
    time.sleep(duration)
    return {'status': 'ok', 'held_seconds': duration}


def fail_once(state_path):
    state = Path(state_path)
    if state.exists():
        return {'status': 'ok', 'recovered': True}
    state.write_text('failed-once\n', encoding='utf-8')
    return {
        'status': 'error',
        'error': 'injected transient failure',
        'recovered': False,
    }


def resume_once(state_path, duration_seconds):
    state = Path(state_path)
    if state.exists():
        return {'status': 'ok', 'resumed': True}
    state.write_text('started\n', encoding='utf-8')
    time.sleep(max(float(duration_seconds), 0.1))
    return {'status': 'ok', 'resumed': False}


def _parameters(properties, required):
    return {
        'type': 'object',
        'properties': properties,
        'required': required,
        'additionalProperties': False,
    }


RESULT_SCHEMA = {
    'type': 'object',
    'required': ['status'],
    'properties': {
        'status': {'type': 'string'},
        'error': {'type': 'string'},
        'held_seconds': {'type': 'number'},
        'recovered': {'type': 'boolean'},
        'resumed': {'type': 'boolean'},
    },
    'additionalProperties': False,
}


def _state_permissions():
    return {
        'filesystem': {
            'read': ['state_path'],
            'write': ['state_path'],
        },
    }


TOOLS = {
    'hold': {
        'description': 'Hold a process long enough to verify cancellation.',
        'parameters': _parameters({
            'duration_seconds': {
                'type': 'number',
                'minimum': 0.1,
                'maximum': 120,
            },
        }, ['duration_seconds']),
        'returns': RESULT_SCHEMA,
        'function': hold,
    },
    'fail_once': {
        'description': 'Fail once and then succeed with the same durable state path.',
        'parameters': _parameters({
            'state_path': {'type': 'string', 'minLength': 1},
        }, ['state_path']),
        'returns': RESULT_SCHEMA,
        'permissions': _state_permissions(),
        'function': fail_once,
    },
    'resume_once': {
        'description': 'Persist a start marker and complete quickly after worker recovery.',
        'parameters': _parameters({
            'state_path': {'type': 'string', 'minLength': 1},
            'duration_seconds': {
                'type': 'number',
                'minimum': 0.1,
                'maximum': 120,
            },
        }, ['state_path', 'duration_seconds']),
        'returns': RESULT_SCHEMA,
        'permissions': _state_permissions(),
        'function': resume_once,
    },
}
