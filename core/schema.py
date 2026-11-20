"""
Minimal JSON-Schema validator for response contract checks.

Covers the subset OpenAPI documents use in practice (type, nullable, enum,
properties, required, items, allOf/anyOf/oneOf). Unknown keywords are ignored,
so a schema the validator does not understand never produces false failures.
"""

from __future__ import annotations

from typing import Any, List

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}

MAX_ERRORS = 5


def validate(instance: Any, schema: Any, path: str = "$", depth: int = 0) -> List[str]:
    """Returns a list of human-readable violations (empty when valid)."""
    if not isinstance(schema, dict) or not schema or depth > 32:
        return []

    errors: List[str] = []

    for sub in schema.get("allOf", []):
        errors.extend(validate(instance, sub, path, depth + 1))
    for keyword in ("anyOf", "oneOf"):
        options = schema.get(keyword)
        if options and all(validate(instance, sub, path, depth + 1) for sub in options):
            errors.append(f"{path}: does not match any allowed schema ({keyword})")

    if instance is None and schema.get("nullable"):
        return errors[:MAX_ERRORS]

    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        known = [t for t in types if t in _TYPE_CHECKS]
        if known and not any(_TYPE_CHECKS[t](instance) for t in known):
            errors.append(f"{path}: expected {'/'.join(known)}, got {type(instance).__name__}")
            return errors[:MAX_ERRORS]

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property '{name}'")
        for name, sub in schema.get("properties", {}).items():
            if name in instance:
                errors.extend(validate(instance[name], sub, f"{path}.{name}", depth + 1))

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(instance[:50]):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]", depth + 1))
            if len(errors) >= MAX_ERRORS:
                break

    return errors[:MAX_ERRORS]
