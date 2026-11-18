#!/usr/bin/env python3
"""Operator daemon/client entrypoint; RPC never carries policy paths."""
from pathlib import Path
import sys

# -I intentionally removes script-directory imports. Add only this fixed,
# operator-owned installed package root, not cwd or a caller's PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Cache-free controller bootstrap (T010): before any controller-package
# import, install a fixed-root loader that compiles the adjacent controller
# source bytes directly. A timestamp-valid poisoned .pyc can never load;
# no caller path or environment selects controller sources. Trusted stdlib
# and external dependency resolution is unchanged; bytecode writes are
# disabled before these imports.
sys.dont_write_bytecode = True
import importlib.abc
import importlib.machinery


class _SourceOnlyLoader(importlib.abc.Loader):
    """Compile the exact source bytes; never read or write a .pyc."""

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        spec = module.__spec__
        assert spec is not None and spec.origin is not None
        path = Path(spec.origin)
        code = compile(path.read_bytes(), str(path), 'exec', dont_inherit=True)
        exec(code, module.__dict__)


class _FixedRootFinder(importlib.abc.MetaPathFinder):
    """Resolve only restart_control modules from this entrypoint's fixed root."""

    def find_spec(self, fullname, path=None, target=None):
        root = Path(__file__).resolve().parent/'restart_control'
        if fullname == 'restart_control':
            source = root/'__init__.py'
            is_package = True
        elif fullname.startswith('restart_control.'):
            parts = fullname.split('.')[1:]
            if not parts or any(not part.isidentifier() for part in parts):
                return None
            source = (root.joinpath(*parts)).with_suffix('.py')
            is_package = False
        else:
            return None
        # A missing source never resolves here: defer to normal machinery,
        # which raises the ordinary ImportError and can still find e.g. a
        # genuine namespace package. No caller path or environment selects
        # controller sources.
        if not source.is_file():
            return None
        spec = importlib.machinery.ModuleSpec(
            fullname, _SourceOnlyLoader(), origin=str(source), is_package=is_package)
        if is_package:
            spec.submodule_search_locations = [str(source.parent)]
        return spec


sys.meta_path.insert(0, _FixedRootFinder())

import argparse
import json
import signal
from restart_control.engine import Controller, Fault, _read_json
from restart_control.shutdown import Shutdown, ShutdownRequested
from restart_control.wire import client, serve


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fault('invalid_request')


class ShutdownView:
    """Read-only Event shape for the wire server's existing drain loop.

    Exposes the shared shutdown deadline budget so the wire drain joins
    accepted workers under the one remaining shared deadline, never a
    fresh budget per worker.
    """

    def __init__(self, shutdown):
        self.shutdown = shutdown

    def is_set(self):
        return self.shutdown.requested

    def remaining(self, limit, *, cleanup):
        return self.shutdown.remaining(limit, cleanup=cleanup)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--policy', required=True)
        commands = parser.add_subparsers(dest='command', required=True)
        commands.add_parser('serve')
        commands.add_parser('arm-canary')
        commands.add_parser('canary-worker').add_argument('nonce')
        for op in ('status', 'prepare'):
            commands.add_parser(op)
        restart = commands.add_parser('restart')
        restart.add_argument('request_id')
        restart.add_argument('asset_digest')
        args = parser.parse_args(argv)
        shutdown = None
        if args.command == 'serve':
            shutdown = Shutdown()
            signal.signal(signal.SIGTERM, lambda *_: shutdown.request())
            signal.signal(signal.SIGINT, lambda *_: shutdown.request())
            shutdown.checkpoint()
        policy = _read_json(Path(args.policy))
        endpoint = Path(policy['state_dir'])/'controller.sock'
        if args.command == 'arm-canary':
            # Operator arm command: no service control, no credentials, no
            # runtime construction — only generation bindings and the
            # exclusive private envelope under the fixed state root.
            from restart_control.canary import arm_bindings, arm_canary
            record = arm_canary(policy['state_dir'], arm_bindings(args.policy))
            print(json.dumps({'status': 'armed', 'nonce': record['nonce'],
                              'label': 'local.restart-canary.' + record['nonce']},
                             sort_keys=True))
            return 0
        if args.command == 'canary-worker':
            # Disposable worker mode: handled before any Runtime
            # construction — no HTTP, no credentials, no launchctl; the
            # scope is derived from the nonce alone.
            from restart_control.canary import run_canary_worker
            run_canary_worker(policy['state_dir'], args.nonce)
            return 0
        if args.command == 'serve':
            shutdown.checkpoint()
            from restart_control.runtime import Runtime
            runtime = Runtime(args.policy, shutdown=shutdown)
            shutdown.checkpoint()
            controller = Controller(runtime.state, runtime, shutdown=shutdown)
            shutdown.checkpoint()
            # T019: the armed startup canary runs inside the exact serve
            # context — after identity/supervision verification, before
            # RPC admission. No arm: zero canary activity. A blocked or
            # refused canary blocks production mutation admission.
            from restart_control.startup import run_startup_canary
            canary = run_startup_canary(runtime, controller, shutdown)
            serve(endpoint, controller, ShutdownView(shutdown))
            return 0
        request = {'op': args.command}
        if args.command == 'restart':
            request.update(request_id=args.request_id, asset_digest=args.asset_digest)
        response = client(endpoint, request, timeout=1000)
        print(json.dumps(response, sort_keys=True, allow_nan=False))
        return 0 if response.get('status') in ('ok', 'prepared', 'restarted', 'rolled_back') else 78
    except Fault as error:
        print(json.dumps({'status': 'blocked', 'code': error.code}))
        return 78
    except ShutdownRequested:
        print('{"status":"blocked","code":"controller_stopping"}')
        return 78
    except (Exception, KeyboardInterrupt):
        print('{"status":"blocked","code":"controller_unavailable"}')
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
