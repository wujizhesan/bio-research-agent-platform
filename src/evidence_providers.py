"""Evidence providers for the omics Agent."""
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

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


class NcbiGeneEvidenceProvider:
    base_endpoint = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'

    def __init__(self, timeout=15, cache_dir=None, organism='Homo sapiens', email=None, api_key=None):
        self.timeout = float(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.organism = str(organism)
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
        key = hashlib.sha256(
            f'ncbi_gene:{self.organism}:{gene_id}'.encode('utf-8')
        ).hexdigest()
        return self.cache_dir / f'ncbi_gene_{key}.json'

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
                db='gene',
                term=f'{gene_id}[gene name] AND {self.organism}[orgn]',
                retmode='json',
                retmax=3,
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
                params=self._params(db='gene', id=','.join(ids), retmode='json'),
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
    def _organism(summary):
        organism = summary.get('organism', '')
        if isinstance(organism, dict):
            return organism.get('scientificname') or organism.get('scientificName') or ''
        return str(organism or '')

    def _records(self, gene_id, payload):
        result = payload.get('summary', {}).get('result', {})
        ids = payload.get('ids') or result.get('uids', [])
        records = []
        for ncbi_gene_id in ids:
            summary = result.get(str(ncbi_gene_id), {})
            if not summary:
                continue
            organism = self._organism(summary)
            chromosome = summary.get('chromosome', '')
            map_location = summary.get('maplocation') or summary.get('map_location', '')
            title = summary.get('description') or summary.get('name') or f'NCBI Gene {ncbi_gene_id}'
            evidence = f'NCBI Gene record; organism={organism}; chromosome={chromosome}; map_location={map_location}'
            records.append({
                'gene_id': gene_id,
                'source': 'NCBI Gene',
                'title': title,
                'evidence': evidence,
                'ncbi_gene_id': str(ncbi_gene_id),
                'organism': organism,
                'chromosome': chromosome,
                'map_location': map_location,
                'url': f'https://www.ncbi.nlm.nih.gov/gene/{ncbi_gene_id}',
            })
        return records

    def search(self, gene_ids):
        requested = sorted({str(gene_id) for gene_id in gene_ids})
        matches = []
        for gene_id in requested:
            matches.extend(self._records(gene_id, self._request(gene_id)))
        return {
            'status': 'ok',
            'provider': 'ncbi_gene',
            'requested_gene_ids': requested,
            'matches': matches,
            'n_matches': len(matches),
            'endpoint': self.endpoint,
            'organism': self.organism,
            'cache_dir': str(self.cache_dir) if self.cache_dir else None,
        }


class KeggEvidenceProvider:
    base_endpoint = 'https://rest.kegg.jp'

    def __init__(self, timeout=15, cache_dir=None, organism='hsa'):
        self.timeout = float(timeout)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.organism = str(organism)
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def endpoint(self):
        return f'{self.base_endpoint}/find/{self.organism}'

    def _cache_path(self, gene_id):
        if not self.cache_dir:
            return None
        key = hashlib.sha256(
            f'kegg:{self.organism}:{gene_id}'.encode('utf-8')
        ).hexdigest()
        return self.cache_dir / f'kegg_{key}.json'

    @staticmethod
    def _parse_find(text):
        matches = []
        for line in str(text or '').splitlines():
            parts = line.split('\t', 1)
            if len(parts) == 2:
                kegg_id, description = parts
                matches.append({
                    'kegg_id': kegg_id.strip(),
                    'description': description.strip(),
                })
        return matches

    @staticmethod
    def _parse_pathways(text):
        pathways = []
        for line in str(text or '').splitlines():
            parts = line.split('\t', 1)
            if len(parts) == 2:
                pathway_id = parts[1].strip()
                if pathway_id and pathway_id not in pathways:
                    pathways.append(pathway_id)
        return pathways

    def _request(self, gene_id):
        cache_path = self._cache_path(gene_id)
        if cache_path and cache_path.exists():
            return json.loads(cache_path.read_text(encoding='utf-8'))
        encoded_gene_id = quote(str(gene_id), safe=':_-.')
        find_response = requests.get(
            f'{self.base_endpoint}/find/{self.organism}/{encoded_gene_id}',
            headers={'Accept': 'text/plain', 'User-Agent': 'cadd-agent/omics'},
            timeout=self.timeout,
        )
        find_response.raise_for_status()
        matches = self._parse_find(find_response.text)
        for match in matches:
            pathway_response = requests.get(
                f'{self.base_endpoint}/link/pathway/{quote(match["kegg_id"], safe=":_-.")}',
                headers={'Accept': 'text/plain', 'User-Agent': 'cadd-agent/omics'},
                timeout=self.timeout,
            )
            pathway_response.raise_for_status()
            match['pathways'] = self._parse_pathways(pathway_response.text)
        payload = {'matches': matches}
        if cache_path:
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
        return payload

    def _records(self, gene_id, payload):
        records = []
        for match in payload.get('matches', []):
            kegg_id = match.get('kegg_id', '')
            pathways = match.get('pathways', [])
            title = match.get('description') or kegg_id
            evidence = f'KEGG gene {kegg_id}; pathways={"|".join(pathways)}'
            records.append({
                'gene_id': gene_id,
                'source': 'KEGG',
                'title': title,
                'evidence': evidence,
                'kegg_id': kegg_id,
                'organism': self.organism,
                'pathways': pathways,
                'url': f'https://www.kegg.jp/entry/{kegg_id}',
            })
        return records

    def search(self, gene_ids):
        requested = sorted({str(gene_id) for gene_id in gene_ids})
        matches = []
        for gene_id in requested:
            matches.extend(self._records(gene_id, self._request(gene_id)))
        return {
            'status': 'ok',
            'provider': 'kegg',
            'requested_gene_ids': requested,
            'matches': matches,
            'n_matches': len(matches),
            'endpoint': self.endpoint,
            'organism': self.organism,
            'cache_dir': str(self.cache_dir) if self.cache_dir else None,
        }


class UcscEvidenceProvider:
    endpoint = 'https://api.genome.ucsc.edu/search'

    def __init__(self, genome='hg38', cache_dir=None, timeout=15):
        self.genome = str(genome or 'hg38')
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = float(timeout)
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, gene_id):
        if not self.cache_dir:
            return None
        key = hashlib.sha256(
            f'ucsc:{self.genome}:{gene_id}'.encode('utf-8')
        ).hexdigest()
        return self.cache_dir / f'ucsc_{key}.json'

    def _request(self, gene_id):
        cache_path = self._cache_path(gene_id)
        if cache_path and cache_path.exists():
            return json.loads(cache_path.read_text(encoding='utf-8'))
        response = requests.get(
            self.endpoint,
            params={'search': str(gene_id), 'genome': self.genome},
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

    def _records(self, gene_id, payload):
        records = []
        for group in payload.get('positionMatches', []):
            track = group.get('trackName') or group.get('name', '')
            for match in group.get('matches', []):
                position = str(match.get('position') or '')
                if ':' not in position or '-' not in position:
                    continue
                chromosome, coordinates = position.rsplit(':', 1)
                start, end = coordinates.split('-', 1)
                try:
                    start = int(start)
                    end = int(end)
                except ValueError:
                    continue
                title = match.get('posName') or match.get('hgFindMatches') or track
                records.append({
                    'gene_id': gene_id,
                    'source': 'UCSC',
                    'title': title,
                    'evidence': match.get('description') or group.get('description', ''),
                    'genome': self.genome,
                    'track': track,
                    'chromosome': chromosome,
                    'start': start,
                    'end': end,
                    'match_id': match.get('hgFindMatches', ''),
                    'canonical': bool(match.get('canonical', False)),
                    'position': position,
                    'url': (
                        'https://genome.ucsc.edu/cgi-bin/hgTracks?'
                        f'db={self.genome}&position={position}'
                    ),
                })
        return records

    def search(self, gene_ids):
        requested = sorted({str(gene_id) for gene_id in gene_ids})
        matches = []
        for gene_id in requested:
            matches.extend(self._records(gene_id, self._request(gene_id)))
        return {
            'status': 'ok',
            'provider': 'ucsc',
            'requested_gene_ids': requested,
            'matches': matches,
            'n_matches': len(matches),
            'endpoint': self.endpoint,
            'genome': self.genome,
            'cache_dir': str(self.cache_dir) if self.cache_dir else None,
        }


class GencodeEvidenceProvider:
    _attribute_pattern = re.compile(r'([A-Za-z][A-Za-z0-9_]*)\s+(?:"([^"]*)"|([^;\s]+))')

    def __init__(self, gencode_gtf=None):
        if not gencode_gtf:
            raise ValueError('gencode_gtf is required for the gencode provider')
        self.gencode_gtf = Path(gencode_gtf)
        if not self.gencode_gtf.is_file():
            raise ValueError(f'GENCODE GTF does not exist: {self.gencode_gtf}')

    @classmethod
    def _attributes(cls, raw):
        attributes = {}
        for match in cls._attribute_pattern.finditer(raw):
            attributes[match.group(1)] = match.group(2) or match.group(3) or ''
        return attributes

    @staticmethod
    def _matches(value, requested):
        value = str(value or '')
        return value in requested or value.split('.', 1)[0] in requested

    def search(self, gene_ids):
        requested = sorted({str(gene_id).strip() for gene_id in gene_ids if str(gene_id).strip()})
        if not requested:
            raise ValueError('gene_ids must contain at least one non-empty identifier')
        matches = []
        opener = gzip.open if self.gencode_gtf.suffix.lower() == '.gz' else open
        with opener(self.gencode_gtf, 'rt', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                if not line.strip() or line.startswith('#'):
                    continue
                fields = line.rstrip('\n\r').split('\t')
                if len(fields) != 9 or fields[2] != 'gene':
                    continue
                attributes = self._attributes(fields[8])
                gene_id = attributes.get('gene_id', '')
                gene_name = attributes.get('gene_name', '')
                if not any(self._matches(value, set(requested)) for value in (gene_id, gene_name)):
                    continue
                matches.append({
                    'gene_id': gene_id,
                    'source': 'GENCODE',
                    'title': gene_name or gene_id,
                    'evidence': (
                        f'GENCODE gene annotation; type={attributes.get("gene_type", "")}; '
                        f'assembly coordinates={fields[0]}:{fields[3]}-{fields[4]}'
                    ),
                    'gene_name': gene_name,
                    'gene_type': attributes.get('gene_type', ''),
                    'chromosome': fields[0],
                    'start': int(fields[3]),
                    'end': int(fields[4]),
                    'strand': fields[6],
                    'level': attributes.get('level', ''),
                    'gencode_gtf': str(self.gencode_gtf),
                })
        return {
            'status': 'ok',
            'provider': 'gencode',
            'requested_gene_ids': requested,
            'matches': matches,
            'n_matches': len(matches),
            'source_file': str(self.gencode_gtf),
        }


def get_evidence_provider(provider='local', evidence_csv=None, cache_dir=None,
                          timeout=15, genome='hg38', gencode_gtf=None):
    if provider == 'local':
        return LocalEvidenceProvider(evidence_csv)
    if provider == 'uniprot':
        return UniProtEvidenceProvider(cache_dir=cache_dir, timeout=timeout)
    if provider == 'pubmed':
        return PubMedEvidenceProvider(cache_dir=cache_dir, timeout=timeout)
    if provider == 'ncbi_gene':
        return NcbiGeneEvidenceProvider(cache_dir=cache_dir, timeout=timeout)
    if provider == 'kegg':
        return KeggEvidenceProvider(cache_dir=cache_dir, timeout=timeout)
    if provider == 'ucsc':
        return UcscEvidenceProvider(genome=genome, cache_dir=cache_dir, timeout=timeout)
    if provider == 'gencode':
        return GencodeEvidenceProvider(gencode_gtf=gencode_gtf)
    raise ValueError(f'unknown evidence provider: {provider}')
