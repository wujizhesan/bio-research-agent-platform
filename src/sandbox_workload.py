import json
from pathlib import Path


LIGHTWEIGHT_ARGUMENT_LIMIT = 1024 * 1024
LIGHTWEIGHT_INDEX_LIMIT = 8 * 1024 * 1024


def is_lightweight_tool(tool, arguments):
    if tool == 'omics_inspect_toolchain':
        return not arguments
    if tool == 'literature_summarize':
        return len(json.dumps(arguments, default=str).encode('utf-8')) <= LIGHTWEIGHT_ARGUMENT_LIMIT
    if tool == 'knowledge_search':
        index_path = arguments.get('index_path')
        if not isinstance(index_path, str):
            return False
        try:
            return Path(index_path).stat().st_size <= LIGHTWEIGHT_INDEX_LIMIT
        except OSError:
            return False
    return False
