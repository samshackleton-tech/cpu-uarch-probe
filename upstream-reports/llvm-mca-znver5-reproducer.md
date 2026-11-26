# Upstream issue draft for `llvm/llvm-project` (`llvm-mca` / `X86ScheduleZnver5.td`)

**Title:** `[llvm-mca][X86] Znver5 scheduling model underpredicts 128-bit and 512-bit integer vector latencies and overpredicts scalar loops (32% within 10% across 25 bare-metal Zen 5 kernels)`

## Summary

We benchmarked 25 loop kernels (real cryptographic/hash bodies, targeted
pipeline probes, and 12 reproducible randomized instruction mixes) under bare-metal
Linux `perf_event_open(PERF_COUNT_HW_CPU_CYCLES)` on both an **AMD EPYC 9255**
(Zen 5 server) and an **AMD Ryzen 7 9700X** (Zen 5 desktop), and compared
against `llvm-mca -mcpu=znver5`.

Across the 25 kernels, `llvm-mca -mcpu=znver5` predicted only **8/25 (32.0%)**
within 10% of measured hardware cycles (**14.5% median**, **28.8% mean**
absolute error), compared to **15/25 (60%)** for `-mcpu=icelake-server` and
**22/24 (92%)** for `-mcpu=ampere1` on the same benchmark methodology.

## Three specific `Znver5` model discrepancies isolated on bare-metal EPYC 9255

The table below shows the largest systematic gaps between `llvm-mca -mcpu=znver5`
and bare-metal `PERF_COUNT_HW_CPU_CYCLES` on AMD EPYC 9255 (identical within
0.1% on Ryzen 7 9700X for all rows shown).

| Kernel | Description | Measured (EPYC 9255) | `llvm-mca -mcpu=znver5` | Error | Root cause isolated by micro-probes |
| --- | --- | ---: | ---: | ---: | --- |
| `real_sse2_g_round` | 128-bit BLAKE3 G-round (`vpaddd`/`vpxor`/`vpslld`/`vpsrld`/`vpor` XMM) | `33.80` cyc | `16.05` cyc | **-52.5%** | Zen 5 executes 128-bit integer vector ALU/logic/shift ops at `2.0` cycles latency (vs. `1.0` on Zen 4, where this exact loop measures `16.02` cyc). |
| `probe_zmm_b3_g4` | 4-col 512-bit BLAKE3 G (`24 vpaddd + 16 vpxord + 16 vprord` ZMM) | `29.87` cyc | `16.05` cyc | **-46.3%** | Bare-metal serial probes on EPYC 9255 measure `2.000` cyc latency for all three of `VPADDD`, `VPXORD`, and `VPRORD (ZMM)` (`g1_14ops` = `28.002` cyc; `g4_56ops` = `30.099` cyc). |
| `probe_zmm_vpaddd` | 16 independent `vpaddd zmm` | `4.01` cyc | `8.04` cyc | **+100.5%** | `Znver5` still double-pumps `VPADDD (ZMM)` at `0.50` cyc/op like Zen 4 (`8.80` cyc), whereas EPYC 9255 & 9700X both execute 4.00 ops/cyc (`4.01` cyc). |
| `probe_zmm_vprord` | 16 independent `vprord zmm, zmm, 7` | `8.00` cyc | `16.04` cyc | **+100.5%** | `Znver5` predicts `1.00` cyc/op like Zen 4 (`16.02` cyc), whereas both Zen 5 CPUs execute 2.00 ops/cyc (`8.00` cyc). |
| `probe_alu_wide` | 12 independent scalar `add` + loop | `3.01` cyc | `2.36` cyc | **-21.6%** | Taken loop back-edge closes op-cache dispatch window on Zen 5 (`3.01`, `2.01`, `6.00` cyc across multiple loops). |

## Minimal `llvm-mca` reproducer

```sh
llvm-mca -mtriple=x86_64-linux-gnu -mcpu=znver5 -iterations=100 <<'ASM'
.intel_syntax noprefix
  vpaddd zmm0, zmm0, zmm8
  vpaddd zmm1, zmm1, zmm8
  vpaddd zmm2, zmm2, zmm8
  vpaddd zmm3, zmm3, zmm8
  vpaddd zmm4, zmm4, zmm8
  vpaddd zmm5, zmm5, zmm8
  vpaddd zmm6, zmm6, zmm8
  vpaddd zmm7, zmm7, zmm8
  vpaddd zmm0, zmm0, zmm8
  vpaddd zmm1, zmm1, zmm8
  vpaddd zmm2, zmm2, zmm8
  vpaddd zmm3, zmm3, zmm8
  vpaddd zmm4, zmm4, zmm8
  vpaddd zmm5, zmm5, zmm8
  vpaddd zmm6, zmm6, zmm8
  vpaddd zmm7, zmm7, zmm8
  add rcx, 1
  cmp rcx, rdi
  jb 0
ASM
```

- `llvm-mca -mcpu=znver5` reports **`8.04` cycles/iter** (same as `-mcpu=znver4`).
- Bare-metal AMD EPYC 9255 and AMD Ryzen 7 9700X both measure **`4.01` cycles/iter**.

Full 25-kernel JSON datasets (`results/amd-epyc-9255-zen5.json`,
`results/amd-ryzen-9700x-zen5.json`, `results/amd-threadripper-7975wx-zen4.json`)
and the zero-dependency reproducer script (`probe.py`) are attached / linked in
our standalone repository.
