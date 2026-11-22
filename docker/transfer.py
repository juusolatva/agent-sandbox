"""Trusted bounded file transfer. Always execute with python3 -I."""
import base64
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys

LIMIT = 16 * 1024 * 1024
# Serialized bundles carry base64 (4/3 expansion) plus paths and JSON framing.
MAX_SERIALIZED = LIMIT * 2
MAX_FILES = 5000
# Shared with sandbox.py preparation so prepared images and snapshots agree.
SKIP = {'.git', '.ssh', '.aws', '.codex', '.agents', '.venv', '__pycache__',
        'node_modules', 'target', 'artifacts', '.npmrc', '.netrc', '.pypirc',
        '.pytest_cache', 'build', 'dist'}
ROOT = Path('/home/agent/workspace')
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK keeps a FIFO swapped in after classification from blocking the open.
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


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
        if not isinstance(entry['executable'], bool) or not isinstance(entry['data'], str):
            raise ValueError('Invalid file record')
        # Reject an oversized field before allocating its decoded form.
        if len(entry['data']) > (LIMIT - total + 2) // 3 * 4:
            raise ValueError('Bundle exceeds size limit')
        content = base64.b64decode(entry['data'], validate=True)
        total += len(content)
        if total > LIMIT:
            raise ValueError('Bundle exceeds size limit')
    return bundle


def load(stream):
    """Parse a serialized bundle from a binary stream without reading past the limit."""
    raw = stream.read(MAX_SERIALIZED + 1)
    if len(raw) > MAX_SERIALIZED:
        raise ValueError('Serialized bundle exceeds limit')
    return validate(json.loads(raw))


def skipped(name):
    return name in SKIP or name.startswith('.env')


def walk(root):
    """Yield (relative path, None) for directories and (relative path, (fd, stat)) for files.

    Traversal is anchored to directory descriptors and never follows a link,
    including the root itself. Symlinks, special files and unreadable
    directories raise instead of being skipped, so callers never receive a
    silently partial tree. Yielded file descriptors are valid only until the
    generator resumes.
    """
    try:
        root_fd = os.open(root, DIRECTORY_FLAGS)
    except OSError as exc:
        raise ValueError(f'Tree root is not a readable directory: {exc.strerror}') from exc
    try:
        yield from _walk(root_fd, PurePosixPath())
    finally:
        os.close(root_fd)


def _checked(operation, description, *args, **kwargs):
    """Run an OS call, turning OSError into a ValueError that names the path."""
    try:
        return operation(*args, **kwargs)
    except OSError as exc:
        raise ValueError(f'{description}: {exc.strerror}') from exc


def _walk(directory_fd, prefix):
    names = sorted(_checked(os.listdir, f'Cannot read directory {prefix.as_posix() or "."}', directory_fd))
    for name in names:
        if skipped(name):
            continue
        relative = prefix / name
        mode = _checked(os.stat, f'Cannot stat {relative}', name, dir_fd=directory_fd,
                        follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            child = _checked(os.open, f'Cannot open directory {relative}', name, DIRECTORY_FLAGS,
                             dir_fd=directory_fd)
            try:
                yield relative, None
                yield from _walk(child, relative)
            finally:
                os.close(child)
        elif stat.S_ISREG(mode):
            yield from _open_regular(directory_fd, name, relative)
        else:
            raise ValueError(f'Only regular files and directories are supported: {relative}')


def _open_regular(directory_fd, name, relative):
    """Yield (relative, (fd, stat)) for a file that is still regular once opened."""
    fd = _checked(os.open, f'Cannot open file {relative}', name, FILE_FLAGS, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'File changed type during traversal: {relative}')
        yield relative, (fd, info)
    finally:
        os.close(fd)


def read_regular(fd, info, budget):
    """Read an opened regular file within budget bytes, rejecting concurrent changes."""
    if info.st_size > budget:
        raise ValueError('Source export exceeds limit')
    chunks, size = [], 0
    while chunk := os.read(fd, min(1 << 20, info.st_size + 1 - size) or 1):
        chunks.append(chunk)
        size += len(chunk)
        if size > info.st_size:
            break
    if size != info.st_size:
        raise ValueError('File changed during export')
    return b''.join(chunks)


def export(root=ROOT):
    bundle = {}
    total = 0
    for relative, opened in walk(root):
        if opened is None:
            continue
        if len(bundle) >= MAX_FILES:
            raise ValueError('Source export exceeds limit')
        fd, info = opened
        content = read_regular(fd, info, LIMIT - total)
        total += len(content)
        bundle[relative.as_posix()] = {'data': base64.b64encode(content).decode(),
                                       'executable': bool(info.st_mode & 0o111)}
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
        restore(load(sys.stdin.buffer))
    else:
        raise ValueError('Unknown transfer operation')
