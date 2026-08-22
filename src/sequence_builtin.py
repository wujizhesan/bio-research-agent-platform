import math


CODON_TABLE = {
    'TTT': 'F', 'TTC': 'F', 'TTA': 'L', 'TTG': 'L',
    'TCT': 'S', 'TCC': 'S', 'TCA': 'S', 'TCG': 'S',
    'TAT': 'Y', 'TAC': 'Y', 'TAA': '*', 'TAG': '*',
    'TGT': 'C', 'TGC': 'C', 'TGA': '*', 'TGG': 'W',
    'CTT': 'L', 'CTC': 'L', 'CTA': 'L', 'CTG': 'L',
    'CCT': 'P', 'CCC': 'P', 'CCA': 'P', 'CCG': 'P',
    'CAT': 'H', 'CAC': 'H', 'CAA': 'Q', 'CAG': 'Q',
    'CGT': 'R', 'CGC': 'R', 'CGA': 'R', 'CGG': 'R',
    'ATT': 'I', 'ATC': 'I', 'ATA': 'I', 'ATG': 'M',
    'ACT': 'T', 'ACC': 'T', 'ACA': 'T', 'ACG': 'T',
    'AAT': 'N', 'AAC': 'N', 'AAA': 'K', 'AAG': 'K',
    'AGT': 'S', 'AGC': 'S', 'AGA': 'R', 'AGG': 'R',
    'GTT': 'V', 'GTC': 'V', 'GTA': 'V', 'GTG': 'V',
    'GCT': 'A', 'GCC': 'A', 'GCA': 'A', 'GCG': 'A',
    'GAT': 'D', 'GAC': 'D', 'GAA': 'E', 'GAG': 'E',
    'GGT': 'G', 'GGC': 'G', 'GGA': 'G', 'GGG': 'G',
}

PREFERRED_CODONS = {
    'A': 'GCT', 'C': 'TGC', 'D': 'GAC', 'E': 'GAG', 'F': 'TTC',
    'G': 'GGC', 'H': 'CAC', 'I': 'ATC', 'K': 'AAG', 'L': 'CTG',
    'M': 'ATG', 'N': 'AAC', 'P': 'CCC', 'Q': 'CAG', 'R': 'CGC',
    'S': 'AGC', 'T': 'ACC', 'V': 'GTG', 'W': 'TGG', 'Y': 'TAC',
    '*': 'TGA',
}


def _normalize_mrna(mrna):
    value = ''.join(str(mrna).upper().split()).replace('U', 'T')
    if not value or any(base not in 'ACGTN' for base in value):
        raise ValueError('mrna contains unsupported nucleotide symbols')
    if len(value) % 3:
        raise ValueError('mrna length must be a multiple of three')
    return value


def _translate(mrna):
    return ''.join(CODON_TABLE.get(mrna[index:index + 3], 'X') for index in range(0, len(mrna), 3))


def _codons_for(protein, method):
    if method == 'naive':
        return {
            amino_acid: sorted(codon for codon, value in CODON_TABLE.items() if value == amino_acid)[0]
            for amino_acid in set(protein)
        }
    return PREFERRED_CODONS


def _score_metrics(mrna):
    gc = (mrna.count('G') + mrna.count('C')) / len(mrna) if mrna else 0.0
    gc3 = sum(base in 'GC' for base in mrna[2::3]) / len(mrna[2::3]) if mrna else 0.0
    aa = _translate(mrna)
    frequencies = []
    for codon in (mrna[index:index + 3] for index in range(0, len(mrna), 3)):
        amino_acid = CODON_TABLE.get(codon)
        synonyms = [item for item, value in CODON_TABLE.items() if value == amino_acid]
        if amino_acid in {'M', 'W', '*'}:
            frequencies.append(1.0)
        else:
            frequencies.append(1 / len(synonyms))
    cai = math.exp(sum(math.log(max(value, 1e-12)) for value in frequencies) / len(frequencies)) if frequencies else 0.0
    return {
        'length_nt': len(mrna),
        'length_aa': len(aa),
        'gc': round(gc, 4),
        'gc3': round(gc3, 4),
        'cai': round(cai, 4),
        'up_a': mrna.count('AT') / len(mrna) if mrna else 0.0,
        'up_u': mrna.count('TT') / len(mrna) if mrna else 0.0,
    }


def _score(mrna, molecule):
    mrna = _normalize_mrna(mrna)
    translated = _translate(mrna)
    metrics = _score_metrics(mrna)
    checks = [
        {'name': 'frame', 'passed': len(mrna) % 3 == 0},
        {'name': 'start_codon', 'passed': mrna.startswith('ATG')},
        {'name': 'translation_defined', 'passed': 'X' not in translated},
        {'name': 'gc_window', 'passed': 0.3 <= metrics['gc'] <= 0.8},
    ]
    return {
        'status': 'ok',
        'mrna': mrna,
        'molecule': molecule,
        'translation': translated,
        'metrics': metrics,
        'checks': checks,
        'verdict': 'PASS' if all(check['passed'] for check in checks) else 'REVIEW',
        'backend': 'builtin-deterministic',
    }


def call(operation, payload):
    if operation == 'optimize':
        protein = payload['protein']
        codons = _codons_for(protein, payload.get('method', 'greedy'))
        mrna = ''.join(codons[amino_acid] for amino_acid in protein)
        scored = _score(mrna, payload.get('molecule', 'linear'))
        return {
            'status': 'ok',
            'protein': protein,
            'mrna': mrna,
            'molecule': payload.get('molecule', 'linear'),
            'method': payload.get('method', 'greedy'),
            'metrics': scored['metrics'],
            'checks': scored['checks'],
            'verdict': scored['verdict'],
            'backend': 'builtin-deterministic',
        }
    if operation == 'score':
        return _score(payload['mrna'], payload.get('molecule', 'linear'))
    if operation == 'verify':
        mrna = _normalize_mrna(payload['mrna'])
        protein = payload['protein']
        translated = _translate(mrna)
        return {
            'status': 'ok',
            'mrna': mrna,
            'expected_protein': protein,
            'translated_protein': translated,
            'identical': translated == protein,
            'backend': 'builtin-deterministic',
        }
    if operation == 'compare':
        current = _score(payload['mrna'], payload.get('molecule', 'linear'))
        baseline_mrna = ''.join(_codons_for(payload['baseline'], 'naive')[amino_acid] for amino_acid in payload['baseline'])
        baseline = _score(baseline_mrna, payload.get('molecule', 'linear'))
        return {
            'status': 'ok',
            'current': current,
            'baseline': baseline,
            'delta': {
                key: round(current['metrics'][key] - baseline['metrics'][key], 4)
                for key in {'gc', 'gc3', 'cai'}
            },
            'backend': 'builtin-deterministic',
        }
    if operation == 'benchmark':
        rows = []
        for method in ('naive', 'greedy'):
            optimized = call('optimize', {
                'protein': payload['protein'],
                'molecule': payload.get('molecule', 'linear'),
                'method': method,
            })
            rows.append({
                'method': method,
                'mrna': optimized['mrna'],
                'metrics': optimized['metrics'],
                'verdict': optimized['verdict'],
            })
        return {
            'status': 'ok',
            'protein': payload['protein'],
            'rows': rows,
            'backend': 'builtin-deterministic',
            'vaxpress': 'not_configured' if payload.get('use_vaxpress') else 'not_requested',
        }
    raise ValueError(f'unsupported sequence operation: {operation}')


def build_report(payload, output_path):
    metrics = payload.get('metrics', {}) if isinstance(payload, dict) else {}
    rows = ''.join(f'<tr><th>{key}</th><td>{value}</td></tr>' for key, value in metrics.items())
    html = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Sequence report</title></head>'
        '<body><h1>Sequence report</h1><p>Deterministic built-in backend</p>'
        f'<table>{rows}</table></body></html>'
    )
    from pathlib import Path
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding='utf-8')
    return str(path)
