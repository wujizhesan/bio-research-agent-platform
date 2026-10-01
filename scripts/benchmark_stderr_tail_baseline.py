BASELINE_SOURCE_COMMIT = 'ea7d473a98630aae8c6858d5911f6b61cf863209'


def legacy_stderr_tail(error_path):
    detail = error_path.read_text(encoding='utf-8', errors='replace')[-2000:].strip()
    return detail
