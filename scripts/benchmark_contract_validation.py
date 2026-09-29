import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from statistics import median
import tempfile
from time import perf_counter
from unittest.mock import patch

from jsonschema.validators import validator_for

from scripts.benchmark_secure_jobs import percentile
from src import domain_registry, plugin_manifest, tool_contracts
from src.knowledge_plugin import knowledge_search
from src.plugin_registry import DomainRegistry


def rebuilding_validator(schema, *, cls=None):
    cls = cls or validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def compare(operation, samples, iterations):
    timings = {'rebuilding': [], 'cached': []}
    for index in range(-1, samples):
        order = tuple(timings) if index % 2 == 0 else tuple(reversed(timings))
        reference = None
        for name in order:
            with ExitStack() as stack:
                if name == 'rebuilding':
                    stack.enter_context(patch.object(
                        tool_contracts, 'contract_validator', rebuilding_validator,
                    ))
                    stack.enter_context(patch.object(
                        plugin_manifest, 'contract_validator', rebuilding_validator,
                    ))
                started = perf_counter()
                result = [operation() for _ in range(iterations)]
                elapsed = (perf_counter() - started) / iterations
            if reference is not None and result != reference:
                raise RuntimeError('contract validator strategies changed tool results or manifests')
            reference = result
            if index >= 0:
                timings[name].append(elapsed)
    result = {
        name: {
            'median_seconds': round(median(values), 8),
            'p95_seconds': round(percentile(values, 95), 8),
            'seconds': [round(value, 8) for value in values],
        }
        for name, values in timings.items()
    }
    result['comparison'] = {
        'cached_median_improvement_percent': round(
            100 * (1 - median(timings['cached']) / median(timings['rebuilding'])), 1,
        ),
        'paired_cached_wins': sum(
            cached < rebuilding
            for cached, rebuilding in zip(timings['cached'], timings['rebuilding'])
        ),
        'paired_samples': samples,
        'iterations_per_sample': iterations,
    }
    return result


def register_builtins():
    registry = DomainRegistry()
    for domain, source, metadata in domain_registry.BUILTIN_DOMAINS:
        registry.register(
            domain, source, source.TOOLS, kind=metadata['kind'], metadata=metadata,
        )
    return registry.manifests


def benchmark(samples, iterations):
    with tempfile.TemporaryDirectory(prefix='contract_validation_benchmark_') as raw:
        index_path = Path(raw) / 'index.json'
        index_path.write_text(json.dumps({'documents': [
            {'id': 'tp53', 'title': 'TP53', 'text': 'TP53 DNA damage repair'},
            {'id': 'egfr', 'title': 'EGFR', 'text': 'EGFR growth signaling'},
        ]}), encoding='utf-8')
        arguments = {'query': 'TP53', 'index_path': str(index_path), 'top_k': 1}
        _, _, spec = domain_registry.REGISTRY.resolve('knowledge_search')
        expected = knowledge_search(**arguments)

        def validation():
            tool_contracts.validate_contract(arguments, spec['parameters'])
            tool_contracts.validate_contract(expected, spec.get('returns') or {})

        def tool_call():
            result = domain_registry.run_tool('knowledge_search', arguments)
            if result != expected:
                raise RuntimeError('contract benchmark produced an invalid knowledge result')
            return result

        validation_result = compare(validation, samples, iterations)
        registration_result = compare(register_builtins, samples, 1)
        tool_result = compare(tool_call, samples, iterations)
    return {
        'scope': 'warm input/output validation, built-in registration, and inline knowledge tool calls; excludes process startup, durable queue, and API',
        'rebuilding_baseline': 'same validation and registration code with schema checks and validators rebuilt per call',
        'warmup_pairs_per_case': 1,
        'contract_validation': validation_result,
        'builtin_registration': registration_result,
        'knowledge_tool_call': tool_result,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=16)
    parser.add_argument('--iterations', type=int, default=25)
    args = parser.parse_args(argv)
    if args.samples < 2 or args.iterations < 1:
        parser.error('samples must be at least two and iterations must be positive')
    print(json.dumps(benchmark(args.samples, args.iterations), sort_keys=True))


if __name__ == '__main__':
    raise SystemExit(main())
