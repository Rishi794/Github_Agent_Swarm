"""Fan-in consolidator: debate transcripts -> persistent sqlite-vec knowledge base.

Runs in the downstream job after every agent has finished every debate round:

1. Reads all ``incoming_findings/round_<r>_<agent>.json`` payloads.
2. Embeds each finding with ``fastembed`` (one process, one pass).
3. Inserts metadata + ``float[dim]`` vectors into ``knowledge_base.db``
   (``findings_metadata`` table plus a ``vec_findings`` ``vec0`` virtual table).
4. Checkpoints the WAL and VACUUMs so the database is a single clean binary file
   that is safe to commit to the ``central-storage`` branch.
5. Renders a human-readable Markdown dashboard next to it.

Environment variables
---------------------
CONFIG_PATH    Path to model_config.json (default: external_config/model_config.json)
INCOMING_DIR   Directory holding round_*.json payloads (default: incoming_findings)
KB_DB_PATH     Database to create/update (default: knowledge_base.db)
REPORT_PATH    Markdown dashboard to write (default: README.md)
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec
from fastembed import TextEmbedding

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "external_config/model_config.json"))
INCOMING_DIR = Path(os.environ.get("INCOMING_DIR", "incoming_findings"))
KB_DB_PATH = Path(os.environ.get("KB_DB_PATH", "knowledge_base.db"))
REPORT_PATH = Path(os.environ.get("REPORT_PATH", "README.md"))

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384


def serialize_f32(vector: list[float]) -> bytes:
    """Pack a float list into the little-endian binary layout sqlite-vec expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def init_db(db: sqlite3.Connection, dim: int) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS findings_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            task TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            round_num INTEGER NOT NULL,
            model_used TEXT NOT NULL,
            summary TEXT NOT NULL,
            key_findings_json TEXT NOT NULL,
            critiques_json TEXT NOT NULL,
            risks_json TEXT NOT NULL,
            confidence_score REAL NOT NULL
        )
        """
    )
    db.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_findings USING vec0(
            finding_id INTEGER PRIMARY KEY,
            embedding float[{dim}]
        )
        """
    )
    try:
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_run_agent_round
                ON findings_metadata(run_id, agent_id, round_num)
            """
        )
    except sqlite3.OperationalError as exc:
        # Pre-existing duplicates from older runs: keep the data, skip the guard.
        print(f"[consolidate] Skipping unique index ({exc}).")


def load_records() -> list[dict]:
    files = sorted(glob.glob(str(INCOMING_DIR / "round_*.json")))
    records: list[dict] = []
    for filepath in files:
        try:
            payload = json.loads(Path(filepath).read_bytes())
        except json.JSONDecodeError as exc:
            print(f"[consolidate] Skipping malformed payload {filepath}: {exc}")
            continue
        payload["_source"] = filepath
        records.append(payload)

    records.sort(key=lambda item: (item.get("round_num", 0), item.get("agent_id", "")))
    return records


def embedding_text(record: dict) -> str:
    return (
        f"Task: {record.get('task', '')} | Agent: {record.get('agent_id', '')} "
        f"(Round {record.get('round_num', '?')}) | Summary: {record.get('summary', '')} | "
        f"Findings: {' '.join(record.get('key_findings', []) or [])}"
    )


def insert_records(
    db: sqlite3.Connection, records: list[dict], embeddings: list[list[float]]
) -> int:
    cursor = db.cursor()
    inserted = 0

    for record, vector in zip(records, embeddings):
        run_id = str(record.get("run_id", "unknown"))
        agent_id = str(record.get("agent_id", "unknown"))
        round_num = int(record.get("round_num", 0))

        already_stored = cursor.execute(
            """
            SELECT id FROM findings_metadata
            WHERE run_id = ? AND agent_id = ? AND round_num = ?
            """,
            (run_id, agent_id, round_num),
        ).fetchone()
        if already_stored:
            print(f"[consolidate] Already indexed: run {run_id} / {agent_id} / round {round_num}")
            continue

        cursor.execute(
            """
            INSERT INTO findings_metadata (
                run_id, timestamp, task, agent_id, round_num, model_used,
                summary, key_findings_json, critiques_json, risks_json, confidence_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                str(record.get("timestamp", "")),
                str(record.get("task", "")),
                agent_id,
                round_num,
                str(record.get("model_used", "unknown")),
                str(record.get("summary", "")),
                json.dumps(record.get("key_findings", []) or []),
                json.dumps(record.get("peer_critiques_and_rebuttals", []) or []),
                json.dumps(record.get("remaining_risks", []) or []),
                float(record.get("confidence_score", 0.0) or 0.0),
            ),
        )
        finding_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vec_findings(finding_id, embedding) VALUES (?, ?)",
            (finding_id, serialize_f32(vector)),
        )
        inserted += 1

    return inserted


def render_report(records: list[dict], total_rows: int, new_rows: int) -> str:
    now_iso = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Centralized Multi-Agent Vector Knowledge Base (`sqlite-vec`)",
        "",
        f"**Last Updated:** `{now_iso}` | **Indexed Vector Entries:** `{total_rows}` "
        f"| **Concept Vectors Added This Run:** `{new_rows}` | **Database File:** `knowledge_base.db`",
        "",
        "Query this store from any agent with:",
        "",
        "```sql",
        "SELECT m.summary, v.distance",
        "FROM vec_findings v JOIN findings_metadata m ON m.id = v.finding_id",
        "WHERE v.embedding MATCH :query_vector AND k = 4",
        "ORDER BY v.distance ASC;",
        "```",
        "",
        "## Latest Multi-Round Debate Transcript",
        "",
    ]

    for record in records:
        lines.append(
            f"### Round {record.get('round_num')} — Agent: `{record.get('agent_id')}` "
            f"(Confidence: `{record.get('confidence_score')}`)"
        )
        lines.append("")
        lines.append(f"**Model:** `{record.get('model_used')}`")
        lines.append("")
        lines.append(f"**Thesis Summary:** {record.get('summary', '')}")
        lines.append("")

        key_findings = record.get("key_findings", []) or []
        if key_findings:
            lines.append("**Key Findings:**")
            lines.extend(f"- {item}" for item in key_findings)
            lines.append("")

        critiques = record.get("peer_critiques_and_rebuttals", []) or []
        if critiques:
            lines.append("**Peer Critiques & Rebuttals:**")
            lines.extend(f"- {item}" for item in critiques)
            lines.append("")

        risks = record.get("remaining_risks", []) or []
        if risks:
            lines.append("**Remaining Risks:**")
            lines.extend(f"- {item}" for item in risks)
            lines.append("")

        lines.extend(["---", ""])

    return "\n".join(lines)


def main() -> int:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    else:
        print(f"[consolidate] Config not found at {CONFIG_PATH}; using built-in defaults.")
        config = {}

    embed_model_name = config.get("embedding_model", DEFAULT_EMBEDDING_MODEL)
    embed_dim = int(config.get("embedding_dim", DEFAULT_EMBEDDING_DIM))

    records = load_records()
    print(f"[consolidate] Loaded {len(records)} round payload(s) from {INCOMING_DIR}/.")

    KB_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(KB_DB_PATH))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)

    # WAL keeps the write burst fast; it is checkpointed into the main file below.
    db.execute("PRAGMA journal_mode = WAL;")
    init_db(db, embed_dim)

    embeddings: list[list[float]] = []
    if records:
        print(f"[consolidate] Embedding {len(records)} finding(s) with {embed_model_name} ...")
        embedder = TextEmbedding(model_name=embed_model_name)
        embeddings = [vector.tolist() for vector in embedder.embed([embedding_text(r) for r in records])]

        for vector in embeddings:
            if len(vector) != embed_dim:
                raise ValueError(
                    f"Embedding dimension mismatch: model returned {len(vector)} values but "
                    f"embedding_dim is {embed_dim}. Update model_config.json (and expect a new table)."
                )

    new_rows = insert_records(db, records, embeddings) if records else 0
    total_rows = db.execute("SELECT COUNT(*) FROM findings_metadata").fetchone()[0]

    db.commit()
    # Collapse WAL/SHM into one atomic file so Git stores a single clean binary blob.
    db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    db.execute("PRAGMA journal_mode = DELETE;")
    db.execute("VACUUM;")
    db.commit()
    db.close()

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(records, total_rows, new_rows), encoding="utf-8")

    size_mb = KB_DB_PATH.stat().st_size / (1024 * 1024) if KB_DB_PATH.exists() else 0.0
    print(
        f"[consolidate] Indexed {new_rows} new finding(s) into {KB_DB_PATH} "
        f"({size_mb:.2f} MB, {total_rows} total vectors). Report -> {REPORT_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
