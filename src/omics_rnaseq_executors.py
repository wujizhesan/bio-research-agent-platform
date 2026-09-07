"""Native RNA-seq alignment and quantification executors."""
import json
from pathlib import Path

import pandas as pd

try:
    from .omics_validation import normalize_alignment_paths, normalize_fastq_paths
except ImportError:
    from omics_validation import normalize_alignment_paths, normalize_fastq_paths


def _write_manifest(output_dir, filename, payload):
    manifest_path = Path(output_dir) / filename
    payload['manifest_path'] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return payload


def _feature_counts_sample_name(value):
    normalized = str(value).replace('\\', '/')
    name = normalized.rsplit('/', 1)[-1]
    for suffix in ('.bam', '.cram'):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return Path(name).stem


def _parse_feature_counts_output(counts_path, output_csv):
    frame = pd.read_csv(counts_path, sep='\t', comment='#')
    if frame.empty or 'Geneid' not in frame.columns:
        raise ValueError('featureCounts output is missing the Geneid column')
    sample_columns = list(frame.columns[6:])
    if not sample_columns:
        raise ValueError('featureCounts output has no sample count columns')
    sample_names = [_feature_counts_sample_name(column) for column in sample_columns]
    if len(set(sample_names)) != len(sample_names):
        raise ValueError('featureCounts sample names are not unique after normalization')
    result = frame.loc[:, ['Geneid', *sample_columns]].rename(columns={'Geneid': 'gene_id'})
    result['gene_id'] = result['gene_id'].astype(str)
    if result['gene_id'].eq('').any() or result['gene_id'].duplicated().any():
        raise ValueError('featureCounts gene identifiers must be non-empty and unique')
    result = result.rename(columns=dict(zip(sample_columns, sample_names)))
    result[sample_names] = result[sample_names].apply(pd.to_numeric, errors='raise')
    if result[sample_names].isna().any().any() or (result[sample_names] < 0).any().any():
        raise ValueError('featureCounts counts must be non-negative numbers')
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_csv, index=False)
    return result, sample_names


def _fastq_sample_name(path):
    name = Path(path).name
    for suffix in ('.fastq.gz', '.fq.gz', '.fastq', '.fq'):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break
    for suffix in ('_R1', '_R2', '.R1', '.R2', '_1', '_2', '.1', '.2'):
        if name.lower().endswith(suffix.lower()):
            name = name[:-len(suffix)]
            break
    return name


def _parse_hisat2_alignment_rate(stderr):
    for line in str(stderr or '').splitlines():
        if 'overall alignment rate' in line.lower():
            return line.strip().split()[0]
    return None


def _hisat2_index_complete(prefix):
    prefix = str(prefix)
    return any(Path(f'{prefix}.{suffix}').is_file() for suffix in ('1.ht2', '1.ht2l'))


def execute_rnaseq_alignment(fastq_paths, reference_fasta, output_dir,
                         output_alignment_paths=None, fastq_r2_paths=None,
                         threads=1, timeout=1800, *, dependencies):
    paths = normalize_fastq_paths(fastq_paths)
    mate_paths = normalize_fastq_paths(fastq_r2_paths) if fastq_r2_paths is not None else None
    if mate_paths is not None and len(mate_paths) != len(paths):
        raise ValueError('fastq_r2_paths must match the number of FASTQ R1 inputs')
    reference_fasta = Path(reference_fasta)
    if not reference_fasta.is_file():
        raise ValueError(f'reference FASTA does not exist: {reference_fasta}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    threads = max(1, min(int(threads), 64))
    timeout = max(1, min(int(timeout), 3600))
    sample_names = [_fastq_sample_name(path) for path in paths]
    if len(set(sample_names)) != len(sample_names):
        raise ValueError('FASTQ sample names must be unique')
    if mate_paths is not None:
        mate_sample_names = [_fastq_sample_name(path) for path in mate_paths]
        if sample_names != mate_sample_names:
            raise ValueError('FASTQ R1 and R2 sample names must match')
    if output_alignment_paths is None:
        alignment_paths = [output_dir / f'{sample_name}.bam' for sample_name in sample_names]
    else:
        values = output_alignment_paths if isinstance(output_alignment_paths, (list, tuple)) else [output_alignment_paths]
        if len(values) != len(paths):
            raise ValueError('output_alignment_paths must match the number of FASTQ inputs')
        alignment_paths = [Path(str(value)) for value in values]
    for path in alignment_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    hisat2 = dependencies.which('hisat2')
    hisat2_build = dependencies.which('hisat2-build')
    samtools = dependencies.which('samtools')
    missing_tools = [
        name for name, path in (
            ('hisat2', hisat2),
            ('hisat2-build', hisat2_build),
            ('samtools', samtools),
        ) if not path
    ]
    index_prefix = output_dir / 'hisat2_index'
    index_metadata = output_dir / 'hisat2_index.json'
    reference_sha256 = dependencies.sha256(reference_fasta)
    provenance = {
        'inputs': {
            'fastq': [
                {'path': str(path), 'sha256': dependencies.sha256(path)} for path in paths
            ],
            'reference_fasta': {
                'path': str(reference_fasta),
                'sha256': reference_sha256,
            },
        },
        'parameters': {
            'layout': 'paired_end' if mate_paths is not None else 'single_end',
            'threads': threads,
        },
        'tools': {},
    }
    if mate_paths is not None:
        provenance['inputs']['fastq_r2'] = [
            {'path': str(path), 'sha256': dependencies.sha256(path)} for path in mate_paths
        ]
    if missing_tools:
        return _write_manifest(output_dir, 'rnaseq_alignment.json', {
            'status': 'unavailable',
            'workflow': 'rnaseq_alignment',
            'alignment_paths': [str(path) for path in alignment_paths],
            'missing_tools': missing_tools,
            'reason': 'HISAT2 and SAMtools are required for RNA-seq alignment',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'hisat2': {'path': hisat2, **dependencies.version(hisat2)},
        'hisat2-build': {'path': hisat2_build, **dependencies.version(hisat2_build)},
        'samtools': {'path': samtools, **dependencies.version(samtools)},
    }
    steps = []

    def execute(step_id, command, stdout_path=None):
        result = dependencies.run_command(command, timeout, stdout_path)
        steps.append({'id': step_id, 'command': command, **result})
        return result

    index_reusable = False
    if _hisat2_index_complete(index_prefix) and index_metadata.is_file():
        try:
            metadata = json.loads(index_metadata.read_text(encoding='utf-8'))
            index_reusable = metadata.get('reference_sha256') == reference_sha256
        except (OSError, ValueError, TypeError):
            index_reusable = False
    if not index_reusable:
        index_result = execute(
            'hisat2_index',
            [hisat2_build, '-p', str(threads), str(reference_fasta), str(index_prefix)],
            output_dir / 'hisat2_build.log',
        )
        if index_result['status'] != 'completed' or not _hisat2_index_complete(index_prefix):
            return _write_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        index_metadata.write_text(
            json.dumps({
                'reference_fasta': str(reference_fasta),
                'reference_sha256': reference_sha256,
                'index_prefix': str(index_prefix),
            }, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
    sample_results = []
    for fastq_path, mate_path, sample_name, alignment_path in zip(
        paths, mate_paths or [None] * len(paths), sample_names, alignment_paths
    ):
        sam_path = output_dir / f'{sample_name}.hisat2.sam'
        raw_bam = output_dir / f'{sample_name}.hisat2.raw.bam'
        hisat2_reads = (
            ['-1', str(fastq_path), '-2', str(mate_path)]
            if mate_path is not None
            else ['-U', str(fastq_path)]
        )
        hisat2_result = execute(
            f'hisat2_{sample_name}',
            [
                hisat2, '-p', str(threads), '--dta',
                '-x', str(index_prefix), *hisat2_reads,
                '-S', str(sam_path),
            ],
            output_dir / f'{sample_name}.hisat2.log',
        )
        if hisat2_result['status'] != 'completed' or not sam_path.is_file():
            return _write_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        view_result = execute(
            f'samtools_view_{sample_name}',
            [samtools, 'view', '-b', '-o', str(raw_bam), str(sam_path)],
            output_dir / f'{sample_name}.view.log',
        )
        if view_result['status'] != 'completed':
            return _write_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        sort_result = execute(
            f'samtools_sort_{sample_name}',
            [samtools, 'sort', '-o', str(alignment_path), str(raw_bam)],
            output_dir / f'{sample_name}.sort.log',
        )
        if sort_result['status'] != 'completed' or not alignment_path.is_file():
            return _write_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        index_result = execute(
            f'samtools_index_{sample_name}',
            [samtools, 'index', str(alignment_path)],
            output_dir / f'{sample_name}.index.log',
        )
        if index_result['status'] != 'completed':
            return _write_manifest(output_dir, 'rnaseq_alignment.json', {
                'status': 'failed',
                'workflow': 'rnaseq_alignment',
                'alignment_paths': [str(path) for path in alignment_paths],
                'steps': steps,
                'provenance': provenance,
            })
        sam_path.unlink(missing_ok=True)
        raw_bam.unlink(missing_ok=True)
        sample_result = {
            'sample_id': sample_name,
            'bam_path': str(alignment_path),
            'bai_path': str(Path(str(alignment_path) + '.bai')),
            'overall_alignment_rate': _parse_hisat2_alignment_rate(hisat2_result.get('stderr')),
        }
        if mate_path is None:
            sample_result['fastq_path'] = str(fastq_path)
        else:
            sample_result['fastq_r1_path'] = str(fastq_path)
            sample_result['fastq_r2_path'] = str(mate_path)
        sample_results.append(sample_result)
    return _write_manifest(output_dir, 'rnaseq_alignment.json', {
        'status': 'completed',
        'workflow': 'rnaseq_alignment',
        'reference_fasta': str(reference_fasta),
        'index_prefix': str(index_prefix),
        'alignment_paths': [str(path) for path in alignment_paths],
        'samples': sample_results,
        'steps': steps,
        'provenance': provenance,
    })


def execute_feature_counts(alignment_paths, annotation_gtf, output_dir, output_csv=None,
                       feature_type='exon', gene_id_attribute='gene_id', strand=0,
                       paired_end=False, threads=1, timeout=900, *, dependencies):
    paths = normalize_alignment_paths(alignment_paths)
    annotation_gtf = Path(annotation_gtf)
    if not annotation_gtf.is_file():
        raise ValueError(f'GTF annotation does not exist: {annotation_gtf}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = Path(output_csv) if output_csv else output_dir / 'expression_counts.csv'
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    raw_counts = output_dir / 'featurecounts.tsv'
    summary_path = Path(str(raw_counts) + '.summary')
    timeout = max(1, min(int(timeout), 3600))
    threads = max(1, min(int(threads), 64))
    strand = int(strand)
    if strand not in {0, 1, 2}:
        raise ValueError('strand must be 0, 1 or 2')
    executable = dependencies.which('featureCounts')
    provenance = {
        'inputs': {
            'alignments': [
                {'path': str(path), 'sha256': dependencies.sha256(path)} for path in paths
            ],
            'annotation_gtf': {
                'path': str(annotation_gtf),
                'sha256': dependencies.sha256(annotation_gtf),
            },
        },
        'parameters': {
            'feature_type': str(feature_type),
            'gene_id_attribute': str(gene_id_attribute),
            'strand': strand,
            'paired_end': bool(paired_end),
            'threads': threads,
        },
        'tool': {},
    }
    if not executable:
        return _write_manifest(output_dir, 'feature_counts.json', {
            'status': 'unavailable',
            'workflow': 'rnaseq_feature_counts',
            'output_csv': str(output_csv),
            'missing_tools': ['featureCounts'],
            'reason': 'featureCounts not found in PATH; install the Subread package',
            'provenance': provenance,
        })
    provenance['tool'] = {
        'path': executable,
        **dependencies.version(executable),
    }
    command = [
        executable, '-T', str(threads), '-a', str(annotation_gtf),
        '-t', str(feature_type), '-g', str(gene_id_attribute),
        '-s', str(strand), '-o', str(raw_counts),
    ]
    if paired_end:
        command.extend(['-p', '--countReadPairs'])
    command.extend(str(path) for path in paths)
    result = dependencies.run_command(command, timeout, output_dir / 'featurecounts.log')
    if result['status'] != 'completed' or not raw_counts.is_file():
        return _write_manifest(output_dir, 'feature_counts.json', {
            'status': 'failed',
            'workflow': 'rnaseq_feature_counts',
            'output_csv': str(output_csv),
            'featurecounts_output': str(raw_counts),
            'summary_path': str(summary_path),
            'command': command,
            'command_result': result,
            'provenance': provenance,
        })
    try:
        counts, sample_names = _parse_feature_counts_output(raw_counts, output_csv)
    except Exception as exc:
        return _write_manifest(output_dir, 'feature_counts.json', {
            'status': 'failed',
            'workflow': 'rnaseq_feature_counts',
            'output_csv': str(output_csv),
            'featurecounts_output': str(raw_counts),
            'summary_path': str(summary_path),
            'command': command,
            'error': str(exc),
            'provenance': provenance,
        })
    return _write_manifest(output_dir, 'feature_counts.json', {
        'status': 'completed',
        'workflow': 'rnaseq_feature_counts',
        'output_csv': str(output_csv),
        'featurecounts_output': str(raw_counts),
        'summary_path': str(summary_path),
        'command': command,
        'n_genes': int(len(counts)),
        'n_samples': len(sample_names),
        'sample_names': sample_names,
        'provenance': provenance,
    })
