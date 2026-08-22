import json
from pathlib import Path
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(config_path=None):
    path = Path(config_path) if config_path else PROJECT_ROOT / 'config.yaml'
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f'配置文件不存在: {path}')
    with path.open(encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def resolve_path(value):
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def latest_run_dir(output_dir):
    output_dir = Path(output_dir)
    pointer = output_dir / 'latest_run.json'
    if not pointer.exists():
        return output_dir
    try:
        payload = json.loads(pointer.read_text(encoding='utf-8'))
        relative_path = payload.get('path')
        if not relative_path:
            return output_dir
        candidate = output_dir / relative_path
        try:
            candidate.resolve().relative_to(output_dir.resolve())
        except ValueError:
            return output_dir
        return candidate if candidate.is_dir() else output_dir
    except (OSError, json.JSONDecodeError, TypeError):
        return output_dir
