"""Exact artifact promotion; the reviewed patch bytes are the only input applied."""
import hashlib
from pathlib import Path
import subprocess
from threading import Lock

_lock = Lock()
class PromotionRefused(Exception): pass

def _git(repo,*args,input=None):
    return subprocess.run(['git','-C',str(repo),*args],input=input,capture_output=True)

def promote(repo, artifact, base_commit, approved_sha256, run_status):
    with _lock:
        if run_status() not in ('active','completed'): raise PromotionRefused('RUN_NOT_ACTIVE')
        repo=Path(repo).resolve()
        patch=Path(artifact).read_bytes()
        if hashlib.sha256(patch).hexdigest()!=approved_sha256: raise PromotionRefused('PATCH_HASH_MISMATCH')
        head=_git(repo,'rev-parse','HEAD')
        if head.returncode or head.stdout.decode().strip()!=base_commit: raise PromotionRefused('STALE_BASE_COMMIT')
        state=_git(repo,'status','--porcelain')
        if state.returncode or state.stdout: raise PromotionRefused('WORKTREE_NOT_CLEAN')
        if _git(repo,'apply','--check','-',input=patch).returncode: raise PromotionRefused('PATCH_NOT_APPLICABLE')
        if run_status() not in ('active','completed'): raise PromotionRefused('RUN_NOT_ACTIVE')
        applied=_git(repo,'apply','-',input=patch)
        if applied.returncode: raise PromotionRefused('PATCH_APPLY_FAILED')
        return approved_sha256
