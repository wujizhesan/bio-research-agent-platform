"""FASTQ and general genomics quality-control executors."""

import gzip
import json
import re
import zipfile
from pathlib import Path

try:
    from .omics_validation import (
        normalize_fastq_paths,
        normalize_qc_paths,
        resolve_qc_type,
    )
except ImportError:
    from omics_validation import (
        normalize_fastq_paths,
        normalize_qc_paths,
        resolve_qc_type,
    )


def _fastq_file_stats(path):
    opener = gzip.open if path.name.lower().endswith('.gz') else open
    reads = 0
    bases = 0
    quality_sum = 0
    min_length = None
    max_length = 0
    with opener(path, 'rt', encoding='utf-8', errors='replace') as handle:
        while True:
            header = handle.readline()
            if not header:
                break
            sequence = handle.readline().rstrip('\r\n')
            separator = handle.readline().rstrip('\r\n')
            quality = handle.readline().rstrip('\r\n')
            if not sequence or not header.startswith('@') or not separator.startswith('+'):
                raise ValueError(f'invalid FASTQ record in: {path}')
            if len(sequence) != len(quality):
                raise ValueError(f'FASTQ sequence/quality length mismatch in: {path}')
            length = len(sequence)
            reads += 1
            bases += length
            quality_sum += sum(max(0, ord(char) - 33) for char in quality)
            min_length = length if min_length is None else min(min_length, length)
            max_length = max(max_length, length)
    return {
        'path': str(path),
        'reads': reads,
        'bases': bases,
        'min_read_length': min_length or 0,
        'max_read_length': max_length,
        'mean_read_length': round(bases / reads, 3) if reads else 0.0,
        'mean_quality': round(quality_sum / bases, 3) if bases else 0.0,
    }


def _write_manifest(output_dir, filename, payload):
    manifest_path = Path(output_dir) / filename
    payload['manifest_path'] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return payload


def _parse_stat_value(text, label):
    for line in str(text or '').splitlines():
        fields = line.split('\t')
        if len(fields) >= 3 and fields[0] == 'SN' and label in fields[2]:
            return fields[3] if len(fields) > 3 else None
        if len(fields) >= 2 and fields[0] == 'SN' and label in fields[1]:
            return fields[2] if len(fields) > 2 else None
    return None


def _parse_flagstat_total(text):
    match = re.search(
        r'^(\d+)\s*\+\s*(\d+)\s+in total',
        str(text or ''),
        re.MULTILINE,
    )
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2))


def execute_genomics_qc(input_path, output_dir, input_type='auto', timeout=300,
                        *, dependencies):
    paths = normalize_qc_paths(input_path)
    resolved_type = resolve_qc_type(paths, input_type)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timeout = max(1, min(int(timeout), 3600))
    if resolved_type == 'fastq':
        file_metrics = [_fastq_file_stats(path) for path in paths]
        totals = {
            'files': len(file_metrics),
            'reads': sum(item['reads'] for item in file_metrics),
            'bases': sum(item['bases'] for item in file_metrics),
        }
        total_bases = totals['bases']
        totals.update({
            'min_read_length': min(
                (item['min_read_length'] for item in file_metrics if item['reads']),
                default=0,
            ),
            'max_read_length': max(
                (item['max_read_length'] for item in file_metrics),
                default=0,
            ),
            'mean_read_length': round(
                totals['bases'] / totals['reads'], 3
            ) if totals['reads'] else 0.0,
            'mean_quality': round(
                sum(item['mean_quality'] * item['bases'] for item in file_metrics)
                / total_bases,
                3,
            ) if total_bases else 0.0,
        })
        return _write_manifest(output_dir, 'genomics_qc.json', {
            'status': 'completed',
            'input_type': resolved_type,
            'tool': 'python-fastq-parser',
            'inputs': [str(path) for path in paths],
            'metrics': totals,
            'files': file_metrics,
        })
    if len(paths) != 1:
        raise ValueError(f'{resolved_type} QC accepts exactly one input file')
    input_file = paths[0]
    tool_name = 'samtools' if resolved_type == 'bam' else 'bcftools'
    executable = dependencies.which(tool_name)
    if not executable:
        return _write_manifest(output_dir, 'genomics_qc.json', {
            'status': 'unavailable',
            'input_type': resolved_type,
            'tool': tool_name,
            'inputs': [str(input_file)],
            'reason': f'{tool_name} not found in PATH',
        })
    if resolved_type == 'bam':
        quickcheck = dependencies.capture_command(
            [executable, 'quickcheck', '-v', str(input_file)],
            output_dir / 'samtools_quickcheck.txt',
            timeout,
        )
        if quickcheck['status'] != 'completed':
            return _write_manifest(output_dir, 'genomics_qc.json', {
                'status': 'failed',
                'input_type': resolved_type,
                'tool': tool_name,
                'inputs': [str(input_file)],
                'quickcheck': quickcheck,
            })
        flagstat = dependencies.capture_command(
            [executable, 'flagstat', str(input_file)],
            output_dir / 'samtools_flagstat.txt',
            timeout,
        )
        return _write_manifest(output_dir, 'genomics_qc.json', {
            'status': flagstat['status'],
            'input_type': resolved_type,
            'tool': tool_name,
            'inputs': [str(input_file)],
            'quickcheck': {'status': 'completed'},
            'flagstat': {
                key: value for key, value in flagstat.items() if key != 'stdout'
            },
            'total_reads': _parse_flagstat_total(flagstat.get('stdout', '')),
        })
    stats = dependencies.capture_command(
        [executable, 'stats', str(input_file)],
        output_dir / 'bcftools_stats.txt',
        timeout,
    )
    return _write_manifest(output_dir, 'genomics_qc.json', {
        'status': stats['status'],
        'input_type': resolved_type,
        'tool': tool_name,
        'inputs': [str(input_file)],
        'stats': {
            key: value for key, value in stats.items() if key != 'stdout'
        },
        'number_of_records': _parse_stat_value(
            stats.get('stdout', ''),
            'number of records',
        ),
    })


def _parse_fastqc_summary(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        summary_name = next(
            (name for name in archive.namelist() if name.endswith('/summary.txt')),
            None,
        )
        if not summary_name:
            return []
        text = archive.read(summary_name).decode('utf-8', errors='replace')
    records = []
    for line in text.splitlines():
        fields = line.split('\t', 2)
        if len(fields) == 3:
            records.append({
                'status': fields[0].lower(),
                'module': fields[1],
                'details': fields[2],
            })
    return records


def _fastqc_reports(output_dir):
    reports = []
    summaries = []
    for zip_path in sorted(output_dir.glob('*_fastqc.zip')):
        try:
            summary = _parse_fastqc_summary(zip_path)
        except (OSError, zipfile.BadZipFile) as exc:
            summary = [{
                'status': 'error',
                'module': 'summary',
                'details': str(exc),
            }]
        summaries.append({
            'archive': str(zip_path),
            'summary': summary,
        })
        reports.append(str(zip_path))
    reports.extend(str(path) for path in sorted(output_dir.glob('*_fastqc.html')))
    return reports, summaries


def execute_fastq_qc(fastq_paths, output_dir, fastq_r2_paths=None, threads=1,
                     timeout=900, *, dependencies):
    paths = normalize_fastq_paths(fastq_paths)
    mate_paths = (
        normalize_fastq_paths(fastq_r2_paths)
        if fastq_r2_paths is not None
        else []
    )
    if mate_paths and len(paths) != len(mate_paths):
        raise ValueError('fastq_r2_paths must match the number of FASTQ R1 inputs')
    all_paths = paths + mate_paths
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fastqc_dir = output_dir / 'fastqc'
    fastqc_dir.mkdir(parents=True, exist_ok=True)
    threads = max(1, min(int(threads), 64))
    timeout = max(1, min(int(timeout), 3600))
    provenance = {
        'inputs': [
            {'path': str(path), 'sha256': dependencies.sha256(path)}
            for path in all_paths
        ],
        'parameters': {
            'threads': threads,
            'layout': 'paired_end' if mate_paths else 'single_end',
        },
        'tools': {},
    }
    fastqc = dependencies.which('fastqc')
    multiqc = dependencies.which('multiqc')
    missing_tools = [
        name
        for name, path in (('fastqc', fastqc), ('multiqc', multiqc))
        if not path
    ]
    if missing_tools:
        return _write_manifest(output_dir, 'fastq_qc.json', {
            'status': 'unavailable',
            'workflow': 'fastq_quality_control',
            'inputs': [str(path) for path in all_paths],
            'missing_tools': missing_tools,
            'reason': 'install FastQC and MultiQC in the execution environment',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'fastqc': {'path': fastqc, **dependencies.version(fastqc)},
        'multiqc': {'path': multiqc, **dependencies.version(multiqc)},
    }
    fastqc_command = [
        fastqc,
        '--quiet',
        '--threads',
        str(threads),
        '--outdir',
        str(fastqc_dir),
        *[str(path) for path in all_paths],
    ]
    fastqc_result = dependencies.run_command(
        fastqc_command,
        timeout,
        output_dir / 'fastqc.log',
    )
    if fastqc_result['status'] != 'completed':
        return _write_manifest(output_dir, 'fastq_qc.json', {
            'status': 'failed',
            'workflow': 'fastq_quality_control',
            'inputs': [str(path) for path in all_paths],
            'fastqc': fastqc_result,
            'command': fastqc_command,
            'provenance': provenance,
        })
    multiqc_command = [
        multiqc,
        '--force',
        '--outdir',
        str(output_dir),
        '--filename',
        'multiqc_report.html',
        str(fastqc_dir),
    ]
    multiqc_result = dependencies.run_command(
        multiqc_command,
        timeout,
        output_dir / 'multiqc.log',
    )
    reports, summaries = _fastqc_reports(fastqc_dir)
    module_status_counts = {}
    for report in summaries:
        for record in report['summary']:
            status = record['status']
            module_status_counts[status] = module_status_counts.get(status, 0) + 1
    if (output_dir / 'multiqc_report.html').is_file():
        reports.append(str(output_dir / 'multiqc_report.html'))
    return _write_manifest(output_dir, 'fastq_qc.json', {
        'status': (
            'completed' if multiqc_result['status'] == 'completed' else 'failed'
        ),
        'workflow': 'fastq_quality_control',
        'inputs': [str(path) for path in all_paths],
        'reports': reports,
        'fastqc_summaries': summaries,
        'module_status_counts': module_status_counts,
        'fastqc': fastqc_result,
        'multiqc': multiqc_result,
        'commands': {
            'fastqc': fastqc_command,
            'multiqc': multiqc_command,
        },
        'provenance': provenance,
    })
