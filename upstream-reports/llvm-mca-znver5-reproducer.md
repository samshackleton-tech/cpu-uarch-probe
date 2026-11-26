# Upstream issue for `llvm/llvm-project` (`[X86][llvm-mca]`)

**Title:** `[X86][llvm-mca] Add dedicated Znver5 scheduling model (znver5 currently reuses Znver4Model at X86.td:2242; 32% within 10% across 25 bare-metal Zen 5 kernels)`

## Summary

On `llvm/llvm-project` `main`, `llvm/lib/Target/X86/X86.td` (line 2242) still
maps `"znver5"` to `Znver4Model`:

```tablegen
def : ProcModel<"znver4", Znver4Model, ProcessorFeatures.ZN4Features, ...>;
def : ProcModel<"znver5", Znver4Model, ProcessorFeatures.ZN5Features, ...>;
```

We benchmarked 25 loop kernels (real cryptographic/hash bodies, targeted
pipeline probes, and 12 reproducible randomized instruction mixes) under
bare-metal Linux `perf_event_open(PERF_COUNT_HW_CPU_CYCLES)` on both an **AMD
EPYC 9255** (Zen 5 server) and an **AMD Ryzen 7 9700X** (Zen 5 desktop), and
compared against `llvm-mca -mcpu=znver5` (Ubuntu LLVM 21.1.8).

Across the 25 kernels, `llvm-mca -mcpu=znver5` predicts only **8/25 (32.0%)**
within 10% of measured hardware cycles (**14.5% median**, **28.8% mean**
absolute error), compared to **15/25 (60%)** for `-mcpu=icelake-server` and
**22/24 (92%)** for `-mcpu=ampere1` on the same benchmark methodology.

## Key Zen 5 vs. Zen 4 differences isolated on bare-metal EPYC 9255

The table below shows the largest systematic gaps between `llvm-mca -mcpu=znver5`
(using `Znver4Model`) and bare-metal `PERF_COUNT_HW_CPU_CYCLES` on AMD EPYC
9255 (identical within 0.1% on Ryzen 7 9700X for all rows shown).

| Kernel | Description | Measured (EPYC 9255) | `llvm-mca -mcpu=znver5` | Error | Root cause isolated by micro-probes |
| --- | --- | ---: | ---: | ---: | --- |
| `real_sse2_g_round` | 128-bit BLAKE3 G-round (`vpaddd`/`vpxor`/`vpslld`/`vpsrld`/`vpor` XMM) | `33.80` cyc | `16.05` cyc | **-52.5%** | Zen 5 executes 128-bit integer vector ALU/logic/shift ops at `2.0` cycles latency (vs. `1.0` on Zen 4, where this exact loop measures `16.02` cyc). |
| `probe_zmm_b3_g4` | 4-col 512-bit BLAKE3 G (`24 vpaddd + 16 vpxord + 16 vprord` ZMM) | `29.87` cyc | `16.05` cyc | **-46.3%** | Bare-metal serial probes on EPYC 9255 measure `2.000` cyc latency for all three of `VPADDD`, `VPXORD`, and `VPRORD (ZMM)` (`g1_14ops` = `28.002` cyc; `g4_56ops` = `30.099` cyc). |
| `probe_zmm_vpaddd` | 16 independent `vpaddd zmm` | `4.01` cyc | `8.04` cyc | **+100.5%** | `Znver4Model` double-pumps `VPADDD (ZMM)` at `0.50` cyc/op (`8.80` cyc on Zen 4), whereas EPYC 9255 & 9700X execute native 512-bit at `4.00` ops/cyc (`4.01` cyc). |
| `probe_zmm_vprord` | 16 independent `vprord zmm, zmm, 7` | `8.00` cyc | `16.04` cyc | **+100.5%** | `Znver4Model` predicts `1.00` cyc/op (`16.02` cyc on Zen 4), whereas both Zen 5 CPUs execute `2.00` ops/cyc (`8.00` cyc). |
| `probe_alu_wide` | 12 independent scalar `add` + loop | `3.01` cyc | `2.36` cyc | **-21.6%** | Taken loop back-edge closes op-cache dispatch window on Zen 5 (`3.01`, `2.01`, `6.00` cyc across multiple loops). |
| `probe_load_dep` | 8 scalar loads + adds + loop | `2.20` cyc | `3.00` cyc | **+36.4%** | Zen 5 has 4 L1D load AGUs (vs. 3 in `Znver4Model`). |

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

Full 25-kernel bare-metal JSON datasets (`results/amd-epyc-9255-zen5.json`,
`results/amd-ryzen-9700x-zen5.json`, `results/amd-threadripper-7975wx-zen4.json`)
and the zero-dependency reproducer (`probe.py`, MIT / Apache-2.0 / TGPPL-1.0 /
BOSL-1.0) are published at https://github.com/samshackleton-tech/cpu-uarch-probe.
