"""Local TF-IDF knowledge retrieval adapter for evidence-grounded Agent answers."""
import json
import re
from pathlib import Path


PLUGIN_NAME = 'Local scientific knowledge retrieval'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = (
    'knowledge.ingest',
    'knowledge.search',
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
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
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


def knowledge_search(query, index_path, top_k=5):
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must be a non-empty string')
    index_file = Path(index_path)
    if not index_file.exists():
        raise ValueError(f'knowledge index does not exist: {index_file}')
    payload = json.loads(index_file.read_text(encoding='utf-8'))
    documents = payload.get('documents', [])
    if not documents:
        return _envelope('search', {
            'status': 'ok',
            'query': query,
            'matches': [],
            'n_matches': 0,
        })
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    texts = [str(item.get('text', '')) for item in documents]
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
        document = documents[index]
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
}
