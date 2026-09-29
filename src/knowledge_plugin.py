"""Local TF-IDF knowledge retrieval adapter for evidence-grounded Agent answers."""
import hashlib
import json
from collections import Counter
from copy import deepcopy
from functools import lru_cache
from math import log, sqrt
import re
from pathlib import Path


PLUGIN_NAME = 'Local scientific knowledge retrieval'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = (
    'knowledge.ingest',
    'knowledge.search',
    'knowledge.graph',
)


def _parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


def _envelope(operation, payload):
    if not isinstance(payload, dict):
        payload = {'value': payload}
    return {
        'status': payload.get('status', 'ok'),
        'plugin': 'knowledge',
        'operation': operation,
        'result': payload,
        'provenance': {
            'backend': PLUGIN_NAME,
            'version': PLUGIN_VERSION,
            'retrieval': 'tfidf-cosine',
        },
    }


def _document_title(path, text):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#'):
            return line.lstrip('#').strip()
    return path.stem


def knowledge_ingest_directory(input_dir, output_path='output/knowledge/index.json',
                               extensions=None):
    root = Path(input_dir)
    if not root.is_dir():
        raise ValueError(f'knowledge input directory does not exist: {root}')
    allowed = set(extensions or ['.md', '.txt', '.html'])
    documents = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        text = path.read_text(encoding='utf-8', errors='replace').strip()
        if not text:
            continue
        documents.append({
            'id': path.relative_to(root).as_posix(),
            'title': _document_title(path, text),
            'source': str(path),
            'text': text,
        })
    if not documents:
        raise ValueError(f'no knowledge documents found in: {root}')
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': 1,
        'retrieval': 'tfidf-cosine',
        'documents': documents,
    }
    if len(documents) > 32:
        payload['tfidf_index'] = _build_tfidf_index([
            str(item.get('text', '')) for item in documents
        ])
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if 'tfidf_index' in payload else json.dumps(payload, ensure_ascii=False, indent=2)
    )
    target.write_text(encoded + '\n', encoding='utf-8')
    return _envelope('ingest_directory', {
        'status': 'ok',
        'output_path': str(target),
        'n_documents': len(documents),
        'document_ids': [item['id'] for item in documents],
    })


def _snippet(text, query):
    terms = [term.lower() for term in re.findall(r'\w+', query) if len(term) > 2]
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    snippet = text[start:start + 320].replace('\n', ' ')
    return snippet + ('...' if start + 320 < len(text) else '')


def _tfidf_terms(text):
    words = re.findall(r'(?u)\b\w+\b', text.lower())
    return Counter(words + [f'{left} {right}' for left, right in zip(words, words[1:])])


def _document_frequencies(rows):
    document_frequency = Counter()
    for row in rows:
        document_frequency.update(row.keys())
    return document_frequency


def _score_frequencies(rows, query_row, document_frequency):
    document_frequency = Counter(document_frequency)
    document_frequency.update(query_row.keys())
    count = len(rows) + 1
    weights = {
        term: log((1 + count) / (1 + frequency)) + 1
        for term, frequency in document_frequency.items()
    }
    query_norm = sqrt(sum(
        (frequency * weights[term]) ** 2
        for term, frequency in query_row.items()
    ))
    if not query_norm:
        return [0.0] * len(rows)
    scores = []
    for row in rows:
        if not any(term in row for term in query_row):
            scores.append(0.0)
            continue
        document_norm = sqrt(sum(
            (frequency * weights[term]) ** 2
            for term, frequency in row.items()
        ))
        dot = sum(
            frequency * query_row.get(term, 0) * weights[term] ** 2
            for term, frequency in row.items()
        )
        scores.append(dot / (document_norm * query_norm) if document_norm else 0.0)
    return scores


def _small_corpus_scores(texts, query):
    rows = [_tfidf_terms(text) for text in texts]
    return _score_frequencies(rows, _tfidf_terms(query), _document_frequencies(rows))


def _tfidf_digest(texts, statistics):
    encoded = json.dumps(
        [texts, statistics], ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _build_tfidf_index(texts):
    rows = [_tfidf_terms(text) for text in texts]
    frequencies = _document_frequencies(rows)
    vocabulary = list(frequencies)
    columns = {term: column for column, term in enumerate(vocabulary)}
    statistics = {
        'version': 1,
        'vocabulary': vocabulary,
        'term_frequencies': [
            [(columns[term], count) for term, count in row.items()] for row in rows
        ],
        'document_frequency': [frequencies[term] for term in vocabulary],
    }
    return {**statistics, 'digest': _tfidf_digest(texts, statistics)}


def _prepare_tfidf_index(texts, index):
    if not isinstance(index, dict) or index.get('version') != 1:
        return None
    statistics = {key: value for key, value in index.items() if key != 'digest'}
    if index.get('digest') != _tfidf_digest(texts, statistics):
        return None
    try:
        vocabulary = statistics['vocabulary']
        frequencies = statistics['document_frequency']
        if (
            not isinstance(vocabulary, list)
            or not all(isinstance(term, str) for term in vocabulary)
            or len(set(vocabulary)) != len(vocabulary)
            or not isinstance(frequencies, list)
            or len(frequencies) != len(vocabulary)
            or any(type(count) is not int or not 1 <= count <= len(texts) for count in frequencies)
        ):
            return None
        stored_rows = statistics['term_frequencies']
        if not isinstance(stored_rows, list) or len(stored_rows) != len(texts):
            return None
        rows = []
        for stored in stored_rows:
            row = {}
            for column, count in stored:
                if (
                    type(column) is not int or not 0 <= column < len(vocabulary)
                    or type(count) is not int or count < 1
                ):
                    return None
                row[vocabulary[column]] = count
            if len(row) != len(stored):
                return None
            rows.append(row)
        return rows, dict(zip(vocabulary, frequencies))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _prepared_corpus_scores(query, prepared):
    if prepared is None:
        return None
    rows, frequencies = prepared
    query_row = _tfidf_terms(query)
    if not frequencies and not query_row:
        return None
    return _score_frequencies(rows, query_row, frequencies)


def _indexed_corpus_scores(texts, query, index):
    return _prepared_corpus_scores(query, _prepare_tfidf_index(texts, index))


def _decode_index(encoded):
    payload = json.loads(encoded.decode('utf-8'))
    documents = payload.get('documents', [])
    texts = [str(item.get('text', '')) for item in documents]
    prepared = (
        _prepare_tfidf_index(texts, payload.get('tfidf_index')) if len(texts) > 32 else None
    )
    return documents, texts, prepared


@lru_cache(maxsize=4)
def _cached_index(encoded):
    return _decode_index(encoded)


def knowledge_search(query, index_path, top_k=5):
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must be a non-empty string')
    index_file = Path(index_path)
    if not index_file.exists():
        raise ValueError(f'knowledge index does not exist: {index_file}')
    encoded = index_file.read_bytes()
    documents, texts, prepared = (
        _cached_index(encoded) if len(encoded) <= 8 * 1024 * 1024 else _decode_index(encoded)
    )
    if not documents:
        return _envelope('search', {
            'status': 'ok',
            'query': query,
            'matches': [],
            'n_matches': 0,
        })
    if len(texts) <= 32:
        scores = _small_corpus_scores(texts, query)
    else:
        scores = _prepared_corpus_scores(query, prepared)
        if scores is None:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
            vectorizer = TfidfVectorizer(
                lowercase=True,
                ngram_range=(1, 2),
                token_pattern=r'(?u)\b\w+\b',
            )
            matrix = vectorizer.fit_transform(texts + [query])
            scores = cosine_similarity(matrix[-1], matrix[:-1]).ravel()
    ranked = sorted(enumerate(scores), key=lambda item: (-item[1], item[0]))
    matches = []
    for index, score in ranked[:max(1, int(top_k))]:
        if score <= 0:
            continue
        document = deepcopy(documents[index])
        matches.append({
            'document_id': document.get('id'),
            'title': document.get('title'),
            'source': document.get('source'),
            'score': round(float(score), 6),
            'snippet': _snippet(texts[index], query),
        })
    return _envelope('search', {
        'status': 'ok',
        'query': query,
        'index_path': str(index_file),
        'retrieval': 'tfidf-cosine',
        'matches': matches,
        'n_matches': len(matches),
    })


def knowledge_build_graph(evidence, output_path='output/knowledge/evidence_graph.json'):
    if not isinstance(evidence, dict):
        raise ValueError('evidence must be an object')
    payload = evidence.get('result', evidence)
    if not isinstance(payload, dict):
        raise ValueError('evidence result must be an object')
    matches = payload.get('matches', [])
    if not isinstance(matches, list):
        raise ValueError('evidence.matches must be an array')
    nodes = {}
    edges = []

    def add_node(node_id, node_type, label):
        nodes.setdefault(node_id, {'id': node_id, 'type': node_type, 'label': label})

    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            continue
        evidence_id = f'evidence:{index + 1}'
        source = str(match.get('source') or 'unknown')
        source_id = f'source:{source}'
        gene_id = match.get('gene_id')
        add_node(evidence_id, 'evidence', str(match.get('title') or evidence_id))
        add_node(source_id, 'source', source)
        edges.append({
            'source': evidence_id,
            'target': source_id,
            'relation': 'from_source',
        })
        if gene_id:
            gene = str(gene_id)
            gene_node_id = f'gene:{gene}'
            add_node(gene_node_id, 'gene', gene)
            edges.append({
                'source': gene_node_id,
                'target': evidence_id,
                'relation': 'supported_by',
            })

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    graph = {
        'version': 1,
        'graph': {
            'nodes': list(nodes.values()),
            'edges': edges,
        },
        'metrics': {
            'n_nodes': len(nodes),
            'n_edges': len(edges),
            'n_evidence': len({node['id'] for node in nodes.values() if node['type'] == 'evidence'}),
            'n_genes': len({node['id'] for node in nodes.values() if node['type'] == 'gene'}),
            'n_sources': len({node['id'] for node in nodes.values() if node['type'] == 'source'}),
        },
        'provenance': {
            'input_operation': payload.get('status', 'unknown'),
            'relation_policy': 'gene-supported_by-evidence-from_source',
        },
        'output_path': str(target),
    }
    target.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return _envelope('build_graph', graph)


TOOLS = {
    'ingest_directory': {
        'description': 'Build a local JSON knowledge index from Markdown, text or HTML documents.',
        'parameters': _parameters({
            'input_dir': {'type': 'string'},
            'output_path': {'type': 'string'},
            'extensions': {'type': 'array', 'items': {'type': 'string'}},
        }, ('input_dir',)),
        'function': knowledge_ingest_directory,
    },
    'search': {
        'description': 'Retrieve ranked evidence snippets from a local knowledge index.',
        'parameters': _parameters({
            'query': {'type': 'string'},
            'index_path': {'type': 'string'},
            'top_k': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        }, ('query', 'index_path')),
        'function': knowledge_search,
    },
    'build_graph': {
        'description': 'Build a traceable evidence knowledge graph with gene, evidence and source nodes.',
        'parameters': _parameters({
            'evidence': {'type': 'object'},
            'output_path': {'type': 'string'},
        }, ('evidence',)),
        'function': knowledge_build_graph,
    },
}
