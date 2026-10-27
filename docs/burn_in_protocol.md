# GPU burn-in protocol: inputs for the health record

Status: draft. Nothing here is implemented in glyphrca yet. Thresholds are not set.

## Purpose

A short, controlled test on each used card that produces the four signals the health record needs and that the current v0.1 analysis does not measure: power under load, PCIe link, ECC and retired memory, and sustained clocks and temperature.

## Procedure

1. **Baseline (idle, 5 min).** Record the card's identity (UUID, serial, board), driver version, power limit, and the PCIe link at idle.
2. **Sample during the run.** Poll once per second with `nvidia-smi --query-gpu=... --format=csv -l 1` and keep the full log.
3. **Load (30–60 min).** Run one sustained load tool, the same one on every card. Choose it once and record it in the output (for example gpu-burn or a DCGM diagnostic level; the choice is open).
4. **Cool-down (5 min).** Record the same fields again. Compare ECC and retired page counters with the baseline.
5. **Collect Xid lines** from the kernel log for the whole window.

## Signals

| Signal | Source | Field to verify | What it tells | Threshold |
|---|---|---|---|---|
| Power under load | nvidia-smi | `power.draw`, `power.limit` | Draw vs. limit; a card pinned at the limit at low throughput is suspect | TBD |
| PCIe link | nvidia-smi | `pcie.link.width.current` vs `.max`, `pcie.link.gen.current` vs `.max` | Degraded link (for example x8 instead of x16) | Expected at full load: max width and gen |
| ECC errors | nvidia-smi | `ecc.errors.corrected.volatile.total` (delta over the run), `ecc.errors.uncorrected.volatile.total` | Corrected errors rising under load; any uncorrected error is a fail flag | Uncorrected > 0 fails; corrected rate TBD |
| Retired memory | nvidia-smi | retired pages and row-remap fields for the driver version | Pages retired during the test or already retired before it | TBD |
| Sustained clocks | nvidia-smi | `clocks.sm` after warm-up | Steady-state clock vs. the card's maximum | TBD |
| Temperature | nvidia-smi | `temperature.gpu` | Steady-state temperature under load | TBD |
| Throttling | nvidia-smi | `clocks_throttle_reasons.active` | Why the clock dropped (thermal, power, other) | Record, do not fail on its own |
| Xid events | kernel log | lines containing `NVRM: Xid` | Driver-reported GPU faults during the run | Any Xid is recorded; severity mapping TBD |

Field names differ between driver versions. Before the first run, check each one with `nvidia-smi --help-query-gpu` on the target machine and fix the table.

## Output (one record per card)

```json
{
  "gpu_uuid": "GPU-...",
  "serial": "...",
  "driver": "...",
  "load_tool": "...",
  "duration_s": 3600,
  "warmup_s": 300,
  "signals": {
    "power_draw_w_p50": null,
    "pcie_width_max_under_load": null,
    "pcie_gen_max_under_load": null,
    "ecc_corrected_delta": null,
    "ecc_uncorrected_delta": null,
    "retired_pages": null,
    "sm_clock_mhz_steady": null,
    "temp_c_steady": null,
    "throttle_reasons": [],
    "xid_count": null
  },
  "flags": []
}
```

## Open items

- Set thresholds with a known-good card and a known-bad card. Without that, every threshold is a guess.
- Choose one load tool and keep it fixed, so records are comparable.
- Decide whether `pcie.txt` (already part of v0.1) should take its values from this test.
- Check the public repository version before publishing: the README there describes DCGM inputs that this local v0.1 copy does not contain.
- Ask the operators in the conversation (Viktor and others) for their burn-in checklists and check our list against them.
