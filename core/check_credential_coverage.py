#!/usr/bin/env python3
"""Refuse a rendered config with credentials absent from the startup inventory."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

import yaml

NAME = re.compile(r'^[A-Z_][A-Z_0-9]*$')
REF = re.compile(r'os\.environ/([A-Z_][A-Z_0-9]*)')


def check(config, manifest, overlay):
    parsed = subprocess.run([sys.executable, str(Path(__file__).with_name('parse_credentials.py')), str(manifest)], capture_output=True, text=True, timeout=10)
    if parsed.returncode:
        raise ValueError('manifest validation failed')
    declared = {line.split('\t')[0] for line in parsed.stdout.splitlines()}
    data = yaml.safe_load(Path(overlay).read_text()) if overlay else {}
    declared.update((data or {}).get('runtime_environment', {}))
    raw = Path(config).read_text()
    referenced = set(REF.findall(raw))
    config_data = yaml.safe_load(raw)
    def has_jev(value):
        if isinstance(value, dict):
            return value.get('classifier_type') == 'jev' or any(has_jev(v) for v in value.values())
        return isinstance(value, list) and any(has_jev(v) for v in value)
    if has_jev(config_data):
        referenced.add('TYPESAFE_API_KEY')
    if any(not NAME.fullmatch(name) for name in declared):
        raise ValueError('invalid declared environment variable')
    missing = sorted(referenced - declared)
    if missing:
        raise ValueError('undeclared config credentials: ' + ', '.join(missing))
    return len(referenced)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--overlay', type=Path)
    args = parser.parse_args()
    try:
        count = check(args.config, args.manifest, args.overlay)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    except Exception:
        print('credential coverage check could not complete', file=sys.stderr)
        return 78
    print(f'credential coverage OK: {count} referenced variables declared')
    return 0


if __name__ == '__main__':
    sys.exit(main())
