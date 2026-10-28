"""Ratchet on instruction-file lint: the deterministic rules of the fleet's prompt audit.

A line that makes the model over-trigger, or a file past its vendor-documented size
cap, costs every session that reads an instruction file. The weekly prompt audit
finds those a week later; this stage refuses a new one at ship time, per file and
per rule, against a committed baseline: it fails only when a count *rises*.

It counts the audit's lint-tier rules (`RULES`; rule text and fix shapes in
fleet-config's `.claude/skills/prompt-audit/rules.md`) over the root `CLAUDE.md` and
`AGENTS.md`, `.claude/rules/*.md`, `<skill_dir>/*/SKILL.md`, `extra_files` and the
`.md` files a skill references (scope is `git ls-files --cached --others --exclude-standard`). Only hits that reach
*violation* for the file's reader count: a single-vendor rule is advisory in an
agent-neutral file. R-14 is the size cap (`SIZE_CAPS`); an over-cap file is baselined
at its size and may only shrink. `NOT_ENFORCED` names the rules left to the audit.

Opt-in per adopter: `[instruction_ratchet]` in its `.fleet.toml`. Exit 0 pass or not
enabled; 1 a count rose or the baseline is missing; 2 bad configuration; 3 NOT
CONFIRMED (never folded into a pass). Modes: `check`, `--init`, `--update-baseline`,
`--update-baseline --allow-additions`. Vendor-verbatim (manifest key
`instruction_ratchet`), stdlib-only; limits and procedure: docs/instruction-ratchet.md.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".instruction-ratchet-baseline.json"
DEFAULT_SKILL_DIRS = (".claude/skills",)
GIT_TIMEOUT_S = 120

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "skill_dirs", "extra_files"}

#: R-14 -- (unit, limit) per file kind; a skill reference doc has no cap.
SIZE_CAPS: dict[str, tuple[str, int]] = {
    "claude-md": ("lines", 200), "rules": ("lines", 200),
    "skill": ("body-lines", 500), "agents-md": ("bytes", 32768),
}
ALWAYS_ON_KINDS = frozenset({"claude-md", "agents-md", "rules"})

#: Enforced rules -> (vendor tag, file scope, title), as tagged in rules.md.
RULES: dict[str, tuple[str, str, str]] = {
    "R-01": ("anthropic", "any", "No ALL-CAPS pressure"),
    "R-02": ("anthropic", "any", "No blanket tool defaults"),
    "R-03": ("shared", "any", "No forced verification or re-check lines"),
    "R-04": ("shared", "any", "No periodic progress-summary scaffolding"),
    "R-05": ("anthropic", "any", "No narration suppression"),
    "R-06": ("anthropic", "any", "No blanket anti-formatting blocks"),
    "R-07": ("anthropic", "any", "No hand-written reasoning plans"),
    "R-08": ("anthropic", "any", 'No "do not think" rules'),
    "R-09": ("anthropic", "any", "No recall-suppressing review instructions"),
    "R-10": ("anthropic", "any", "No prefill or thinking-budget references"),
    "R-11": ("anthropic", "any", "No time-sensitive phrasing"),
    "R-12": ("openai", "any", "No upfront-plan or preamble demands"),
    "R-14": ("shared", "any", "Instruction-file size cap"),
    "R-15": ("anthropic", "skill", "Skill description: short, third person"),
    "R-16": ("shared", "any", "No contradictory rules"),
    "R-17": ("shared", "any", "Vendor-specific guidance carries a neutrality marker"),
    "R-30": ("anthropic", "any", "No reasoning-in-the-answer instructions"),
    "R-34": ("anthropic", "skill", "Reference files one level deep"),
    "R-35": ("anthropic", "skill-ref", "Long reference files open with a contents list"),
    "R-36": ("anthropic", "skill", "Forward-slash paths"),
    "R-45": ("anthropic", "any", "No blanket tool-use discouragement"),
}
#: Lint-tier rules this stage leaves to the weekly audit, and why.
NOT_ENFORCED: dict[str, str] = {
    "R-13": "a per-line count of negative wording that the audit judges as a ratio; a ratchet on "
            "the count would refuse every legitimate new prohibition",
    "R-37": "its vocabulary is every qualified tool name across the whole fleet's skills, so a "
            "commit in another repo could fail this one; it is not decidable from one tree",
}

NEUTRAL = "neutral"


@dataclass(frozen=True)
class Audience:
    vendor: str
    markers: tuple[str, ...]  # a heading carrying one scopes its section to this reader
    terms: tuple[str, ...]  # product words R-17 looks for, case-sensitive whole words
    path_globs: tuple[str, ...]  # fnmatch over "<repo>/<path>" -- whole files scoped to this reader
    unpointed: tuple[str, ...]  # basenames scoped to this reader unless AGENTS.md points at them


#: Mirrors the prompt audit's `sources.toml` [audiences]; tests/test_instruction_ratchet.py checks it.
AUDIENCES: dict[str, Audience] = {
    "claude": Audience("anthropic", ("Claude Code only",), ("Claude", "Anthropic"),
                       ("*/.claude/rules/*.md",), ("CLAUDE.md",)),
    "openai": Audience("openai", ("Codex only",), ("Codex", "OpenAI", "GPT"), (), ()),
}

_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
#: Occurrence patterns: every match counts, headings included.
PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "R-01": [re.compile(r"\b(?:MUST|NEVER|ALWAYS|CRITICAL|IMPORTANT|REQUIRED|MANDATORY|NOT|DON'T)\b")],
    "R-02": [re.compile(r"\b(?:if|when) in doubt\b|\bdefault to (?:using|calling|running|invoking)\b", re.I)],
    "R-03": [re.compile(r"\bdouble[- ]check|\bre-?verify\b|\bverify your (?:answer|work|output|response|changes)\b"
                        r"|\bfinal verification step\b|\bsubagent to verify\b|\bre-?check your\b", re.I)],
    "R-04": [re.compile(r"\bevery\s+(?:\d+|few|couple of)\s+tool[- ]calls?\b"
                        r"|\bafter every\s+\d+\s+(?:tool[- ]calls?|steps)\b", re.I)],
    "R-05": [re.compile(r"\bhold (?:all |every |any )?(?:findings|results|updates|output)\s+"
                        r"(?:for|until)\s+(?:the\s+)?(?:final|end)\b", re.I)],
    "R-07": [re.compile(r"\bthink step[- ]by[- ]step\b|\bstep[- ]by[- ]step (?:reasoning|thinking)\b"
                        r"|\breason step[- ]by[- ]step\b", re.I),
             re.compile(r"^\s*\d+[.)]\s+(?:think|reason|reflect)\b", re.I)],
    "R-08": [re.compile(r"\b(?:do not|don't|never)\s+(?:think|reason)\b(?!\s+(?:about|of|that)\b)", re.I)],
    "R-09": [re.compile(r"\bonly report (?:high|critical)[- ]severity\b|\bbe conservative\b"
                        r"|\b(?:do not|don't) nitpick\b", re.I)],
    "R-10": [re.compile(r"\bprefill(?:ed|s|ing)?\b|\bbudget_tokens\b", re.I)],
    "R-11": [re.compile(rf"\b(?:before|after|until|since|as of|by)\s+(?:{_MONTHS})\s+\d{{4}}\b", re.I)],
    "R-12": [re.compile(r"\b(?:outline|present|share|state|write)\s+(?:a|an|your)\s+(?:upfront\s+)?plan\s+before\b"
                        r"|\bupfront plan\b|\bbegin (?:each|every) (?:response|turn) with a plan\b", re.I)],
    "R-30": [re.compile(r"\b(?:explain|show|write out) (?:your|its) reasoning in (?:the|your) (?:response|answer|reply|output)\b"
                        r"|\bwrite out (?:your|its) reasoning\b|\bshow your work\b"
                        r"|\bthink out loud in (?:the|your) (?:response|answer|reply|output)\b", re.I)],
    "R-45": [re.compile(r"\bonly use tools when strictly necessary\b|\bminimi[sz]e tool calls\b"
                        r"|\bavoid (?:using|calling) tools unless\b", re.I)],
}
#: Line patterns: one hit per matching non-heading line.
LINE_PATTERNS: dict[str, re.Pattern[str]] = {
    "R-06": re.compile(r"\b(?:do not|don't|never|avoid|no)\s+(?:use\s+|using\s+)?"
                       r"(?:markdown|bullet(?:s| points)?|headers|headings|bold|lists|formatting)\b", re.I),
}
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_MODAL = re.compile(r"\b(always|must not|must|never|do not|don't)\s+([a-z][a-z-]*(?:\s+[a-z][a-z-]*){0,2})", re.I)
# "I" only as a capitalised standalone word (never the I in "I/O"); the rest any case.
_PERSON = re.compile(r"\bI\b(?!/)|\b(?i:me|my|we|our|you|your)\b")
_QUOTED = re.compile(r"\"[^\"]+\"")
_SKILL_NAME = re.compile(r"[a-z0-9-]{1,64}")
_XML_TAG = re.compile(r"</?[A-Za-z][\w.-]*(?:\s[^<>]*)?>")
_CONTENTS = re.compile(r"^#{1,6}\s+(?:table of\s+)?contents\b", re.I)
REF_TOC_LINES, REF_TOC_WINDOW = 100, 40
DESCRIPTION_MAX_CHARS = 1024
# R-36: a drive path or a Windows venv path is a host-local fact (caps at consider); any
# other relative backslash path caps at violation. A segment after a backslash never
# opens with `_`, so a markdown escape (`snake\_case`) is not a path.
_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s`'\"<>|*?()]*")
_REL_PATH = re.compile(r"(?<![\w.:\\/%-])(?:[\w.-]+\\)+[A-Za-z0-9.][\w.-]*")
_VENV_PATH = re.compile(r"(?:^|\\)\.venv\\", re.I)
_MD_LINK = re.compile(r"\]\(([^)\s]+)")
_TICKED = re.compile(r"`([^`\s]+)`")
REF_SKIP_DIRS = {"conversations", "evals", "__pycache__"}


class ConfigError(RuntimeError):
    """The `[instruction_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A file could not be linted, so its count is unknown."""


@dataclass(frozen=True)
class Config:
    baseline: str
    skill_dirs: tuple[str, ...]
    extra_files: tuple[str, ...]


@dataclass(frozen=True)
class Doc:
    path: str  # repo-relative, forward slashes
    kind: str  # claude-md | agents-md | rules | skill | skill-ref
    text: str
    size_bytes: int  # CRLF normalised, so a Windows checkout and CI agree
    audience: str = NEUTRAL


@dataclass(frozen=True)
class Hit:
    rule: str
    line: int
    count: int
    text: str
    cap: str  # "violation" or "consider": the strongest verdict the rule can reach here


@dataclass(frozen=True)
class Entry:
    """One file's standing: violation-cap hits per rule, plus its size when over its cap."""

    hits: dict[str, int] = field(default_factory=dict)
    size: int | None = None


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip("/") for v in value):
        raise ConfigError(f"[instruction_ratchet] {name} must be a list of non-empty paths")
    return tuple(v.replace("\\", "/").strip("/") for v in value)


def parse_config(toml_text: str) -> Config | None:
    """`[instruction_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key must not
    quietly run with defaults.
    """
    table = tomllib.loads(toml_text).get("instruction_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[instruction_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[instruction_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[instruction_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[instruction_ratchet] baseline must be a non-empty string")
    extra = _str_list(table, "extra_files", ())
    for path in extra:
        if kind_of(path) is None:
            raise ConfigError(f"[instruction_ratchet] extra_files: {path} is not an instruction file "
                              "(a *CLAUDE.md, AGENTS.md, .claude/rules/*.md or SKILL.md)")
    return Config(baseline, _str_list(table, "skill_dirs", DEFAULT_SKILL_DIRS), extra)


def load_config(root: Path) -> Config | None:
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ---------------------------------------------------------- markdown shape

def fence_flags(lines: list[str]) -> list[bool]:
    """True for each line that is a fence or inside one; raises `NotConfirmed` on a fence left open.

    A fence closes only on the same character, at least as long, with nothing
    after it -- so a four-backtick example can carry three-backtick lines.
    """
    flags: list[bool] = []
    fence: str | None = None
    opened = 0
    for no, line in enumerate(lines, 1):
        m = _FENCE.match(line)
        if fence is None:
            opens = m is not None and not (m.group(1)[0] == "`" and "`" in m.group(2))
            if m is not None and opens:
                fence, opened = m.group(1), no
            flags.append(opens)
            continue
        flags.append(True)
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
            fence = None
    if fence is not None:
        raise NotConfirmed(f"the fenced block opened on line {opened} never closes")
    return flags


def unfenced_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, raw text) for every line outside a code fence."""
    lines = text.splitlines()
    return [(i, line) for i, (line, fenced) in enumerate(zip(lines, fence_flags(lines)), start=1) if not fenced]


def body_start(text: str) -> int:
    """1-based line number where a frontmatter-bearing file's body begins."""
    if not text.startswith("---"):
        return 1
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return i + 2
    return 1


def instruction_lines(text: str) -> list[tuple[int, str]]:
    """(line number, text) for lintable prose: no frontmatter, fences or inline code."""
    start = body_start(text)
    return [(i, _INLINE_CODE.sub(" ", raw)) for i, raw in unfenced_lines(text) if i >= start]


def paragraphs(lines: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    paras: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    for n, line in lines:
        if line.strip():
            cur.append((n, line))
        elif cur:
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    return paras


# ------------------------------------------------------------- frontmatter

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
    """The `<name>:` value from a `---` frontmatter block, or None."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    m = re.search(rf"^{re.escape(name)}:\s*(.+)$", block, re.MULTILINE)
    return m.group(1).strip() if m else None


# --------------------------------------------------------- files + readers

def kind_of(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name == "SKILL.md":
        return "skill"
    if "/.claude/rules/" in f"/{path}" and name.endswith(".md"):
        return "rules"
    if name.endswith("CLAUDE.md"):
        return "claude-md"
    if name == "AGENTS.md":
        return "agents-md"
    return None


def _ref_target(raw: str, bases: tuple[Path, ...], root: Path) -> str | None:
    """The in-repo `.md` a link or backticked path names, else None."""
    target = raw.split("#", 1)[0]
    if (not target.endswith(".md") or "://" in target or target.startswith(("/", "~", "mailto:"))
            or re.match(r"^[A-Za-z]:", target) or any(c in target for c in "<>*$")):
        return None
    real_root = root.resolve()
    for base in bases:
        cand = (base / target).resolve()
        if cand.is_file() and cand.is_relative_to(real_root):
            rel = cand.relative_to(real_root)
            if REF_SKIP_DIRS.intersection(rel.parts) or kind_of(rel.as_posix()) is not None:
                return None  # a case file, another skill's SKILL.md (delegation), or an always-on file
            return rel.as_posix()
    return None


def references(path: str, text: str, root: Path, ticked: bool = True) -> list[tuple[int, str]]:
    """(line, path) for each local `.md` a file references, first mention only, fences excluded.

    A reference is a markdown link (resolved against the file's folder) or, with
    `ticked`, a backticked relative path that exists (the file's folder, then the
    repo root).
    """
    here = (root / path).parent
    seen: dict[str, int] = {}
    for n, line in unfenced_lines(text):
        cands: list[tuple[str, tuple[Path, ...]]] = [(t, (here,)) for t in _MD_LINK.findall(line)]
        if ticked:
            cands += [(t, (here, root)) for t in _TICKED.findall(line)]
        for raw, bases in cands:
            target = _ref_target(raw, bases, root)
            if target is not None and target != path and target not in seen:
                seen[target] = n
    return sorted(((n, t) for t, n in seen.items()), key=lambda x: x[0])


def audience_of(path: str, root: Path) -> str:
    for name, aud in AUDIENCES.items():
        if any(fnmatch.fnmatch(f"repo/{path}", g) for g in aud.path_globs):
            return name
    base = path.rsplit("/", 1)[-1]
    agents = root / "AGENTS.md"
    try:
        pointer = agents.read_text(encoding="utf-8", errors="replace") if agents.is_file() else ""
    except OSError:
        pointer = ""
    for name, aud in AUDIENCES.items():
        if base in aud.unpointed and base not in pointer:
            return name
    return NEUTRAL


def sections(text: str, file_audience: str) -> list[tuple[int, int, str]]:
    """(start, end, audience) for each heading section scoped to a reader other than the file's."""
    lines = text.splitlines()
    heads = []
    for i, line in unfenced_lines(text):
        m = _HEADING.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    out = []
    for idx, (start, level, heading) in enumerate(heads):
        aud = next((n for n, a in AUDIENCES.items() if any(mk in heading for mk in a.markers)), None)
        if not aud or aud == file_audience:
            continue
        end = len(lines)
        for nxt, nlevel, _ in heads[idx + 1:]:
            if nlevel <= level:
                end = nxt - 1
                break
        out.append((start, end, aud))
    return out


def audience_at(line_no: int, file_audience: str, secs: list[tuple[int, int, str]]) -> str:
    inner = [s for s in secs if s[0] <= line_no <= s[1]]
    return max(inner, key=lambda s: s[0])[2] if inner else file_audience


def rule_applies(scope: str, kind: str) -> bool:
    if scope == "any":
        return kind != "skill-ref"  # a skill's reference docs answer only to rules scoped to them
    if scope == "claude-md":
        return kind in ALWAYS_ON_KINDS
    return scope == kind


def verdict_cap(vendor: str, audience: str) -> str | None:
    """Strongest verdict a rule can reach for a reader; None = does not apply."""
    if vendor == "shared":
        return "violation"
    if vendor == "conflict":
        return "violation" if audience == NEUTRAL else None
    if audience == NEUTRAL:
        return "consider"
    aud = AUDIENCES.get(audience)
    return "violation" if aud is not None and aud.vendor == vendor else None


# ------------------------------------------------------------------- lint

def _terms(aud: Audience) -> re.Pattern[str]:
    return re.compile("|".join(rf"\b{re.escape(t)}\b" for t in aud.terms) if aud.terms else r"(?!)")


class _Lint:
    """One file's lint pass; each `_rule_*` method adds the hits of one rule family."""

    def __init__(self, doc: Doc, root: Path) -> None:
        self.doc, self.root = doc, root
        self.raw = doc.text.splitlines()
        self.lines = instruction_lines(doc.text)
        self.secs = sections(doc.text, doc.audience)
        self.hits: list[Hit] = []
        self.size: int | None = None

    def add(self, rule: str, line: int, count: int, excerpt: str, cap_as: str | None = None) -> None:
        vendor, scope, _ = RULES[rule]
        if not rule_applies(scope, self.doc.kind):
            return
        cap = verdict_cap(vendor, audience_at(line, self.doc.audience, self.secs))
        if cap is not None:
            text = re.sub(r"\s+", " ", excerpt).strip()[:120]
            self.hits.append(Hit(rule, line, count, text, cap_as or cap))

    def rule_phrases(self) -> None:
        for n, line in self.lines:
            stripped = line.strip()
            if not stripped:
                continue
            for rule, pats in PATTERNS.items():
                count = sum(len(p.findall(line)) for p in pats)
                if count:
                    self.add(rule, n, count, self.raw[n - 1])
            if _HEADING.match(stripped):
                continue
            for rule, pat in LINE_PATTERNS.items():
                if pat.search(line):
                    self.add(rule, n, 1, self.raw[n - 1])

    def rule_size(self) -> None:
        if self.doc.kind not in SIZE_CAPS:
            return
        unit, limit = SIZE_CAPS[self.doc.kind]
        if unit == "lines":
            size = len(self.raw)
        elif unit == "body-lines":
            size = len(self.raw) - body_start(self.doc.text) + 1
        else:
            size = self.doc.size_bytes
        self.size = size
        if size > limit:
            self.add("R-14", 1, 1, f"{unit} {size} over cap {limit}")

    def rule_skill_description(self) -> None:
        text = self.doc.text
        err = frontmatter_error(text)
        desc = "" if err else (frontmatter_field(text, "description") or "")
        if not desc:
            raise NotConfirmed(f"its description cannot be read ({err or 'no description: field'})")
        if len(desc) > DESCRIPTION_MAX_CHARS:
            self.add("R-15", 1, 1, f"description {len(desc)} chars over {DESCRIPTION_MAX_CHARS}")
        prose = _QUOTED.sub(" ", desc)  # quoted trigger phrases are the routing surface, kept verbatim
        person = len(_PERSON.findall(prose))
        if person:
            self.add("R-15", 1, person, f"description: {prose}")
        tags = _XML_TAG.findall(desc)
        if tags:
            self.add("R-15", 1, len(tags), f"description XML tag: {' '.join(tags)}")
        name = (frontmatter_field(text, "name") or "").strip().strip("\"'")
        if not _SKILL_NAME.fullmatch(name):
            self.add("R-15", 1, 1, f"name {name!r} is not 1-64 chars of [a-z0-9-]")

    def rule_nested_references(self) -> None:
        direct = references(self.doc.path, self.doc.text, self.root)
        named = {p for _, p in direct}
        for n, ref in direct:
            text = _read_text(self.root, ref)
            # Onward references count markdown links only: a backticked path in a reference doc is a mention.
            leaves = [leaf for _, leaf in references(ref, text, self.root, ticked=False) if leaf not in named]
            if leaves:
                self.add("R-34", n, len(leaves), f"{ref} references {', '.join(leaves)} - not referenced from SKILL.md")

    def rule_backslash_paths(self) -> None:
        body = body_start(self.doc.text)
        for n, line in unfenced_lines(self.doc.text):
            if n < body:
                continue  # inline code is included: it is what gets copied
            rel_paths = _REL_PATH.findall(_DRIVE_PATH.sub(" ", line))
            host = _DRIVE_PATH.findall(line) + [p for p in rel_paths if _VENV_PATH.search(p)]
            skill = [p for p in rel_paths if not _VENV_PATH.search(p)]
            if host:
                self.add("R-36", n, len(host), line, cap_as="consider")
            if skill:
                self.add("R-36", n, len(skill), line, cap_as="violation")

    def rule_contents_list(self) -> None:
        if len(self.raw) > REF_TOC_LINES:
            top = [line for n, line in unfenced_lines(self.doc.text) if n <= REF_TOC_WINDOW]
            if not any(_CONTENTS.match(line) for line in top):
                self.add("R-35", 1, 1, f"{len(self.raw)} lines, no contents heading in the first {REF_TOC_WINDOW}")

    def rule_contradictions(self) -> None:
        polarity: dict[str, dict[str, int]] = {}
        for n, line in self.lines:
            for modal, phrase in _MODAL.findall(line):
                sign = "+" if modal.lower() in ("always", "must") else "-"
                polarity.setdefault(phrase.lower(), {}).setdefault(sign, n)
        for _, signs in sorted(polarity.items()):
            if len(signs) == 2:
                first = min(signs.values())
                self.add("R-16", first, 1, self.raw[first - 1])

    def rule_neutrality(self) -> None:
        markers = [mk for a in AUDIENCES.values() for mk in a.markers]
        for para in paragraphs(self.lines):
            first = para[0][0]
            if audience_at(first, self.doc.audience, self.secs) != NEUTRAL:
                continue
            joined = " ".join(line for _, line in para)
            if any(mk in joined for mk in markers):
                continue
            named = [a for a in AUDIENCES.values() if _terms(a).search(joined)]
            if len(named) == 1:
                terms = _terms(named[0])
                anchor = next((n for n, line in para if terms.search(line)), first)
                self.add("R-17", anchor, 1, self.raw[anchor - 1])


def lint_doc(doc: Doc, root: Path) -> tuple[list[Hit], int | None]:
    """Every hit in one file, at every cap, plus its R-14 size (None when its kind has no cap)."""
    lint = _Lint(doc, root)
    lint.rule_phrases()
    lint.rule_size()
    if doc.kind == "skill":
        lint.rule_skill_description()
        lint.rule_nested_references()
        lint.rule_backslash_paths()
    if doc.kind == "skill-ref":
        lint.rule_contents_list()
    lint.rule_contradictions()
    lint.rule_neutrality()
    return sorted(lint.hits, key=lambda h: (h.line, h.rule)), lint.size


# ---------------------------------------------------------------- the tree

def tracked_files(root: Path) -> list[str]:
    """Repo-relative, forward-slash paths of every tracked or untracked-but-not-ignored file."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True, env=env,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git ls-files timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git ls-files exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


def _read_bytes(root: Path, path: str) -> bytes:
    try:
        return (root / path).read_bytes()
    except OSError as exc:
        raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc


def _read_text(root: Path, path: str) -> str:
    try:
        return _read_bytes(root, path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotConfirmed(f"{path}: is not valid UTF-8: {exc}") from exc


def _doc(root: Path, path: str, kind: str) -> Doc:
    raw = _read_bytes(root, path)
    return Doc(path, kind, _read_text(root, path), len(raw.replace(b"\r\n", b"\n")), audience_of(path, root))


def inventory(root: Path, cfg: Config) -> list[Doc]:
    """The instruction files in scope, then the reference docs their skills name."""
    tracked = tracked_files(root)
    present = {p for p in tracked if (root / p).is_file()}  # tracked but deleted: nothing to lint
    missing = [p for p in cfg.extra_files if p not in present]
    if missing:
        raise ConfigError(f"[instruction_ratchet] extra_files not found in this tree: {', '.join(missing)}")
    wanted = [*cfg.extra_files, "CLAUDE.md", "AGENTS.md"]
    wanted += [p for p in tracked if PurePosixPath(p).parent.as_posix() == ".claude/rules" and p.endswith(".md")]
    for skill_dir in cfg.skill_dirs:
        wanted += [p for p in tracked if re.fullmatch(re.escape(skill_dir) + r"/[^/]+/SKILL\.md", p)]
    docs: dict[str, Doc] = {}
    for path in wanted:
        kind = kind_of(path)
        if path in present and path not in docs and kind is not None:
            docs[path] = _doc(root, path, kind)
    for skill in [d for d in docs.values() if d.kind == "skill"]:
        for _, ref in references(skill.path, skill.text, root):
            if ref not in docs:
                docs[ref] = _doc(root, ref, "skill-ref")
    return list(docs.values())


@dataclass(frozen=True)
class Scan:
    files: int
    hits: dict[str, list[Hit]]  # violation-cap hits per file, R-14 included; only files with one
    sizes: dict[str, int]  # measured R-14 size per capped file


def scan_tree(root: Path, cfg: Config) -> Scan:
    docs = inventory(root, cfg)
    hits: dict[str, list[Hit]] = {}
    sizes: dict[str, int] = {}
    for doc in docs:
        try:
            found, size = lint_doc(doc, root)
        except NotConfirmed as exc:
            raise NotConfirmed(f"{doc.path}: {exc}") from exc
        violations = [h for h in found if h.cap == "violation"]
        if violations:
            hits[doc.path] = violations
        if size is not None:
            sizes[doc.path] = size
    return Scan(len(docs), hits, sizes)


def current_entries(scan: Scan) -> dict[str, Entry]:
    """What the baseline would record: violation counts per rule, and the size of a file over its cap."""
    out: dict[str, Entry] = {}
    for path, found in scan.hits.items():
        counts: dict[str, int] = {}
        for h in found:
            if h.rule != "R-14":
                counts[h.rule] = counts.get(h.rule, 0) + h.count
        over = any(h.rule == "R-14" for h in found)
        out[path] = Entry(dict(sorted(counts.items())), scan.sizes[path] if over else None)
    return out


# ---------------------------------------------------------------- baseline

def _entry_from(item: object, where: str) -> tuple[str, Entry]:
    if not isinstance(item, dict) or not isinstance(item.get("key"), str):
        raise ConfigError(f"{where}: malformed entry {item!r}")
    hits, size = item.get("hits", {}), item.get("size")
    if not isinstance(hits, dict) or not all(
        k in RULES and k != "R-14" and isinstance(v, int) and not isinstance(v, bool) and v > 0 for k, v in hits.items()
    ):
        raise ConfigError(f"{where}: malformed hits in {item!r}")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 1):
        raise ConfigError(f"{where}: malformed size in {item!r}")
    if not hits and size is None:
        raise ConfigError(f"{where}: entry {item!r} records nothing")
    return item["key"], Entry(dict(sorted(hits.items())), size)


def read_baseline(path: Path) -> dict[str, Entry]:
    """Baseline file -> ``{path: Entry}``. Raises `ConfigError` on anything off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ConfigError(f"baseline {path}: `entries` must be a list")
    return dict(_entry_from(item, f"baseline {path}") for item in raw_entries)


def render_baseline(entries: dict[str, Entry]) -> str:
    """Sorted, one entry per line, no line numbers, so a baseline diff reviews cleanly."""
    lines = []
    for key in sorted(entries):
        entry = entries[key]
        record: dict[str, object] = {"key": key}
        if entry.hits:
            record["hits"] = entry.hits
        if entry.size is not None:
            record["size"] = entry.size
        lines.append("    " + json.dumps(record, ensure_ascii=False))
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def risen_rules(cur: Entry, base: Entry) -> list[str]:
    """Rules whose count rose, plus R-14 when the file is over its cap and above its baselined size."""
    rose = [r for r in sorted(cur.hits) if cur.hits[r] > base.hits.get(r, 0)]
    if cur.size is not None and (base.size is None or cur.size > base.size):
        rose.append("R-14")
    return rose


def diff(current: dict[str, Entry], baseline: dict[str, Entry]) -> tuple[list[str], list[str]]:
    """(files that rose or are new, files that fell or are gone), sorted."""
    none = Entry()
    rose, shrank = [], []
    for path in sorted(set(current) | set(baseline)):
        cur, base = current.get(path, none), baseline.get(path, none)
        if risen_rules(cur, base):
            rose.append(path)
        if risen_rules(base, cur):
            shrank.append(path)
    return rose, shrank


# -------------------------------------------------------------------- CLI

def _total(entries: dict[str, Entry]) -> int:
    return sum(sum(e.hits.values()) + (e.size is not None) for e in entries.values())


def _report_rose(rose: list[str], scan: Scan, current: dict[str, Entry], baseline: dict[str, Entry]) -> None:
    print(f"instruction ratchet: {len(rose)} file(s) above the baseline:")
    for path in rose:
        cur, base = current[path], baseline.get(path, Entry())
        for rule in risen_rules(cur, base):
            title = RULES[rule][2]
            if rule == "R-14":
                was = f"baseline {base.size}" if base.size is not None else "not baselined"
                print(f"  {path}: {rule} {title}: {cur.size} ({was})")
            else:
                print(f"  {path}: {rule} {title}: {cur.hits[rule]} hit(s) (baseline {base.hits.get(rule, 0)})")
            for hit in scan.hits[path]:
                if hit.rule == rule:
                    print(f"      line {hit.line}: {hit.text}")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        print(f"instruction ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("instruction ratchet: not enabled -- set [instruction_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("instruction ratchet: not enabled (no [instruction_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline

    baseline: dict[str, Entry] = {}
    if init:
        if baseline_path.exists():
            print(f"instruction ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
    elif baseline_path.is_file():
        try:
            baseline = read_baseline(baseline_path)
        except ConfigError as exc:
            print(f"instruction ratchet: BASELINE ERROR: {exc}")
            return EXIT_CONFIG
    else:
        print(f"instruction ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/instruction_ratchet.py --init`")
        return EXIT_RISE

    try:
        scan = scan_tree(root, cfg)
    except ConfigError as exc:
        print(f"instruction ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    except NotConfirmed as exc:
        print(f"instruction ratchet: NOT CONFIRMED: {exc}")
        print("  (the count could not be established, so it is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_entries(scan)

    if init:
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"instruction ratchet: wrote {cfg.baseline} with {len(current)} file(s), {_total(current)} hit(s)")
        return EXIT_PASS

    rose, shrank = diff(current, baseline)
    if update:
        if rose and not allow_additions:
            _report_rose(rose, scan, current, baseline)
            print("instruction ratchet: nothing written -- fix the lines, or raise the baseline with --allow-additions")
            return EXIT_RISE
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"instruction ratchet: {cfg.baseline} now has {len(current)} file(s), {_total(current)} hit(s) "
              f"({len(shrank)} lowered or dropped, {len(rose)} raised or added)")
        return EXIT_PASS

    if rose:
        _report_rose(rose, scan, current, baseline)
        print("  Each rule's reason and fix shape: fleet-config's .claude/skills/prompt-audit/rules.md. Over a size "
              "cap, move scoped detail into a skill, rule or referenced doc (an @import does not shrink it); or "
              "(reviewed) raise the baseline with `--update-baseline --allow-additions`.")
        return EXIT_RISE
    print(f"instruction ratchet: PASS ({_total(current)} hit(s) in {len(current)} of {scan.files} file(s), "
          "none above the baseline)")
    if shrank:
        print(f"  {len(shrank)} file(s) are below the baseline -- run "
              "`python scripts/instruction_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="lower or drop entries (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also raise or add entries (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    sys.exit(main())
