"""Trusted, standard-library-only transport. CLI paths are never caller controlled."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

PROJECTION = Path("/projection")
STAGING = Path("/stage")
OUTPUT = Path("/output")
CONTROL = Path("/control")
MAX_INPUT = 1048576
MAX_CONTEXT = 16384
MAX_CHUNK = 196608
MAX_CHUNKS = 8


def encode(value):
    def check(item, depth=0):
        if depth > 64:
            raise ValueError("JSON nesting exceeds transport bounds")
        if isinstance(item, dict):
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)

    check(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate JSON keys")
        value[key] = item
    return value


def invalid_constant(_):
    raise ValueError("Nonfinite JSON is unsupported")


def decode(raw):
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid bounded JSON transport") from None


def projected(name, limit):
    # Kubelet's trusted projection uses symlinks. This volume is never mounted
    # in the action container; manifest digests authenticate every input chunk.
    try:
        with (PROJECTION / name).open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError()
        return raw
    except (OSError, ValueError):
        raise ValueError("Projected payload is unavailable or exceeds bounds") from None


def write_immutable(name, raw):
    try:
        fd = os.open(
            STAGING / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444
        )
    except FileExistsError:
        fd = os.open(STAGING / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if (
                not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                or stream.read(len(raw) + 1) != raw
            ):
                raise ValueError("Staged payload is not immutable")
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def initialize():
    try:
        manifest = decode(projected("manifest.json", 65536))
        count = manifest.get("count")
        chunks = manifest.get("chunks")
        size = manifest.get("length")
        if (
            type(manifest.get("version")) is not int
            or manifest.get("version") != 1
            or type(count) is not int
            or not 1 <= count <= MAX_CHUNKS
            or not isinstance(chunks, list)
            or len(chunks) != count
            or type(size) is not int
            or not 0 <= size <= MAX_INPUT
        ):
            raise ValueError("Invalid input projection proof")
        raw = bytearray()
        for index, proof in enumerate(chunks):
            if type(proof.get("index")) is not int or proof["index"] != index:
                raise ValueError("Invalid input chunk order")
            chunk = projected("chunk-" + str(index), MAX_CHUNK)
            if (
                type(proof.get("length")) is not int
                or len(chunk) != proof.get("length")
                or hashlib.sha256(chunk).hexdigest() != proof.get("sha256")
            ):
                raise ValueError("Invalid input chunk proof")
            raw.extend(chunk)
            if len(raw) > MAX_INPUT:
                raise ValueError("Input projection exceeds bounds")
        if (
            len(raw) != size
            or hashlib.sha256(raw).hexdigest() != manifest.get("sha256")
            or not isinstance(decode(raw), dict)
        ):
            raise ValueError("Invalid input envelope proof")
        context = manifest.get("context")
        context_raw = None
        if context is not None:
            if not isinstance(context, dict):
                raise ValueError("Invalid context envelope")
            context_raw = encode(context)
            if len(context_raw) > MAX_CONTEXT:
                raise ValueError("Context exceeds bounds")
        write_immutable("input.json", bytes(raw))
        if context_raw is not None:
            write_immutable("context.json", context_raw)
    except (OSError, TypeError, AttributeError, KeyError, RecursionError):
        raise ValueError("Input projection is unavailable or invalid") from None


def capture():
    directory = None
    fd = None
    try:
        directory = os.open(OUTPUT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = os.open(
            "result.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_INPUT
        ):
            raise ValueError("Result must be one bounded regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(MAX_INPUT + 1)
            after = os.fstat(stream.fileno())
        if (
            len(raw) > MAX_INPUT
            or after.st_size != len(raw)
            or after.st_nlink != 1
            or (before.st_mtime_ns, before.st_size)
            != (after.st_mtime_ns, after.st_size)
        ):
            raise ValueError("Result changed or exceeded bounds during capture")
        result = decode(raw)
        if (
            not isinstance(result, dict)
            or set(result) - {"output", "usage"}
            or not isinstance(result.get("output"), dict)
        ):
            raise ValueError("Invalid result envelope")
        usage = result.get("usage", [])
        if (
            not isinstance(usage, list)
            or len(usage) > 256
            or any(not isinstance(record, dict) for record in usage)
        ):
            raise ValueError("Invalid result usage envelope")
        envelope = {"output": result["output"], "usage": usage}
        if len(encode(envelope)) > MAX_INPUT:
            raise ValueError("Normalized result exceeds transport bounds")
        return envelope
    except (OSError, RecursionError):
        raise ValueError(
            "Result is unavailable or is not a safe regular file"
        ) from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def release():
    try:
        fd = os.open(
            CONTROL / "released",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        if not (CONTROL / "released").is_file() or (CONTROL / "released").is_symlink():
            raise ValueError("Invalid collector control state") from None
    else:
        os.close(fd)


def serve():
    while not (CONTROL / "released").exists():
        time.sleep(0.2)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("init", "serve", "capture", "release"):
        print("Unsupported helper invocation", file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == "init":
            initialize()
        elif sys.argv[1] == "serve":
            serve()
        elif sys.argv[1] == "capture":
            sys.stdout.buffer.write(encode(capture()))
        else:
            release()
        return 0
    except (ValueError, OSError, RecursionError):
        print("Bounded job transport is unavailable or invalid", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
