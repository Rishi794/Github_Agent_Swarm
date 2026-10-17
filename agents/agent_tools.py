"""Native tool bindings for the debate agents.

Real, dependency-free tools (stdlib only) that the model can invoke mid-reasoning
through the OpenAI function-calling protocol served by ``llama-server``:

===================  ==========================================================
``search_web``       Live web search. DuckDuckGo HTML (rich snippets) with a
                     DuckDuckGo Instant Answer and Wikipedia API fallback chain,
                     so a datacenter IP that gets rate limited still returns data.
``query_arxiv``      arXiv Atom API: titles, authors, dates, abstracts, links.
``fetch_url``        Follow a specific URL and return tag-stripped text
                     (SSRF-guarded, size-capped).
``compute``          AST-restricted arithmetic, so the model can do exact math
                     instead of hallucinating it.
===================  ==========================================================

Every tool returns a compact JSON string, never raises, and is truncated to
``MAX_TOOL_RESULT_CHARS`` so a chatty tool cannot blow up the KV cache of a
CPU-bound 8B model on a 16 GB runner.

Environment variables
---------------------
TOOL_ALLOWLIST           Comma-separated tool names to expose (default: all).
MAX_TOOL_RESULT_CHARS    Hard cap on any single tool result (default: 6000).
TOOL_HTTP_TIMEOUT        Per-request timeout in seconds (default: 15).
ALLOW_PRIVATE_FETCH      Set to ``1`` to let ``fetch_url`` reach private/loopback
                         addresses (default: blocked, SSRF guard).
"""

from __future__ import annotations

import ast
import html
import ipaddress
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

USER_AGENT = "multi-agent-swarm/1.0 (+https://github.com/)"
# DuckDuckGo serves a stub/anomaly page to non-browser agents; the HTML endpoint
# needs a browser-like UA to return real result markup.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HTTP_TIMEOUT = float(os.environ.get("TOOL_HTTP_TIMEOUT", "15"))
MAX_TOOL_RESULT_CHARS = int(os.environ.get("MAX_TOOL_RESULT_CHARS", "6000"))
MAX_SNIPPET_CHARS = 500
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


# --------------------------------------------------------------------------- #
# Low level helpers
# --------------------------------------------------------------------------- #
def _http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read()


def _truncate(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _TextExtractor(HTMLParser):
    """Strip tags from HTML, dropping script/style payloads."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "tr"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


class _DdgParser(HTMLParser):
    """Extract (title, url) pairs and snippets from DuckDuckGo HTML endpoints.

    Handles both markup conventions: the ``html.duckduckgo.com`` endpoint
    (``result__a`` / ``result__snippet``) and the plain Lite endpoint
    (``result-link`` / ``result-snippet``).
    """

    TITLE_CLASSES = ("result__a", "result-link")
    SNIPPET_CLASSES = ("result__snippet", "result-snippet")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._mode: str | None = None
        self._buffer: list[str] = []
        self._url = ""

    @staticmethod
    def _matches(classes: str, wanted: tuple[str, ...]) -> bool:
        return any(name in classes for name in wanted)

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        attributes = dict(attrs)
        classes = attributes.get("class") or ""
        if tag == "a" and self._matches(classes, self.TITLE_CLASSES):
            self._mode, self._buffer, self._url = "title", [], attributes.get("href", "")
        elif self._matches(classes, self.SNIPPET_CLASSES):
            self._mode, self._buffer = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._mode:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._mode == "title" and tag == "a":
            title = _truncate("".join(self._buffer), 200)
            if title:
                self.links.append((title, _normalize_ddg_url(self._url)))
            self._mode = None
        elif self._mode == "snippet" and tag in ("a", "td", "div", "span"):
            snippet = _truncate("".join(self._buffer))
            if snippet:
                self.snippets.append(snippet)
            self._mode = None


def _normalize_ddg_url(url: str) -> str:
    """Unwrap DuckDuckGo redirect links (``/l/?uddg=<target>``) into real URLs."""
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return target
    return url


def _json(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[: MAX_TOOL_RESULT_CHARS - 20] + '... [truncated]"}'
    return text


# --------------------------------------------------------------------------- #
# Tool: search_web
# --------------------------------------------------------------------------- #
def _query_variants(query: str) -> list[str]:
    """Original query first, then a simplified retry for hyphen/symbol-heavy phrasings."""
    simplified = re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", query)).strip()
    variants = [query]
    if simplified and simplified != query:
        variants.append(simplified)
    return variants


def _search_duckduckgo_html(query: str, max_results: int) -> list[dict]:
    body = _http_get(
        "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}),
        headers={"User-Agent": BROWSER_USER_AGENT},
    )
    parser = _DdgParser()
    parser.feed(body.decode("utf-8", errors="replace"))

    results = []
    for index, (title, url) in enumerate(parser.links[:max_results]):
        snippet = parser.snippets[index] if index < len(parser.snippets) else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _search_duckduckgo_api(query: str, max_results: int) -> list[dict]:
    raw = _http_get(
        "https://api.duckduckgo.com/?"
        + urllib.parse.urlencode({"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    )
    data = json.loads(raw.decode("utf-8", errors="replace"))

    results = []
    if data.get("AbstractText"):
        results.append(
            {
                "title": data.get("Heading") or query,
                "url": data.get("AbstractURL", ""),
                "snippet": _truncate(data["AbstractText"]),
            }
        )
    for topic in data.get("RelatedTopics", []):
        if len(results) >= max_results:
            break
        if topic.get("Text") and topic.get("FirstURL"):
            results.append(
                {"title": _truncate(topic["Text"], 120), "url": topic["FirstURL"], "snippet": _truncate(topic["Text"])}
            )
    return results[:max_results]


def _search_wikipedia(query: str, max_results: int) -> list[dict]:
    raw = _http_get(
        "https://en.wikipedia.org/w/api.php?"
        + urllib.parse.urlencode(
            {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": max_results}
        )
    )
    data = json.loads(raw.decode("utf-8", errors="replace"))
    results = []
    for hit in data.get("query", {}).get("search", []):
        title = hit.get("title", "")
        results.append(
            {
                "title": title,
                "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                "snippet": _truncate(re.sub(r"<[^>]+>", "", hit.get("snippet", ""))),
            }
        )
    return results


def search_web(query: str, max_results: int = 3) -> str:
    """Search the live web for documentation, benchmarks, specs, or advisories."""
    query = (query or "").strip()
    if not query:
        return _json({"error": "query must not be empty"})

    max_results = max(1, min(int(max_results or 3), 8))
    errors: list[str] = []

    for attempt, candidate in enumerate(_query_variants(query)):
        for source, searcher in (
            ("duckduckgo_html", _search_duckduckgo_html),
            ("duckduckgo_api", _search_duckduckgo_api),
            ("wikipedia", _search_wikipedia),
        ):
            try:
                results = searcher(candidate, max_results)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
                continue
            if results:
                payload = {"query": candidate, "source": source, "results": results}
                if attempt:
                    payload["note"] = f"original query returned nothing; retried as {candidate!r}"
                return _json(payload)

    return _json(
        {
            "query": query,
            "results": [],
            "errors": errors,
            "note": "no results returned; try fewer, more generic keywords",
        }
    )


# --------------------------------------------------------------------------- #
# Tool: query_arxiv
# --------------------------------------------------------------------------- #
def query_arxiv(search_query: str, max_results: int = 2) -> str:
    """Query the arXiv API for papers, preprints, and formal results."""
    search_query = (search_query or "").strip()
    if not search_query:
        return _json({"error": "search_query must not be empty"})

    max_results = max(1, min(int(max_results or 2), 8))
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {
            "search_query": f"all:{search_query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
    )

    try:
        root = ET.fromstring(_http_get(url))
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as exc:
        return _json({"error": f"{type(exc).__name__}: {exc}", "arxiv_query": search_query})

    papers = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        def text(tag: str, limit: int = MAX_SNIPPET_CHARS) -> str:
            node = entry.find(tag, ARXIV_NS)
            return _truncate(node.text, limit) if node is not None and node.text else ""

        papers.append(
            {
                "title": text("atom:title", 250),
                "url": text("atom:id", 200),
                "published": text("atom:published", 40),
                "summary": text("atom:summary"),
                "authors": [
                    _truncate(author.findtext("atom:name", default="", namespaces=ARXIV_NS), 80)
                    for author in entry.findall("atom:author", ARXIV_NS)[:5]
                ],
            }
        )

    if not papers:
        return _json({"arxiv_query": search_query, "papers": [], "note": "no papers matched"})
    return _json({"arxiv_query": search_query, "papers": papers})


# --------------------------------------------------------------------------- #
# Tool: fetch_url
# --------------------------------------------------------------------------- #
def _host_is_public(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return False
    return True


def fetch_url(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return tag-stripped text. Private hosts are blocked (SSRF guard)."""
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return _json({"error": f"unsupported scheme: {parsed.scheme or 'none'}"})
    if not parsed.hostname:
        return _json({"error": "missing hostname"})

    if os.environ.get("ALLOW_PRIVATE_FETCH") != "1" and not _host_is_public(parsed.hostname):
        return _json({"error": f"refusing to fetch non-public host: {parsed.hostname}"})

    max_chars = max(500, min(int(max_chars or 4000), 20000))
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(400_000)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _json({"error": f"{type(exc).__name__}: {exc}", "url": url})

    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type)
    if match:
        charset = match.group(1)
    body = raw.decode(charset, errors="replace")

    if "html" in content_type.lower() or body.lstrip()[:1] == "<":
        extractor = _TextExtractor()
        extractor.feed(body)
        body = extractor.text()
    elif "json" in content_type.lower():
        try:
            body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass

    body = html.unescape(body)
    return _json(
        {
            "url": url,
            "content_type": content_type.split(";")[0],
            "chars": len(body),
            "text": body[:max_chars],
            "truncated": len(body) > max_chars,
        }
    )


# --------------------------------------------------------------------------- #
# Tool: compute
# --------------------------------------------------------------------------- #
_ALLOWED_FUNCTIONS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "floor": math.floor, "ceil": math.ceil,
}
_ALLOWED_NAMES = {
    "pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf,
    **_ALLOWED_FUNCTIONS,
}
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Call, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Tuple, ast.List,
)


def _eval_node(node: ast.AST) -> float:
    if not isinstance(node, _ALLOWED_NODES):
        raise ValueError(f"disallowed syntax: {type(node).__name__}")
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numeric constants are allowed")
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in _ALLOWED_NAMES:
            raise ValueError(f"unknown name: {node.id}")
        return _ALLOWED_NAMES[node.id]
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        return -operand if isinstance(node.op, ast.USub) else +operand
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left), _eval_node(node.right)
        operations = {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right,
            ast.FloorDiv: lambda: left // right,
            ast.Mod: lambda: left % right,
            ast.Pow: lambda: left**right,
        }
        return operations[type(node.op)]()
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
            raise ValueError("only whitelisted functions can be called")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        return _ALLOWED_FUNCTIONS[node.func.id](*[_eval_node(argument) for argument in node.args])
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval_node(element) for element in node.elts]  # type: ignore[return-value]
    raise ValueError(f"unsupported node: {type(node).__name__}")


def compute(expression: str) -> str:
    """Evaluate an arithmetic expression exactly (no ``eval`` of arbitrary code)."""
    expression = (expression or "").strip()
    if not expression:
        return _json({"error": "expression must not be empty"})
    if len(expression) > 500:
        return _json({"error": "expression too long"})

    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_node(tree)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        return _json({"expression": expression, "error": f"{type(exc).__name__}: {exc}"})

    return _json({"expression": expression, "result": value})


# --------------------------------------------------------------------------- #
# Registry, OpenAI tool schema, dispatch
# --------------------------------------------------------------------------- #
TOOL_REGISTRY: dict[str, dict] = {
    "search_web": {
        "function": search_web,
        "description": (
            "Search the live web for technical documentation, specifications, benchmarks, "
            "CVEs, or real-world facts. Use this instead of relying on parametric memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords."},
                "max_results": {"type": "integer", "description": "How many results to return (1-8, default 3)."},
            },
            "required": ["query"],
        },
    },
    "query_arxiv": {
        "function": query_arxiv,
        "description": (
            "Search arXiv for academic papers, preprints, formal proofs, and algorithmic "
            "results. Returns titles, authors, dates, and abstracts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "Academic search terms."},
                "max_results": {"type": "integer", "description": "How many papers to return (1-8, default 2)."},
            },
            "required": ["search_query"],
        },
    },
    "fetch_url": {
        "function": fetch_url,
        "description": "Fetch a specific URL (documentation page, RFC, paper) and return its readable text.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL to read."},
                "max_chars": {"type": "integer", "description": "Maximum characters of text to return (default 4000)."},
            },
            "required": ["url"],
        },
    },
    "compute": {
        "function": compute,
        "description": "Evaluate an arithmetic expression exactly (supports sqrt/log/exp/trig, pi, e, **). Use for all math.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "e.g. '(1 - 0.2) ** 3 * 4096'"}},
            "required": ["expression"],
        },
    },
}


def enabled_tool_names() -> list[str]:
    allowlist = [name.strip() for name in os.environ.get("TOOL_ALLOWLIST", "").split(",") if name.strip()]
    if allowlist:
        return [name for name in allowlist if name in TOOL_REGISTRY]
    return list(TOOL_REGISTRY)


def tool_definitions() -> list[dict]:
    """OpenAI-compatible tool schemas for the currently enabled tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_REGISTRY[name]["description"],
                "parameters": TOOL_REGISTRY[name]["parameters"],
            },
        }
        for name in enabled_tool_names()
    ]


def describe_tool_call(name: str, arguments: dict) -> str:
    """Short human-readable form for logs and the results report."""
    if not isinstance(arguments, dict):
        return f"{name}()"
    rendered = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
    return f"{name}({_truncate(rendered, 160)})"


def dispatch_tool(name: str, arguments: dict) -> str:
    """Run a tool by name; never raises, always returns a JSON string."""
    if name not in TOOL_REGISTRY:
        return _json({"error": f"unknown tool: {name}", "available": enabled_tool_names()})
    if name not in enabled_tool_names():
        return _json({"error": f"tool disabled by TOOL_ALLOWLIST: {name}"})
    if not isinstance(arguments, dict):
        return _json({"error": "arguments must be an object"})

    try:
        return TOOL_REGISTRY[name]["function"](**arguments)
    except TypeError as exc:  # bad/extra arguments produced by the model
        return _json({"error": f"invalid arguments for {name}: {exc}"})
    except Exception as exc:  # noqa: BLE001 - tool failures must never kill the debate
        return _json({"error": f"{type(exc).__name__}: {exc}"})


def run_tool_loop(
    client,  # noqa: ANN001 - openai.OpenAI
    model: str,
    messages: list[dict],
    agent_id: str,
    max_iterations: int = 3,
    temperature: float = 0.2,
    trace: list[dict] | None = None,
) -> list[dict]:
    """Let the model call tools until it stops asking, mutating ``messages`` in place.

    Returns the extended message list. If the server does not support tool calling
    (older build, or a chat template without tool support) the loop degrades
    gracefully and the caller continues with a plain structured turn.

    ``trace`` (optional) collects one entry per dispatched call so the caller can
    persist evidence of what was actually looked up.
    """
    definitions = tool_definitions()
    if not definitions:
        return messages

    for iteration in range(max(1, int(max_iterations))):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=definitions,
                tool_choice="auto",
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001 - server may reject tools entirely
            print(f"[{agent_id}] Tool calling unavailable ({type(exc).__name__}: {exc}); continuing without tools.")
            return messages

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            if message.content:
                messages.append({"role": "assistant", "content": message.content})
            return messages

        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in tool_calls
                ],
            }
        )

        for call in tool_calls:
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            print(f"[{agent_id}] Tool call (iteration {iteration + 1}): {describe_tool_call(name, arguments)}")
            result = dispatch_tool(name, arguments)
            if trace is not None:
                trace.append(
                    {
                        "tool": name,
                        "arguments": arguments,
                        "call": describe_tool_call(name, arguments),
                        "result_excerpt": _truncate(result, 300),
                    }
                )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    print(f"[{agent_id}] Tool iteration budget exhausted; forcing a final answer.")
    return messages
