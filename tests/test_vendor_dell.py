"""v0.3 draft tests: Dell iDRAC message IDs. Run: python -m unittest discover -s tests -v

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glyphrca.encode import Codebook, Encoder  # noqa: E402

CB = Codebook.load()
ENC = Encoder(CB)
T = "2026-10-13T14:00:01 "


class DellMessageIds(unittest.TestCase):
    def test_every_id_has_a_known_concept_and_a_source(self):
        self.assertGreaterEqual(len(CB.message_ids), 11)
        for mid, rule in CB.message_ids.items():
            with self.subTest(mid=mid):
                self.assertIn(rule["concept"], CB.concepts)
                self.assertTrue(rule.get("ref"))

    def test_id_decides_the_concept(self):
        cases = {
            "MEM0001 Multi-bit memory errors detected on a memory device at location(s) DIMM A2.": "MEM.ECC.UNCORR",
            "MEM0702 Correctable memory error rate exceeded for DIMM B1.": "MEM.ECC.CORR",
            "CPU0000 CPU 1 has an internal error (IERR).": "CPU.IERR.CATERR",
            "CPU0704 CPU 2 machine check error detected.": "CPU.MCE.LOGGED",
            "PSU0003 The power input for power supply 2 is lost.": "PSU.INPUT.LOST",
            "RDU0012 Lost PSU redundancy.": "PSU.REDUNDANCY.LOST",
            "FAN0001 Fan 3 RPM is less than the lower critical threshold.": "FAN.FAIL",
            "TMP0104 The system board Inlet Temp temperature is outside of range.": "THERMAL.OUT_OF_RANGE",
            "VLT0204 The system board PS1 PG FAIL voltage is outside of range.": "VOLT.OUT_OF_RANGE",
            "NIC100 The NIC Slot 4 Port 1 network link is down.": "NET.LINK.DOWN",
            "PDR16 Predictive failure reported for Disk 0 in Backplane 1.": "DISK.PREDICTIVE_FAIL",
        }
        for line, concept in cases.items():
            with self.subTest(line=line):
                e = ENC.encode(T + line)
                self.assertEqual(e.concept, concept)
                self.assertEqual(e.args[0], "msg=" + line.split()[0])
                self.assertIsNotNone(e.ts)

    def test_dell_pcie_fatal_text_without_an_id(self):
        e = ENC.encode(T + "A bus fatal error was detected on a component at slot 5.")
        self.assertEqual(e.concept, "PCIE.FATAL")
        self.assertIn("slot=5", e.args)
        e = ENC.encode(T + "A fatal error was detected on a component at bus 2 device 0 function 0.")
        self.assertEqual(e.concept, "PCIE.FATAL")
        self.assertIn("pci=bus2.dev0.fn0", e.args)

    def test_unknown_id_falls_back_to_words(self):
        e = ENC.encode(T + "XYZ9999 PCIe correctable error on device 0000:41:00.0")
        self.assertEqual(e.concept, "PCIE.CORR")


if __name__ == "__main__":
    unittest.main()
