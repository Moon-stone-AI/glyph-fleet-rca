# glyph-fleet-rca

**Find the one server that knows why 5,000 servers died.**

When a whole fleet fails at once, the servers that died instantly could not write down why. Their logs are a storm of identical errors. The cause is usually in the log of the one machine that lived a second longer, and finding it by hand can take weeks.

`glyph-fleet-rca` applies GlyphAI semantic compression (monograph on Zenodo: [zenodo.org/records/20813531](https://zenodo.org/records/20813531)) to server event logs. It turns raw lines into concept glyphs, collapses storms into counters, and compares whole servers by their glyph signatures. The survivor stands alone.

> Synthetic data only. No real operator logs are included or were used.

## How it works: four levels

| Level | What happens | Example |
|---|---|---|
| **L1** word → word glyph | Vendor vocabulary is normalised; times, record ids and hex are split off | `DIMM`, `Memory Module`, `mem` → `MEM` |
| **L2** phrase → concept glyph + arguments | One meaning per line, whatever the vendor or format | three vendor phrasings and a binary record → `MEM.ECC.CORR` `dimm=A2` |
| **L3** run → concept × count | Identical concepts in a row become one run with a counter, first/last time and argument histogram | `MEM.ECC.CORR ×499 in 51 s, mostly DIMM A2` |
| **L4** fleet → signatures × servers | Servers with the same story collapse into one signature | `4,999 × … > CPU.IERR.CATERR > SILENCE` |

Three roads lead to the same glyph:
- **binary IPMI SEL records** (16 bytes) are decoded by sensor type, sensor number and event offset, following Intel's *SEL Troubleshooting Guide*, Rev. 3.8 (tables cited in the codebook);
- **text SEL logs** in any dialect go through the lexicon (L1) and phrase rules (L2);
- **Linux kernel logs** (v0.2: `dmesg -T` or `journalctl -o short-iso`) go through kernel rules: NVIDIA Xid and SXid, MCE, EDAC, PCIe AER, pciehp, IOMMU/DMAR faults, GPU init failures, Fabric Manager failures, kernel panic, lockups, OOM, thermal throttling, NIC link loss. Xid meanings follow NVIDIA's Xid catalog. Routine kernel chatter is ignored and counted.
- **GPU telemetry** (v0.2: DCGM samples in `dcgm.csv`) goes through metric rules: NVLink CRC rate, PCIe replay rate, row-remap failure and pending, spare-row banks, power and thermal violation time. Each metric is understood under both its dcgm-exporter name and its DCGM field name. This is the early-warning road: it fires before any Xid is written.
- **Active diagnostics** (v0.2: results in `diag.csv`, from `dcgmi diag -r 3` or NVIDIA field diagnostics, run on a drained node): a failed matrix-math test (`diagnostic`, `targeted_stress`, `targeted_power`) is silent data corruption, `GPU.SDC`, and goes to RMA; other failed tests keep the node drained.

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

### v0.2: the kernel joins the story

With `kernel.log` next to `sel.log`, each server's story is told by two witnesses merged in time. Results on the same synthetic fleet of 6,000 servers, now with 4.1 million events (SEL + kernel), in about 98 seconds:

```
Device left the bus (one event, several witnesses):
  RP1/SW1/GPU5          6 servers   6 by kernel + pcie_tree + sel

Software-wide: GPU.EXEC.EXCEPTION on 40 servers within 28.0 s -> look at the job, driver or library, not at the GPUs

Lemon nodes (several hardware failure kinds on one server -> drain and run diagnostics):
  node-05066: GPU.MEM.ECC.DBE, GPU.MEM.REMAP.EVENT, MEM.ECC.CORR, PCIE.CORR

Data gaps (Xid codes not in the codebook): {'xid=154': 2}
```

- **One event, three witnesses.** A GPU that falls off the bus is reported by the BMC (SEL: PCIe surprise link down), by the GPU driver (Xid 79) and by the PCIe tree (device missing). Three formats, one glyph: `GPU.BUS.LOST`.
- **Application or hardware?** Xid 13, 31 and 43 can be either. On one GPU, look at the GPU. On 40 servers within half a minute, look at the job: nobody should RMA 40 GPUs for one bad kernel launch.
- **Lemon nodes.** A server failing in several hardware categories (double-bit ECC, row remapping, host memory, PCIe) is drained as a whole, not fixed one symptom at a time.
- **Escalation by rate.** A light triage repeated too often on one GPU (contained ECC errors, row remapping events: more than 3 in 24 h) is escalated to drain and diagnostics: the hardware is degrading.
- **Practitioner triage.** The Xid triage was reviewed from operations experience: Xid 95 plans for a PCIe reset or reboot because a plain GPU reset often hangs; Xid 63 resets when idle but with a 12 h deadline; Xid 64 drains the node at once before RMA; Xid 13/31/43 return logs to the user instead of punishing the node.
- **Early warnings from telemetry.** NVLink CRC errors at 300/min, PCIe replays, banks running out of spare rows, a GPU power-braked 60 % of the time: flagged before the hard error.
- **Zombie GPUs.** Uncontained ECC or bus loss followed by a task blocked in the kernel (state D): soft restarts would hang, go straight to reboot or PCIe hot-plug reset.
- **Silent cluster killers.** Fabric Manager failures (multi-node jobs hang at the first AllReduce with no Xid), IOMMU/DMAR faults (GPUDirect traffic stalls), AER storms (the host locks up writing logs), driver init failures after a reboot.
- **Lagging signs.** Xid 45 with no earlier error on the same GPU: the root cause line was probably lost; run a health check.
- **GSP errors (Xid 119/120)** are treated as driver-or-hardware: reset, then watch; repeated on one GPU escalates, many servers at once points to the driver.
- **Unknown Xid codes are reported, not guessed.**
- **Stable signatures.** A signature is the order in which concepts first appear, so two sources interleaving line by line still tell one story: the survivor is still found.

### v0.2.1: a health index for every GPU

With `gpus.csv` (host, bdf, uuid, model, hours) each GPU gets a card with a health index from 0 to 100 %, built from its own history across all sources:

```
GPU health index (0-100 %, from each GPU's own history; not a survival probability):
  H100       2520 GPUs   mean  98.0 %   median  98.1 %   excellent 2506, good 13, do not sell 1
  Lowest:
       10 %  do not sell A100   node-00329 0000:45:00.0  age -1.9  [RMA, not for sale: failed matrix-math diagnostic]
     84.3 %  good        H100   node-00332 0000:25:00.0  memory -15 · age -0.7
```

- Six components with ceilings: GPU memory 35, severe Xid 25, links 15, throttling 10, diagnostics 10, age 5.
- Hard caps: exhausted row remapping or a failed matrix-math test caps the GPU at 10 %; banks with no spare rows at 30 %.
- Uncorrectable memory: one Xid 48 or row remap caps the GPU at 74 % (caution); a repeat caps it at 49 % (do not sell). Xid 48 and the remap it triggers count as one incident.
- Every score shows its deductions; weights live in `codebook/health_v0.2.1.yaml`.
- The index describes the past. It is not a probability of survival: that would need real failure data.
- PCIe floods during a common-mode crash are not charged to the GPUs.
- `--health-csv cards.csv` writes one row per GPU, for a fleet sale or a refresh decision.

### v0.3: vendor codebooks from the official guides

BMC logs now read by message ID (or, for HPE, by message text), each entry taken from the vendor's own reference:

| Vendor | Source | Coverage |
|---|---|---|
| Dell iDRAC9 | PowerEdge Servers Error and Event Messages Reference Guide, July 2026 | 69 IDs: PCIe fatal/time-out/downtrain, CPU, memory, power, cooling, GPU (GPU0001, TMP0401/0402, HWC9051/9052, ACC0001) |
| Lenovo XCC / UEFI | ThinkSystem SR650 V4, SR680a V3, SR780a V3 Messages and Codes Reference | 67 IDs, plus component and severity decoded from any `FQXppnnxxxxc` ID |
| HPE iLO IML | IML Messages and Troubleshooting Guide, Gen10/Gen10 Plus and Gen11 | 53 message templates keyed by Error Class / Error Code |

- A line with a known ID goes straight to its glyph; anything else still takes the word road.
- Explicit-hex PCIe addresses (`Bus 0x41 Device 0x00 Function 0x0`) become a bdf; decimal-looking forms are kept as written, never guessed.
- iDRAC names GPUs by index or slot. An optional `idrac_slot` column in `gpus.csv`, taken from each server's own inventory, binds those events to the right GPU card. No platform topology is assumed.
- What the guides show: BMCs see GPUs coarsely (temperature, power, board status). Xid, ECC, row remapping and NVLink faults come from the kernel and DCGM roads, which is why the tool reads all of them.

### v0.4: work queues and the failure-pattern loop

Faulty nodes are sorted into **work queues by symptom**: GPU off the bus, GPU memory, NVLink, PCIe and risers, CPU, host memory, power, thermal, network, drives, OS, unknown codes. Each node sits in one queue, by its most severe symptom; a fleet-wide crash with one cause is one queue for engineering, not thousands of tickets, and application errors on many servers go back to the job owner. Lemons lead their queue. `--buckets-csv` writes one row per node for ticketing; queues, owners and actions live in `codebook/buckets_v0.4.yaml`.

From a queue to knowledge, the way a practitioner works:

1. `python -m glyphrca patterns FLEET --bucket gpu_memory` splits the queue into its patterns and mines each one: which events, in which order, how far apart, on how many GPUs, how it ends.
2. Each pattern becomes a **draft record** in the C_GPU_Faults format, anonymous by construction (no hosts, addresses or serials).
3. A practitioner reviews it, gives it a fault code and the fix, and adds it to `codebook/fault_library.yaml`.
4. From then on, matching nodes are labelled with the known pattern and its fix in every report. Drafts never label nodes.

### v0.5: collect from your own servers

For a handful of servers, debugging and triage start from one script. `tools/glyph-collect.sh` runs on each Linux GPU server, **reads only**, installs nothing and sends nothing: it writes one `.tar.gz` that you inspect before it goes anywhere.

```bash
sudo ./tools/glyph-collect.sh               # kernel log (30 days), ipmitool SEL, nvidia-smi -q, PCIe links from sysfs
sudo ./tools/glyph-collect.sh --diag 2      # also DCGM diagnostics: loads the GPUs, idle and drained nodes only
sudo ./tools/glyph-collect.sh --bmc ./exports  # also vendor BMC exports (iDRAC, XCC, iLO) from a folder, redacted too
python -m glyphrca ingest glyph-collect-*.tar.gz --out fleet/
python -m glyphrca analyze fleet/ --health-csv health.csv --buckets-csv queues.csv
```

- **Redacted by default**, in every text file of the archive including `bmc/`: the hostname becomes `node-<hash>`; IPv4, IPv6 and MAC addresses are masked; serial numbers, board part numbers and Dell service tags are masked in `Key: value`, `Key=value` and `<Key>value</Key>` form. GPU UUIDs and PCIe addresses stay, because they carry a card's history. `--no-redact` keeps everything.
- Vendor BMC logs (iDRAC Lifecycle Log, XCC event log, HPE IML) need an export from the BMC page into a folder, passed with `--bmc FOLDER`: the files are copied (the originals are not touched) and redacted before archiving. Files added to `bmc/` by hand after the archive is built are not redacted. Serial numbers that sit in an unlabelled CSV column are not recognised; look before you send. They are read with the v0.3 vendor codebooks.
- `nvidia-smi -q` adds what the card itself remembers: uncorrectable remapped rows, pending remaps, remap failure and banks low on spare rows. One incident is counted once: if the kernel log already holds Xid 63 or 64 for that GPU, the snapshot of the same fact is not added.
- On one or a few servers you get per-node triage, the GPU health index, work queues and each node's timeline. Survivor search, firmware correlation and pattern mining need a fleet.
- The DCGM diagnostics reader is **experimental**: it was built without a real customer sample yet. If it recognises nothing, it says so.

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
python -m pytest tests                                      # 66 tests
```

### Input layout
```
<fleet>/<host>/sel.log     raw SEL: binary hex records and/or text, any dialect, mixed is fine
<fleet>/<host>/kernel.log  optional: Linux kernel log, dmesg -T or journalctl -o short-iso
<fleet>/<host>/dcgm.csv    optional: GPU telemetry, columns timestamp,gpu,field,value
<fleet>/<host>/diag.csv    optional: active diagnostics results, columns timestamp,gpu,test,result
<fleet>/<host>/pcie.txt    optional: <BDF> <parent BDF|-> <kind> <slot> x<w>/x<max> gen<g>/gen<max>
<fleet>/inventory.csv      optional: host,fw,bios,...
<fleet>/gpus.csv           optional: host,bdf,uuid,model,hours[,idrac_slot] (GPU identity for the health index)
```

## Repository

```
codebook/sel_v0.1.yaml   SEL codebook: concepts, binary SEL decoding, lexicon, phrase rules
codebook/kernel_v0.2.yaml kernel codebook: GPU/NVLink/host concepts, Xid table, kernel rules, escalation
codebook/telemetry_v0.2.yaml telemetry codebook: DCGM metrics (exporter and field names), thresholds
codebook/diag_v0.2.yaml   active diagnostics codebook: DCGM diagnostic plugins, SDC vs other failures
codebook/health_v0.2.1.yaml GPU health index: components, penalties, caps, bands
codebook/vendor_*_v0.3.yaml vendor codebooks: Dell iDRAC, Lenovo XCC/UEFI, HPE IML; shared vendor concepts
codebook/buckets_v0.4.yaml work queues: symptom buckets, owners, actions
codebook/fault_library.yaml reviewed failure patterns (C_GPU_Faults records) that label matching nodes
glyphrca/encode.py       L1, L2 (binary, text and kernel roads, template cache)
glyphrca/fleet.py        L3 runs, L4 signatures, survivors, inventory join, simultaneity,
                         software-wide failures, lemon nodes
glyphrca/pcie.py         PCIe tree: golden shape, missing/degraded devices, switch vs device attribution,
                         bus-loss witnesses across SEL, kernel and tree
glyphrca/tier2.py        opaque tokens and private codebook
glyphrca/ingest.py       glyph-collect archives -> fleet directory (v0.5)
tools/glyph-collect.sh   read-only collector for one Linux GPU server (v0.5.1: --bmc, IPv6 and service-tag redaction)
glyphrca/report.py       pipeline, size accounting, text report
glyphrca/synth.py        synthetic fleet generator (all scenarios above)
tests/                   unit and end-to-end tests
```

## Limits

- The vendor codebooks cover the fleet-relevant part of each guide, not every message. Supermicro, AMD-specific records and Windows Event Log (WHEA) are not yet covered.
- Thresholds (rates, escalation counts, storm sizes) are starting values from practice, to be tuned per fleet.
- Kernel rules match the common message formats; drivers and kernel versions vary, and new formats surface as ignored lines or data gaps.
- Synthetic scenarios are cleaner than reality: SEL overflow, BMC clock skew and truncated logs will create noise that v0.1 does not model.
- Causality is inferred from topology and timing, not proven.

## Roadmap

- ~~v0.2 Linux kernel and NVIDIA Xid~~ (done)
- ~~v0.3 vendor codebooks: Dell iDRAC, Lenovo XCC, HPE iLO~~ (done); next: Supermicro, full Intel tables, AMD
- agent (beyond log analysis): job-aware blast radius (requeue the whole multi-node job through Slurm, Volcano or Ray, not just drain one node); NVLink state polling for degraded links; health-query watchdog for baseboard management hangs; ghost GPU contexts after container crashes; scheduling pre-flight diagnostics on drained nodes
- ~~v0.5 collector for small fleets~~ (done); next: a real `dcgmi diag -j` sample, Redfish export of BMC logs
- Windows Event Log in several languages: one glyph whatever the language
- later: rate-based states (a burst vs. a slow leak), interactive fleet view

## Acknowledgements

Triage notes and failure patterns in the kernel and telemetry codebooks come from the operational records of Vitali de Mille (data-centre and GPU fleet operations). Metric names and Xid meanings were checked against NVIDIA DCGM and Xid documentation. Vendor message IDs and texts come from the Dell, Lenovo and HPE reference guides named above.

## License

Copyright (c) 2026 Vitali de Mille.

Free for noncommercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE): research, study, personal projects, evaluation. **Any commercial use requires a commercial license**; see [COMMERCIAL.md](COMMERCIAL.md).
