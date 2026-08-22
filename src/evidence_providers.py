"""Evidence providers for the omics Agent."""
import hashlib
import os
import json
from pathlib import Path

import pandas as pd
import requests


class LocalEvidenceProvider:
    def __init__(self, evidence_csv):
        if not evidence_csv:
            raise ValueError('evidence_csv is required for the local provider')
        self.evidence_csv = str(evidence_csv)

    def search(self, gene_ids):
        evidence = pd.read_csv(self.evidence_csv)
        required = {'gene_id', 'source', 'title', 'evidence'}
        missing = required - set(evidence.columns)
        if missing:
            raise ValueError(f'evidence table missing columns: {sorted(missing)}')
        requested = {str(gene_id) for gene_id in gene_ids}
        matches = evidence[evidence['gene_id'].astype(str).isin(requested)].copy()
        return {
            'status': 'ok',
            'provider': 'local',
            'requested_gene_ids': sorted(requested),
            'matches': matches.fillna('').to_dict('records'),
            'n_matches': int(len(matches)),
            'source_file': self.evidence_csv,
        }


class UniProtEvidenceProvider:
    endpoint = 'https://rest.uniprot.org/uniprotkb/search'

    def __init__(self, organism_id=9606, timeout=15, cache_dir=None):
        self.organism_id = int(organism_id)
        self.timeout = float(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, gene_id):
        if not self.cache_dir:
            return None
        key = hashlib.sha256(
            f'{self.organism_id}:{gene_id}'.encode('utf-8')
        ).hexdigest()
        return self.cache_dir / f'{key}.json'

    def _request(self, gene_id):
        cache_path = self._cache_path(gene_id)
        if cache_path and cache_path.exists():
            return json.loads(cache_path.read_text(encoding='utf-8'))
        response = requests.get(
            self.endpoint,
            params={
                'query': f'gene:{gene_id} AND organism_id:{self.organism_id}',
                'format': 'json',
                'size': 3,
                'fields': 'accession,id,gene_names,protein_name,cc_function,organism_name',
            },
            headers={'Accept': 'application/json', 'User-Agent': 'cadd-agent/omics'},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if cache_path:
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
        return payload

    @staticmethod
    def _text(value):
        if isinstance(value, dict):
            return value.get('value', '')
        return str(value or '')

    def _records(self, gene_id, payload):
        records = []
        for result in payload.get('results', []):
            accession = result.get('primaryAccession', '')
            genes = result.get('genes', [])
            gene_names = []
            for gene in genes:
                if gene.get('geneName'):
                    gene_names.append(self._text(gene['geneName']))
                gene_names.extend(
                    self._text(item)
                    for item in gene.get('synonyms', [])
                )
            recommended = result.get('proteinDescription', {}).get('recommendedName', {})
            title = self._text(recommended.get('fullName', ''))
            functions = []
            for comment in result.get('comments', []):
                if comment.get('commentType') == 'FUNCTION':
                    functions.extend(self._text(item) for item in comment.get('texts', []))
            organism = result.get('organism', {}).get('scientificName', '')
            records.append({
                'gene_id': gene_id,
                'source': 'UniProt',
                'title': title or result.get('uniProtkbId', accession),
                'evidence': ' '.join(functions),
                'accession': accession,
                'gene_names': '|'.join(gene_names),
                'organism': organism,
                'url': f'https://www.uniprot.org/uniprotkb/{accession}/entry',
            })
        return records

    def search(self, gene_ids):
        requested = sorted({str(gene_id) for gene_id in gene_ids})
        matches = []
        for gene_id in requested:
            matches.extend(self._records(gene_id, self._request(gene_id)))
        return {
            'status': 'ok',
            'provider': 'uniprot',
            'requested_gene_ids': requested,
            'matches': matches,
            'n_matches': len(matches),
            'endpoint': self.endpoint,
            'organism_id': self.organism_id,
            'cache_dir': str(self.cache_dir) if self.cache_dir else None,
        }


class PubMedEvidenceProvider:
    base_endpoint = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'

    def __init__(self, timeout=15, cache_dir=None, retmax=5, email=None, api_key=None):
        self.timeout = float(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.retmax = int(retmax)
        self.email = email or os.environ.get('NCBI_EMAIL')
        self.api_key = api_key or os.environ.get('NCBI_API_KEY')
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def endpoint(self):
        return f'{self.base_endpoint}/esearch.fcgi'

    def _cache_path(self, gene_id):
        if not self.cache_dir:
            return None
        key = hashlib.sha256(f'pubmed:{gene_id}:{self.retmax}'.encode('utf-8')).hexdigest()
        return self.cache_dir / f'pubmed_{key}.json'

    def _params(self, **values):
        params = dict(values)
        if self.email:
            params['email'] = self.email
        if self.api_key:
            params['api_key'] = self.api_key
        params['tool'] = 'cadd-agent'
        return params

    def _request(self, gene_id):
        cache_path = self._cache_path(gene_id)
        if cache_path and cache_path.exists():
            return json.loads(cache_path.read_text(encoding='utf-8'))
        search_response = requests.get(
            self.endpoint,
            params=self._params(
                db='pubmed',
                term=f'{gene_id}[gene name] OR {gene_id}[title/abstract]',
                retmode='json',
                retmax=self.retmax,
            ),
            headers={'Accept': 'application/json', 'User-Agent': 'cadd-agent/omics'},
            timeout=self.timeout,
        )
        search_response.raise_for_status()
        search_payload = search_response.json()
        ids = search_payload.get('esearchresult', {}).get('idlist', [])
        summary_payload = {'result': {'uids': []}}
        if ids:
            summary_response = requests.get(
                f'{self.base_endpoint}/esummary.fcgi',
                params=self._params(db='pubmed', id=','.join(ids), retmode='json'),
                headers={'Accept': 'application/json', 'User-Agent': 'cadd-agent/omics'},
                timeout=self.timeout,
            )
            summary_response.raise_for_status()
            summary_payload = summary_response.json()
        payload = {'ids': ids, 'summary': summary_payload}
        if cache_path:
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
        return payload

    @staticmethod
    def _authors(summary):
        return '|'.join(
            author.get('name', '')
            for author in summary.get('authors', [])
            if author.get('name')
        )

    def _records(self, gene_id, payload):
        result = payload.get('summary', {}).get('result', {})
        records = []
        for pmid in payload.get('ids', []):
            summary = result.get(str(pmid), {})
            if not summary:
                continue
            doi = summary.get('elocationid', '')
            if doi.lower().startswith('doi:'):
                doi = doi[4:]
            records.append({
                'gene_id': gene_id,
                'source': 'PubMed',
                'title': summary.get('title', ''),
                'evidence': (
                    f'PubMed citation metadata; journal={summary.get("fulljournalname", "")}; '
                    f'pubdate={summary.get("pubdate", "")}'
                ),
                'pmid': str(pmid),
                'authors': self._authors(summary),
                'journal': summary.get('fulljournalname', ''),
                'pubdate': summary.get('pubdate', ''),
                'doi': doi,
                'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
            })
        return records

    def search(self, gene_ids):
        requested = sorted({str(gene_id) for gene_id in gene_ids})
        matches = []
        for gene_id in requested:
            matches.extend(self._records(gene_id, self._request(gene_id)))
        return {
            'status': 'ok',
            'provider': 'pubmed',
            'requested_gene_ids': requested,
            'matches': matches,
            'n_matches': len(matches),
            'endpoint': self.endpoint,
            'retmax': self.retmax,
            'cache_dir': str(self.cache_dir) if self.cache_dir else None,
        }

def get_evidence_provider(provider='local', evidence_csv=None, cache_dir=None, timeout=15):
    if provider == 'local':
        return LocalEvidenceProvider(evidence_csv)
    if provider == 'uniprot':
        return UniProtEvidenceProvider(cache_dir=cache_dir, timeout=timeout)
    if provider == 'pubmed':
        return PubMedEvidenceProvider(cache_dir=cache_dir, timeout=timeout)
    raise ValueError(f'unknown evidence provider: {provider}')
