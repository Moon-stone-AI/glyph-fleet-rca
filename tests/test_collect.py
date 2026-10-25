"""v0.5.1: glyph-collect.sh redaction, run end to end with stub tools on PATH.

The stubs stand in for hostname, journalctl, ipmitool and nvidia-smi so the script runs anywhere
with bash and GNU sed. Checks: vendor BMC exports passed with --bmc are copied and redacted;
IPv6, IPv4, MAC, serials and Dell service tags are masked; PCIe addresses, clock times, GPU UUIDs
and vendor message IDs survive, so ingest still reads the archive.
"""
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "glyph-collect.sh"
HAVE_BASH = shutil.which("bash") is not None and shutil.which("sed") is not None

STUBS = {
    "hostname": """#!/bin/sh
case "$1" in -f) echo gpu-node-17.dc1.example.com ;; *) echo gpu-node-17 ;; esac
""",
    "journalctl": """#!/bin/sh
cat <<'EOF'
2026-10-19T10:23:45+0000 gpu-node-17 kernel: NVRM: Xid (PCI:0000:1b:00): 79, pid=0, GPU has fallen off the bus.
2026-10-19T10:23:46+0000 gpu-node-17 kernel: pcieport 0000:17:01.0: AER: Uncorrected (Fatal) error received: 0000:1b:00.0
2026-10-19T10:24:00+0000 gpu-node-17 kernel: mlx5_core: link up 10.20.30.40 fe80::1ff:fe23:4567:890a%ib0
2026-10-19T10:24:01+0000 gpu-node-17 kernel: std::vector trace at 0xdead from module foo::bar
EOF
""",
    "ipmitool": """#!/bin/sh
cat <<'EOF'
   1 | 10/19/2026 | 10:23:44 | Power Supply PSU1 | Power Supply AC lost | Asserted
   2 | 10/19/2026 | 10:23:50 | System Board | BMC address 2001:db8:85a3::8a2e:370:7334 | Asserted
EOF
""",
    "nvidia-smi": """#!/bin/sh
case "$*" in *driver_version*) echo 550.90.07; exit 0 ;; esac
cat <<'EOF'
==============NVSMI LOG==============
Driver Version                            : 550.90.07
Attached GPUs                             : 1
GPU 00000000:1B:00.0
    Product Name                          : NVIDIA H100 80GB HBM3
    Serial Number                         : 1652322012345
    GPU UUID                              : GPU-3f2a9c1e-0000-4000-8000-00000000abcd
    Board Part Number                     : 692-2G520-0200-000
EOF
""",
}

BMC_DELL = """<Event AgentID="iDRAC" Severity="3" s="" Category="System Health">
<SerialNumber>CN0ABC12345678</SerialNumber>
<ServiceTag>7XK2Q93</ServiceTag>
<MessageID>PSU0003</MessageID><Message>The power input for power supply 1 is lost.</Message>
<FQDD>iDRAC.Embedded.1 at 192.168.5.20 / 2001:0db8:85a3:0000:0000:8a2e:0370:7334 mac 3c:fd:fe:aa:bb:cc</FQDD>
</Event>
"""
BMC_TXT = """Service Tag: 7XK2Q93
System host gpu-node-17.dc1.example.com loopback ::1 mapped ::ffff:192.168.1.10
FQXSPPU0001M Power supply 1 failed at 10:23:44
"""

SECRETS = ["gpu-node-17", "1652322012345", "692-2G520", "CN0ABC12345678", "7XK2Q93",
           "10.20.30.40", "192.168.5.20", "192.168.1.10", "3c:fd:fe:aa:bb:cc",
           "fe80::1ff", "2001:db8:85a3::8a2e", "2001:0db8:85a3:0000", "::1 ", "::ffff"]
KEPT = ["0000:1b:00", "0000:17:01.0", "0000:1b:00.0", "10:23:45", "10:23:44", "Xid", ": 79",
        "GPU-3f2a9c1e-0000-4000-8000-00000000abcd", "00000000:1B:00.0", "std::vector", "foo::bar",
        "PSU0003", "FQXSPPU0001M", "Power Supply AC lost"]


def _run(tmp_path: Path, *args: str) -> dict[str, str]:
    stub = tmp_path / "bin"
    stub.mkdir()
    for name, body in STUBS.items():
        (stub / name).write_text(body)
        (stub / name).chmod(0o755)
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "lclog.xml").write_text(BMC_DELL)
    (exports / "xcc-events.txt").write_text(BMC_TXT)
    work = tmp_path / "run"
    work.mkdir()
    env = dict(os.environ, PATH=f"{stub}:/usr/bin:/bin")
    r = subprocess.run(["bash", str(SCRIPT), "--bmc", str(exports), *args], cwd=work, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    (archive,) = list(work.glob("glyph-collect-*.tar.gz"))
    files = {}
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            if m.isfile():
                files[m.name] = t.extractfile(m).read().decode()
    # the originals in the export folder are never changed
    assert (exports / "lclog.xml").read_text() == BMC_DELL
    return files


@unittest.skipUnless(HAVE_BASH, "needs bash and GNU sed")
class Collector(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bmc_exports_are_copied_and_redacted(self):
        tmp_path = self.tmp
        files = _run(tmp_path)
        names = {Path(n).relative_to(Path(n).parts[0]).as_posix() for n in files}
        assert {"bmc/lclog.xml", "bmc/xcc-events.txt", "bmc/README.txt", "kernel.log",
                "sel.txt", "nvidia-smi-q.txt", "meta.txt"} <= names
        text = "\n".join(v for k, v in files.items() if not k.endswith("README.txt"))
        for s in SECRETS:
            assert s not in text, f"not redacted: {s}"
        for k in KEPT:
            assert k in text, f"redaction damaged: {k}"
        assert "<SerialNumber>REDACTED</SerialNumber>" in text
        assert "<ServiceTag>REDACTED</ServiceTag>" in text
        assert "x:x:x:x" in text and "x.x.x.x" in text and "xx:xx:xx:xx:xx:xx" in text

    def test_no_redact_keeps_everything(self):
        tmp_path = self.tmp
        files = _run(tmp_path, "--no-redact")
        text = "\n".join(files.values())
        assert "7XK2Q93" in text and "fe80::1ff:fe23:4567:890a" in text and "gpu-node-17" in text

    def test_bmc_folder_must_exist(self):
        tmp_path = self.tmp
        r = subprocess.run(["bash", str(SCRIPT), "--bmc", str(tmp_path / "missing")],
                           capture_output=True, text=True, cwd=tmp_path)
        assert r.returncode == 2 and "--bmc needs a folder" in r.stderr
        assert not list(tmp_path.glob("glyph-collect-*.tar.gz"))

    def test_ingest_reads_redacted_archive(self):
        tmp_path = self.tmp
        from glyphrca.ingest import ingest
        _run(tmp_path)
        (archive,) = list((tmp_path / "run").glob("glyph-collect-*.tar.gz"))
        out = tmp_path / "fleet"
        summaries = ingest([str(archive)], str(out))
        (host,) = [s["host"] for s in summaries]
        assert host.startswith("node-")
        sel = (out / host / "sel.log").read_text()
        assert "PSU0003" in sel and "FQXSPPU0001M" in sel and "AC lost" in sel


if __name__ == "__main__":
    unittest.main()
