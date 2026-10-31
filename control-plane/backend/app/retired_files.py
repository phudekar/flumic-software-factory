"""Explicit offline retirement of positively identified worker files.

The caller must verify retired identities against the database and stop their
writers first. This helper never discovers candidates by age or project ID.
"""
import hashlib
import os
from pathlib import Path
import shutil
import tarfile


def retire_files(root, paths, retired_worker_ids, mounted_paths, archive):
    root = Path(root).resolve(strict=True)
    paths = [Path(path) for path in paths]
    mounted = [Path(path).resolve() for path in mounted_paths]
    if any(identity in {'.', '..'} or Path(identity).name != identity
           for identity in retired_worker_ids):
        raise ValueError('Invalid retired worker identity')
    allowed = {root / 'control-plane/snapshots' / identity
               for identity in retired_worker_ids}
    allowed.update(root / 'control-spool' / identity for identity in retired_worker_ids)
    allowed.update(root / name for name in (
        'factory.sqlite3', 'factory.sqlite3-wal', 'factory.sqlite3-shm', 'factory.sqlite3.locks'))
    files = {}
    tree = set()
    for path in paths:
        if path not in allowed or path.is_symlink() or path.resolve() != path:
            raise ValueError('Path is not an approved canonical retirement target')
        if any(mount == path or path in mount.parents for mount in mounted):
            raise ValueError('Retirement target contains a current runtime mount')
        if not path.exists():
            raise ValueError('Retirement target changed or is missing')
        entries = [path, *path.rglob('*')] if path.is_dir() else [path]
        for entry in entries:
            tree.add(str(entry))
            if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                raise ValueError('Retirement target contains a symlink or special file')
            if entry.is_file():
                files[str(entry.relative_to(root))] = hashlib.sha256(entry.read_bytes()).hexdigest()
    archive = Path(archive)
    if any(archive.resolve() == path or path in archive.resolve().parents for path in paths):
        raise ValueError('Backup must be outside retirement targets')
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output:
        with tarfile.open(fileobj=output, mode='w:gz', dereference=False) as tar:
            for path in paths:
                tar.add(path, arcname=str(path.relative_to(root)))
    with tarfile.open(archive, 'r:gz') as tar:
        for name, digest in files.items():
            with tar.extractfile(name) as content:
                if hashlib.sha256(content.read()).hexdigest() != digest:
                    raise ValueError('Backup verification failed')
    # Check all contents again before the first deletion; callers keep writers stopped.
    current_tree = set()
    for path in paths:
        entries = [path, *path.rglob('*')] if path.is_dir() else [path]
        if any(entry.is_symlink() for entry in entries):
            raise ValueError('Retirement tree changed during backup')
        current_tree.update(str(entry) for entry in entries)
    if tree != current_tree:
        raise ValueError('Retirement tree changed during backup')
    for name, digest in files.items():
        path = root / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Retirement files changed during backup')
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return {'paths': [str(path.relative_to(root)) for path in paths], 'files': len(files)}
