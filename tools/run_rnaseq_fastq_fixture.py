import argparse
import json
from pathlib import Path

from src.omics_agent import run_feature_counts, run_omics_analysis, run_rnaseq_alignment


def run_fixture(fixture_dir, output_dir, statistics_backend='scipy'):
    fixture_dir = Path(fixture_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fastq_paths = sorted(fixture_dir.glob('*.fastq'))
    if len(fastq_paths) != 6:
        raise ValueError(f'expected six FASTQ fixture files, found {len(fastq_paths)}')
    alignment = run_rnaseq_alignment(
        fastq_paths,
        fixture_dir / 'reference.fa',
        output_dir,
        threads=1,
    )
    if alignment.get('status') != 'completed':
        raise RuntimeError(json.dumps(alignment, ensure_ascii=False))
    counts = run_feature_counts(
        alignment['alignment_paths'],
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
        'alignment': alignment,
        'feature_counts': counts,
        'analysis': analysis,
    }
    manifest_path = output_dir / 'rnaseq_fastq_fixture_manifest.json'
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description='Run the reproducible FASTQ-to-report RNA-seq fixture.')
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--fixture-dir', default=root / 'examples' / 'omics' / 'rnaseq_fastq_fixture')
    parser.add_argument('--output-dir', default=root / 'output' / 'rnaseq_fastq_fixture')
    parser.add_argument('--statistics-backend', choices=('scipy', 'auto'), default='scipy')
    args = parser.parse_args()
    result = run_fixture(args.fixture_dir, args.output_dir, args.statistics_backend)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
