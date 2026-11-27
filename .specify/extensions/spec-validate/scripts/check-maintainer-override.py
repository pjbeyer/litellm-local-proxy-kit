"""Validate the local override and this extension's opt-in; fail closed."""

import json
from pathlib import Path
import sys


def allows_override(state, config, review, feature=None):
    if not isinstance(state, dict):
        return False
    if review == 'tasks':
        record_key = 'task_review_override'
        config_key = 'allow_task_review_override'
        if not feature or state.get('feature') != feature or state.get('tasks_review_status') != 'pending':
            return False
    elif review == 'spec':
        record_key = 'override'
        config_key = 'allow_maintainer_override'
    else:
        return False

    override = state.get(record_key)
    if not isinstance(override, dict) or override.get('used') is not True:
        return False
    if review == 'tasks' and override.get('feature') != feature:
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
    setting = lookup(lookup(data, 'gate'), config_key)
    return (isinstance(setting, yaml.ScalarNode)
            and setting.tag == 'tag:yaml.org,2002:bool' and setting.value == 'true')


def main():
    args = sys.argv[1:]
    if args and (len(args) != 2 or args[0] != 'tasks'):
        return 1
    review, feature = args or ('spec', None)
    config = Path(__file__).resolve().parent.parent / 'spec-validate-config.yml'
    try:
        return 0 if allows_override(json.load(sys.stdin), config, review, feature) else 1
    except (OSError, ValueError, RecursionError):
        return 1


if __name__ == '__main__':
    sys.exit(main())
