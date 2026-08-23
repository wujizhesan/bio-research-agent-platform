import argparse
import json
import shutil
import subprocess
from pathlib import Path

from src.omics_agent import run_feature_counts, run_omics_analysis


def _run(command, log_path):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    log_path.write_text(
        (result.stdout or '') + (result.stderr or ''),
        encoding='utf-8',
    )
    if result.returncode != 0:
        raise RuntimeError(f'command failed with exit code {result.returncode}: {command}')


def run_fixture(fixture_dir, output_dir, statistics_backend='scipy'):
    fixture_dir = Path(fixture_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    samtools = shutil.which('samtools')
    if not samtools:
        raise RuntimeError('samtools is required to convert the SAM fixture into sorted BAM files')
    sam_paths = sorted(fixture_dir.glob('*.sam'))
    if len(sam_paths) != 6:
        raise ValueError(f'expected six SAM fixture files, found {len(sam_paths)}')
    alignments = []
    for sam_path in sam_paths:
        raw_bam = output_dir / f'{sam_path.stem}.raw.bam'
        sorted_bam = output_dir / f'{sam_path.stem}.bam'
        _run(
            [samtools, 'view', '-b', '-o', str(raw_bam), str(sam_path)],
            output_dir / f'{sam_path.stem}.view.log',
        )
        _run(
            [samtools, 'sort', '-o', str(sorted_bam), str(raw_bam)],
            output_dir / f'{sam_path.stem}.sort.log',
        )
        _run(
            [samtools, 'index', str(sorted_bam)],
            output_dir / f'{sam_path.stem}.index.log',
        )
        raw_bam.unlink()
        alignments.append(sorted_bam)
    counts = run_feature_counts(
        alignments,
        fixture_dir / 'genes.gtf',
        output_dir,
        output_csv=output_dir / 'expression_counts.csv',
        threads=1,
    )
    if counts.get('status') != 'completed':
        raise RuntimeError(json.dumps(counts, ensure_ascii=False))
    analysis = run_omics_analysis(
        counts['output_csv'],
        fixture_dir / 'metadata.csv',
        fixture_dir / 'gene_sets.csv',
        output_dir,
        evidence_provider='local',
        statistics_backend=statistics_backend,
    )
    manifest = {
        'status': 'completed',
        'fixture_dir': str(fixture_dir),
        'output_dir': str(output_dir),
        'alignments': [str(path) for path in alignments],
        'feature_counts': counts,
        'analysis': analysis,
    }
    manifest_path = output_dir / 'rnaseq_fixture_manifest.json'
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description='Run the reproducible SAM-to-BAM RNA-seq fixture.')
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--fixture-dir', default=root / 'examples' / 'omics' / 'rnaseq_fixture')
    parser.add_argument('--output-dir', default=root / 'output' / 'rnaseq_fixture')
    parser.add_argument('--statistics-backend', choices=('scipy', 'auto'), default='scipy')
    args = parser.parse_args()
    result = run_fixture(args.fixture_dir, args.output_dir, args.statistics_backend)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
