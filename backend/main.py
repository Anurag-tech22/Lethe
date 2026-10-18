import json, os, re, time, uuid, urllib.request
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

# ── DB connection (Docker, embedded, or None) ─────────────────────────
db = None
if os.getenv("FALKOR_HOST"):
    from falkordb import FalkorDB
    try:
        db = FalkorDB(host=os.getenv("FALKOR_HOST"), port=int(os.getenv("FALKOR_PORT", 6379)))
    except Exception:
        pass
else:
    try:
        from redislite.falkordb_client import FalkorDB as Embedded
        db = Embedded(os.getenv("LETHE_DB", os.path.join(os.path.dirname(__file__), "lethe.db")))
    except Exception:
        pass

app = FastAPI(title="Lethe")

# ── Seed data ─────────────────────────────────────────────────────────
SEED = """CREATE
 (s1:Source {id:'old_doc', name:'Old API doc (2024)', trust:0.3}),
 (s2:Source {id:'slack', name:'Slack message', trust:0.5}),
 (s3:Source {id:'changelog', name:'Official changelog', trust:0.9}),
 (:Belief {id:'B1', text:'API limit is 100 requests/min', status:'active'}),
 (:Belief {id:'B2', text:'Batch size must stay under 100', status:'active'}),
 (:Belief {id:'B3', text:'Nightly sync needs about 2 hours', status:'active'}),
 (:Belief {id:'B4', text:'Do not run bulk import on Friday', status:'active'}),
 (:Belief {id:'B5', text:'API limit is 500 requests/min', status:'active'}),
 (:Belief {id:'B6', text:'Nightly sync starts at 2 AM', status:'active'})"""
LINKS = [
    "MATCH (s:Source {id:'old_doc'}),(b:Belief {id:'B1'}) CREATE (s)-[:STATED]->(b)",
    "MATCH (s:Source {id:'changelog'}),(b:Belief {id:'B5'}) CREATE (s)-[:STATED]->(b)",
    "MATCH (s:Source {id:'slack'}),(b:Belief {id:'B6'}) CREATE (s)-[:STATED]->(b)",
    "MATCH (a:Belief {id:'B2'}),(b:Belief {id:'B1'}) CREATE (a)-[:DERIVED_FROM]->(b)",
    "MATCH (a:Belief {id:'B3'}),(b:Belief {id:'B2'}) CREATE (a)-[:DERIVED_FROM]->(b)",
    "MATCH (a:Belief {id:'B4'}),(b:Belief {id:'B3'}) CREATE (a)-[:DERIVED_FROM]->(b)",
    "MATCH (a:Belief {id:'B5'}),(b:Belief {id:'B1'}) CREATE (a)-[:CONTRADICTS]->(b)",
]

# ── Agent names for handoff pipeline ──────────────────────────────────
AGENTS = ["Researcher", "Auditor", "Advisor"]

# ── Per-session state (episodic memory, Step 3) ───────────────────────
_sessions: dict[str, str] = {}  # user -> session_id

def _session_id(user: str) -> str:
    if user not in _sessions:
        _sessions[user] = str(uuid.uuid4())[:8]
    return _sessions[user]

# ── Graph helpers ─────────────────────────────────────────────────────
def graph(user):
    if db is None:
        return None
    return db.select_graph("shared_base" if user == "shared" else f"mem_{user}")

def ensure(user="shared"):
    g = graph(user)
    if g is None:
        return None
    try:
        empty = g.query("MATCH (n) RETURN count(n)").result_set[0][0] == 0
    except Exception:
        empty = True
    if empty:
        g.query(SEED)
        for q in LINKS:
            g.query(q)
    return g

# ── Handoff writer (Step 2) ───────────────────────────────────────────
def write_handoffs(g, belief_id: str) -> list[dict]:
    """Write the Researcher -> Auditor -> Advisor handoff chain for a belief."""
    if g is None:
        return []
    handoffs = []
    for i in range(len(AGENTS) - 1):
        ts = int(time.time() * 1000) + i  # unique timestamp per hop
        hid = f"H_{belief_id}_{i}"
        g.query(
            "CREATE (:Handoff {id:$hid, from_agent:$fr, to_agent:$to, ts:$ts, belief_id:$bid})",
            {"hid": hid, "fr": AGENTS[i], "to": AGENTS[i + 1], "ts": ts, "bid": belief_id},
        )
        handoffs.append({"id": hid, "from": AGENTS[i], "to": AGENTS[i + 1], "ts": ts, "belief_id": belief_id})
    return handoffs

# ── Episodic memory writer (Step 3) ──────────────────────────────────
def write_question(g, user: str, question: str, belief_ids: list[str]) -> None:
    """Write (:Session)-[:ASKED]->(:Question)-[:FOUND]->(:Belief) chain."""
    if g is None:
        return
    sid = _session_id(user)
    # Ensure session node exists
    g.query("MERGE (:Session {id:$sid})", {"sid": sid})
    qid = f"Q_{sid}_{int(time.time()*1000)}"
    ts = int(time.time() * 1000)
    g.query(
        "MATCH (s:Session {id:$sid}) CREATE (s)-[:ASKED]->(:Question {id:$qid, text:$text, ts:$ts})",
        {"sid": sid, "qid": qid, "text": question, "ts": ts},
    )
    for bid in belief_ids:
        g.query(
            "MATCH (q:Question {id:$qid}), (b:Belief {id:$bid}) CREATE (q)-[:FOUND]->(b)",
            {"qid": qid, "bid": bid},
        )

# ── Procedural memory (Step 4) ───────────────────────────────────────
def check_procedural(g, source_id: str) -> dict | None:
    """When 2+ retractions involve the same Source, create a Playbook rule."""
    if g is None:
        return None
    # Count how many stale/retracted beliefs this source has
    res = g.query(
        "MATCH (s:Source {id:$sid})-[:STATED]->(b:Belief) WHERE b.status IN ['stale','retracted'] RETURN count(b)",
        {"sid": source_id},
    )
    count = res.result_set[0][0] if res.result_set else 0
    if count < 2:
        return None

    # Check if playbook already exists for this source
    existing = g.query(
        "MATCH (p:Playbook)-[:APPLIES_TO]->(s:Source {id:$sid}) RETURN p.rule",
        {"sid": source_id},
    )
    if existing.result_set:
        return None  # already created

    # Get source name and current trust
    src_info = g.query(
        "MATCH (s:Source {id:$sid}) RETURN s.name, s.trust",
        {"sid": source_id},
    )
    if not src_info.result_set:
        return None
    src_name, old_trust = src_info.result_set[0]
    new_trust = max(0.1, round(old_trust - 0.1, 2))

    rule = f"{src_name} was retracted {count} times, trust dropped from {old_trust} to {new_trust}"
    ts = int(time.time() * 1000)
    g.query(
        "MATCH (s:Source {id:$sid}) SET s.trust=$nt",
        {"sid": source_id, "nt": new_trust},
    )
    g.query(
        "MATCH (s:Source {id:$sid}) CREATE (:Playbook {rule:$rule, created_at:$ts})-[:APPLIES_TO]->(s)",
        {"sid": source_id, "rule": rule, "ts": ts},
    )
    return {"rule": rule, "source": source_id, "source_name": src_name, "old_trust": old_trust, "new_trust": new_trust}

# ── Text matching helpers ─────────────────────────────────────────────
STOP = set("the are what can and how for why you does this that with run is".split())
words = lambda t: set(re.findall(r"[a-z]{3,}", t.lower())) - STOP
nums = lambda t: "".join(re.findall(r"\d+", t))

# ── Gemini extraction (Step 6) ────────────────────────────────────────
def gemini_extract(paragraph: str) -> list[str] | None:
    """Use Gemini to extract a list of short beliefs from a paragraph. Returns None without a key."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    prompt = (
        "Extract a JSON list of short factual beliefs from this text. "
        "Each belief should be one sentence. Return ONLY a JSON array of strings.\n\n"
        f"Text: {paragraph}"
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{os.getenv('GEMINI_MODEL','gemini-2.0-flash')}:generateContent?key={key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode()
    try:
        res = json.load(urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"})))
        beliefs = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
        if isinstance(beliefs, list):
            return [str(b).strip() for b in beliefs if str(b).strip()]
    except Exception:
        pass
    return None

# ── API routes ────────────────────────────────────────────────────────

@app.get("/api/state")
def state(user: str = "shared"):
    g = ensure(user)
    if g is None:
        return {"nodes": [], "edges": [], "stale": {}, "error": "no_db"}
    rows = g.query("MATCH (n) WHERE n:Source OR n:Belief RETURN labels(n)[0], n.id, n.name, n.text, n.trust, n.status").result_set
    nodes = [{"id": i, "k": "s" if l == "Source" else "b", "name": n, "text": t, "trust": tr} for l, i, n, t, tr, st in rows]
    edges = [[a, t, b] for a, t, b in g.query("MATCH (a)-[r]->(b) WHERE (a:Source OR a:Belief) AND (b:Source OR b:Belief) RETURN a.id, type(r), b.id").result_set]

    # Belief counts for header
    active = sum(1 for r in rows if r[0] == "Belief" and r[5] == "active")
    stale_count = sum(1 for r in rows if r[0] == "Belief" and r[5] in ("stale", "retracted"))

    # Handoffs (Step 2)
    try:
        handoff_rows = g.query("MATCH (h:Handoff) RETURN h.id, h.from_agent, h.to_agent, h.ts, h.belief_id ORDER BY h.ts DESC LIMIT 50").result_set
        handoffs = [{"id": r[0], "from": r[1], "to": r[2], "ts": r[3], "belief_id": r[4]} for r in handoff_rows]
    except Exception:
        handoffs = []

    # Playbooks (Step 4)
    try:
        pb_rows = g.query("MATCH (p:Playbook)-[:APPLIES_TO]->(s:Source) RETURN p.rule, s.id, s.name, p.created_at").result_set
        playbooks = [{"rule": r[0], "source": r[1], "source_name": r[2], "created_at": r[3]} for r in pb_rows]
    except Exception:
        playbooks = []

    return {
        "nodes": nodes,
        "edges": edges,
        "stale": {r[1]: r[5] for r in rows if r[5] in ("stale", "retracted")},
        "active_count": active,
        "stale_count": stale_count,
        "handoffs": handoffs,
        "playbooks": playbooks,
    }

@app.post("/api/retract/{sid}")
def retract(sid: str, user: str = "shared"):
    g = ensure(user)
    if g is None:
        return {"stale": [], "error": "no_db"}
    g.query("MATCH (s:Source {id:$sid}) SET s.status='retracted'", {"sid": sid})
    r = g.query(
        """MATCH (s:Source {id:$sid})-[:STATED]->(b:Belief)<-[:DERIVED_FROM*0..5]-(d:Belief)
           WITH DISTINCT d SET d.status='stale', d.valid_to=timestamp() RETURN d.id""",
        {"sid": sid},
    )
    stale_ids = [x[0] for x in r.result_set]

    # Write handoffs for the retraction event (Step 2)
    handoffs = write_handoffs(g, f"retract_{sid}")

    # Check procedural memory trigger (Step 4)
    playbook = check_procedural(g, sid)

    return {"stale": stale_ids, "handoffs": handoffs, "playbook": playbook}

@app.post("/api/reset")
def reset(user: str = "shared"):
    g = graph(user)
    if g is None:
        return {"ok": True}
    try:
        g.delete()
    except Exception:
        pass
    ensure(user)
    # Reset session for this user
    _sessions.pop(user, None)
    return {"ok": True}

@app.get("/api/ask")
def ask(q: str, user: str = "shared"):
    g = ensure(user)
    key = os.getenv("GEMINI_API_KEY")

    used_ids = []
    answer_text = None

    if key and g is not None:
        act = g.query("MATCH (b:Belief) WHERE b.status='active' RETURN b.id, b.text").result_set
        prompt = f"Answer using ONLY these active beliefs: {act}. Question: {q}. Reply JSON {{\"answer\": str, \"used\": [ids]}}. If they do not cover it, say so."
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{os.getenv('GEMINI_MODEL','gemini-2.0-flash')}:generateContent?key={key}"
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}}).encode()
        try:
            res = json.load(urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"})))
            data = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
            answer_text = data.get("answer")
            used_ids = data.get("used", [])
        except Exception:
            pass

    # Graph-native TMS reasoning fallback if Gemini is not configured or didn't answer
    if not answer_text and g is not None:
        try:
            act_rows = g.query("MATCH (b:Belief) WHERE b.status='active' RETURN b.id, b.text").result_set
            stale_rows = g.query("MATCH (b:Belief) WHERE b.status<>'active' RETURN b.id, b.text").result_set
            stop_words = {"the","are","what","can","and","how","for","why","you","does","this","that","with","run","is","a","an","to","in","on","of"}
            q_words = set(re.findall(r'[a-zA-Z]{3,}', q.lower())) - stop_words

            best_active = None
            best_score = 0
            for bid, btext in act_rows:
                sc = sum(1 for w in q_words if w in btext.lower())
                if sc > best_score:
                    best_score = sc
                    best_active = (bid, btext)

            if best_active and best_score > 0:
                used_ids = [best_active[0]]
                # Trace provenance from FalkorDB
                sources = g.query(
                    "MATCH (b:Belief {id:$bid})<-[:DERIVED_FROM*0..5]-(dep)<-[:STATED]-(s:Source) RETURN DISTINCT s.name, s.trust",
                    {"bid": best_active[0]}
                ).result_set
                src_str = ", ".join(f"{s[0]} ({int(float(s[1])*100)}% trust)" for s in sources) if sources else "verified source"
                answer_text = f"{best_active[1]}. (Grounding: {src_str})"
            else:
                for bid, btext in stale_rows:
                    if any(w in btext.lower() for w in q_words):
                        answer_text = f"I have no active belief that answers this safely. Notice: premise '{btext}' was revoked and marked stale."
                        used_ids = [bid]
                        break
        except Exception:
            pass

    # Write episodic memory (Step 3)
    if g is not None:
        write_question(g, user, q, used_ids)

    # Write handoffs (Step 2) for the ask operation
    handoffs = []
    if g is not None and used_ids:
        handoffs = write_handoffs(g, used_ids[0] if used_ids else f"ask_{int(time.time()*1000)}")

    if not answer_text:
        return {"answer": "I have no active belief that answers this question in the current knowledge graph.", "handoffs": handoffs}
    return {"answer": answer_text, "used": used_ids, "handoffs": handoffs}

@app.post("/api/learn")
def learn(body: dict, user: str = "shared"):
    g = ensure(user)
    if g is None:
        return {"id": None, "conflicts": [], "error": "no_db"}

    text, src, frm = body["text"].strip(), body["source"], body.get("derived_from")
    is_paragraph = body.get("paragraph", False)

    all_results = []

    # Step 6: Gemini extraction — if paragraph mode and key is set
    texts_to_learn = []
    if is_paragraph:
        extracted = gemini_extract(text)
        if extracted:
            texts_to_learn = extracted
        else:
            texts_to_learn = [text]  # fallback to single belief
    else:
        texts_to_learn = [text]

    for belief_text in texts_to_learn:
        bid = "B%d" % (g.query("MATCH (b:Belief) RETURN count(b)").result_set[0][0] + 1)
        g.query(
            "MATCH (s:Source {id:$s}) CREATE (s)-[:STATED]->(:Belief {id:$i, text:$t, status:'active', valid_from:timestamp()})",
            {"s": src, "i": bid, "t": belief_text},
        )
        if frm:
            g.query(
                "MATCH (a:Belief {id:$a}),(b:Belief {id:$b}) CREATE (a)-[:DERIVED_FROM]->(b)",
                {"a": bid, "b": frm},
            )
        conflicts = []
        for oid, ot in g.query(
            "MATCH (b:Belief) WHERE b.status='active' AND b.id<>$i RETURN b.id, b.text",
            {"i": bid},
        ).result_set:
            if len(words(belief_text) & words(ot)) >= 2 and nums(belief_text) and nums(ot) and nums(belief_text) != nums(ot):
                g.query(
                    "MATCH (a:Belief {id:$a}),(b:Belief {id:$b}) CREATE (a)-[:CONTRADICTS]->(b)",
                    {"a": bid, "b": oid},
                )
                conflicts.append(oid)

        # Write handoffs (Step 2)
        handoffs = write_handoffs(g, bid)

        all_results.append({"id": bid, "conflicts": conflicts, "handoffs": handoffs})

    if len(all_results) == 1:
        return all_results[0]
    return {"beliefs": all_results, "extracted_count": len(all_results)}

@app.get("/api/history")
def history(user: str = "shared"):
    """Step 3: Return the last 10 questions for the current session."""
    g = ensure(user)
    if g is None:
        return {"questions": []}
    sid = _session_id(user)
    try:
        rows = g.query(
            """MATCH (s:Session {id:$sid})-[:ASKED]->(q:Question)
               OPTIONAL MATCH (q)-[:FOUND]->(b:Belief)
               RETURN q.id, q.text, q.ts, collect(b.id)
               ORDER BY q.ts DESC LIMIT 10""",
            {"sid": sid},
        ).result_set
        return {
            "questions": [
                {"id": r[0], "text": r[1], "ts": r[2], "belief_ids": [x for x in r[3] if x]}
                for r in rows
            ]
        }
    except Exception:
        return {"questions": []}

@app.get("/api/users")
def list_users():
    """Step 5: Return known users based on existing graphs."""
    if db is None:
        return {"users": ["shared", "alice", "bob"]}
    base = ["shared", "alice", "bob"]
    try:
        graphs = db.list_graphs()
        for gname in graphs:
            if gname.startswith("mem_"):
                uname = gname[4:]
                if uname not in base:
                    base.append(uname)
    except Exception:
        pass
    return {"users": base}

@app.post("/api/users/add")
def add_user(body: dict):
    """Step 5: Create a new user graph seeded with base beliefs."""
    name = body.get("name", "").strip().lower()
    if not name or not re.match(r"^[a-z0-9_]{1,20}$", name):
        return {"error": "Invalid name (lowercase letters, numbers, underscores, max 20 chars)"}
    ensure(name)
    return {"ok": True, "user": name}

app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "frontend"), html=True))
