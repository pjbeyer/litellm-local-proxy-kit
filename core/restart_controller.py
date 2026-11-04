#!/usr/bin/env python3
"""Operator daemon/client entrypoint; RPC never carries policy paths."""
from pathlib import Path
import sys

# -I intentionally removes script-directory imports. Add only this fixed,
# operator-owned installed package root, not cwd or a caller's PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import argparse
import json
import signal
import threading
from restart_control.engine import Controller, Fault, _read_json
from restart_control.wire import client, serve


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fault('invalid_request')


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--policy', required=True)
        commands = parser.add_subparsers(dest='command', required=True)
        commands.add_parser('serve')
        for op in ('status', 'prepare'):
            commands.add_parser(op)
        restart = commands.add_parser('restart')
        restart.add_argument('request_id')
        restart.add_argument('asset_digest')
        args = parser.parse_args(argv)
        policy = _read_json(Path(args.policy))
        endpoint = Path(policy['state_dir'])/'controller.sock'
        if args.command == 'serve':
            from restart_control.runtime import Runtime
            runtime = Runtime(args.policy)
            controller = Controller(runtime.state, runtime)
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            serve(endpoint, controller, stop)
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
    except (Exception, KeyboardInterrupt):
        print('{"status":"blocked","code":"controller_unavailable"}')
        return 78


if __name__ == '__main__':
    raise SystemExit(main())
