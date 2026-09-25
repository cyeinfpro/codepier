"""Combine explicit, checked coverage files rather than guessing dotted suffixes."""
from pathlib import Path


def combine(paths, output):
    from coverage import Coverage, CoverageData
    output = Path(output)
    inputs = [Path(path) for path in paths]
    if not inputs or len({path.resolve() for path in inputs}) != len(inputs):
        raise ValueError('Missing or duplicate coverage inputs')
    kinds = set()
    for path in inputs:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError('Coverage input must be a bounded regular file')
        data = CoverageData(basename=str(path));data.read()
        if not data.measured_files():
            raise ValueError('Coverage input contains no measurements')
        kinds.add(data.has_arcs())
    if len(kinds) != 1:
        raise ValueError('Coverage inputs mix branch and statement modes')
    output.mkdir(parents=True, exist_ok=True)
    coverage = Coverage(data_file=str(output / '.coverage'))
    coverage.combine(data_paths=[str(path) for path in inputs], strict=True, keep=True)
    coverage.save()
    coverage.xml_report(outfile=str(output / 'coverage.xml'))
    coverage.json_report(outfile=str(output / 'coverage.json'))
    return coverage
