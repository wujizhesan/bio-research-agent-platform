"""Tool protocol contracts for the research application plugin."""

try:
    from .tool_contracts import bind_tool_contracts, object_parameters
except ImportError:
    from tool_contracts import bind_tool_contracts, object_parameters


def build_research_tools(functions, presets):
    parameters = object_parameters
    contracts = {
        'catalog': {
            'description': 'List available bioinformatics domains, plugins, versions and health status.',
            'parameters': parameters({}),
        },
        'presets': {
            'description': 'List reproducible research application presets.',
            'parameters': parameters({}),
        },
        'run_preset': {
            'description': 'Run a named research preset in dry-run or execution mode.',
            'parameters': parameters({
                'preset': {'type': 'string', 'enum': list(presets)},
                'output_path': {'type': 'string'},
                'report_path': {'type': 'string'},
                'dry_run': {'type': 'boolean'},
                'continue_on_error': {'type': 'boolean'},
                'resume': {'type': 'boolean'},
            }, ('preset',)),
        },
        'plan': {
            'description': 'Build a traceable research plan and infer evidence sources without inventing measurements.',
            'parameters': parameters({
                'task': {'type': 'string'},
                'domains': {'type': 'array', 'items': {'type': 'string'}},
                'inputs': {'type': 'object'},
                'output_dir': {'type': 'string'},
                'planner_mode': {'type': 'string', 'enum': ['deterministic', 'auto', 'llm']},
            }, ('task',)),
        },
        'build_workflow': {
            'description': 'Build an executable cross-domain workflow from a research task and validated inputs.',
            'parameters': parameters({
                'task': {'type': 'string'},
                'domains': {'type': 'array', 'items': {'type': 'string'}},
                'inputs': {'type': 'object'},
                'output_dir': {'type': 'string'},
                'planner_mode': {'type': 'string', 'enum': ['deterministic', 'auto', 'llm']},
            }, ('task', 'inputs')),
        },
        'execute': {
            'description': 'Execute or dry-run a validated cross-domain research workflow with an audit manifest.',
            'parameters': parameters({
                'workflow': {'type': 'object'},
                'domains': {'type': 'array', 'items': {'type': 'string'}},
                'output_path': {'type': 'string'},
                'report_path': {'type': 'string'},
                'dry_run': {'type': 'boolean'},
                'continue_on_error': {'type': 'boolean'},
                'resume': {'type': 'boolean'},
            }, ('workflow',)),
        },
        'verify_reproducibility': {
            'description': 'Verify a research manifest against current inputs, outputs, runtime and plugin implementations.',
            'parameters': parameters({
                'manifest_path': {'type': 'string'},
                'check_environment': {'type': 'boolean'},
            }, ('manifest_path',)),
        },
    }
    return bind_tool_contracts(contracts, functions)
