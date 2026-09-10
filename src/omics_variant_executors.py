"""Native variant calling and normalization executors."""
import json
from pathlib import Path


def _write_manifest(output_dir, filename, payload):
    manifest_path = Path(output_dir) / filename
    payload['manifest_path'] = str(manifest_path)
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return payload


def _parse_bcftools_stat_value(text, label):
    for line in str(text or '').splitlines():
        fields = line.split('\t')
        if len(fields) >= 3 and fields[0] == 'SN' and label in fields[2]:
            return fields[3] if len(fields) > 3 else None
        if len(fields) >= 2 and fields[0] == 'SN' and label in fields[1]:
            return fields[2] if len(fields) > 2 else None
    return None


def execute_variant_calling(bam_path, reference_fasta, output_dir, output_vcf=None,
                        region=None, min_mapping_quality=0, min_base_quality=13,
                        timeout=600, *, dependencies):
    bam_path = Path(bam_path)
    reference_fasta = Path(reference_fasta)
    if not bam_path.is_file():
        raise ValueError(f'BAM/CRAM input does not exist: {bam_path}')
    if bam_path.suffix.lower() not in {'.bam', '.cram'}:
        raise ValueError('variant calling requires a BAM or CRAM input')
    if not reference_fasta.is_file():
        raise ValueError(f'reference FASTA does not exist: {reference_fasta}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_vcf = Path(output_vcf) if output_vcf else output_dir / 'variants.vcf'
    output_vcf.parent.mkdir(parents=True, exist_ok=True)
    raw_bcf = output_dir / 'mpileup.bcf'
    stats_path = output_dir / 'bcftools_stats.txt'
    timeout = max(1, min(int(timeout), 3600))
    min_mapping_quality = max(0, int(min_mapping_quality))
    min_base_quality = max(0, int(min_base_quality))
    samtools = dependencies.which('samtools')
    bcftools = dependencies.which('bcftools')
    missing_tools = [name for name, path in (('samtools', samtools), ('bcftools', bcftools)) if not path]
    provenance = {
        'inputs': {
            'bam_or_cram': {'path': str(bam_path), 'sha256': dependencies.sha256(bam_path)},
            'reference_fasta': {'path': str(reference_fasta), 'sha256': dependencies.sha256(reference_fasta)},
        },
        'parameters': {
            'region': region,
            'min_mapping_quality': min_mapping_quality,
            'min_base_quality': min_base_quality,
        },
        'tools': {},
    }
    if missing_tools:
        return _write_manifest(output_dir, 'genomics_qc.json', {
            'status': 'unavailable',
            'workflow': 'reference_based_variant_calling',
            'input_type': 'bam' if bam_path.suffix.lower() == '.bam' else 'cram',
            'output_vcf': str(output_vcf),
            'missing_tools': missing_tools,
            'reason': 'required native variant-calling tools are not installed',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'samtools': {'path': samtools, **dependencies.version(samtools)},
        'bcftools': {'path': bcftools, **dependencies.version(bcftools)},
    }
    steps = []

    def execute(step_id, command, stdout_path=None):
        result = dependencies.run_command(command, timeout, stdout_path)
        steps.append({
            'id': step_id,
            'command': command,
            **result,
        })
        return result

    reference_index = Path(str(reference_fasta) + '.fai')
    if not reference_index.is_file():
        result = execute('reference_index', [samtools, 'faidx', str(reference_fasta)])
        if result['status'] != 'completed':
            return _write_manifest(output_dir, 'genomics_qc.json', {
                'status': 'failed',
                'workflow': 'reference_based_variant_calling',
                'output_vcf': str(output_vcf),
                'steps': steps,
                'provenance': provenance,
            })
    bam_index = Path(str(bam_path) + '.bai')
    cram_index = Path(str(bam_path) + '.crai')
    alternate_index = bam_path.with_suffix('.bai')
    if not any(path.is_file() for path in (bam_index, cram_index, alternate_index)):
        result = execute('alignment_index', [samtools, 'index', str(bam_path)])
        if result['status'] != 'completed':
            return _write_manifest(output_dir, 'genomics_qc.json', {
                'status': 'failed',
                'workflow': 'reference_based_variant_calling',
                'output_vcf': str(output_vcf),
                'steps': steps,
                'provenance': provenance,
            })
    mpileup_command = [
        bcftools, 'mpileup', '-Ou', '-f', str(reference_fasta),
        '-q', str(min_mapping_quality), '-Q', str(min_base_quality),
    ]
    if region:
        mpileup_command.extend(['-r', str(region)])
    mpileup_command.extend(['-o', str(raw_bcf), str(bam_path)])
    result = execute('mpileup', mpileup_command, output_dir / 'mpileup.log')
    if result['status'] != 'completed':
        return _write_manifest(output_dir, 'genomics_qc.json', {
            'status': 'failed',
            'workflow': 'reference_based_variant_calling',
            'output_vcf': str(output_vcf),
            'steps': steps,
            'provenance': provenance,
        })
    result = execute(
        'variant_call',
        [bcftools, 'call', '-mv', '-Ov', '-o', str(output_vcf), str(raw_bcf)],
        output_dir / 'variant_call.log',
    )
    if result['status'] != 'completed' or not output_vcf.is_file():
        return _write_manifest(output_dir, 'genomics_qc.json', {
            'status': 'failed',
            'workflow': 'reference_based_variant_calling',
            'output_vcf': str(output_vcf),
            'steps': steps,
            'provenance': provenance,
        })
    stats = execute('variant_stats', [bcftools, 'stats', str(output_vcf)], stats_path)
    stats_text = stats_path.read_text(encoding='utf-8') if stats_path.is_file() else ''
    return _write_manifest(output_dir, 'genomics_qc.json', {
        'status': 'completed' if stats['status'] == 'completed' else 'failed',
        'workflow': 'reference_based_variant_calling',
        'input_type': 'bam' if bam_path.suffix.lower() == '.bam' else 'cram',
        'inputs': [str(bam_path), str(reference_fasta)],
        'output_vcf': str(output_vcf),
        'raw_bcf': str(raw_bcf),
        'number_of_records': _parse_bcftools_stat_value(stats_text, 'number of records'),
        'steps': steps,
        'provenance': provenance,
    })
def execute_variant_normalization(vcf_path, reference_fasta, output_dir, output_vcf=None,
                       region=None, timeout=300, *, dependencies):
    vcf_path = Path(vcf_path)
    reference_fasta = Path(reference_fasta)
    if not vcf_path.is_file():
        raise ValueError(f'VCF input does not exist: {vcf_path}')
    if not reference_fasta.is_file():
        raise ValueError(f'reference FASTA does not exist: {reference_fasta}')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_vcf = Path(output_vcf) if output_vcf else output_dir / 'normalized.vcf'
    output_vcf.parent.mkdir(parents=True, exist_ok=True)
    stats_path = output_dir / 'normalized_bcftools_stats.txt'
    timeout = max(1, min(int(timeout), 3600))
    bcftools = dependencies.which('bcftools')
    samtools = dependencies.which('samtools')
    reference_index = Path(str(reference_fasta) + '.fai')
    missing_tools = []
    if not bcftools:
        missing_tools.append('bcftools')
    if not reference_index.is_file() and not samtools:
        missing_tools.append('samtools')
    provenance = {
        'inputs': {
            'vcf': {'path': str(vcf_path), 'sha256': dependencies.sha256(vcf_path)},
            'reference_fasta': {'path': str(reference_fasta), 'sha256': dependencies.sha256(reference_fasta)},
        },
        'parameters': {'region': region, 'normalization': 'left-align-indels-and-split-multiallelic'},
        'tools': {},
    }
    if missing_tools:
        return _write_manifest(output_dir, 'variant_normalization.json', {
            'status': 'unavailable',
            'workflow': 'vcf_normalization',
            'input_type': 'vcf',
            'output_vcf': str(output_vcf),
            'missing_tools': sorted(set(missing_tools)),
            'reason': 'required native VCF normalization tools are not installed',
            'provenance': provenance,
        })
    provenance['tools'] = {
        'bcftools': {'path': bcftools, **dependencies.version(bcftools)},
    }
    if samtools:
        provenance['tools']['samtools'] = {
            'path': samtools,
            **dependencies.version(samtools),
        }
    steps = []

    def execute(step_id, command, stdout_path=None):
        result = dependencies.run_command(command, timeout, stdout_path)
        steps.append({'id': step_id, 'command': command, **result})
        return result

    if not reference_index.is_file():
        result = execute('reference_index', [samtools, 'faidx', str(reference_fasta)])
        if result['status'] != 'completed':
            return _write_manifest(output_dir, 'variant_normalization.json', {
                'status': 'failed',
                'workflow': 'vcf_normalization',
                'output_vcf': str(output_vcf),
                'steps': steps,
                'provenance': provenance,
            })
    output_format = '-Ob' if output_vcf.suffix.lower() == '.bcf' else '-Oz' if output_vcf.suffix.lower() == '.gz' else '-Ov'
    normalize_command = [
        bcftools, 'norm', '-f', str(reference_fasta), '-m', '-any',
        output_format, '-o', str(output_vcf),
    ]
    if region:
        normalize_command.extend(['-r', str(region)])
    normalize_command.append(str(vcf_path))
    result = execute('normalize', normalize_command, output_dir / 'normalize.log')
    if result['status'] != 'completed' or not output_vcf.is_file():
        return _write_manifest(output_dir, 'variant_normalization.json', {
            'status': 'failed',
            'workflow': 'vcf_normalization',
            'output_vcf': str(output_vcf),
            'steps': steps,
            'provenance': provenance,
        })
    stats = execute('stats', [bcftools, 'stats', str(output_vcf)], stats_path)
    stats_text = stats_path.read_text(encoding='utf-8') if stats_path.is_file() else ''
    return _write_manifest(output_dir, 'variant_normalization.json', {
        'status': 'completed' if stats['status'] == 'completed' else 'failed',
        'workflow': 'vcf_normalization',
        'input_type': 'vcf',
        'inputs': [str(vcf_path), str(reference_fasta)],
        'output_vcf': str(output_vcf),
        'number_of_records': _parse_bcftools_stat_value(stats_text, 'number of records'),
        'steps': steps,
        'provenance': provenance,
    })
