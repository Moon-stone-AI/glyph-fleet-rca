"""PCIe tree as a map for the logs.

Input per server (one line per device):
    <BDF> <parent BDF or -> <kind> <slot label> x<width>/x<max width> gen<gen>/gen<max gen>
    0000:41:00.0 0000:40:01.0 gpu GPU3 x16/x16 gen5/gen5

Bus numbers differ from server to server; slot paths (RP0/SW1/GPU3) do not. The topology glyph is the
set of slot paths with their device kinds, BDF-free. The fleet's most common shape is the golden tree.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .encode import Event

LINE = re.compile(r"^(\S+)\s+(\S+)\s+(\w+)\s+(\S+)\s+x(\d+)/x(\d+)\s+gen(\d+)/gen(\d+)\s*$", re.I)


@dataclass(frozen=True)
class Node:
    bdf: str
    parent: str | None
    kind: str
    slot: str
    width: int
    max_width: int
    gen: int
    max_gen: int


def parse_tree(path) -> dict[str, Node]:
    nodes = {}
    with open(path) as f:
        for line in f:
            m = LINE.match(line.strip())
            if not m:
                continue
            bdf, parent, kind, slot, w, mw, g, mg = m.groups()
            nodes[bdf.lower()] = Node(bdf.lower(), None if parent == "-" else parent.lower(), kind.lower(),
                                      slot, int(w), int(mw), int(g), int(mg))
    return nodes


def slot_path(tree: dict[str, Node], bdf: str) -> str | None:
    parts, cur, seen = [], tree.get(bdf), set()
    while cur is not None and cur.bdf not in seen:
        seen.add(cur.bdf)
        parts.append(cur.slot)
        cur = tree.get(cur.parent) if cur.parent else None
    return "/".join(reversed(parts)) if parts else None


def shape(tree: dict[str, Node]) -> frozenset[tuple[str, str]]:
    return frozenset((slot_path(tree, b), n.kind) for b, n in tree.items())


@dataclass
class TopologyFindings:
    golden_size: int
    missing: dict[str, list[str]]       # slot path -> hosts where the device is gone
    degraded: dict[str, list[tuple[str, str]]]  # slot path -> [(host, "x8/x16 gen5/gen5")]
    upstream_suspects: dict[str, list[str]]     # switch path -> hosts with errors on >=2 children
    device_suspects: dict[str, list[str]]       # device path -> hosts with errors on that device only


PCIE_CONCEPTS = {"PCIE.CORR", "PCIE.FATAL", "PCIE.LINK.DOWN"}


def analyse(trees: dict[str, dict[str, Node]], events_by_host: dict[str, list[Event]],
            exclude: set[str] = frozenset()) -> TopologyFindings:
    """`exclude`: hosts whose PCIe errors are already explained by a common-mode crash;
    their storm noise must not be blamed on a switch or a GPU."""
    shapes = Counter(shape(t) for t in trees.values())
    golden = shapes.most_common(1)[0][0] if shapes else frozenset()
    missing, degraded = defaultdict(list), defaultdict(list)
    for h, t in trees.items():
        present = shape(t)
        for p, kind in golden - present:
            missing[p].append(h)
        for b, n in t.items():
            if n.width < n.max_width or n.gen < n.max_gen:
                degraded[slot_path(t, b)].append((h, f"x{n.width}/x{n.max_width} gen{n.gen}/gen{n.max_gen}"))

    upstream, device = defaultdict(list), defaultdict(list)
    for h, events in events_by_host.items():
        t = trees.get(h)
        if not t or h in exclude:
            continue
        by_parent: dict[str, set[str]] = defaultdict(set)
        for e in events:
            if e.concept not in PCIE_CONCEPTS:
                continue
            for a in e.args:
                if a.startswith("bdf=") and a[4:] in t:
                    node = t[a[4:]]
                    parent = slot_path(t, node.parent) if node.parent else "ROOT"
                    by_parent[parent].add(slot_path(t, node.bdf))
        for parent, children in by_parent.items():
            if len(children) >= 2:
                upstream[parent].append(h)
            else:
                device[next(iter(children))].append(h)
    return TopologyFindings(len(golden), dict(missing), dict(degraded), dict(upstream), dict(device))
