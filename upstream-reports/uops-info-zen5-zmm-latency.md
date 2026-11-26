# Upstream report for `uops.info` (`andreas-abel/nanoBench`)

**Title:** Zen 5 (`ZEN5`) `VPXORD (ZMM)` / `VPRORD (ZMM)` measure 2.000 cycles latency on bare-metal EPYC 9255 (2026-03-29 XML lists `cycles="1"`), plus EPYC 9255 vs. Ryzen 7 9700X `VPXORD (ZMM)` throughput difference

## Summary

While validating a static out-of-order pipeline simulator against bare-metal
`perf_event_open(PERF_COUNT_HW_CPU_CYCLES)` counters on AMD Zen 5 processors
(AMD EPYC 9255 server and AMD Ryzen 7 9700X desktop, compared directly against
AMD Threadripper PRO 7975WX Zen 4), we found a concrete latency discrepancy in
the `2026-03-29` `uops.info/instructions.xml` `ZEN5` table, plus an unexpected
EPYC vs. Ryzen Zen 5 throughput difference:

1. **`VPXORD (ZMM, ZMM, ZMM)` and `VPRORD (ZMM, ZMM, I8)` measure `2.000`
   cycles latency on Zen 5, not `1` cycle.** The `2026-03-29` `instructions.xml`
   lists `cycles="2"` for `VPADDD (ZMM, ZMM, ZMM)` on `ZEN5`, but `cycles="1"`
   for both `VPXORD (ZMM, ZMM, ZMM)` and `VPRORD (ZMM, ZMM, I8)`. On bare-metal
   EPYC 9255, all three measure `2.000` cycles in serial chains, and a
   14-instruction dependent BLAKE3 G-column (`6 VPADDD + 4 VPXORD + 4 VPRORD`)
   measures `28.002` cycles (`14 * 2.000`), compared to `18.021` cycles
   (`6*1 + 4*1 + 4*2`) on Zen 4.
2. **EPYC 9255 (Turin) vs. Ryzen 7 9700X (Granite Ridge) throughput on
   `VPXORD (ZMM)`:** on the exact same compiled object bytes, 24 of 25 kernels
   match between 9700X and EPYC 9255 to `0.999`–`1.000`, including 16
   independent `VPADDD (ZMM)` (`4.01` cyc on both, matching `TP_loop="0.25"`)
   and 16 independent `VPRORD (ZMM)` (`8.00` cyc on both, matching
   `TP_loop="0.50"`), whereas 16 independent `VPXORD (ZMM)` measures `7.11`
   cycles on Ryzen 7 9700X vs. `4.01` cycles on EPYC 9255. (For comparison,
   `instructions.xml` lists `TP_loop="0.25"`, `TP_unrolled="0.37"`.)

## Bare-metal hardware measurements (`PERF_COUNT_HW_CPU_CYCLES`, pinned core, median of 11 rounds)

The table below compares direct serial-chain and compound-kernel cycle counts on
AMD Zen 4 (Threadripper PRO 7975WX) and AMD Zen 5 (EPYC 9255).

| Probe | Instructions per iter | AMD Zen 4 (7975WX) | AMD Zen 5 (EPYC 9255) | `uops.info` `ZEN5` XML (`2026-03-29`) |
| --- | --- | ---: | ---: | --- |
| `lat_vpaddd(x1)` | 16 serial `vpaddd zmm0, zmm0, zmm8` (per op) | `1.023` cyc | **`2.000` cyc** | `cycles="2"` |
| `lat_vpxord(x1)` | 16 serial `vpxord zmm0, zmm0, zmm8` (per op) | `1.024` cyc | **`2.000` cyc** | `cycles="1"` |
| `lat_vprord(x1)` | 16 serial `vprord zmm0, zmm0, 7` (per op) | `2.004` cyc | **`2.000` cyc** | `cycles="1"` |
| `lat_axr(x3op)` | 8 serial `vpaddd -> vpxord -> vprord` (per 3 ops) | `4.003` cyc | **`6.000` cyc** | `4` |
| `g1_14ops` | 1-col BLAKE3 G (`6 add + 4 xor + 4 ror`) | `18.021` cyc | **`28.002` cyc** | `20` |
| `g2_28ops` | 2-col BLAKE3 G (28 ops) | `24.911` cyc | **`28.002` cyc** | `20` |
| `g4_56ops` | 4-col BLAKE3 G (56 ops) | `36.924` cyc | **`30.099` cyc** | `20` |
| `probe_zmm_vpaddd` | 16 independent `vpaddd zmm` | `8.80` cyc | **`4.01` cyc** | `TP_loop="0.25"`, `TP_unrolled="0.37"` |
| `probe_zmm_vpxord` | 16 independent `vpxord zmm` | `8.80` cyc | **`4.01` cyc** (`7.11` on 9700X) | `TP_loop="0.25"`, `TP_unrolled="0.37"` |
| `probe_zmm_vprord` | 16 independent `vprord zmm` | `16.02` cyc | **`8.00` cyc** | `TP_loop="0.50"` |

## Minimal reproducer

```asm
.intel_syntax noprefix
.text
.p2align 6
.globl k_lat_vpxord
k_lat_vpxord:
  vpxord zmm0, zmm0, zmm0
  vpxord zmm8, zmm8, zmm8
1:
  .rept 16
  vpxord zmm0, zmm0, zmm8
  .endr
  sub rdi, 1
  jnz 1b
  vzeroupper
  ret

.p2align 6
.globl k_lat_vprord
k_lat_vprord:
  vpxord zmm0, zmm0, zmm0
1:
  .rept 16
  vprord zmm0, zmm0, 7
  .endr
  sub rdi, 1
  jnz 1b
  vzeroupper
  ret
```

Running `k_lat_vpxord(20000)` and `k_lat_vprord(20000)` under
`PERF_COUNT_HW_CPU_CYCLES` (`exclude_kernel=1`, pinned core) gives `32.00`
cycles/iteration (`2.000` cycles/op) for both on EPYC 9255.

Full raw JSON datasets and the standalone zero-dependency probe harness are
available at https://github.com/samshackleton-tech/cpu-uarch-probe.
