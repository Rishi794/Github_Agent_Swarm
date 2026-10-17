"""Hygiene for raw model output.

8B-class models on CPU runners drift out of schema mode: they wrap JSON in
markdown fences, echo it after report headings, or answer in prose and append an
object at the end. The single-pass Ollama agent (``run_agent_ollama.py``) and
the llama.cpp debate engine (``debate_runner.py``) both funnel every completion
through :func:`parse_model_json`, so a formatting wobble degrades into a
best-effort parse instead of a crashed matrix leg.

The module also hosts the small coercion helpers that let the pydantic schemas
accept the shapes a model actually emits (``confidence_score: "87%"``, a plain
string where a list was requested, numbers inside a list of strings) and
:func:`task_identity`, which keeps multi-kilobyte mission prompts from being
copied into every persisted record.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterator, Type, TypeVar

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

MAX_CANDIDATE_CHARS = 400_000
MAX_CANDIDATES = 64
TASK_EXCERPT_CHARS = 2000

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n?(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #
def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every balanced ``{...}`` span, aware of strings and escapes."""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start != -1:
                yield text[start : index + 1]
                start = -1


def json_candidates(text: str) -> Iterator[str]:
    """Yield plausible JSON payloads found in ``text``, deduplicated.

    Ordering only decides which candidate wins when several parse: the full
    reply, then every fenced code block, then every balanced object span. The
    caller prefers candidates whose keys actually match the schema, so order
    matters less than coverage.
    """
    if not text:
        return
    seen: set[str] = set()
    for candidate in _raw_candidates(text):
        candidate = candidate.strip()
        if not candidate or candidate in seen or len(candidate) > MAX_CANDIDATE_CHARS:
            continue
        seen.add(candidate)
        yield candidate


def _raw_candidates(text: str) -> Iterator[str]:
    yield text
    for match in _FENCE_RE.finditer(text):
        yield match.group(1)
    yield from _balanced_objects(text)


def _load_lenient(candidate: str) -> Any | None:
    """``json.loads`` with one repair pass for the common trailing-comma slip."""
    for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_model_json(text: str, model_cls: Type[ModelT]) -> ModelT | None:
    """Validate the first schema-shaped JSON object found in ``text``.

    Returns ``None`` when nothing usable is present; never raises.
    """
    expected = set(model_cls.model_fields)
    first_valid: ModelT | None = None

    for index, candidate in enumerate(json_candidates(text)):
        if index >= MAX_CANDIDATES:
            break
        data = _load_lenient(candidate)
        if isinstance(data, list):
            data = next((item for item in data if isinstance(item, dict)), None)
        if not isinstance(data, dict):
            continue
        try:
            instance = model_cls.model_validate(data)
        except ValidationError:
            continue
        if expected & data.keys():
            return instance
        if first_valid is None:
            first_valid = instance

    return first_valid


def salvage_from_exception(exc: BaseException) -> str:
    """Recover the raw text a pydantic ``ValidationError`` was raised on.

    The OpenAI SDK validates ``.beta.chat.completions.parse`` content against
    the requested model, so a server that ignores ``json_schema`` surfaces the
    model's actual bytes inside the exception — no second request needed.
    """
    try:
        errors = exc.errors()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not every exception carries details
        return ""
    for error in errors:
        raw = error.get("input")
        if isinstance(raw, str) and raw.strip():
            return raw
    return ""


# --------------------------------------------------------------------------- #
# Coercions for the shapes models actually emit
# --------------------------------------------------------------------------- #
def coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        source: list[Any] = [f"{key}: {item}" for key, item in value.items()]
    elif isinstance(value, (list, tuple, set)):
        source = list(value)
    elif isinstance(value, str):
        source = [value]
    else:
        source = [value]

    items: list[str] = []
    for entry in source:
        text = coerce_text(entry)
        if text:
            items.append(text)
    return items


def coerce_confidence(value: Any) -> float:
    """Accept ``87``, ``"87"``, ``"87%"``, ``"high (0.9)"``; else 0.0."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            return float(match.group())
    return 0.0


# --------------------------------------------------------------------------- #
# Mission prompt identity
# --------------------------------------------------------------------------- #
def task_identity(task_prompt: str, limit: int = TASK_EXCERPT_CHARS) -> dict:
    """Compact, hash-stable descriptor of the mission prompt.

    ``system_prompt.md`` can be tens of kilobytes; persisting it verbatim in
    every finding/DB row inflates the knowledge base and the RAG context until
    they no longer fit the model window. The excerpt keeps reports readable and
    the hash ties each record to the exact prompt revision.
    """
    text = (task_prompt or "").strip()
    excerpt = text if len(text) <= limit else text[:limit].rstrip() + " …"
    return {
        "task": excerpt,
        "task_chars": len(text),
        "task_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }
