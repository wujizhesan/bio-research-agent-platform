"""Optional LLM intent planner with deterministic workflow validation."""
import json
import os
import urllib.request


PLANNER_MODES = ('deterministic', 'auto', 'llm')
DEFAULT_BASE_URL = 'https://api.openai.com/v1/chat/completions'
DEFAULT_MODEL = 'gpt-4o-mini'


def _completion_url(value):
    base = str(value or DEFAULT_BASE_URL).rstrip('/')
    if base.endswith('/v1'):
        return base + '/chat/completions'
    return base if base.endswith('/chat/completions') else base + '/v1/chat/completions'


def _config():
    try:
        from .config_loader import load_config
        configured = load_config()
    except (ImportError, FileNotFoundError, OSError, TypeError, ValueError):
        configured = {}
    llm = configured.get('llm', {}) if isinstance(configured, dict) else {}
    if not isinstance(llm, dict):
        llm = {}
    return {
        'base_url': _completion_url(
            os.environ.get('RESEARCH_PLANNER_BASE_URL')
            or os.environ.get('OPENAI_BASE_URL')
            or llm.get('base_url')
        ),
        'model': os.environ.get('RESEARCH_PLANNER_MODEL') or llm.get('model') or DEFAULT_MODEL,
        'api_key': (
            os.environ.get('RESEARCH_PLANNER_API_KEY')
            or os.environ.get('CADD_API_KEY')
            or os.environ.get('OPENAI_API_KEY')
            or llm.get('api_key')
        ),
    }


def _parse_content(content):
    if isinstance(content, list):
        content = ''.join(str(item.get('text', '')) for item in content if isinstance(item, dict))
    text = str(content or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else text
        text = text.rsplit('```', 1)[0].strip()
    return json.loads(text)


def _normalize_domains(value, available_domains):
    if not isinstance(value, list):
        raise ValueError('LLM planner response must contain a domains array')
    aliases = {'rna-seq': 'omics', 'rnaseq': 'omics', 'mrna': 'sequence'}
    selected = []
    for item in value:
        raw_domain = str(item).strip().lower()
        domain = aliases.get(raw_domain, raw_domain)
        if domain not in available_domains:
            raise ValueError(f'LLM planner selected unavailable domain: {domain}')
        if domain not in selected:
            selected.append(domain)
    if not selected:
        raise ValueError('LLM planner selected no domains')
    return selected


def _call_llm(task, available_domains, inputs, config):
    system = (
        'You are a bioinformatics research intent planner. Return JSON only with keys '
        'domains and rationale. domains must be a subset of the available domains. '
        'Choose domains, not concrete tools. Never invent measurements or experimental results.'
    )
    user = json.dumps({
        'task': task,
        'available_domains': sorted(available_domains),
        'input_keys': sorted(inputs),
    }, ensure_ascii=False)
    payload = {
        'model': config['model'],
        'temperature': 0,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
    }
    request = urllib.request.Request(
        config['base_url'],
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={
            'Authorization': f"Bearer {config['api_key']}",
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode('utf-8'))
    choices = body.get('choices') if isinstance(body, dict) else None
    if not choices or not isinstance(choices[0], dict):
        raise ValueError('LLM planner response has no choices')
    message = choices[0].get('message', {})
    parsed = _parse_content(message.get('content'))
    domains = _normalize_domains(parsed.get('domains'), available_domains)
    rationale = parsed.get('rationale', [])
    if not isinstance(rationale, list):
        rationale = [str(rationale)]
    return {
        'backend': 'llm',
        'mode': 'llm',
        'model': config['model'],
        'domains': domains,
        'rationale': [str(item) for item in rationale[:6]],
    }


def select_domains(task, available_domains, inputs=None, mode='deterministic'):
    mode = str(mode or 'deterministic').lower()
    if mode not in PLANNER_MODES:
        raise ValueError(f'unknown planner mode: {mode}')
    if mode == 'deterministic':
        return {'backend': 'deterministic', 'mode': mode, 'domains': None}
    config = _config()
    if not config['api_key']:
        if mode == 'llm':
            raise RuntimeError('LLM planner requires RESEARCH_PLANNER_API_KEY, CADD_API_KEY or OPENAI_API_KEY')
        return {
            'backend': 'deterministic',
            'mode': mode,
            'domains': None,
            'fallback_reason': 'planner API key is not configured',
        }
    try:
        return _call_llm(task, set(available_domains), dict(inputs or {}).keys(), config)
    except Exception as exc:
        if mode == 'llm':
            raise RuntimeError(f'LLM planner failed: {exc}') from exc
        return {
            'backend': 'deterministic',
            'mode': mode,
            'domains': None,
            'fallback_reason': f'LLM planner unavailable: {type(exc).__name__}',
        }
