"""Ask the swarm's sqlite-vec knowledge base a question — straight from GitHub.

Used by ``.github/workflows/swarm_search.yml``, which can be triggered either
manually (Actions -> Run workflow) or by opening an issue titled
``[Search] ...`` / ``[Ask] ...``. The same script backs both paths.

Flow: embed the question with ``fastembed`` -> KNN over ``vec_findings`` in
``knowledge_base.db`` (published on ``central-storage`` by the debate swarm) ->
optionally synthesize an executive answer with a local ``llama-server`` when
``LLM_BASE_URL`` points at a healthy one -> write ``search_results.md`` (the
report that is appended to ``$GITHUB_STEP_SUMMARY``, posted as an issue comment,
and republished to ``central-storage``) plus the raw hits JSON.

The synthesis step degrades gracefully: if no LLM endpoint is available, the
report still ships the ranked vector matches with a clear note.

Environment variables
---------------------
SEARCH_QUERY          The question to ask (required).
TOP_K                 Vector matches to retrieve (default 8, clamped 1-25).
CONFIG_PATH           model_config.json (default: external_config/model_config.json)
KB_DB_PATH            sqlite-vec database (default: central_memory/knowledge_base.db)
OUTPUT_MD             Markdown report path (default: search_results.md)
HITS_JSON_PATH        Raw hits JSON path (default: /tmp/retrieved_hits.json)
LLM_BASE_URL          Optional OpenAI-compatible endpoint (e.g. http://127.0.0.1:8080/v1)
LLM_MODEL             Model alias for synthesis (default: model_alias from the config)
LLM_API_KEY           API key for the endpoint (default: llama-cpp)
SYNTHESIS_MAX_TOKENS  Synthesis budget (default: 900)
LLM_TIMEOUT_SEC       Per-request timeout for synthesis (default: 240)
RUN_URL               Optional link to the workflow run, rendered in the report.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "external_config/model_config.json"))
DB_PATH = Path(os.environ.get("KB_DB_PATH", "central_memory/knowledge_base.db"))
OUTPUT_MD = Path(os.environ.get("OUTPUT_MD", "search_results.md"))
HITS_JSON_PATH = Path(os.environ.get("HITS_JSON_PATH", "/tmp/retrieved_hits.json"))

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_TOP_K = 8
MAX_TOP_K = 25
SUMMARY_PROMPT_CHARS = 800
TASK_PROMPT_CHARS = 400
FINDING_PROMPT_CHARS = 300
FINDINGS_PER_HIT = 5


def serialize_f32(vector: list[float]) -> bytes:
    """Pack a float list into the little-endian binary layout sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"[search] Config not found at {CONFIG_PATH}; using built-in defaults.")
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def embed_query(query: str, model_name: str) -> list[float]:
    from fastembed import TextEmbedding

    print(f"[search] Embedding the question with {model_name} ...")
    embedder = TextEmbedding(model_name=model_name)
    return list(embedder.embed([query]))[0].tolist()


def count_rows(db_path: Path) -> int:
    try:
        db = sqlite3.connect(str(db_path))
        try:
            return int(db.execute("SELECT COUNT(*) FROM findings_metadata").fetchone()[0])
        finally:
            db.close()
    except sqlite3.Error:
        return 0


def knn_search(db_path: Path, query_vec: list[float], top_k: int) -> tuple[list[dict], str | None]:
    """KNN over vec_findings; returns (hits, note). ``note`` explains empty results."""
    import sqlite_vec

    db = sqlite3.connect(str(db_path))
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        cursor = db.cursor()
        try:
            rows = cursor.execute(
                """
                SELECT
                    v.distance,
                    m.agent_id,
                    m.round_num,
                    m.run_id,
                    m.timestamp,
                    m.task,
                    m.summary,
                    m.key_findings_json,
                    m.confidence_score
                FROM vec_findings v
                JOIN findings_metadata m ON m.id = v.finding_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance ASC
                """,
                (serialize_f32(query_vec), top_k),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return [], f"vector tables unavailable ({exc})"
    finally:
        db.close()

    hits = []
    for distance, agent_id, round_num, run_id, timestamp, task, summary, findings_json, confidence in rows:
        try:
            key_findings = json.loads(findings_json)
        except (TypeError, json.JSONDecodeError):
            key_findings = []
        hits.append(
            {
                "distance": round(float(distance), 4),
                "agent_id": agent_id,
                "round": round_num,
                "run_id": run_id,
                "timestamp": timestamp,
                "task_excerpt": (task or "").strip()[:TASK_PROMPT_CHARS],
                "summary": (summary or "").strip(),
                "key_findings": [str(item) for item in (key_findings or [])][:FINDINGS_PER_HIT],
                "confidence": round(float(confidence or 0.0), 2),
            }
        )
    return hits, None


def synthesize(query: str, hits: list[dict], config: dict) -> tuple[str | None, str | None]:
    """Run the local model over the retrieved hits; returns (text, failure_note)."""
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    if not base_url:
        return None, "no LLM endpoint configured for this run"
    if not hits:
        return None, "no findings matched the question"

    from openai import OpenAI

    model = os.environ.get("LLM_MODEL", "").strip() or str(config.get("model_alias", "swarm-agent"))
    api_key = os.environ.get("LLM_API_KEY", "llama-cpp")
    max_tokens = int(os.environ.get("SYNTHESIS_MAX_TOKENS", "900"))
    timeout = float(os.environ.get("LLM_TIMEOUT_SEC", "240"))

    payload = [
        {
            "agent_id": hit["agent_id"],
            "round": hit["round"],
            "run_id": hit["run_id"],
            "distance": hit["distance"],
            "confidence": hit["confidence"],
            "summary": hit["summary"][:SUMMARY_PROMPT_CHARS],
            "key_findings": [finding[:FINDING_PROMPT_CHARS] for finding in hit["key_findings"]],
        }
        for hit in hits
    ]

    print(f"[search] Synthesizing an answer with {model} ({len(hits)} findings in prompt) ...")
    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the synthesis engine of a multi-agent research swarm. Answer the "
                        "question strictly from the retrieved findings, citing evidence inline like "
                        "`[research_agent_01 · round 2 · run 123456]` using the agent_id, round and run_id "
                        "fields. Call out consensus, disagreements between agents, and unresolved "
                        "risks. Plain markdown, concise. Never invent citations. /no_think"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"### Question\n{query}\n\n"
                        f"### Retrieved findings (ranked by vector distance, closest first)\n"
                        f"{json.dumps(payload, indent=2)}"
                    ),
                },
            ],
            max_tokens=max_tokens,
            temperature=0.2,
            timeout=timeout,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return None, "the model returned an empty answer"
        return text, None
    except Exception as exc:  # noqa: BLE001 - a report must ship even when the LLM is down
        return None, f"{type(exc).__name__}: {exc}"


def _cell(text: str, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    text = text.replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_markdown(
    query: str,
    hits: list[dict],
    total_rows: int,
    synthesis: str | None,
    synthesis_note: str | None,
    top_k: int,
) -> str:
    run_url = os.environ.get("RUN_URL", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Swarm Knowledge Search",
        "",
        f"**Question:** {query}",
        f"**Matches:** {len(hits)} of {total_rows} indexed findings (top-{top_k} by vector distance)",
        f"**Knowledge base:** `central-storage/knowledge_base.db` · **Generated:** `{generated}`"
        + (f" · **[Workflow run]({run_url})**" if run_url else ""),
        "",
        "## Executive synthesis",
        "",
    ]
    if synthesis:
        lines.append(synthesis)
    else:
        lines.append(f"_Synthesis unavailable ({synthesis_note}); ranked vector matches below._")

    lines += ["", "## Ranked vector matches", ""]
    if not hits:
        lines.append("_No findings matched. The knowledge base may be empty, or the question "
                     "may be too far from anything the swarm has investigated so far._")
    else:
        lines += [
            "| # | Distance | Agent (`round`) | Conf | Run | Summary |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for index, hit in enumerate(hits, 1):
            lines.append(
                f"| {index} | `{hit['distance']}` | `{hit['agent_id']}` (r{hit['round']}) | "
                f"**{hit['confidence']}** | `{hit['run_id']}` | {_cell(hit['summary'])} |"
            )
        lines += ["", "## Key findings behind the top matches", ""]
        for index, hit in enumerate(hits, 1):
            lines.append(
                f"### {index}. `{hit['agent_id']}` — round {hit['round']} · run `{hit['run_id']}` "
                f"· distance `{hit['distance']}` · confidence `{hit['confidence']}`"
            )
            if hit["key_findings"]:
                lines += [f"- {_cell(finding, 320)}" for finding in hit["key_findings"]]
            else:
                lines.append("- (no key findings recorded)")
            lines.append("")

    lines += ["---"]
    if repo:
        lines.append(
            f"_Sources: [`knowledge_base.db` + `agentic_results.md`]"
            f"(https://github.com/{repo}/tree/central-storage) — asked from GitHub, "
            f"answered by the swarm's own vector memory._"
        )
    else:
        lines.append("_Answered from the swarm's own vector memory._")
    return "\n".join(lines) + "\n"


def write_outputs(query: str, hits: list[dict], total_rows: int, synthesis: str | None,
                  synthesis_note: str | None, top_k: int) -> None:
    HITS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    HITS_JSON_PATH.write_text(
        json.dumps(
            {
                "query": query,
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "total_indexed_rows": total_rows,
                "top_k": top_k,
                "synthesis_available": synthesis is not None,
                "synthesis_note": synthesis_note,
                "top_hits": hits,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(
        render_markdown(query, hits, total_rows, synthesis, synthesis_note, top_k),
        encoding="utf-8",
    )


def main() -> int:
    query = os.environ.get("SEARCH_QUERY", "").strip()
    if not query:
        print("[search] SEARCH_QUERY is empty; nothing to search for.", file=sys.stderr)
        return 2

    config = load_config()
    top_k = max(1, min(int(os.environ.get("TOP_K", DEFAULT_TOP_K)), MAX_TOP_K))

    if not DB_PATH.exists():
        print(f"[search] No knowledge base at {DB_PATH}; publishing a how-to message.")
        write_outputs(
            query,
            [],
            0,
            None,
            "no knowledge base yet — the scheduled swarm has not completed a "
            "consolidating run, so `central-storage/knowledge_base.db` does not exist",
            top_k,
        )
        return 0

    total_rows = count_rows(DB_PATH)
    model_name = config.get("embedding_model", DEFAULT_EMBEDDING_MODEL)
    query_vec = embed_query(query, model_name)
    hits, note = knn_search(DB_PATH, query_vec, top_k)
    if note:
        print(f"[search] Vector search note: {note}")

    synthesis, synthesis_note = synthesize(query, hits, config)
    write_outputs(query, hits, total_rows, synthesis, synthesis_note, top_k)

    print(
        f"[search] Retrieved {len(hits)} hit(s) from {total_rows} indexed row(s); "
        f"synthesis={'yes' if synthesis else f'no ({synthesis_note})'}; report -> {OUTPUT_MD}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
