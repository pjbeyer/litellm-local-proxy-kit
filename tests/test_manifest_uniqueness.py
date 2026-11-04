"""Manifest ambiguity is rejected before credential resolution."""
import importlib.util
import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('manifest_uniqueness_test', ROOT/'core/parse_credentials.py')
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
ROW = '[[credential]]\nenv_var="FIXTURE_VAR"\nop_ref="op://fixture/item/value"\nrequired=true\n'

@pytest.mark.parametrize('text', [
    ROW + ROW,
    ROW.replace('required=true', 'required=true\nrequired=false'),
    ROW.replace('env_var="FIXTURE_VAR"', 'env_var="FIXTURE_VAR"\nenv_var="OTHER_FIXTURE"'),
    'identity="fixture"\nidentity="other"\n' + ROW,
])
def test_ambiguous_manifest_refuses(text):
    with pytest.raises(ValueError):
        module.parse(text)


def test_cli_has_no_partial_rows_on_duplicate(tmp_path):
    path = tmp_path/'credentials.toml'
    path.write_text(ROW + ROW)
    result = subprocess.run([sys.executable, str(ROOT/'core/parse_credentials.py'), str(path)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'op://fixture' not in result.stdout
