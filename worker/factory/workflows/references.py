"""Small mapping-key reference language; it never evaluates expressions."""

import json
import re

CONTEXT_FIELDS = frozenset(
    {
        "project_id",
        "repository_id",
        "repository",
        "run_id",
        "stage_id",
        "execution_id",
        "attempt",
    }
)
SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
SLUG = re.compile(r"^[a-z][a-z0-9-]{0,99}$")


def json_bytes(value):
    def check(item, depth=0):
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON mapping keys must be strings")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError("Value must be JSON data")

    check(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Value must be finite JSON data") from None


def is_reference(value):
    return isinstance(value, dict) and set(value) == {"$ref"}


def parse_reference(path):
    if not isinstance(path, str) or len(path) > 1024:
        raise ValueError("Invalid binding reference")
    parts = path.split(".")
    if any(not SEGMENT.fullmatch(part) or part.startswith("__") for part in parts):
        raise ValueError("References support mapping keys only")
    if parts[0] == "input" and len(parts) >= 2:
        return tuple(parts)
    if parts[0] == "context" and len(parts) == 2 and parts[1] in CONTEXT_FIELDS:
        return tuple(parts)
    if (
        parts[0] == "stages"
        and len(parts) >= 3
        and SLUG.fullmatch(parts[1])
        and parts[2] == "output"
    ):
        return tuple(parts)
    raise ValueError("Reference must address input, reserved context or a stage output")


def references(value, path=(), depth=0):
    if depth > 64:
        raise ValueError("JSON binding nesting exceeds 64 levels")
    if is_reference(value):
        yield path, parse_reference(value["$ref"])
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from references(item, (*path, key), depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from references(item, (*path, index), depth + 1)


def contains_reference(value):
    return next(references(value), None) is not None
