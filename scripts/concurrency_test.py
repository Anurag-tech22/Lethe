# 100 parallel reads and writes across isolated per-user FalkorDB graphs
# Tests: belief reads/writes, Handoff nodes (Step 2), Question nodes (Step 3)
# Prints p50 and p95 latency
import sys, time, statistics, concurrent.futures as cf, uuid
sys.path.insert(0, "backend"); from main import ensure, write_handoffs, write_question

def job(i):
    user = f"user{i % 10}"
    g = ensure(user)
    if g is None:
        return 0.0  # no DB available
    t = time.perf_counter()

    if i % 4 == 0:
        # Read: count beliefs
        g.query("MATCH (b:Belief) RETURN count(b)")
    elif i % 4 == 1:
        # Write: update a belief
        g.query("MATCH (b:Belief {id:'B1'}) SET b.touched=$i RETURN b.id", {"i": i})
    elif i % 4 == 2:
        # Write: handoff nodes (Step 2)
        bid = f"test_B{i}"
        write_handoffs(g, bid)
    else:
        # Write: question nodes (Step 3)
        write_question(g, user, f"Test question {i}?", ["B1"])

    return (time.perf_counter() - t) * 1000

if __name__ == "__main__":
    with cf.ThreadPoolExecutor(20) as ex:
        ms = sorted(ex.map(job, range(100)))
    ms = [x for x in ms if x > 0]  # filter out no-db results
    if not ms:
        print("No DB available — skipping concurrency test.")
        sys.exit(0)
    print(f"100 ops across 10 tenant graphs (beliefs + handoffs + questions):")
    print(f"  p50 = {statistics.median(ms):.1f} ms")
    print(f"  p95 = {ms[int(len(ms)*0.95)]:.1f} ms")
    print(f"  min = {min(ms):.1f} ms, max = {max(ms):.1f} ms")
