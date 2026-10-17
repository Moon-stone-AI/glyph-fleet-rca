"""v0.3 tests: HPE ProLiant IML message texts. Run: python -m unittest discover -s tests -v

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


class HpeIml(unittest.TestCase):
    def test_every_template_is_sourced(self):
        self.assertGreaterEqual(len(CB.templates), 25)
        for _, t in CB.templates:
            with self.subTest(t=t["hpe"]):
                self.assertRegex(t["hpe"], r"^[0-9A-F]{4}/[0-9A-F]{4}$")
                self.assertIn(t["concept"], CB.concepts)

    def test_iml_text_reaches_the_concept(self):
        cases = {
            "Uncorrectable Memory Error: The failed memory module could not be determined.": "MEM.ECC.UNCORR",
            "DIMM failure - Uncorrectable memory error - Processor 1 Dimm 4.": "MEM.ECC.UNCORR",
            "Fan failure (Fan 3, Location System)": "FAN.FAIL",
            "System power supplies are not redundant": "PSU.REDUNDANCY.LOST",
            "Automatic Server Recovery (ASR) NMI Detected: The ASR NMI has been signaled.": "OS.HANG.RESET",
            "Network adapter link down on slot 2, port 1.": "NET.LINK.DOWN",
            "Uncorrectable machine check exception (Processor 1, APIC ID 0x00, Bank 0x05, Status 0xBE000000'00800400).":
                "CPU.IERR.CATERR",
        }
        for line, concept in cases.items():
            with self.subTest(line=line):
                self.assertEqual(ENC.encode(T + line).concept, concept)

    def test_pcie_hex_address_becomes_bdf(self):
        e = ENC.encode(T + "Uncorrectable PCI Express Error Detected. Slot 3 (Segment 0x00, Bus 0x41, Device 0x00, "
                           "Function 0x0). Uncorrectable Error Status: 0x00004000")
        self.assertEqual(e.concept, "PCIE.UNCORR.NONFATAL")
        self.assertIn("bdf=0000:41:00.0", e.args)
        self.assertEqual(e.args[0], "hpe=0008/0002")


if __name__ == "__main__":
    unittest.main()
