# Agents Need Memory They Can Audit and Undo

When an AI agent tells you "don't run bulk import on Friday," do you know why it believes that? Can you trace the belief to its source, check whether that source is still trustworthy, and undo the belief if the source turns out to be wrong?

Most agent memory systems treat knowledge as a flat key-value store or a vector embedding. Once something goes in, it's there forever — or until you wipe everything. That's a problem. **Memory poisoning and stale facts are the silent killers of agent reliability.**

## The Problem: Stale Facts Cascade

Consider a real scenario: an old API doc says "rate limit is 100 req/min." An agent learns this. Then it derives "batch size must stay under 100" from that belief. Then "nightly sync needs 2 hours" from the batch size. Then "don't run bulk import on Friday" from the sync time. Four beliefs, one source.

Six months later, the official changelog says the rate limit is actually 500 req/min. The old doc was wrong. But the agent still believes all four downstream facts. Worse, it doesn't know *why* it believes them, so it can't fix itself.

This is not hypothetical. Every production agent that learns from documents, Slack messages, wikis, and changelogs faces this problem. The question is whether you can fix it surgically — or whether you have to nuke all memory and start over.

## The Graph Design: Beliefs, Sources, and Trust

Lethe models agent memory as a directed graph in FalkorDB with three core node types:

- **Sources** with trust scores (0.0–1.0): documents, messages, changelogs
- **Beliefs** with status (active/stale): facts the agent uses to answer questions
- **Edges**: `STATED` (source → belief), `DERIVED_FROM` (belief → belief), `CONTRADICTS` (belief ↔ belief)

The key insight: when you retract a source, FalkorDB's Cypher traversal walks the `DERIVED_FROM` chain up to 5 hops deep and marks every downstream belief as stale. One query. No manual cleanup. The agent immediately stops using those beliefs in its answers.

```cypher
MATCH (s:Source {id:$sid})-[:STATED]->(b:Belief)<-[:DERIVED_FROM*0..5]-(d:Belief)
WITH DISTINCT d SET d.status='stale', d.valid_to=timestamp()
RETURN d.id
```

## Multi-Agent Coordination Through the Graph

Lethe runs three agents in a pipeline: **Researcher** extracts beliefs, **Auditor** checks trust and contradictions, **Advisor** answers questions. Every operation writes `(:Handoff)` nodes into the same FalkorDB graph, creating an auditable trail of which agent touched which belief and when.

This isn't just logging — it's shared state. The Auditor can see what the Researcher extracted. The Advisor knows what the Auditor flagged. And the timeline is visible in the UI, so a human can trace exactly what happened.

## Procedural Memory: Learning From Mistakes

When the same source gets retracted twice, Lethe creates a `(:Playbook)` rule and automatically lowers that source's trust score. The agent learns *procedures* from its own experience: "Old API doc has been wrong twice, so trust it less going forward."

This is procedural memory — the agent doesn't just store facts, it develops heuristics about which sources to trust. New answers immediately reflect the updated trust scores.

## Per-Tenant Isolation

Each user gets a completely isolated FalkorDB graph (`mem_alice`, `mem_bob`, etc.). Alice can retract a source without affecting Bob's beliefs. This is multi-tenant agent memory built on FalkorDB's graph selection — one line of code, total isolation.

## Results

The concurrency test runs 100 parallel operations (reads, writes, handoffs, and question tracking) across 10 tenant graphs simultaneously. On a local Docker setup, we see sub-millisecond p50 latency and single-digit p95. FalkorDB handles the concurrent graph mutations without breaking a sweat.

The cascade retraction — walking 5 hops of `DERIVED_FROM` edges and marking everything stale — completes in a single Cypher query. No application-level loops, no race conditions, no partial updates.

## The Takeaway

Agent memory isn't just about storing facts. It's about **provenance** (where did this come from?), **trust** (how reliable is the source?), **retraction** (what if the source is wrong?), and **auditability** (who believed what, and when?).

A graph database is the natural fit for this. Beliefs form a DAG. Sources have trust. Retractions cascade through edges. And every agent handoff is a node you can query later.

If your agent can't explain *why* it believes something — and you can't undo that belief when the source turns out to be wrong — your agent memory system is a liability, not an asset.

---

*Built with [FalkorDB](https://www.falkordb.com/), FastAPI, and a single-file frontend. Submitted to the FalkorDB "Graph Hacks: Context for AI Agents" hackathon, Track 2: Agent Memory and Coordination.*
