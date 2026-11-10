"""Which build of Orchard is running: package version plus git commit.

The commit is resolved once, when the server starts, so it names the code the
running process actually loaded (a later `git checkout` without a restart does
not change it). Sources, in order:

1. ``ORCHARD_COMMIT`` environment variable (for builds without a .git folder).
2. The ``.git`` directory of the checkout Orchard runs from, read directly.
   No ``git`` subprocess: on a server the checkout is often owned by root while
   Orchard runs as its own user, and git refuses such "dubious ownership" repos.
"""
import os
import re
from pathlib import Path
from . import __version__

ROOT=Path(__file__).resolve().parent.parent
SHA=re.compile(r'[0-9a-f]{40}([0-9a-f]{24})?')


def _git_dir(root):
    dot=root/'.git'
    if dot.is_dir(): return dot
    if dot.is_file():  # worktree or submodule: "gitdir: <path>"
        text=dot.read_text().strip()
        if text.startswith('gitdir:'):
            path=Path(text[len('gitdir:'):].strip())
            return path if path.is_absolute() else (root/path).resolve()
    return None


def _resolve_ref(git,ref):
    common=git
    commondir=git/'commondir'
    if commondir.is_file():
        common=(git/commondir.read_text().strip()).resolve()
    for base in (git,common):
        loose=base/ref
        if loose.is_file(): return loose.read_text().strip()
    packed=common/'packed-refs'
    if packed.is_file():
        for line in packed.read_text().splitlines():
            parts=line.split()
            if len(parts)==2 and parts[1]==ref: return parts[0]
    return None


def git_commit(root=ROOT):
    """Full commit SHA of the checkout at ``root``, or None if it cannot be read."""
    try:
        git=_git_dir(Path(root))
        if git is None: return None
        head=(git/'HEAD').read_text().strip()
        sha=_resolve_ref(git,head[4:].strip()) if head.startswith('ref:') else head
    except OSError:
        return None
    return sha if sha and SHA.fullmatch(sha) else None


def build_info(root=ROOT,env=None):
    env=os.environ if env is None else env
    commit=(env.get('ORCHARD_COMMIT') or '').strip().lower() or git_commit(root)
    if commit and not re.fullmatch(r'[0-9a-f]{7,64}',commit): commit=None
    return {'version':__version__,'commit':commit,'commit_short':commit[:7] if commit else None}
