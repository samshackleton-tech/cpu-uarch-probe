# cpu-uarch-probe

A small, zero-dependency hardware microarchitecture probe, open bare-metal dataset,
and uiCA-style static out-of-order pipeline simulator covering both **x86-64**
(AMD Zen 3 / Zen 4 / Zen 5, Intel Ice Lake) and **AArch64** (Apple Silicon,
AmpereOne).

## Why this exists

Static loop-throughput simulators such as `uiCA` (Intel-only, last updated in
2024) and `llvm-mca` (often 15%–30% off on recent AMD Zen 5 and Zen 4 silicon)
leave a gap for anyone tuning tight cryptographic, hashing, or compression loops
across modern x86-64 and AArch64 hardware.

This repository pairs:
1. **`probe.py`**: a single self-contained script (plain Python 3 standard
   library + your system C compiler `cc`) that runs in ~5 seconds on any Linux or
   macOS x86-64 or AArch64 machine, measuring exact instruction latencies,
   fusion/rename behavior, port widths, real hash loops, and reproducible
   randomized mixes;
2. **`sim.py`**: a purely static, input-free out-of-order pipeline simulator
   (modeling macro-op fusion, micro-fusion, rename move/zero elimination, split
   vs. unified schedulers, and true out-of-order per-port reservation sets) plus
   compile-time microarchitectural lints; and
3. **`results/`**: open, reproducible hardware JSON datasets across seven real
   processors so anyone can calibrate static models and submit measurements from
   additional CPUs.

## Hardware validation scoreboard (7 real processors, 169 kernel-runs)

The table below shows how many kernels each static estimator predicted within 10%
of real hardware measured cycles across our seven bundled processors, alongside
median and mean absolute percentage error (`results/*.json`).

| Processor | Counter method | `llvm-mca` | `sim.py` (within 10%) | `sim.py` median / mean err |
| --- | --- | --- | --- | --- |
| AMD EPYC 9255 (Zen 5 server) | `perf_event_open` | 8/25 (32%, mean 28.8%) | **19/25 (76%)** | **1.1% / 6.3%** |
| AMD Ryzen 7 9700X (Zen 5 desktop) | `perf_event_open` | 8/25 (32%, mean 28.8%) | **19/25 (76%)** | **5.3% / 7.9%** |
| AMD Threadripper PRO 7975WX (Zen 4) | `perf_event_open` | 14/25 (56%, mean 16.7%) | **20/25 (80%)** | **2.4% / 5.3%** |
| AMD EPYC 7443 (Zen 3) | `perf_event_open` | 13/21 (62%, mean 13.6%) | **17/21 (81%)** | **3.9% / 5.0%** |
| Intel Xeon Gold 6330 (Ice Lake) | `perf_event_open` | 15/25 (60%, mean 15.3%) | **19/25 (76%)** | **8.1% / 8.2%** |
| Apple M6 Super core (AArch64 + NEON) | `cal_add64` ref clock | n/a | **17/24 (71%)** | **2.2% / 7.1%** |
| AmpereOne (AArch64 + NEON) | `perf_event_open` | 22/24 (92%, mean 4.4%) | **21/24 (88%)** | **0.7% / 3.5%** |

### Notable hardware findings recorded in this corpus

- **AMD Zen 5 512-bit latency (`VPXORD`/`VPRORD (ZMM)`):** bare-metal EPYC 9255
  probes measured `2.000` cycles for all three of `VPADDD`, `VPXORD`, and
  `VPRORD (ZMM)` (a 14-op BLAKE3 G column measured `28.002` cycles, and 4
  parallel columns `30.099` cycles), whereas `uops.info`'s XML currently lists
  `1` cycle for `VPXORD`/`VPRORD (ZMM)`. EPYC 9255 also sustains `4.00` ops/cyc
  on 16 independent `VPADDD`/`VPXORD (ZMM)` (`4.01` cycles) and 4 L1D load AGUs
  (`2.20` cycles for 8 loads).
- **AMD Zen 4 vs. Zen 5 AVX-512:** Threadripper PRO 7975WX measured `1.001` /
  `1.001` / `2.001` cycles for `VPADDD` / `VPXORD` / `VPRORD (ZMM)` (`g1_14ops`
  = `18.02` cyc, `g4_56ops` = `36.92` cyc), reflecting double-pumped 256-bit
  micro-op execution and a streaming op-cache across taken loop back-edges.
- **Apple M6 NEON saturation threshold:** `add.4s`, `eor`, `ushr`, `rev32`, and
  `ext` all measure `2.000` cycles latency and reach `4.00` ops/cyc across 4
  symmetric NEON pipes when given 16 independent vector chains (`3.98` cyc for 16
  ops), dropping to `3.00` ops/cyc when only 8 independent chains are active
  (`5.33` cyc for 16 ops). Vector loads (`ldr qN`) share the 3-wide load queue
  rather than NEON ALU dispatch.
- **AmpereOne macro-op fusion:** bare-metal `perf_event_open` counters confirmed
  AmpereOne fuses `cmp + b.lo` (`3.52`, `4.54`, `5.03`, `6.52` cycles on
  15/19/21/27-instruction loops).

## Quick start and contributing your CPU

Run the probe on any quiet bare-metal Linux or macOS machine (requires only
Python 3 and `cc`, no `pip install` and no root required on macOS):

```sh
python3 probe.py --run --json-out results/my-cpu.json
```

If `llvm-mca` is installed, include `--mcpu <name>` (for example `znver5`,
`arrowlake`, `graniterapids`, `neoverse-v2`, or `apple-m4`) to record a side-by-side
comparison.

To contribute your processor to `results/`:
1. Run `python3 probe.py --run --json-out results/<vendor>-<model>.json` on an
   idle machine (avoid shared cloud VMs when possible, or note VM status in your PR).
2. Verify `lat_add_gpr` and `lat_or_gpr` / `lat_orr_gpr` both read `1.000 ± 0.01`
   cycles/op and `alt_over_add` is within `0.99..1.01`.
3. Open a pull request adding `results/<vendor>-<model>.json` — no hostnames,
   IP addresses, or user paths are written by `probe.py`.

## Self-tests

```sh
python3 test_sim.py -v
```

## Upstream reports

`upstream-reports/` holds self-contained bug and calibration notes prepared for
upstream projects (`uops.info`, `RRZE-HPC/OSACA`, and `llvm/llvm-project`).

## License

Quad-licensed under your choice of **MIT**, **Apache-2.0**, **TGPPL-1.0**, or
**BOSL-1.0** (see `LICENSE`).
