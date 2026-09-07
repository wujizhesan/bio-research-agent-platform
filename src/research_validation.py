"""Input validation and normalization for research application tools."""


def validate_preset(preset, presets):
    if preset not in presets:
        raise ValueError(f'unknown research preset: {preset}')
    return presets[preset]


def validate_planning_request(task, inputs=None, require_inputs=False):
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task must be a non-empty string')
    if require_inputs and not isinstance(inputs, dict):
        raise ValueError('inputs must be an object')
    if inputs is not None and not isinstance(inputs, dict):
        raise ValueError('inputs must be an object')
    return task, inputs


def validate_workflow(workflow):
    if not isinstance(workflow, dict):
        raise ValueError('workflow must be an object')
    return workflow


def select_domains(task, requested, available, domain_keywords):
    available = set(available) - {'research'}
    if requested:
        selected = []
        for domain in requested:
            if domain not in available:
                raise ValueError(f'unknown or unavailable research domain: {domain}')
            if domain not in selected:
                selected.append(domain)
        return selected
    text = task.lower()
    scores = {
        domain: sum(keyword.lower() in text for keyword in keywords)
        for domain, keywords in domain_keywords.items()
        if domain in available
    }
    selected = [domain for domain, score in scores.items() if score > 0]
    return selected or sorted(available)
