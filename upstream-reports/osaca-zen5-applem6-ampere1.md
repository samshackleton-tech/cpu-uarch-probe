# Upstream contribution note for `RRZE-HPC/OSACA`

**Title:** Bare-metal hardware calibration data for AMD Zen 5 (`zen5.yml`), Apple M6 (AArch64/NEON), and AmpereOne (AArch64/NEON)

## Summary

`osaca/data/` currently includes `zen3.yml`, `zen4.yml`, `zen5.yml`, `icl.yml`,
and `m1.yml`, but does not yet have entries for **Apple M6** or **AmpereOne**,
and `zen5.yml` imports 512-bit `VPXORD`/`VPRORD (ZMM)` latencies from
`uops.info`.

We ran a reproducible set of bare-metal microarchitecture probes across seven
real processors (AMD EPYC 9255 Zen 5, AMD Ryzen 7 9700X Zen 5, AMD Threadripper
PRO 7975WX Zen 4, AMD EPYC 7443 Zen 3, Intel Xeon Gold 6330 Ice Lake, Apple M6
Super core, and AmpereOne) and would be glad to submit YAML model updates or
new architecture files if useful to OSACA.

## Key measured parameters

The table below lists the bare-metal hardware latency and throughput findings
that directly affect OSACA machine models.

| Microarchitecture | Operation / Feature | Bare-metal hardware measurement |
| --- | --- | --- |
| AMD Zen 5 (EPYC 9255) | `VPADDD` / `VPXORD` / `VPRORD (ZMM)` latency | **`2.000` cyc** for all three (`g1_14ops` = `28.002` cyc; `uops.info` lists `1` for `VPXORD`/`VPRORD`) |
| AMD Zen 5 (EPYC 9255) | 512-bit `VPADDD`/`VPXORD` throughput | **`0.25` cyc/op** (4.00/cyc; 16 ops = `4.01` cyc); `VPRORD (ZMM)` = **`0.50` cyc/op** (16 ops = `8.00` cyc) |
| AMD Zen 5 (EPYC 9255) | L1D scalar loads | **4 load AGUs** (`8` dependent load-add ops = `2.20` cyc) |
| Apple M6 Super core | GPR `add`/`orr` latency & ALU width | `1.000` cyc; 6–7 integer ALU pipes (`probe_alu_wide` = `2.52` cyc), 3 load pipes (`ld_x_12` = `3.96` cyc, `ld_q_12` = `3.99` cyc) |
| Apple M6 Super core | NEON `add.4s`/`eor`/`ushr`/`rev32`/`ext` | **`2.000` cyc** latency; **4 symmetric pipes** (`4.00` ops/cyc with 16 independent chains, `3.00` ops/cyc with 8 chains) |
| AmpereOne | GPR / NEON latencies & fusion | GPR `1.001` cyc; NEON `add.4s`/`eor`/`ushr`/`rev32`/`ext` `2.002` cyc; **2 NEON pipes** (16 ops = `8.04` cyc); macro-op fuses `cmp + b.lo` (`3.52` / `4.54` / `5.03` / `6.52` cyc on 15/19/21/27-insn loops) |

Full JSON datasets and the standalone zero-dependency probe script (`probe.py`)
are available in our repository (quad-licensed MIT / Apache-2.0 / TGPPL-1.0 /
BOSL-1.0) for direct import into `osaca/data/`.
