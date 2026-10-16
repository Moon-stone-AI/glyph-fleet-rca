"""Command line.

  python -m glyphrca synth   OUT_DIR [--bad N --ok N --flood LO HI --seed S]
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
    a.add_argument("--tier2-key")
    a.add_argument("--public", default="public_report.json")
    a.add_argument("--private", default="private_codebook.json")
    args = p.parse_args(argv)

    if args.cmd == "synth":
        truth = generate(args.out, bad=args.bad, ok=args.ok, flood=tuple(args.flood), seed=args.seed)
        print(json.dumps(truth, indent=1))
        return 0
    r = analyse_fleet(args.fleet)
    print(render_text(r))
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
