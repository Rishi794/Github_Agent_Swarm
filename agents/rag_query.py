"""Pre-LLM semantic retrieval over the persistent sqlite-vec knowledge base.

This script is deliberately a **short-lived standalone process**. It runs before
``llama-server`` boots, writes the retrieved context to disk, and exits so the
interpreter (plus ``fastembed``/ONNX Runtime and the embedding weights) releases
every byte of RAM back to the operating system.

That ordering is the first layer of the OOM-prevention strategy: embedding
weights and LLM weights never coexist in the memory of a 16 GB GitHub runner.

Environment variables
---------------------
CONFIG_PATH       Path to ``model_config.json`` (default: external_config/model_config.json)
KB_DB_PATH        Path to the sqlite-vec database (default: central_memory/knowledge_base.db)
RAG_CONTEXT_PATH  Where the retrieved context JSON is written (default: /tmp/rag_context.json)
TASK_PROMPT       The user task to embed (required)
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
from pathlib import Path

import sqlite_vec
from fastembed import TextEmbedding

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "external_config/model_config.json"))
DB_PATH = Path(os.environ.get("KB_DB_PATH", "central_memory/knowledge_base.db"))
OUT_PATH = Path(os.environ.get("RAG_CONTEXT_PATH", "/tmp/rag_context.json"))

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_TOP_K = 4


def serialize_f32(vector: list[float]) -> bytes:
    """Pack a float list into the little-endian binary layout sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"[RAG] Config not found at {CONFIG_PATH}; using built-in defaults.")
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_context(memories: list[dict]) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"retrieved_memories": memories}, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    config = load_config()
    task_prompt = os.environ.get("TASK_PROMPT", "").strip()
    if not task_prompt:
        print("[RAG] TASK_PROMPT is empty; nothing to retrieve.", file=sys.stderr)
        write_context([])
        return 0

    if not DB_PATH.exists():
        print(f"[RAG] No {DB_PATH} found. Starting with a clean slate.")
        write_context([])
        return 0

    embed_model_name = config.get("embedding_model", DEFAULT_EMBEDDING_MODEL)
    top_k = int(config.get("rag_top_k", DEFAULT_TOP_K))

    # --- Embed the task (heavy: model weights live only inside this process) ---
    print(f"[RAG] Embedding query with {embed_model_name} ...")
    embedder = TextEmbedding(model_name=embed_model_name)
    query_vec = list(embedder.embed([task_prompt]))[0].tolist()

    # --- KNN search over the persisted vector table ---
    db = sqlite3.connect(str(DB_PATH))
    rows: list[tuple] = []
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
                    m.task,
                    m.summary,
                    m.key_findings_json,
                    m.timestamp
                FROM vec_findings v
                JOIN findings_metadata m ON m.id = v.finding_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance ASC
                """,
                (serialize_f32(query_vec), top_k),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            print(f"[RAG] Vector tables unavailable ({exc}); continuing without memory.")
            rows = []
    finally:
        db.close()

    memories = []
    for dist, agent_id, round_num, task, summary, key_findings_json, timestamp in rows:
        try:
            key_findings = json.loads(key_findings_json)
        except (TypeError, json.JSONDecodeError):
            key_findings = []
        memories.append(
            {
                "distance": round(float(dist), 4),
                "agent_id": agent_id,
                "round": round_num,
                "past_task": task,
                "summary": summary,
                "key_findings": key_findings,
                "timestamp": timestamp,
            }
        )

    write_context(memories)
    print(
        f"[RAG] Retrieved {len(memories)} relevant historical finding(s) "
        f"-> {OUT_PATH}. Process exiting to free RAM."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
