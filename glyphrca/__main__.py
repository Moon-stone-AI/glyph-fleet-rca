"""Command line.

  python -m glyphrca synth   OUT_DIR [--bad N --ok N --flood LO HI --seed S]
  python -m glyphrca patterns FLEET_DIR --bucket NAME [--out drafts.yaml] [--library fault_library.yaml]
  python -m glyphrca analyze FLEET_DIR [--json report.json]
                             [--tier2-key KEY --public public.json --private private_codebook.json]

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import argparse
import json
import sys

from .report import analyse_fleet, render_text
from .synth import generate
from .tier2 import public_report


def main(argv=None):
    p = argparse.ArgumentParser(prog="glyphrca", description="GlyphAI fleet root-cause analysis")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth", help="write a synthetic fleet")
    s.add_argument("out")
    s.add_argument("--bad", type=int, default=500)
    s.add_argument("--ok", type=int, default=200)
    s.add_argument("--flood", type=int, nargs=2, default=(30, 80))
    s.add_argument("--seed", type=int, default=42)
    a = sub.add_parser("analyze", help="analyse a fleet directory")
    a.add_argument("fleet")
    a.add_argument("--json")
    a.add_argument("--health-csv", help="write one row per GPU with its health index")
    a.add_argument("--buckets-csv", help="write the work queues: one row per faulty node, by symptom bucket")
    a.add_argument("--tier2-key")
    a.add_argument("--public", default="public_report.json")
    a.add_argument("--private", default="private_codebook.json")
    a.add_argument("--library", help="reviewed fault library (default: codebook/fault_library.yaml)")
    pt = sub.add_parser("patterns", help="mine the failure pattern of one work queue into a draft fault record")
    pt.add_argument("fleet")
    pt.add_argument("--bucket", required=True, help="queue name, e.g. gpu_memory (see the work-queue report)")
    pt.add_argument("--out", default="draft_records.yaml")
    args = p.parse_args(argv)

    if args.cmd == "synth":
        truth = generate(args.out, bad=args.bad, ok=args.ok, flood=tuple(args.flood), seed=args.seed)
        print(json.dumps(truth, indent=1))
        return 0
    if args.cmd == "patterns":
        import datetime
        import yaml
        from .encode import Codebook
        from .patterns import draft_record, mine
        keep: dict = {}
        r = analyse_fleet(args.fleet, keep=keep)
        q = next((q for q in r["buckets"] if q["bucket"] == args.bucket), None)
        if q is None:
            print(f"No queue named {args.bucket}. Queues: {', '.join(x['bucket'] for x in r['buckets'])}")
            return 1
        from .buckets import load_buckets
        from .patterns import split_queue
        cb = Codebook.load()
        prefixes = next((b.get("match", []) for b in load_buckets() if b["name"] == args.bucket), [])
        groups = split_queue(q, prefixes) if prefixes else [[n["host"] for n in q["nodes"]]]
        recs = []
        print(f"Queue '{q['label']}': {len(q['nodes'])} nodes, {len(groups)} pattern(s)")
        for i, hosts in enumerate(groups, 1):
            if len(hosts) < 2 and len(groups) > 1:
                continue  # one node is an anecdote, not a pattern
            pat = mine(keep["events"], hosts, cb)
            recs.append(draft_record(q, pat, cb, datetime.date.today().isoformat(), index=len(recs) + 1))
            print(f"  pattern {len(recs)}: {pat['nodes']} nodes, " + (" -> ".join(pat["order"]) or "no common sequence"))
        with open(args.out, "w") as f:
            yaml.safe_dump(recs, f, sort_keys=False, allow_unicode=True, width=110)
        print(f"{len(recs)} draft record(s) -> {args.out}  (status: draft; review, set fault_code and the fix,"
              " then add to the fault library)")
        return 0
    r = analyse_fleet(args.fleet, library_path=args.library)
    print(render_text(r))
    if args.health_csv and r.get("gpu_health"):
        from .health import write_csv
        write_csv(r["gpu_health"], args.health_csv)
        print(f"\nGPU health cards -> {args.health_csv}")
    if args.buckets_csv and r.get("buckets"):
        from .buckets import write_csv as write_buckets
        write_buckets(r["buckets"], args.buckets_csv)
        print(f"Work queues -> {args.buckets_csv}")
    if args.json:
        json.dump(r, open(args.json, "w"), indent=1)
    if args.tier2_key:
        pub, priv = public_report(r, args.tier2_key)
        json.dump(pub, open(args.public, "w"), indent=1)
        json.dump(priv, open(args.private, "w"), indent=1)
        print(f"\nTier 2: public report -> {args.public}; private codebook -> {args.private} (keep it inside)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
