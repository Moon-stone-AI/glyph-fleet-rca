"""Levels 3 and 4: runs with counters per server, signatures across the fleet.

L3  consecutive identical concepts -> one run: concept x count, first/last time, argument histogram.
L4  servers with the same run sequence -> one fleet signature x number of servers.
Rare signatures are the 'survivors': the few servers that recorded something the storm did not.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .encode import Codebook, Event, readable

SILENCE = "SILENCE"


@dataclass
class Run:
    concept: str
    count: int
    first: float | None
    last: float | None
    args: Counter = field(default_factory=Counter)
    sample: str = ""  # one raw line kept verbatim (the first of the run)

    @property
    def rate_per_s(self) -> float | None:
        if self.first is None or self.last is None or self.last <= self.first:
            return None
        return self.count / (self.last - self.first)


def to_runs(events: list[Event]) -> list[Run]:
    runs: list[Run] = []
    for e in events:
        if runs and runs[-1].concept == e.concept:
            r = runs[-1]
            r.count += 1
            r.last = e.ts if e.ts is not None else r.last
            r.args.update(e.args)
        else:
            runs.append(Run(e.concept, 1, e.ts, e.ts, Counter(e.args), e.raw))
    last_t = runs[-1].last if runs else None
    runs.append(Run(SILENCE, 1, last_t, None))  # the log ends: silence is an event too
    return runs


def signature(runs: list[Run], cb: Codebook) -> tuple[str, ...]:
    """Concepts that matter, in order of first appearance. Info-level events are dropped.
    Order of first appearance (not run order) keeps the signature stable when two sources
    (SEL and kernel) interleave the same story line by line."""
    seen, out = set(), []
    for r in runs:
        if cb.severity(r.concept) == "info" or r.concept in seen:
            continue
        seen.add(r.concept)
        out.append(r.concept)
    if SILENCE in seen:  # silence always closes the story
        out.remove(SILENCE)
        out.append(SILENCE)
    return tuple(out)


@dataclass
class FleetView:
    runs: dict[str, list[Run]]
    groups: dict[tuple[str, ...], list[str]]
    last_words: dict[str, str]

    def ranked(self):
        return sorted(self.groups.items(), key=lambda kv: -len(kv[1]))

    def rare(self, max_size: int | None = None) -> list[tuple[tuple[str, ...], list[str]]]:
        """Signatures shared by very few servers, next to a big group (a storm)."""
        n = sum(len(h) for h in self.groups.values())
        limit = max_size if max_size is not None else max(3, n // 1000)
        biggest = max(len(h) for h in self.groups.values())
        return [(s, h) for s, h in self.groups.items() if len(h) <= limit and biggest > 10 * len(h)]

    def survivors(self, storm_min: int = 50):
        """Rare signatures that contain a storm's whole story and then say something more:
        the servers that lived a moment longer than the storm and recorded why."""
        storms = [s[:-1] for s, h in self.groups.items() if len(h) >= storm_min and len(s) > 1]
        out = []
        for s, hosts in self.rare():
            body = s[:-1]
            for st in storms:
                it = iter(body)
                if len(body) > len(st) and all(c in it for c in st):  # storm is a subsequence of body
                    out.append((s, hosts))
                    break
        return out


def build_fleet(events_by_host: dict[str, list[Event]], cb: Codebook) -> FleetView:
    runs = {h: to_runs(ev) for h, ev in events_by_host.items()}
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for h, rs in runs.items():
        groups[signature(rs, cb)].append(h)
    def last(ev):  # the last thing that mattered, not the last routine line
        for e in reversed(ev):
            if cb.severity(e.concept) != "info":
                return readable(e)
        return readable(ev[-1]) if ev else ""
    last_words = {h: last(ev) for h, ev in events_by_host.items()}
    return FleetView(runs, dict(groups), last_words)


def crashed(fv: FleetView, cb: Codebook) -> set[str]:
    """Servers that died: they recorded a fatal concept (CATERR, panic, thermal trip, ...)."""
    return {h for h, rs in fv.runs.items() if any(cb.fatal(r.concept) for r in rs)}


def simultaneity(fv: FleetView, cb: Codebook, hosts: set[str]) -> float | None:
    """Spread (seconds) of the last critical event across the crashed cohort.
    A small spread over thousands of servers points to a common cause."""
    times = []
    for h in hosts:
        crit = [r.last for r in fv.runs[h] if cb.fatal(r.concept) and r.last is not None]
        if crit:
            times.append(min(crit))
    if len(times) < 2:
        return None
    times.sort()
    lo, hi = times[int(0.05 * len(times))], times[int(0.95 * len(times)) - 1]
    return hi - lo


def software_wide(events_by_host: dict[str, list[Event]], cb: Codebook, exclude: set[str],
                  window_s: float = 120.0, min_hosts: int | None = None):
    """Concepts whose cause can be application OR hardware (e.g. Xid 13, 31, 43).
    Many servers hitting the same one inside a short window points to software (a job, a driver,
    a library), not to the GPUs. Returns [(concept, hosts_in_window, window_start, window_end, others)]."""
    n = len(events_by_host)
    need = min_hosts if min_hosts is not None else max(5, n // 200)
    first: dict[str, dict[str, float]] = defaultdict(dict)
    for h, ev in events_by_host.items():
        if h in exclude:
            continue
        for e in ev:
            if cb.cause(e.concept) in ("app_or_hw", "driver_or_hw") and e.ts is not None:
                first[e.concept].setdefault(h, e.ts)
    out = []
    for concept, hosts in first.items():
        items = sorted(hosts.items(), key=lambda kv: kv[1])
        best, j = (0, 0, 0), 0
        for i in range(len(items)):
            while items[i][1] - items[j][1] > window_s:
                j += 1
            if i - j + 1 > best[0]:
                best = (i - j + 1, j, i)
        k, j, i = best
        if k >= need:
            inside = [h for h, _ in items[j:i + 1]]
            out.append((concept, inside, items[j][1], items[i][1], [h for h, _ in items if h not in set(inside)]))
    return out


def escalations(events_by_host: dict[str, list[Event]], cb: Codebook, window_s: float = 86400.0):
    """A light triage repeated too often on one GPU means degrading hardware.
    For each escalation rule, count the concept per server and GPU inside a sliding 24 h window.
    Returns [(host, bdf, concept, peak_count, rule_to)]."""
    rules = {r["concept"]: r for r in cb.escalation}
    out = []
    for h, ev in events_by_host.items():
        times: dict[tuple[str, str], list[float]] = defaultdict(list)
        for e in ev:
            if e.concept in rules and e.ts is not None:
                bdf = next((a[4:] for a in e.args if a.startswith("bdf=")), "?")
                times[(e.concept, bdf)].append(e.ts)
        for (concept, bdf), ts in times.items():
            ts.sort()
            peak, j = 0, 0
            for i in range(len(ts)):
                while ts[i] - ts[j] > window_s:
                    j += 1
                peak = max(peak, i - j + 1)
            if peak > rules[concept]["max_per_day"]:
                out.append((h, bdf, concept, peak, rules[concept]["to"]))
    return sorted(out)


def _bdf(e: Event) -> str:
    return next((a[4:] for a in e.args if a.startswith("bdf=")), "?")


def zombie_gpus(events_by_host: dict[str, list[Event]], window_s: float = 1800.0):
    """A GPU lost from the bus or hit by an uncontained ECC error, followed on the same server by a
    task blocked in the kernel (state D): soft restarts will hang. Returns {host: (trigger, gpu)}."""
    out = {}
    for h, ev in events_by_host.items():
        trig = [e for e in ev if e.concept in ("GPU.BUS.LOST", "GPU.MEM.ECC.UNCONTAINED") and e.ts is not None]
        hung = [e.ts for e in ev if e.concept == "OS.HUNG_TASK" and e.ts is not None]
        for t in trig:
            if any(0 <= x - t.ts <= window_s for x in hung):
                out[h] = (t.concept, _bdf(t))
                break
    return out


def orphan_cleanups(events_by_host: dict[str, list[Event]], window_s: float = 600.0):
    """Xid 45 (driver cleanup) with no earlier GPU error on the same GPU within the window:
    the root cause was probably lost from the ring buffer. Returns [(host, gpu)]."""
    out = []
    for h, ev in events_by_host.items():
        for i, e in enumerate(ev):
            if e.concept != "GPU.DRIVER.CLEANUP" or e.ts is None:
                continue
            b = _bdf(e)
            prior = any(p.concept.startswith(("GPU.", "LINK.")) and p.concept != "GPU.DRIVER.CLEANUP"
                        and _bdf(p) == b and p.ts is not None and 0 <= e.ts - p.ts <= window_s for p in ev[:i])
            if not prior:
                out.append((h, b))
    return sorted(set(out))


def aer_storms(events_by_host: dict[str, list[Event]], cb: Codebook):
    """Servers flooding the kernel log with PCIe AER messages: the host can lock up writing them,
    and with it any agent that should fix it. Returns {host: peak messages per window}."""
    cfg = cb.aer_storm or {"per_window": 1000, "window_s": 60}
    out = {}
    for h, ev in events_by_host.items():
        ts = [e.ts for e in ev if e.source == "kernel" and e.concept.startswith("PCIE.") and e.ts is not None]
        peak, j = 0, 0
        for i in range(len(ts)):
            while ts[i] - ts[j] > cfg["window_s"]:
                j += 1
            peak = max(peak, i - j + 1)
        if peak >= cfg["per_window"]:
            out[h] = peak
    return out


def lemon_nodes(fv: "FleetView", cb: Codebook, exclude: set[str], min_kinds: int = 3):
    """Servers with recurrent failures across several hardware categories (FLEET.LEMON_NODE).
    Returns {host: [hardware concepts]} for hosts outside a common-mode crash."""
    out = {}
    for h, rs in fv.runs.items():
        if h in exclude:
            continue
        kinds = sorted({r.concept for r in rs if cb.cause(r.concept) == "hardware"})
        if len(kinds) >= min_kinds:
            out[h] = kinds
    return out


def load_inventory(path: str) -> dict[str, dict[str, str]]:
    with open(path, newline="") as f:
        return {row["host"]: {k: v for k, v in row.items() if k != "host"} for row in csv.DictReader(f)}


def inventory_join(inv: dict[str, dict[str, str]], cohort: set[str], min_rate=0.9, max_other=0.1):
    """For every inventory attribute, find a value that (almost) all crashed servers share and
    (almost) no healthy server has. Returns [(attribute, value, crash_rate, other_rate, n)]."""
    findings = []
    attrs = {k for row in inv.values() for k in row}
    for a in sorted(attrs):
        total, bad = Counter(), Counter()
        for h, row in inv.items():
            v = row.get(a, "")
            total[v] += 1
            bad[v] += h in cohort
        for v, n in total.items():
            rate = bad[v] / n
            others_bad = sum(bad[x] for x in total if x != v)
            others_n = sum(total[x] for x in total if x != v)
            other_rate = others_bad / others_n if others_n else 0.0
            covers = bad[v] / max(1, len(cohort))
            if rate >= min_rate and other_rate <= max_other and covers >= min_rate:
                findings.append((a, v, rate, other_rate, n))
    return findings
