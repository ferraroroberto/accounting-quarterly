"""YAML frontmatter checks for `instruction_ratchet.py`: is a `SKILL.md` block readable, and what is a field's value.

A skill whose `description:` cannot be read cannot be linted, so the ratchet reports NOT CONFIRMED rather
than a clean count. This is a deliberately small subset of YAML (plain, quoted and flow scalars, block
scalars), enough to tell a real description from one a loader would reject. Vendor-verbatim beside
`instruction_ratchet.py` (manifest key `instruction_frontmatter`), stdlib-only.
"""

from __future__ import annotations

import re

_KEY_LINE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?: +(.*))?$")
_BLOCK_SCALAR_HEADER = re.compile(r"^[|>][1-9+-]{0,2}\s*(?:#.*)?$")
# A plain scalar cannot open with one of these (YAML 1.2 c-indicator); `-`, `?`
# and `:` are indicators only when followed by whitespace.
_FORBIDDEN_FIRST = set(",]}#&*!|>'\"%@`")
_BARE_INDICATOR = re.compile(r"^[-?:](?:\s|$)")


def _quoted_error(value: str) -> str | None:
    quote = value[0]
    i = 1
    while i < len(value):
        ch = value[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and value[i + 1:i + 2] == "'":
                i += 2  # '' is an escaped quote inside a single-quoted scalar
                continue
            rest = value[i + 1:]
            if rest.strip() and not re.match(r"^\s+#", rest):
                return f"text after the closing {quote} quote"
            return None
        i += 1
    return f"{quote}-quoted value is not closed on its own line"


def _flow_error(value: str) -> str | None:
    closers = {"[": "]", "{": "}"}
    stack: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in closers:
            stack.append(closers[ch])
        elif ch in "]}":
            if not stack or stack.pop() != ch:
                return f"unbalanced {ch!r} in flow collection"
            if not stack:
                rest = value[i + 1:]
                if rest.strip() and not re.match(r"^\s+#", rest):
                    return "text after the flow collection"
                return None
    return "flow collection is not closed on its own line"


def _plain_error(value: str) -> str | None:
    if value[0] in _FORBIDDEN_FIRST or _BARE_INDICATOR.match(value):
        return f"unquoted value starts with the YAML indicator {value[0]!r}"
    if re.search(r":\s", value) or value.endswith(":"):
        return "unquoted value contains ': ' (a YAML mapping indicator)"
    if re.search(r"\s#", value):
        return "unquoted value is cut off at ' #' (a YAML comment)"
    return None


def _value_error(key: str, value: str) -> str | None:
    if value[0] in "[{":
        if key == "description":
            return "`description` is a YAML flow collection, not text"
        return _flow_error(value)
    if value[0] in "'\"":
        return _quoted_error(value)
    return _plain_error(value)


def frontmatter_error(text: str) -> str | None:
    """Why `text`'s `---` frontmatter would not parse as written, or None (also for no frontmatter).

    Checks the narrow YAML subset a SKILL.md uses -- top-level `key: value` lines
    with single-line scalars or flow collections; block scalars and nested blocks
    are accepted unread. A value a YAML loader would reject or silently truncate
    loses the skill's routing text, so it is reported, never read past.
    """
    if not text.startswith("---"):
        return None
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return "opening '---' line carries trailing text"
    close = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if close is None:
        return "frontmatter has no closing '---' line"
    seen: set[str] = set()
    nested_ok = False  # indented lines are content of the previous key
    plain_key: str | None = None  # previous key held a plain scalar
    for n, raw in enumerate(lines[1:close], start=2):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if line[0] == "\t":
                return f"line {n}: tab indentation is not allowed in YAML"
            if plain_key is not None:
                return f"line {n}: unquoted `{plain_key}:` value continues onto the next line"
            if not nested_ok:
                return f"line {n}: indented line does not belong to any key"
            continue
        m = _KEY_LINE.match(line)
        if not m:
            return f"line {n}: not a `key: value` entry"
        key, value = m.group(1), (m.group(2) or "").strip()
        if key in seen:
            return f"line {n}: duplicate key `{key}`"
        seen.add(key)
        nested_ok, plain_key = False, None
        if not value or value.startswith("#"):
            nested_ok = True
            continue
        if value[0] in "|>":
            if not _BLOCK_SCALAR_HEADER.match(value):
                return f"line {n}: malformed block scalar header"
            nested_ok = True
            continue
        error = _value_error(key, value)
        if error:
            return f"line {n}: `{key}`: {error}"
        if value[0] not in "'\"[{":
            plain_key = key
    return None


def frontmatter_field(text: str, name: str) -> str | None:
    """The `<name>:` value of a `---` block, or None; an empty value or `>`/`|` header takes the indented lines below."""
    m = text.startswith("---") and re.search(
        rf"^{re.escape(name)}:[ \t]*(.*)((?:\n(?:[ \t].*)?)*)", text[3:].split("\n---", 1)[0], re.MULTILINE)
    if not m:
        return None
    value, rest = m.group(1).strip(), m.group(2)
    if value and not _BLOCK_SCALAR_HEADER.match(value):
        return value
    return ("\n" if value.startswith("|") else " ").join(s.strip() for s in rest.split("\n") if s.strip()) or None
