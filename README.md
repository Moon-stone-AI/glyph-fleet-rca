# glyph-fleet-rca

**Find the one server that knows why 5,000 servers died.**

When a whole fleet fails at once, the servers that died instantly could not write down why. Their logs are a storm of identical errors. The cause is usually in the log of the one machine that lived a second longer, and finding it by hand can take weeks.

`glyph-fleet-rca` applies GlyphAI semantic compression (monograph on Zenodo: [ZENODO LINK]) to server event logs. It turns raw lines into concept glyphs, collapses storms into counters, and compares whole servers by their glyph signatures. The survivor stands alone.

> Synthetic data only. No real operator logs are included or were used.

## How it works: four levels

| Level | What happens | Example |
|---|---|---|
| **L1** word → word glyph | Vendor vocabulary is normalised; times, record ids and hex are split off | `DIMM`, `Memory Module`, `mem` → `MEM` |
| **L2** phrase → concept glyph + arguments | One meaning per line, whatever the vendor or format | three vendor phrasings and a binary record → `MEM.ECC.CORR` `dimm=A2` |
| **L3** run → concept × count | Identical concepts in a row become one run with a counter, first/last time and argument histogram | `MEM.ECC.CORR ×499 in 51 s, mostly DIMM A2` |
| **L4** fleet → signatures × servers | Servers with the same story collapse into one signature | `4,999 × … > CPU.IERR.CATERR > SILENCE` |

Two roads lead to the same glyph:
- **binary IPMI SEL records** (16 bytes) are decoded by sensor type, sensor number and event offset, following Intel's *SEL Troubleshooting Guide*, Rev. 3.8 (tables cited in the codebook);
- **text logs** in any dialect go through the lexicon (L1) and phrase rules (L2).

Words the codebook does not know are not guessed: they surface as data gaps (`?word`) so the codebook can be extended.

## What it finds

On a synthetic fleet of 6,000 servers (4.0 million SEL lines, four dialects including raw binary):

```
Level                                   bytes      ratio
L0 raw text                       355,280,198         1x
   gzip baseline (no meaning)      20,728,784        17x
L1 word glyphs + args              81,990,837         4x
L2 concept + args                  71,501,917         5x
L3 runs: concept x count            1,688,439       210x
L4 fleet signatures                    12,734    27,900x

Fleet signatures:
    4999 x  MEM.ECC.CORR > PCIE.CORR > NET.LINK.DOWN > CPU.IERR.CATERR > SILENCE   fw {'3.2.1': 4999}
     981 x  SILENCE   fw {'3.1.9': 981}
      13 x  PCIE.CORR > SILENCE   fw {'3.1.9': 13}
       6 x  PCIE.LINK.DOWN > SILENCE   fw {'3.1.9': 6}
       1 x  MEM.ECC.CORR > PCIE.CORR > NET.LINK.DOWN > CPU.IERR.CATERR > OS.KERNEL.PANIC > SILENCE   fw {'3.2.1': 1}

Survivors (rare signatures next to a storm):
  node-04480: ... > CPU.IERR.CATERR > OS.KERNEL.PANIC > SILENCE

Crashed servers: 5,000; 90% of them failed within 1.0 s  -> common-mode failure
Inventory: fw=3.2.1 -> 100% crashed vs 0% elsewhere. Suspect this value, not the parts.

PCIe tree:
  device missing                                             RP1/SW1/GPU5          6 servers
  link degraded                                              RP0/SW0/GPU1          3 servers
  errors on >=2 devices behind one parent -> suspect parent  RP1/SW1               8 servers
  errors on one device only -> suspect device                RP0/SW0/GPU2          5 servers
```

Analysed in about 95 seconds on one CPU core, pure Python, no tuning.

What this shows, honestly:
- **L1–L2 give meaning, not size.** gzip compresses better at those levels. What gzip cannot do is see that three vendors and a binary record say the same thing.
- **L3–L4 are lossy on purpose.** Per-line times are replaced by first/last time and counters. The raw last words of every server are kept verbatim, and raw logs should stay in your own storage.
- **Silence is an event.** Every log ends with `SILENCE`. Thousands of servers going silent in the same second is evidence of a common cause.
- **The vendor guide is written for one server.** For CATERR it says "update firmware"; for IERR it says "cross test the processors". Applied server by server, that means swapping thousands of CPUs. Seen as a fleet, it is one firmware rollback.

### PCIe tree as a map
Bus numbers differ between servers; slot paths (`RP1/SW1/GPU5`) do not. The most common tree shape is the golden tree. Errors carrying a BDF are mapped onto it:
- errors on two or more devices behind the same switch, on several servers → **suspect the switch or riser**, not the GPUs;
- errors on one device only → suspect that device;
- devices missing from the tree, or links trained below their maximum width or generation, are listed by slot path.

PCIe noise from servers already explained by a common-mode crash is excluded, so a storm cannot frame an innocent switch.

### Tier 2: share the analysis, not the secrets
With `--tier2-key`, the report is also written with opaque tokens (HMAC-SHA256). A vendor, contractor or ML pipeline can see the structure: which signature is a storm, which is rare, which attribute value correlates with the crash. They cannot see which events, hosts, slots or firmware versions are involved. The private codebook that reads the tokens back stays inside.

## Quick start

Python 3.10+ and PyYAML.

```bash
python -m glyphrca synth fleet_demo --bad 500 --ok 200      # writes a synthetic fleet, prints ground truth
python -m glyphrca analyze fleet_demo                        # the report
python -m glyphrca analyze fleet_demo --json report.json \
       --tier2-key "your-secret" --public public.json --private private_codebook.json
python -m unittest discover -s tests -v                      # 14 tests
```

### Input layout
```
<fleet>/<host>/sel.log     raw SEL: binary hex records and/or text, any dialect, mixed is fine
<fleet>/<host>/pcie.txt    optional: <BDF> <parent BDF|-> <kind> <slot> x<w>/x<max> gen<g>/gen<max>
<fleet>/inventory.csv      optional: host,fw,bios,...
```

## Repository

```
codebook/sel_v0.1.yaml   the codebook: concepts, binary SEL decoding, lexicon, phrase rules
glyphrca/encode.py       L1, L2 (binary and text roads, template cache)
glyphrca/fleet.py        L3 runs, L4 signatures, survivors, inventory join, simultaneity
glyphrca/pcie.py         PCIe tree: golden shape, missing/degraded devices, switch vs device attribution
glyphrca/tier2.py        opaque tokens and private codebook
glyphrca/report.py       pipeline, size accounting, text report
glyphrca/synth.py        synthetic fleet generator (all scenarios above)
tests/                   unit and end-to-end tests
```

## Limits of v0.1

- The codebook covers a core set of events. Real fleets need the full Intel tables, each vendor's OEM records, Windows Event Log (WHEA) and Linux kernel logs.
- Synthetic scenarios are cleaner than reality: SEL overflow, BMC clock skew and truncated logs will create noise that v0.1 does not model.
- Causality is inferred from topology and timing, not proven.

## Roadmap

Windows Event Log in several languages · Linux dmesg / NVIDIA Xid · full Intel SEL tables · OEM dialects · rate-based states (a burst vs. a slow leak) · interactive fleet view.

## License

Copyright (c) 2026 Vitali de Mille.

Free for noncommercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE): research, study, personal projects, evaluation. **Any commercial use requires a commercial license**; see [COMMERCIAL.md](COMMERCIAL.md).
