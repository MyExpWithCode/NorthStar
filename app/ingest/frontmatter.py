"""Minimal YAML-subset frontmatter reader/writer.

Knowledge-base documents carry their own provenance so a single `.md` file is
self-describing when a reviewer opens it. We only ever store flat string/int/bool
values, so a dependency-free reader/writer is enough -- and it cannot silently
execute anything the way a full YAML loader can.

`data/kb/sources.json` remains the authority on what is in the knowledge base;
frontmatter is the colocated copy used by the chunker.
"""

from __future__ import annotations

DELIMITER = "---"


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    text = str(value)
    needs_quotes = (
        text == ""
        or text.strip() != text
        or any(ch in text for ch in ':#"\n')
        or text[0] in "[]{}&*!|>%@`"
    )
    if needs_quotes:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _parse_value(raw: str) -> str | bool:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if raw == "true":
        return True
    if raw == "false":
        return False
    return raw


def dumps(metadata: dict[str, object], body: str) -> str:
    """Render `metadata` as a frontmatter block followed by `body`."""
    lines = [DELIMITER]
    lines += [f"{key}: {_format_value(value)}" for key, value in metadata.items()]
    lines += [DELIMITER, ""]
    return "\n".join(lines) + "\n" + body.lstrip("\n")


def loads(text: str) -> tuple[dict[str, str | bool], str]:
    """Split a document into `(metadata, body)`.

    A document with no frontmatter returns an empty metadata dict and the full
    text, so callers can handle hand-written files without special-casing.
    """
    # keepends so the body comes back byte-for-byte; rejoining would silently
    # drop the document's trailing newline.
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIMITER:
        return {}, text

    metadata: dict[str, str | bool] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == DELIMITER:
            body = "".join(lines[index + 1 :])
            return metadata, body.lstrip("\n")
        if not line.strip() or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        metadata[key.strip()] = _parse_value(raw)

    # Unterminated frontmatter -- treat the whole file as body rather than
    # guessing where the metadata ended.
    return {}, text
