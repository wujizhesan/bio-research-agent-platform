import argparse
import json
import os
from pathlib import Path
import shutil

from src.plugin_container import container_tool_executor_from_env


MARKER = 'BIOAGENT_PLUGIN_SANDBOX_E2E_MARKER'


def _inside(path, root):
    return path != root and root in path.parents


def prepare_fixture(test_root, artifact_root):
    test_root = Path(test_root).resolve()
    artifact_root = Path(artifact_root).resolve()
    if not _inside(test_root, artifact_root):
        raise ValueError('security smoke root must be inside the artifact root')
    relative = test_root.relative_to(artifact_root)
    if not relative.parts or relative.parts[0] != 'plugin-sandbox-e2e':
        raise ValueError('security smoke root must use plugin-sandbox-e2e')
    shutil.rmtree(test_root, ignore_errors=True)
    input_dir = test_root / 'input'
    input_dir.mkdir(parents=True)
    (input_dir / 'evidence.md').write_text(
        f'# Sandbox evidence\n{MARKER}\n',
        encoding='utf-8',
    )
    return input_dir, test_root / 'artifacts' / 'knowledge-index.json'


def verify(test_root, workspace_root, executor_factory=None):
    artifact_root = Path(
        os.environ.get('PLUGIN_ARTIFACT_ROOT', '/app/output')
    ).resolve()
    input_dir, output_path = prepare_fixture(test_root, artifact_root)
    workspace_root = Path(workspace_root).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    executor = (executor_factory or container_tool_executor_from_env)()
    try:
        result = executor.execute('knowledge_ingest_directory', {
            'input_dir': str(input_dir),
            'output_path': str(output_path),
            'extensions': ['.md'],
        })
    finally:
        executor.shutdown()
    if result.get('status') != 'ok':
        raise RuntimeError('sandbox smoke plugin did not complete successfully')
    details = result.get('result') or {}
    if details.get('n_documents') != 1:
        raise RuntimeError('sandbox smoke plugin returned an unexpected document count')
    if Path(details.get('output_path', '')).resolve() != output_path:
        raise RuntimeError('sandbox smoke plugin returned an unexpected artifact path')
    payload = json.loads(output_path.read_text(encoding='utf-8'))
    documents = payload.get('documents') or []
    if len(documents) != 1 or MARKER not in str(documents[0].get('text') or ''):
        raise RuntimeError('sandbox smoke artifact failed content verification')
    leftovers = tuple(workspace_root.iterdir())
    if leftovers:
        raise RuntimeError('sandbox request workspace was not cleaned')
    return {
        'status': 'ok',
        'tool': 'knowledge_ingest_directory',
        'artifact': str(output_path),
        'workspace_clean': True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--test-root',
        default='/app/output/plugin-sandbox-e2e',
    )
    parser.add_argument(
        '--workspace-root',
        default='/run/bioagent/plugin-exchange',
    )
    args = parser.parse_args(argv)
    print(json.dumps(
        verify(args.test_root, args.workspace_root),
        ensure_ascii=True,
        sort_keys=True,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
