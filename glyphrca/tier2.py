"""Tier 2: the same fleet report with opaque tokens, plus a private codebook to read it back.

An outside analyst (vendor support, a contractor, an ML pipeline) can still see the structure:
which signature is a storm, which one is rare, which attribute value correlates with the crash.
They cannot see what the events are, which hosts, slots or firmware versions are involved.
Tokens are HMAC-SHA256 of the clear value under a secret key, so they are stable across runs
with the same key and useless without it.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import hashlib
import hmac


class Tokenizer:
    def __init__(self, key: str | bytes):
        self.key = key.encode() if isinstance(key, str) else key
        self.private: dict[str, str] = {}

    def tok(self, value: str, kind: str = "G") -> str:
        d = hmac.new(self.key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:8].upper()
        t = f"{kind}_{d[:4]}_{d[4:]}"
        self.private[t] = value
        return t


def public_report(report: dict, key: str) -> tuple[dict, dict]:
    """Returns (public_report, private_codebook). Counts, sizes and rates stay in clear."""
    T = Tokenizer(key)
    g = lambda c: c if c == "SILENCE" else T.tok(c, "G")   # silence carries no secret
    h = lambda x: T.tok(x, "H")
    v = lambda x: T.tok(x, "V")
    pub = {
        "levels": report["levels"],
        "signatures": [{"concepts": [g(c) for c in s["concepts"]], "servers": s["servers"],
                        "attributes": {T.tok(a, "A"): {v(k): n for k, n in vals.items()}
                                       for a, vals in s["attributes"].items()}}
                       for s in report["signatures"]],
        "survivors": [{"host": h(s["host"]), "signature": [g(c) for c in s["signature"]]}
                      for s in report["survivors"]],
        "inventory": [{"attribute": T.tok(f["attribute"], "A"), "value": v(f["value"]),
                       "crash_rate": f["crash_rate"], "other_rate": f["other_rate"], "servers": f["servers"]}
                      for f in report["inventory"]],
        "crash_spread_s": report["crash_spread_s"],
        "pcie": {k: {T.tok(p, "P"): len(hosts) for p, hosts in d.items()} for k, d in report["pcie"].items()},
    }
    return pub, dict(sorted(T.private.items()))
