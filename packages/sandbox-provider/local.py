"""Disposable Docker sandbox for the frozen TypeScript/Node demo scope."""
import hashlib
import gzip
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

IMAGE = 'intentlock-twinguard:local'
MAX_PATCH = 1_000_000

class SandboxError(Exception):
    pass


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def clean_base(repo):
    repo = Path(repo).resolve()
    if git(repo, 'status', '--porcelain'):
        raise SandboxError('repository must be clean')
    return git(repo, 'rev-parse', 'HEAD')


def _archive(repo, base):
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', '--format=tar', base])
    if len(archive) > 100_000_000:
        raise SandboxError('snapshot too large')
    import io
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = tar.getmembers()
        if len(members) > 2000 or any(not m.isfile() and not m.isdir() for m in members):
            raise SandboxError('snapshot contains unsupported entry')
        if any(part.startswith('.env') or part in ('.aws', '.ssh', '.git', '.npmrc', '.pypirc', 'credentials', 'credentials.json', 'id_rsa', 'id_ed25519')
               or part.endswith(('.pem', '.key')) for m in members for part in Path(m.name).parts):
            raise SandboxError('snapshot contains protected path')
    return archive


def archive_repository(repo, base):
    return gzip.compress(_archive(repo, base), mtime=0)


def snapshot(repo, base, target):
    import io
    with tarfile.open(fileobj=io.BytesIO(_archive(repo, base))) as tar:
        tar.extractall(target, filter='data')


def execute(repo, base, patch, *, run_test=True, timeout=60):
    if not isinstance(patch, bytes) or not patch or len(patch) > MAX_PATCH:
        raise SandboxError('invalid patch size')
    if any(marker in patch for marker in (b'GIT binary patch', b'new file mode 120000', b'new file mode 100755', b'old mode 100644\nnew mode 100755')):
        raise SandboxError('unsupported patch type')
    if clean_base(repo) != base:
        raise SandboxError('STALE_BASE_COMMIT')
    with tempfile.TemporaryDirectory(prefix='intentlock-sandbox-') as tmp:
        root = Path(tmp)
        inp, patch_dir, out = (root / x for x in ('input', 'patch', 'out'))
        for p in (inp, patch_dir, out): p.mkdir()
        snapshot(repo, base, inp)
        (patch_dir / 'input.patch').write_bytes(patch)
        if run_test: (out / 'run-test').touch()
        out.chmod(0o777)
        runner = Path(__file__).with_name('runner.sh').resolve()
        container = 'intentlock-tg-' + uuid.uuid4().hex[:16]
        cmd = ['docker', 'run', '--rm', '--name', container, '--network', 'none', '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '512m', '--cpus', '1',
               '--user', '65534:65534', '--tmpfs', '/work:rw,mode=1777,size=128m',
               '--mount', f'type=bind,src={inp},dst=/input,readonly',
               '--mount', f'type=bind,src={patch_dir},dst=/patch,readonly',
               '--mount', f'type=bind,src={out},dst=/out',
               '--mount', f'type=bind,src={runner},dst=/runner.sh,readonly', IMAGE, 'sh', '/runner.sh']
        try:
            started = time.monotonic()
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
            raise SandboxError('SANDBOX_TIMEOUT') from None
        if p.returncode:
            raise SandboxError('SANDBOX_PATCH_FAILED')
        return {'patch': (out/'result.patch').read_bytes(), 'numstat': (out/'numstat.txt').read_text(),
                'names': (out/'names.txt').read_text(),
                'duration_ms': int((time.monotonic() - started) * 1000),
                'test_exit': int((out/'test-exit.txt').read_text()) if (out/'test-exit.txt').exists() else None,
                'test_output': (out/'test-output.txt').read_bytes() if (out/'test-output.txt').exists() else b''}
