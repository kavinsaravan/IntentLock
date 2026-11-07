"""Small stdio MCP execution adapter. Authorization remains owned by Person 1."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

MAX_FILE = 65536

from control_plane import Client, Denied
from twinguard import preview

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

TOOLS = [{'name': 'read_file', 'description': 'Read an authorized repository-relative UTF-8 file, at most 64 KiB.',
          'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}},
         {'name':'propose_patch','description':'Preview an authorized unified Git patch in TwinGuard without modifying the protected repository.',
          'inputSchema':{'type':'object','properties':{'patch':{'type':'string'}},'required':['patch'],'additionalProperties':False}}]

class Server:
    def __init__(self, repository, authorize, run_id, contract_id, version):
        self.repository, self.authorize = repository, authorize
        self.run_id, self.contract_id, self.version = run_id, contract_id, version

    def dispatch(self, message):
        method = message.get('method')
        if method == 'initialize':
            return {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'intentlock', 'version': '0.1.0'}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': TOOLS}
        if method != 'tools/call':
            raise Denied('METHOD_NOT_FOUND')
        params = message.get('params', {})
        args = params.get('arguments', {})
        tool = params.get('name')
        if not isinstance(args, dict) or (tool == 'read_file' and (set(args) != {'path'} or not isinstance(args['path'], str))) or (tool == 'propose_patch' and (set(args) != {'patch'} or not isinstance(args['patch'], str))) or tool not in ('read_file','propose_patch'):
            raise Denied('INVALID_ARGUMENTS')
        envelope = {'request_id': str(uuid.uuid4()), 'run_id': self.run_id, 'contract_id': self.contract_id,
                    'contract_version': self.version, 'agent_id': 'kiro-cli', 'tool': tool,
                    'arguments': args, 'timestamp': datetime.now(timezone.utc).isoformat()}
        self.authorize(envelope)
        if tool == 'read_file':
            result=self.repository.read(args['path'])
        else:
            try:
                result=json.dumps(preview(Client.from_env(), self.repository.root, self.run_id, self.contract_id, self.version, args['patch']))
            except Exception:
                raise Denied('IMPACT_BLOCKED') from None
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
