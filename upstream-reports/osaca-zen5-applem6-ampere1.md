# Upstream contribution note for `RRZE-HPC/OSACA`

**Title:** Bare-metal hardware measurements available for Apple M6 (AArch64/NEON) and AmpereOne (AArch64/NEON)

## Summary

`osaca/data/` already has solid models for `zen5.yml` (including `latency: 2`
for `vpxord`/`vprord` on `zmm`), `zen4.yml`, `zen3.yml`, `icl.yml`, and
`m1.yml`, but does not yet include entries for **Apple M6** or **AmpereOne**.

We ran a reproducible set of bare-metal microarchitecture latency and
throughput probes across both of those AArch64 processors (alongside five
x86-64 baselines) and published the full JSON datasets and zero-dependency
probe script at https://github.com/samshackleton-tech/cpu-uarch-probe
(quad-licensed MIT / Apache-2.0 / TGPPL-1.0 / BOSL-1.0, free to import into
`osaca/data/`).

## Key measured parameters

The table below lists the bare-metal latency, throughput, and fusion numbers
measured on Apple M6 Super core (interleaved `cal_add64` reference clock,
cross-checked against `cal_alt64`) and bare-metal AmpereOne Linux
(`perf_event_open(PERF_COUNT_HW_CPU_CYCLES)`).

| Microarchitecture | Operation / Feature | Bare-metal hardware measurement |
| --- | --- | --- |
| Apple M6 Super core | GPR `add` / `orr` latency & ALU width | `1.000` cyc; 6–7 integer ALU pipes (`16` independent adds + loop = `2.52` cyc) |
| Apple M6 Super core | L1D loads (`ldr xN` and `ldr qN`) | **3 load pipes** (`ld_x_12` = `3.96` cyc, `ld_q_12` = `3.99` cyc; `q` loads use the same 3-wide load queue) |
| Apple M6 Super core | NEON `add.4s`, `eor.16b`, `ushr.4s`, `rev32.8h`, `ext.16b` | **`2.000` cyc** latency for all five; **4 symmetric pipes** (`4.00` ops/cyc with 16 independent chains = `3.99` cyc; drops to `3.00` ops/cyc = `5.33` cyc with only 8 independent chains) |
| Apple M6 Super core | Scalar loop control + self-move | `mov x, x` eliminated (`0` ALU cycles); `cmp + b.lo` macro-op fused |
| AmpereOne | GPR `add` / `orr` latency & width | `1.001` cyc; 4 integer ALU pipes, 2 L1D load pipes (`ld_x_12` = `6.03` cyc, `ld_q_12` = `6.02` cyc) |
| AmpereOne | NEON `add.4s`, `eor.16b`, `ushr.4s`, `rev32.8h`, `ext.16b` | **`2.002` cyc** latency for all five; **2 symmetric NEON pipes** (`16` independent ops = `8.04` cyc) |
| AmpereOne | Scalar loop control | Macro-op fuses `cmp + b.lo` (15/19/21/27-instruction loops measure `3.52` / `4.54` / `5.03` / `6.52` cyc/iter, matching `ceil((N-1)/4)` dispatch) |

## Reproducibility & data

- Full JSON datasets: `results/apple-m6-super.json` and `results/ampere-ampere1.json` in https://github.com/samshackleton-tech/cpu-uarch-probe.
- Reproducer script: `python3 probe.py --run` (Python 3 standard library + system `cc`, zero dependencies).

Happy to prepare YAML drafts or run additional instruction probes on either CPU
if helpful to the OSACA maintainers.
