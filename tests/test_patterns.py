"""v0.4 tests: failure patterns, draft records and the reviewed fault library.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glyphrca.buckets import load_buckets  # noqa: E402
from glyphrca.encode import Codebook  # noqa: E402
from glyphrca.patterns import IDENTIFYING, draft_record, mine, split_queue  # noqa: E402
from glyphrca.report import analyse_fleet  # noqa: E402
from glyphrca.synth import generate  # noqa: E402


class Patterns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.truth = generate(cls.dir, bad=300, ok=150, flood=(10, 20), seed=13)
        cls.keep = {}
        cls.r = analyse_fleet(cls.dir, keep=cls.keep)
        cls.cb = Codebook.load()
        cls.q = next(q for q in cls.r["buckets"] if q["bucket"] == "gpu_memory")
        prefixes = next(b["match"] for b in load_buckets() if b["name"] == "gpu_memory")
        cls.groups = split_queue(cls.q, prefixes)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_lemon_pattern_is_mined_in_order(self):
        lemons = next(g for g in self.groups if set(g) == set(self.truth["lemons"]))
        pat = mine(self.keep["events"], lemons, self.cb)
        self.assertEqual(pat["order"][:2], ["GPU.MEM.ECC.DBE", "GPU.MEM.REMAP.EVENT"])  # 48, then its remap

    def test_draft_is_anonymous_and_a_draft(self):
        for g in self.groups:
            rec = draft_record(self.q, mine(self.keep["events"], g, self.cb), self.cb, "2026-10-17")
            self.assertEqual(rec["review"]["status"], "draft")
            self.assertEqual(rec["confidence"], "hypothesis")
            self.assertFalse(IDENTIFYING.findall(yaml.safe_dump(rec)))
            self.assertRegex(rec["fault_code"], r"^(GPU|LINK|HOST|FABRIC|FAC|SW|FLEET)(\.[A-Z0-9_]+){1,3}$")

    def test_reviewed_record_labels_matching_nodes_and_drafts_do_not(self):
        lemons = next(g for g in self.groups if set(g) == set(self.truth["lemons"]))
        rec = draft_record(self.q, mine(self.keep["events"], lemons, self.cb), self.cb, "2026-10-17")
        lib = Path(self.dir) / "lib.yaml"
        lib.write_text(yaml.safe_dump([rec]))
        r = analyse_fleet(self.dir, library_path=lib)
        self.assertFalse(any(n.get("known") for q in r["buckets"] for n in q["nodes"]))  # still a draft
        rec.update(fault_code="GPU.MEM.DBE_REMAP", practitioner_notes="Drain, reset when idle, RMA if Xid 48 repeats",
                   review={"status": "practitioner_reviewed", "reviewer": "V. de Mille", "date": "2026-10-17"})
        lib.write_text(yaml.safe_dump([rec]))
        r = analyse_fleet(self.dir, library_path=lib)
        labelled = sorted(n["host"] for q in r["buckets"] for n in q["nodes"] if n.get("known"))
        self.assertEqual(labelled, sorted(self.truth["lemons"]))
        self.assertTrue(all(n["known"]["fix"].startswith("Drain") for q in r["buckets"] for n in q["nodes"]
                            if n.get("known")))


if __name__ == "__main__":
    unittest.main()
