"""Small stdio MCP execution adapter. Authorization remains owned by Person 1."""
import argparse
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

MAX_FILE = 65536

from control_plane import Client, Denied
from twinguard import preview

MAX_ENTRIES = 2000
MAX_RESULTS = 100

def _matches(pattern, path):
    """Root-anchored glob; ** matches zero or more path segments."""
    rule, parts = pattern.split('/'), path.split('/')
    def walk(i, j):
        if i == len(rule):
            return j == len(parts)
        if rule[i] == '**':
            return walk(i + 1, j) or (j < len(parts) and walk(i, j + 1))
        return j < len(parts) and fnmatchcase(parts[j], rule[i]) and walk(i + 1, j + 1)
    return walk(0, 0) or (pattern.endswith('/**') and path == pattern[:-3])

def _visible(path, contract):
    protected = ['.git', '.env*', *contract['protected_paths']]
    for rule in protected:
        if '/' not in rule:
            if any(fnmatchcase(part.lower(), rule.lower()) for part in path.split('/')):
                return False
        elif _matches(rule.lower(), path.lower()):
            return False
    return any(_matches(rule, path) for rule in contract['allowed_paths'])

def _contract(client, contract_id, version):
    contract = client.request(f'/api/contracts/{contract_id}/versions/{version}')
    if contract.get('contract_id') != contract_id or contract.get('version') != version or contract.get('status') != 'active':
        raise Denied('CONTRACT_NOT_ACTIVE')
    return contract

class Repository:
    """Reads regular files without following symlinks, including intermediate ones."""
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)

    def open_path(self, path):
        if not isinstance(path, str) or not path or path.startswith('/') or '\\' in path or '\x00' in path:
            raise Denied('INVALID_ARGUMENTS')
        parts = path.split('/')
        if any(p in ('', '.', '..') for p in parts):
            raise Denied('INVALID_ARGUMENTS')
        if any(p.startswith('.env') or p in ('.git', '.github', '.aws', '.ssh', 'infra') for p in parts):
            raise Denied('PROTECTED_PATH')
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for i, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if i < len(parts)-1:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, path):
        fd = self.open_path(path)
        with os.fdopen(fd, 'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise Denied('INVALID_ARGUMENTS')
            data = f.read(MAX_FILE + 1)
            if len(data) > MAX_FILE:
                raise Denied('FILE_TOO_LARGE')
            return data.decode('utf-8')

    def entries(self, path):
        fd = self.open_path(path)
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise Denied('INVALID_ARGUMENTS')
            names = sorted(os.listdir(fd))
            if len(names) > MAX_ENTRIES:
                raise Denied('TOO_MANY_FILES')
            result = []
            for name in names:
                mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
                    result.append((path + '/' + name, stat.S_ISDIR(mode)))
            return result
        finally:
            os.close(fd)

    def search(self, query, patterns, contract):
        results, seen, inspected = [], set(), 0
        for pattern in patterns:
            base = []
            for part in pattern.split('/'):
                if any(c in part for c in '*?['):
                    break
                base.append(part)
            if not base:
                raise Denied('INVALID_ARGUMENTS')
            start = '/'.join(base)
            pending = [start]
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                inspected += 1
                if inspected > MAX_ENTRIES:
                    raise Denied('TOO_MANY_FILES')
                try:
                    fd = self.open_path(current)
                    mode = os.fstat(fd).st_mode
                    os.close(fd)
                except (OSError, Denied):
                    continue
                if stat.S_ISDIR(mode):
                    for child, is_dir in reversed(self.entries(current)):
                        if is_dir and not _visible(child, contract):
                            continue
                        pending.append(child)
                elif stat.S_ISREG(mode) and _matches(pattern, current) and _visible(current, contract):
                    try:
                        contents = self.read(current)
                    except (OSError, UnicodeError, Denied):
                        continue
                    for line_no, line in enumerate(contents.splitlines(), 1):
                        if query in line:
                            results.append({'path': current, 'line': line_no, 'text': line[:300]})
                            if len(results) >= MAX_RESULTS:
                                return results
        return results

TOOLS = [{'name': 'list_files', 'description': 'List authorized direct children of a repository directory.',
          'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}},
         {'name': 'read_file', 'description': 'Read an authorized repository-relative UTF-8 file, at most 64 KiB.',
          'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}},
         {'name': 'search_code', 'description': 'Search authorized paths for a literal query, returning at most 100 matches.',
          'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'paths': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['query','paths'], 'additionalProperties': False}},
         {'name':'propose_patch','description':'Preview an authorized unified Git patch in TwinGuard without modifying the protected repository. The returned impact decision includes checks run against the patched snapshot; after a passing report, finish the task for human review.',
          'inputSchema':{'type':'object','properties':{'patch':{'type':'string'}},'required':['patch'],'additionalProperties':False}},
         {'name':'run_approved_test','description':'Run an approved test (by test_id) on the original clean base in the isolated Docker sandbox. This does not test a proposed patch; use the propose_patch impact report for that.',
          'inputSchema':{'type':'object','properties':{'test_id':{'type':'string'}},'required':['test_id'],'additionalProperties':False}},
         {'name':'run_approved_command','description':'Run an approved check (test or static analysis) by catalogue command_id on the original clean base in the isolated, network-less Docker sandbox. You supply only the ID; the command line is fixed by the administrator catalogue. This does not test a proposed patch.',
          'inputSchema':{'type':'object','properties':{'command_id':{'type':'string'}},'required':['command_id'],'additionalProperties':False}},
         {'name':'request_permission','description':'Record a permission request for the human reviewer; it does not grant access.',
          'inputSchema':{'type':'object','properties':{'tool':{'type':'string'},'resource':{'type':'string'},'reason':{'type':'string'}},'required':['tool','resource','reason'],'additionalProperties':False}},
         {'name':'finish_task','description':'Record the agent task summary. The supervisor reports terminal run state.',
          'inputSchema':{'type':'object','properties':{'summary':{'type':'string'}},'required':['summary'],'additionalProperties':False}}]

class Server:
    def __init__(self, repository, authorize, run_id, contract_id, version):
        self.repository, self.authorize = repository, authorize
        self.run_id, self.contract_id, self.version = run_id, contract_id, version

    def _tools_with_approved_ids(self):
        """Advertise only contract-available tools and their approved check IDs.

        This is discovery, not authorization: every call is still checked by the control plane.
        On lookup failure, serve the plain descriptions; calls then fail closed at authorization.
        """
        try:
            from twinguard import approved_check_ids
            client = Client.from_env()
            contract = _contract(client, self.contract_id, self.version)
            ids = approved_check_ids(client, self.contract_id, self.version, contract=contract)
            allowed = set(contract['allowed_tools']) | {'request_permission', 'finish_task'}
        except Exception:
            return TOOLS
        tools = []
        for tool in TOOLS:
            if tool['name'] not in allowed:
                continue
            if tool['name'] == 'run_approved_test':
                listed = ids['test']
            elif tool['name'] == 'run_approved_command':
                listed = ids['test'] + ids['static']
            else:
                tools.append(tool)
                continue
            note = f" Approved IDs for this run: {', '.join(listed)}." if listed else ' This run has no approved IDs; do not guess.'
            tools.append({**tool, 'description': tool['description'] + note})
        return tools

    def dispatch(self, message):
        method = message.get('method')
        if method == 'initialize':
            return {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'intentlock', 'version': '0.1.0'}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': self._tools_with_approved_ids()}
        if method != 'tools/call':
            raise Denied('METHOD_NOT_FOUND')
        params = message.get('params', {})
        args = params.get('arguments', {})
        tool = params.get('name')
        keys = {'list_files': {'path'}, 'read_file': {'path'}, 'search_code': {'query','paths'},
                'propose_patch': {'patch'}, 'run_approved_test': {'test_id'}, 'run_approved_command': {'command_id'},
                'request_permission': {'tool','resource','reason'}, 'finish_task': {'summary'}}
        if not isinstance(args, dict) or tool not in keys or set(args) != keys[tool]:
            raise Denied('INVALID_ARGUMENTS')
        if tool in ('list_files', 'read_file', 'propose_patch', 'run_approved_test', 'run_approved_command', 'finish_task') and not all(isinstance(v, str) for v in args.values()):
            raise Denied('INVALID_ARGUMENTS')
        if tool == 'search_code' and (not isinstance(args['query'], str) or not args['query'] or len(args['query']) > 256
                                      or not isinstance(args['paths'], list) or not 1 <= len(args['paths']) <= 16
                                      or not all(isinstance(p, str) for p in args['paths'])):
            raise Denied('INVALID_ARGUMENTS')
        if tool == 'request_permission' and not all(isinstance(v, str) and v and len(v) <= 2000 for v in args.values()):
            raise Denied('INVALID_ARGUMENTS')
        if tool == 'finish_task' and len(args['summary']) > 2000:
            raise Denied('INVALID_ARGUMENTS')
        envelope = {'request_id': str(uuid.uuid4()), 'run_id': self.run_id, 'contract_id': self.contract_id,
                    'contract_version': self.version, 'agent_id': 'kiro-cli', 'tool': tool,
                    'arguments': args, 'timestamp': datetime.now(timezone.utc).isoformat()}
        self.authorize(envelope)
        if tool == 'read_file':
            result=self.repository.read(args['path'])
        elif tool == 'list_files':
            contract = _contract(Client.from_env(), self.contract_id, self.version)
            result = json.dumps([{'path': p, 'type': 'directory' if is_dir else 'file'}
                                 for p, is_dir in self.repository.entries(args['path']) if _visible(p, contract)][:MAX_RESULTS])
        elif tool == 'search_code':
            contract = _contract(Client.from_env(), self.contract_id, self.version)
            result = json.dumps(self.repository.search(args['query'], args['paths'], contract))
        elif tool == 'run_approved_test':
            from twinguard import approved_test
            result = json.dumps(approved_test(Client.from_env(), self.repository.root, self.run_id,
                                              self.contract_id, self.version, args['test_id']))
        elif tool == 'run_approved_command':
            from twinguard import run_command
            result = json.dumps(run_command(Client.from_env(), self.repository.root, self.run_id,
                                            self.contract_id, self.version, args['command_id']))
        elif tool == 'request_permission':
            from twinguard import record_agent_note
            result = json.dumps(record_agent_note(Client.from_env(), self.run_id, 'permissions', args))
        elif tool == 'finish_task':
            from twinguard import record_agent_note
            result = json.dumps(record_agent_note(Client.from_env(), self.run_id, 'summaries', args))
        else:
            try:
                result=json.dumps(preview(Client.from_env(), self.repository.root, self.run_id, self.contract_id, self.version, args['patch']))
            except Exception as exc:
                reason = str(exc)
                safe_reason = reason if re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', reason) else type(exc).__name__
                print('TWIN_GUARD_ERROR=' + safe_reason, file=sys.stderr, flush=True)
                raise Denied('IMPACT_BLOCKED:' + safe_reason) from None
        return {'content': [{'type': 'text', 'text': result}], 'isError': False}

    def serve(self):
        while True:
            line = sys.stdin.readline(131074)
            if not line:
                return
            if len(line) > 131072:
                return
            message = None
            try:
                if len(line) > 131072:
                    raise ValueError('oversized input')
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError('invalid request')
                if 'id' not in message:
                    continue
                result = self.dispatch(message)
                output = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
            except Exception as exc:
                code = str(exc) if isinstance(exc, Denied) else 'EXECUTION_ERROR'
                if isinstance(exc, Denied) and isinstance(message, dict) and message.get('method') == 'tools/call':
                    output = {'jsonrpc': '2.0', 'id': message.get('id'), 'result': {'isError': True, 'content': [{'type': 'text', 'text': code}]}}
                else:
                    output = {'jsonrpc': '2.0', 'id': message.get('id') if isinstance(message, dict) else None, 'error': {'code': -32603, 'message': code}}
            print(json.dumps(output), flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--contract-id', required=True)
    parser.add_argument('--contract-version', required=True, type=int)
    args = parser.parse_args()
    Server(Repository(args.repository), Client.from_env().authorize, args.run_id, args.contract_id, args.contract_version).serve()
