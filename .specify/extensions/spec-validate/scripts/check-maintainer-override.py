"""Validate the local override and this extension's opt-in; fail closed."""

import json
from pathlib import Path
import sys


def allows_override(state, config):
    override = state.get('override') if isinstance(state, dict) else None
    if not isinstance(override, dict) or override.get('used') is not True:
        return False
    if not all(isinstance(override.get(key), str) and override[key].strip()
               for key in ('reason', 'by')):
        return False

    if any(not state.get(key) or override.get(key) != state[key]
           for key in ('spec_hash', 'tasks_hash')):
        return False

    # Inspect the YAML structure rather than accepting a matching line from
    # another section. Absent, duplicate or non-scalar settings do not opt in.
    try:
        import yaml
    except ImportError:
        print('Maintainer override requires Python 3 with PyYAML.', file=sys.stderr)
        return False

    def lookup(mapping, name):
        if not isinstance(mapping, yaml.MappingNode):
            return None
        matches = [value for key, value in mapping.value
                   if isinstance(key, yaml.ScalarNode)
                   and key.tag == 'tag:yaml.org,2002:str' and key.value == name]
        return matches[0] if len(matches) == 1 else None

    try:
        with config.open(encoding='utf-8') as stream:
            data = yaml.compose(stream, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return False
    setting = lookup(lookup(data, 'gate'), 'allow_maintainer_override')
    return (isinstance(setting, yaml.ScalarNode)
            and setting.tag == 'tag:yaml.org,2002:bool' and setting.value == 'true')


def main():
    config = Path(__file__).resolve().parent.parent / 'spec-validate-config.yml'
    try:
        return 0 if allows_override(json.load(sys.stdin), config) else 1
    except (OSError, ValueError, RecursionError):
        return 1


if __name__ == '__main__':
    sys.exit(main())
