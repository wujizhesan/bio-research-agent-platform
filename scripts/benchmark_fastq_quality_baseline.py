import gzip

BASELINE_SOURCE_COMMIT = '6cdcf7e8fef89d84edde2b518754b3c75d236518'


def legacy_fastq_file_stats(path):
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
            if not sequence or not header.startswith('@') or (not separator.startswith('+')):
                raise ValueError(f'invalid FASTQ record in: {path}')
            if len(sequence) != len(quality):
                raise ValueError(f'FASTQ sequence/quality length mismatch in: {path}')
            length = len(sequence)
            reads += 1
            bases += length
            quality_sum += sum((max(0, ord(char) - 33) for char in quality))
            min_length = length if min_length is None else min(min_length, length)
            max_length = max(max_length, length)
    return {'path': str(path), 'reads': reads, 'bases': bases, 'min_read_length': min_length or 0, 'max_read_length': max_length, 'mean_read_length': round(bases / reads, 3) if reads else 0.0, 'mean_quality': round(quality_sum / bases, 3) if bases else 0.0}
