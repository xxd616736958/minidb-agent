"""Result limiting and sensitive data masking for PostgreSQL tools."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse


SENSITIVE_FIELD_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|credential|private[_-]?key|email|phone|id_card)",
    re.IGNORECASE,
)
POSTGRES_URL_RE = re.compile(r"(postgres(?:ql)?://[^:\s]+:)([^@\s]+)(@[^/\s]+)", re.IGNORECASE)
PASSWORD_PARAM_RE = re.compile(r"(password\s*=\s*)(['\"]?)([^'\"\s;]+)(['\"]?)", re.IGNORECASE)


def obfuscate_password(text: str | None) -> str | None:
    """Mask passwords in URLs, DSN fragments, and free-form error text."""
    if text is None or text == "":
        return text
    try:
        parsed = urlparse(text)
        if parsed.scheme and parsed.netloc and parsed.password:
            netloc = parsed.netloc.replace(parsed.password, "****")
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    text = POSTGRES_URL_RE.sub(r"\1****\3", text)
    text = PASSWORD_PARAM_RE.sub(r"\1\2****\4", text)
    return text


def _mask_scalar(key: str, value: Any) -> tuple[Any, bool]:
    if SENSITIVE_FIELD_RE.search(key):
        return "***MASKED***", True
    if isinstance(value, str):
        masked = obfuscate_password(value)
        return masked, masked != value
    return value, False


def mask_sensitive(value: Any, parent_key: str = "") -> tuple[Any, list[str]]:
    """Return a copy with sensitive values masked and the masked field paths."""
    masked_fields: list[str] = []
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            path = f"{parent_key}.{key}" if parent_key else str(key)
            if SENSITIVE_FIELD_RE.search(str(key)):
                output[key] = "***MASKED***"
                masked_fields.append(path)
                continue
            output[key], nested = mask_sensitive(item, path)
            masked_fields.extend(nested)
        return output, masked_fields
    if isinstance(value, list):
        output_list = []
        for idx, item in enumerate(value):
            masked_item, nested = mask_sensitive(item, f"{parent_key}[{idx}]")
            output_list.append(masked_item)
            masked_fields.extend(nested)
        return output_list, masked_fields
    masked, changed = _mask_scalar(parent_key, value)
    if changed and parent_key:
        masked_fields.append(parent_key)
    return masked, masked_fields


def limit_rows(
    rows: list[dict[str, Any]],
    *,
    max_rows: int = 100,
    max_cell_chars: int = 500,
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """Mask and truncate row results for model-safe output."""
    truncated = len(rows) > max_rows
    limited = rows[:max_rows]
    masked_rows, masked_fields = mask_sensitive(limited)
    assert isinstance(masked_rows, list)

    cell_truncated = False
    normalized: list[dict[str, Any]] = []
    for row in masked_rows:
        normalized_row: dict[str, Any] = {}
        for key, value in row.items():
            if value == "***MASKED***":
                normalized_row[key] = value
            elif isinstance(value, str) and len(value) > max_cell_chars:
                normalized_row[key] = value[:max_cell_chars] + "...[truncated]"
                cell_truncated = True
            else:
                normalized_row[key] = value
        normalized.append(normalized_row)
    return normalized, truncated or cell_truncated, masked_fields


def limit_payload(
    payload: dict[str, Any],
    *,
    max_payload_chars: int = 20_000,
) -> tuple[dict[str, Any], bool, list[str]]:
    """Mask sensitive values and truncate oversized JSON-ish payloads."""
    masked, fields = mask_sensitive(payload)
    assert isinstance(masked, dict)
    text = repr(masked)
    if len(text) <= max_payload_chars:
        return masked, False, fields
    return {
        "truncated": True,
        "preview": text[:max_payload_chars],
        "original_size_chars": len(text),
    }, True, fields
