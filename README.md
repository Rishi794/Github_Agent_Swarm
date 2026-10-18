# `central-storage` — Persistent Multi-Agent Knowledge

This is an **orphan branch**: it contains no application code, only the artifacts
produced by the swarm's fan-in (consolidation) jobs. Keeping it separate means the
`main` branch's history stays free of automated data commits.

| Path | Written by | Contents |
| --- | --- | --- |
| `knowledge_base.db` | `Multi-Agent Debate Swarm` | SQLite database with `findings_metadata` + a `sqlite-vec` `vec0` vector table (`float[384]`) |
| `agentic_results.md` | `Multi-Agent Debate Swarm` | Human-readable multi-round debate transcript, including every tool call each agent dispatched |
| `README.md` | `Multi-Agent Debate Swarm` | This file (dashboard + query instructions) |
| `knowledge_base.json` | `Multi-Agent Swarm (Ollama)` | Flat JSON knowledge store for the lightweight single-pass workflow |
| `SWARM_REPORT.md` | `Multi-Agent Swarm (Ollama)` | Findings report for the lightweight workflow |

## Reading the latest results

```
https://raw.githubusercontent.com/Rishi794/Github_Agent_Swarm/central-storage/agentic_results.md
https://raw.githubusercontent.com/Rishi794/Github_Agent_Swarm/central-storage/README.md
https://raw.githubusercontent.com/Rishi794/Github_Agent_Swarm/central-storage/knowledge_base.json
```

## Querying the vector database locally

```bash
pip install sqlite-vec
curl -O https://raw.githubusercontent.com/Rishi794/Github_Agent_Swarm/central-storage/knowledge_base.db
```

```python
import sqlite3, struct, sqlite_vec
db = sqlite3.connect("knowledge_base.db")
db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False)

query_vector = struct.pack("384f", *[0.0] * 384)   # 384-dim embedding of your question
for agent, rnd, summary, dist in db.execute(
    "SELECT m.agent_id, m.round_num, m.summary, v.distance "
    "FROM vec_findings v JOIN findings_metadata m ON m.id = v.finding_id "
    "WHERE v.embedding MATCH ? AND k = 4 ORDER BY v.distance ASC", (query_vector,)):
    print(f"[{dist:.4f}] {agent} round {rnd}: {summary[:100]}")
```

> The `sqlite-vec` extension must be loaded before touching `vec_findings`; plain
> SQLite clients cannot read virtual vector tables without it.

Every push to this branch is performed by a single concurrency-guarded writer job,
so parallel agents never collide here.
