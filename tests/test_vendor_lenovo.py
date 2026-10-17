"""v0.3 tests: Lenovo XCC / UEFI event IDs. Run: python -m unittest discover -s tests -v

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


class LenovoEventIds(unittest.TestCase):
    def test_every_lenovo_id_is_sourced(self):
        ids = [k for k in CB.message_ids if k.startswith("FQX")]
        self.assertGreaterEqual(len(ids), 45)
        for k in ids:
            with self.subTest(k=k):
                r = CB.message_ids[k]
                self.assertIn(r["concept"], CB.concepts)
                self.assertIn(r["lenovo_severity"], ("Error", "Warning", "Info"))
                self.assertTrue(r["text"])

    def test_ids_decide_the_concept(self):
        cases = {
            "FQXSPIO0013N: A Fatal Bus Error has occurred on bus PCIe Slot 3.": "PCIE.FATAL",
            "FQXSFMA0002M: An uncorrectable memory error has been detected on DIMM 5 at address 0x1f.": "MEM.ECC.UNCORR",
            "FQXSFPU0018N: CATERR(IERR) has asserted on processor 1.": "CPU.IERR.CATERR",
            "FQXSPPW0110M: Power redundancy is lost with insufficient remaining power.": "PSU.REDUNDANCY.LOST",
            "FQXSPCA0041N: Liquid is leaking from closed loop Loop1.": "COOLING.LEAK",
            "FQXSFIO0022J: PCIe Link Width has degraded from x16 to x8 in physical slot number 3.": "LINK.DEGRADED",
        }
        for line, concept in cases.items():
            with self.subTest(line=line):
                e = ENC.encode(T + line)
                self.assertEqual(e.concept, concept)
                self.assertEqual(e.args[0], "msg=" + line.split(":")[0])

    def test_unmapped_lenovo_id_is_decoded(self):
        e = ENC.encode(T + "FQXSPMA0130N: Memory PMIC Group1 has transitioned to non-recoverable.")
        self.assertIn("component=MA", e.args)
        self.assertIn("vendor_severity=fatal", e.args)

    def test_uefi_hex_address_becomes_bdf(self):
        e = ENC.encode(T + "FQXSFIO0035M: An Uncorrectable PCIe Error has Occurred at Segment 0x0 Bus 0x41 "
                           "Device 0x00 Function 0x0. The Vendor ID for the device is 0x10de and the Device ID is 0x2330.")
        self.assertEqual(e.concept, "PCIE.UNCORR.NONFATAL")
        self.assertIn("bdf=0000:41:00.0", e.args)


if __name__ == "__main__":
    unittest.main()
