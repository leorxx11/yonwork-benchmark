#!/usr/bin/env python3
"""Isolated model-entry probe; metadata only, not a production monitoring proxy.

Run from the repository: .venv/bin/python -m scripts.probe_model_entry
Uses a local model stub by default. --forward-newapi makes four real text calls
using the existing WorkBuddy custom model's NewAPI credential, held in memory.
Creates temporary YonWork providers through its API, then deletes only those.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from runner.catalog import list_model_choices
from runner.client import ChatClient
from runner.discovery import discover
from runner.drivers.workbuddy import WorkBuddyDriver, _default_config_dir
from runner.job_store import exclusive_worker_lock
from runner.transport import build_opener, request_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--forward-newapi', action='store_true')
    parser.add_argument('--product', choices=['both', 'yonwork', 'workbuddy'], default='both')
    parser.add_argument('--shared-entry', action='store_true',
                        help='Reuse one endpoint per product; correlate using native request headers')
    args = parser.parse_args()
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    out = Path('results') / ('model-entry-' + stamp)
    out.mkdir(parents=True)
    evidence: dict = {'probe_id': stamp, 'forward_newapi': args.forward_newapi,
                      'shared_entry': args.shared_entry,
                      'requests': [], 'turns': [], 'cleanup': {}}
    routes: dict = {}
    endpoint = discover()
    original = _default_config_dir()
    watched = [original / name for name in ['models.json', 'settings.json']]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    models = json.loads((original / 'models.json').read_text())
    if isinstance(models, dict):
        models = models['models']
    model = next(m for m in models if m.get('id') == 'deepseek-flash')
    upstream = urlsplit(model['url'])
    if upstream.scheme != 'http' or upstream.hostname not in ('localhost', '127.0.0.1'):
        raise RuntimeError('Probe requires an existing local NewAPI endpoint')
    api_key = model.get('apiKey', '')
    if args.forward_newapi and (not api_key or api_key.startswith('${')):
        raise RuntimeError('Expected a configured local NewAPI credential')
    write_lock = threading.Lock()

    def save() -> None:
        with write_lock:
            (out / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused) -> None:
            pass

        def reply(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            # Provider creation may discover model context limits. No model call.
            self.reply(200, {'object': 'list', 'data': [
                {'id': 'deepseek-flash', 'object': 'model', 'context_length': 131072}]})

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            data = json.loads(body)
            if args.shared_entry:
                native = self.headers.get('x-yonwork-run-id') or self.headers.get('X-Conversation-ID')
                route = next((v for v in routes.values() if v['run'] == native
                              and self.path.startswith(v['prefix'] + '/')), None)
            else:
                route = next((v for k, v in routes.items() if self.path.startswith(k + '/')), None)
            entry = {'path': self.path, 'method': 'POST', 'started': time.time(),
                     'assigned_run': route['run'] if route else None,
                     'product': route['product'] if route else None,
                     'model': data.get('model'), 'stream': data.get('stream'),
                     'body_keys': sorted(data), 'header_names': sorted(k.lower() for k in self.headers),
                     'explicit_run_header': self.headers.get('X-Benchmark-Run-Id'),
                     'message_count': len(data.get('messages', []))}
            # Inspect correlation outside message text without recording prompts or credentials.
            if route:
                entry['native_run_headers'] = [k for k, v in self.headers.items()
                                               if k.lower() not in ('authorization', 'x-benchmark-run-id')
                                               and route['run'] in v]
                entry['native_run_headers_exact'] = [k for k in entry['native_run_headers']
                                                     if self.headers[k] == route['run']]
                entry['native_run_body_fields'] = [k for k, v in data.items()
                                                   if k not in ('messages', 'input', 'tools')
                                                   and route['run'] in json.dumps(v)]
            evidence['requests'].append(entry)
            save()
            if route is None:
                entry['status'] = 404
                self.reply(404, {'error': 'unregistered probe route'})
            elif self.headers.get('Authorization') != 'Bearer ' + route['token']:
                entry['status'] = 401
                self.reply(401, {'error': 'incorrect probe credential'})
            elif route['closed']:
                entry['status'] = 410
                self.reply(410, {'error': 'probe run has ended'})
            elif args.forward_newapi:
                forwarded = {k: v for k, v in self.headers.items() if k.lower() in (
                    'x-yonwork-run-id', 'x-yonclaw-run-id', 'x-conversation-id',
                    'x-benchmark-run-id', 'traceparent')}
                req = urllib.request.Request(
                    f'{upstream.scheme}://{upstream.netloc}/v1/chat/completions',
                    data=body, headers={**forwarded, 'Content-Type': 'application/json',
                                        'Authorization': 'Bearer ' + api_key})
                try:
                    try:
                        response = build_opener().open(req, timeout=55)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        entry['status'] = response.status
                        entry['upstream_ids'] = {k.lower(): v for k, v in response.headers.items()
                                                  if k.lower() in ('x-request-id', 'x-oneapi-request-id',
                                                                   'x-newapi-request-id')}
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        pending = b''
                        while chunk := response.read1(65536):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            pending += chunk
                            while b'\n' in pending:
                                line, pending = pending.split(b'\n', 1)
                                if line.startswith(b'data:'):
                                    value = line[5:].strip()
                                    if value == b'[DONE]':
                                        entry['sse_done'] = True
                                    else:
                                        try:
                                            obj = json.loads(value)
                                            if obj.get('usage'):
                                                entry['usage'] = obj['usage']
                                            if obj.get('model'):
                                                entry['response_model'] = obj['model']
                                        except (ValueError, AttributeError):
                                            entry['parse_error'] = True
                        self.close_connection = True
                except Exception as exc:
                    entry['transport_error'] = type(exc).__name__
                    self.close_connection = True
            else:
                entry['status'] = 200
                obj = {'id': 'probe-' + secrets.token_hex(4), 'object': 'chat.completion',
                       'created': int(time.time()), 'model': data.get('model'),
                       'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ENTRY_OK'},
                                    'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}
                if data.get('stream'):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    obj['object'] = 'chat.completion.chunk'
                    obj['choices'][0]['delta'] = obj['choices'][0].pop('message')
                    self.wfile.write(('data: ' + json.dumps(obj) + '\n\ndata: [DONE]\n\n').encode())
                    self.wfile.flush()
                else:
                    self.reply(200, obj)
            entry['ended'] = time.time()
            save()

    created: list[str] = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    evidence['listen_port'] = port
    print('Probe started:', out, 'port', port, flush=True)
    try:
        with exclusive_worker_lock(), tempfile.TemporaryDirectory(prefix='model-entry-') as temporary:
            baseline = request_json(endpoint.url('/api/provider-accounts'), token=endpoint.token)
            for product in ['workbuddy', 'yonwork']:
                if args.product not in ('both', product):
                    continue
                shared_token = secrets.token_hex(24)
                choice = None
                for number in [1, 2]:
                    run = f'entry-{stamp}-{product}-{number}'
                    prefix = '/run/' + run
                    token = shared_token if args.shared_entry else secrets.token_hex(24)
                    actual_prefix = '/shared/' + product if args.shared_entry else prefix
                    routes[prefix] = {'run': run, 'product': product, 'token': token,
                                      'prefix': actual_prefix, 'closed': False}
                    url = f'http://127.0.0.1:{port}{actual_prefix}/v1'
                    prompt = '请只回复 ENTRY_OK，不调用工具。'
                    print('Starting', run, flush=True)
                    if args.shared_entry and number == 2:
                        # Outsider on the same endpoint while a valid run is registered.
                        req = urllib.request.Request(url + '/chat/completions',
                            data=b'{"model":"probe-outsider","messages":[]}',
                            headers={'Content-Type': 'application/json',
                                     'Authorization': 'Bearer ' + token})
                        try:
                            build_opener().open(req, timeout=5).close()
                        except urllib.error.HTTPError as exc:
                            exc.close()
                    try:
                        if product == 'workbuddy':
                            config = Path(temporary) / run
                            config.mkdir()
                            custom = {**model, 'url': url, 'apiKey': token}
                            (config / 'models.json').write_text(json.dumps([custom]))
                            driver = WorkBuddyDriver(model_query=model['id'], config_dir=config,
                                                     timeout_seconds=65, allow_tools=False)
                            list(driver.preflight())
                            native_env = driver._environment()
                            added = {'CODEBUDDY_API_KEY': token,
                                     'CODEBUDDY_CUSTOM_HEADERS': 'X-Benchmark-Run-Id: ' + run,
                                     'NO_PROXY': '*', 'no_proxy': '*'}
                            native_env.update(added)
                            native_env['WSLENV'] += ':' + ':'.join(added)
                            driver._environment = lambda: native_env
                            turn = driver.run_turn(benchmark_id=run, prompt=prompt)
                        elif not args.shared_entry or choice is None:
                            account_id = 'entry-' + secrets.token_hex(8)
                            # Save receipt before creating anything for interrupted-probe recovery.
                            created.append(account_id)
                            evidence['temporary_providers'] = created.copy()
                            save()
                            result = request_json(endpoint.url('/api/provider-accounts'),
                                token=endpoint.token, method='POST', timeout=30,
                                payload={'account': {'id': account_id, 'vendorId': 'custom',
                                    'label': run, 'authMode': 'api_key', 'baseUrl': url,
                                    'apiProtocol': 'openai-completions', 'model': model['id'],
                                    'fallbackModels': [], 'enabled': True, 'isDefault': False},
                                    'apiKey': token})
                            if not result.get('success'):
                                raise RuntimeError('Temporary provider creation failed')
                            if result['account']['id'] != account_id:
                                created.append(result['account']['id'])
                            choice = next(c for c in list_model_choices(endpoint)
                                          if c.provider_account_id == result['account']['id'])
                            # Host API returns before gateway auth/config application.
                            # This is a probe setup delay, not proof of readiness or measured latency.
                            time.sleep(5)
                        if product == 'yonwork':
                            turn = ChatClient(endpoint, model_choice=choice, timeout_seconds=65).send(
                                benchmark_id=run, session_key=f'agent:main:{run}', prompt=prompt)
                        evidence['turns'].append({'run': run, 'product': product,
                            'run_id': turn.run_id, 'terminated_by': turn.terminated_by,
                            'stop_reason': turn.stop_reason, 'final_state': turn.final_state,
                            'answer_matches': (turn.answer or '').strip() == 'ENTRY_OK',
                            'duration_seconds': turn.duration_seconds, 'ended': time.time()})
                    except Exception as exc:
                        evidence['turns'].append({'run': run, 'product': product,
                                                  'exception': type(exc).__name__})
                        # Exception bodies may contain product configuration; keep console sanitized.
                        print('Turn exception:', type(exc).__name__, flush=True)
                    finally:
                        routes[prefix]['closed'] = True
                        save()
                    print('Finished', run, evidence['turns'][-1], flush=True)
                # Synthetic outsider and late requests: never forwarded, no model cost.
                first = next(v for v in routes.values() if v['product'] == product)
                for path, matched in [('/unregistered/v1/chat/completions', None),
                                      (first['prefix'] + '/v1/chat/completions', first)]:
                    correlation = {}
                    if matched:
                        key = 'x-yonwork-run-id' if product == 'yonwork' else 'X-Conversation-ID'
                        correlation[key] = matched['run']
                    req = urllib.request.Request(f'http://127.0.0.1:{port}' + path,
                        data=b'{"model":"probe-outsider","messages":[]}',
                        headers={**correlation, 'Content-Type': 'application/json',
                                 'Authorization': 'Bearer ' + (matched['token'] if matched else '')})
                    try:
                        build_opener().open(req, timeout=5).close()
                    except urllib.error.HTTPError as exc:
                        exc.close()
            # Delete temporary providers while the benchmark lock is still held.
            for account_id in created.copy():
                result = request_json(endpoint.url('/api/provider-accounts/' + account_id),
                                      token=endpoint.token, method='DELETE', timeout=30)
                if result.get('success'):
                    created.remove(account_id)
            after = request_json(endpoint.url('/api/provider-accounts'), token=endpoint.token)
            evidence['cleanup']['providers_equal'] = baseline == after
    finally:
        for account_id in created.copy():
            try:
                result = request_json(endpoint.url('/api/provider-accounts/' + account_id),
                                      token=endpoint.token, method='DELETE', timeout=30)
                if result.get('success'):
                    created.remove(account_id)
            except Exception as exc:
                evidence['cleanup']['provider_delete_error'] = type(exc).__name__
        evidence['cleanup']['remaining_providers'] = created
        evidence['cleanup']['workbuddy_configs_unchanged'] = all(
            hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p,h in hashes.items())
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        evidence['cleanup']['listener_stopped'] = not thread.is_alive()
        save()
    print('Evidence:', out / 'evidence.json', flush=True)
    print('Cleanup:', evidence['cleanup'], flush=True)
    main_requests = [r for r in evidence['requests'] if r.get('model') == model['id']]
    success = (bool(evidence['turns'])
               and all(t.get('answer_matches') and t.get('final_state') in ('final', 'completed')
                       for t in evidence['turns'])
               and len(main_requests) == len(evidence['turns'])
               and {r['assigned_run'] for r in main_requests} == {t['run'] for t in evidence['turns']}
               and all(r.get('status') == 200 and r.get('ended') and not r.get('transport_error')
                       for r in main_requests)
               and (not args.shared_entry or all(r.get('native_run_headers_exact') for r in main_requests))
               and (not args.forward_newapi or all(r.get('sse_done') and r.get('usage')
                                                    for r in main_requests))
               and all(r.get('status') == (404 if r['assigned_run'] is None else 410)
                       for r in evidence['requests'] if r.get('model') == 'probe-outsider')
               and not created and evidence['cleanup'].get('providers_equal')
               and evidence['cleanup']['workbuddy_configs_unchanged']
               and evidence['cleanup']['listener_stopped'])
    evidence['entry_check_passed'] = bool(success)
    save()
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
