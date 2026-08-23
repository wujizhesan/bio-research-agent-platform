"""Pluggable literature and evidence adapter."""
from collections import Counter


PLUGIN_NAME = 'Literature and evidence'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = (
    'literature.search',
    'literature.summarize',
)


def _parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


def _provider_factory(provider, evidence_csv=None, cache_dir=None, timeout=15,
                      genome='hg38', gencode_gtf=None):
    try:
        from .evidence_providers import get_evidence_provider
    except ImportError:
        from evidence_providers import get_evidence_provider
    return get_evidence_provider(
        provider=provider,
        evidence_csv=evidence_csv,
        cache_dir=cache_dir,
        timeout=timeout,
        genome=genome,
        gencode_gtf=gencode_gtf,
    )


def _normalize_gene_ids(gene_ids):
    if not isinstance(gene_ids, list) or not gene_ids:
        raise ValueError('gene_ids must be a non-empty array')
    values = [str(item).strip() for item in gene_ids if str(item).strip()]
    if not values:
        raise ValueError('gene_ids must contain at least one non-empty identifier')
    return sorted(set(values))


def _envelope(operation, payload):
    if not isinstance(payload, dict):
        payload = {'value': payload}
    status = payload.get('status', 'ok')
    if status == 'error' or 'error' in payload:
        return {
            'status': 'error',
            'plugin': 'literature',
            'operation': operation,
            'error': payload.get('error', 'literature operation failed'),
            'result': payload,
            'provenance': {'backend': PLUGIN_NAME, 'version': PLUGIN_VERSION},
        }
    return {
        'status': 'ok',
        'plugin': 'literature',
        'operation': operation,
        'result': payload,
        'provenance': {'backend': PLUGIN_NAME, 'version': PLUGIN_VERSION},
    }


def literature_search(gene_ids, provider='local', evidence_csv=None,
                      cache_dir=None, timeout=15, genome='hg38',
                      gencode_gtf=None):
    values = _normalize_gene_ids(gene_ids)
    result = _provider_factory(
        provider,
        evidence_csv=evidence_csv,
        cache_dir=cache_dir,
        timeout=timeout,
        genome=genome,
        gencode_gtf=gencode_gtf,
    ).search(values)
    return _envelope('search', result)


def literature_summarize(evidence):
    if not isinstance(evidence, dict):
        raise ValueError('evidence must be an object')
    payload = evidence.get('result', evidence)
    matches = payload.get('matches', [])
    if not isinstance(matches, list):
        raise ValueError('evidence.matches must be an array')
    source_counts = Counter(str(item.get('source', 'unknown')) for item in matches)
    summary = {
        'status': 'ok',
        'n_matches': len(matches),
        'sources': dict(sorted(source_counts.items())),
        'genes': sorted({
            str(item.get('gene_id'))
            for item in matches
            if item.get('gene_id') is not None
        }),
        'citations': [
            {
                key: item.get(key)
                for key in ('gene_id', 'source', 'title', 'url', 'pmid', 'doi')
                if item.get(key)
            }
            for item in matches[:20]
        ],
    }
    return _envelope('summarize', summary)


TOOLS = {
    'search': {
        'description': 'Search local evidence, UniProt, PubMed, NCBI Gene, KEGG, UCSC or GENCODE through the shared evidence provider layer.',
        'parameters': _parameters({
            'gene_ids': {'type': 'array', 'items': {'type': 'string'}},
            'provider': {'type': 'string', 'enum': ['local', 'uniprot', 'pubmed', 'ncbi_gene', 'kegg', 'ucsc', 'gencode']},
            'evidence_csv': {'type': 'string'},
            'cache_dir': {'type': 'string'},
            'timeout': {'type': 'number'},
            'genome': {'type': 'string'},
            'gencode_gtf': {'type': 'string'},
        }, ('gene_ids',)),
        'function': literature_search,
    },
    'summarize': {
        'description': 'Create a deterministic citation summary from literature or evidence search results.',
        'parameters': _parameters({
            'evidence': {'type': 'object'},
        }, ('evidence',)),
        'function': literature_summarize,
    },
}
