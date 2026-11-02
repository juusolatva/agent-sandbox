"""Trusted bounded file transfer. Always execute with python3 -I."""
import base64
import json
import os
from pathlib import Path, PurePosixPath
import sys

LIMIT = 16 * 1024 * 1024
MAX_FILES = 5000
SKIP = {'.git', '.ssh', '.aws', '.codex', '.agents', '.venv', '__pycache__',
        'node_modules', 'target', 'artifacts', '.npmrc', '.netrc', '.pypirc',
        '.pytest_cache', 'build', 'dist'}
ROOT = Path('/home/agent/workspace')


def valid_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or str(path) != name or any(
            p in {'', '.', '..'} or p in SKIP or p.startswith('.env') for p in path.parts):
        raise ValueError('Invalid bundle path')
    return path


def validate(bundle):
    if not isinstance(bundle, dict) or len(bundle) > MAX_FILES:
        raise ValueError('Invalid bundle or too many files')
    total = 0
    for name, entry in bundle.items():
        valid_path(name)
        if not isinstance(entry, dict) or set(entry) != {'data', 'executable'}:
            raise ValueError('Invalid file record')
        if not isinstance(entry['executable'], bool):
            raise ValueError('Invalid executable flag')
        content = base64.b64decode(entry['data'], validate=True)
        total += len(content)
        if total > LIMIT:
            raise ValueError('Bundle exceeds size limit')
    return bundle


def skipped(name):
    return name in SKIP or name.startswith('.env')


def walk_directories(directory, dirs):
    """Prune skipped directories in place and reject directory symlinks."""
    dirs[:] = sorted(d for d in dirs if not skipped(d))
    if any((Path(directory) / name).is_symlink() for name in dirs):
        raise ValueError('Directory symlinks are not supported in Stage 3')


def read_regular(path, budget):
    """Read a regular, non-symlink file within budget bytes, rejecting concurrent changes."""
    if path.is_symlink() or not path.is_file():
        raise ValueError('Only regular source files are supported in Stage 3')
    size = path.stat().st_size
    if size > budget:
        raise ValueError('Source export exceeds limit')
    with path.open('rb') as stream:
        content = stream.read(LIMIT + 1)
    if len(content) != size:
        raise ValueError('File changed during export')
    return content


def export(root=ROOT):
    bundle = {}
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        walk_directories(directory, dirs)
        for name in sorted(files):
            if skipped(name):
                continue
            path = Path(directory) / name
            if len(bundle) >= MAX_FILES:
                raise ValueError('Source export exceeds limit')
            content = read_regular(path, LIMIT - total)
            total += len(content)
            bundle[path.relative_to(root).as_posix()] = {
                'data': base64.b64encode(content).decode(),
                'executable': bool(path.stat().st_mode & 0o111)}
    return validate(bundle)


def restore(bundle, root=ROOT):
    validate(bundle)
    # The destination is a fresh prepared image volume, never a host checkout.
    original = export(root)
    for name in original:
        (root / name).unlink()
    parents = {parent for name in original for parent in (root / name).parents if parent.is_relative_to(root) and parent != root}
    for path in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
    for name, entry in bundle.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(entry['data'], validate=True))
        path.chmod(0o755 if entry['executable'] else 0o644)


if __name__ == '__main__':
    if sys.argv[1] == 'export':
        print(json.dumps(export(), sort_keys=True))
    elif sys.argv[1] == 'restore':
        raw = sys.stdin.buffer.read(LIMIT * 2 + 1)
        if len(raw) > LIMIT * 2:
            raise ValueError('Input exceeds limit')
        restore(json.loads(raw))
    else:
        raise ValueError('Unknown transfer operation')
