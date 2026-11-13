"""Cache-free controller bootstrap: isolated fixtures only.

Proves the real installed entrypoint compiles adjacent controller source
bytes and never loads a timestamp-valid poisoned .pyc, for the package
__init__, the early controller import chain, and runtime_identity itself.
"""
import importlib.util
import marshal
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'core'))


def _staged_controller(private_root):
    """Stage an installed controller bundle exactly like the installer."""
    from restart_control.runtime_identity import CONTROLLER_INPUTS
    destination = private_root/'controller'
    for name in CONTROLLER_INPUTS:
        source = ROOT/name
        target = destination/name.removeprefix('core/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return destination


def _poison_cache(destination, module, marker):
    """Write a timestamp-valid poisoned .pyc that, if loaded, writes a marker.

    The .pyc embeds code compiled from a different source but carries a
    matching magic, source mtime and size, so the default loader would
    accept it.
    """
    source = destination/'restart_control'/(module+'.py')
    real = py_compile.compile(str(source), doraise=True)
    cache = Path(real)
    # Poisoned payload: different code, header kept matching the real source.
    payload = compile('from pathlib import Path\n'
                      'Path('+repr(str(marker))+').write_text("poisoned")\n',
                      str(source), 'exec', dont_inherit=True)
    data = cache.read_bytes()
    cache.write_bytes(data[:16]+marshal.dumps(payload))
    return cache


def test_installed_entrypoint_ignores_matching_poisoned_caches():
    with tempfile.TemporaryDirectory(prefix='cf-', dir=os.environ['TMPDIR']) as name:
        private_root = Path(name)
        destination = _staged_controller(private_root)
        markers = private_root/'markers'
        markers.mkdir()
        # Poison the early import chain and runtime_identity with caches the
        # default loader would accept (matching magic/mtime/size).
        poisoned = [_poison_cache(destination, '__init__', markers/'init.txt'),
                    _poison_cache(destination, 'engine', markers/'engine.txt'),
                    _poison_cache(destination, 'runtime_identity',
                                  markers/'identity.txt')]
        for cache in poisoned:
            assert cache.exists()
        # Run the real installed entrypoint with -I: the fixed-root loader
        # compiles source bytes; poisoned code is never executed. serve
        # exercises the full early chain including runtime_identity
        # (imported by runtime.py before the policy validation fails).
        (private_root/'policy.json').write_text('{"version": 1}\n')
        result = subprocess.run(
            [sys.executable, '-I', str(destination/'restart_controller.py'),
             '--policy', str(private_root/'policy.json'), 'serve'],
            capture_output=True, text=True, timeout=60,
            env={'HOME': os.environ.get('HOME', '/tmp'), 'PATH': os.defpath,
                 'TMPDIR': os.environ['TMPDIR']})
        assert result.returncode == 78  # blocked (invalid policy), not crashed
        assert '"blocked"' in result.stdout
        # No poisoned payload executed anywhere in the import chain.
        assert list(markers.iterdir()) == [], 'a poisoned cache was executed'


def test_bootstrap_never_writes_bytecode():
    with tempfile.TemporaryDirectory(prefix='cf-', dir=os.environ['TMPDIR']) as name:
        private_root = Path(name)
        destination = _staged_controller(private_root)
        result = subprocess.run(
            [sys.executable, '-I', str(destination/'restart_controller.py'),
             '--policy', str(private_root/'policy.json'), 'serve'],
            capture_output=True, text=True, timeout=60,
            env={'HOME': os.environ.get('HOME', '/tmp'), 'PATH': os.defpath,
                 'TMPDIR': os.environ['TMPDIR']})
        assert result.returncode == 78
        assert '"blocked"' in result.stdout
        assert not list(destination.rglob('__pycache__')), 'bytecode was written'


def test_poisoned_caches_would_load_without_bootstrap():
    """Control: the same poisoned caches DO execute without the fixed loader.

    This pins that the test's poison is genuinely loadable by the default
    machinery, so the entrypoint's refusal is the loader's doing.
    """
    with tempfile.TemporaryDirectory(prefix='cf-', dir=os.environ['TMPDIR']) as name:
        private_root = Path(name)
        destination = _staged_controller(private_root)
        markers = private_root/'markers'
        markers.mkdir()
        _poison_cache(destination, 'engine', markers/'engine.txt')
        # Import via the plain default loader (no -I entrypoint, no meta path
        # finder): the poisoned cache executes.
        code = ('import sys; sys.path.insert(0, %r); '
                'import restart_control.engine as e; '
                'print("loaded", e.__name__)' % str(destination))
        result = subprocess.run([sys.executable, '-B', '-c', code],
                                 capture_output=True, text=True, timeout=60,
                                 env={'HOME': os.environ.get('HOME', '/tmp'),
                                      'PATH': os.defpath,
                                      'TMPDIR': os.environ['TMPDIR']})
        assert (markers/'engine.txt').exists(), 'control failed: poison not loadable'