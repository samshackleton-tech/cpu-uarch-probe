#!/usr/bin/env python3
"""probe.py: standalone, zero-dependency CPU microarchitecture pipeline probe and
validation suite for x86-64 (AVX2 / AVX-512) and AArch64 (NEON).

Measures exact instruction latencies, front-end/fusion/rename behavior, execution port
widths, real cryptographic/hash loop bodies, and reproducible randomized instruction mixes
on real silicon, then compares hardware cycles against `sim.py` (our uiCA-style static
out-of-order model) and `llvm-mca` (when installed).

Zero external dependencies: requires only Python 3 and a C compiler (`cc`).
  - Linux (x86-64 / AArch64): uses `perf_event_open(PERF_COUNT_HW_CPU_CYCLES)` when
    exposed by the kernel, automatically falling back to an interleaved 64-op serial
    `add`-chain clock reference on VMs without vPMU.
  - macOS (Apple Silicon): pins to a Performance/Super core via
    `pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)` and uses the
    interleaved 64-op `add`-chain clock reference via `CLOCK_UPTIME_RAW`, verified
    against an independent `orr`-chain cross-check.

Usage:
    python3 probe.py --run
    python3 probe.py --run --arch zen5 --mcpu znver5 --json-out results/my-cpu.json
"""
import argparse, datetime, json, os, platform, random, re, statistics, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import sim


def op(row, cls, asm, reads, writes):
    return {"pc": 0, "end": 4, "len": 4, "row": row, "class": cls, "asm": asm,
            "reads": set(reads), "writes": set(writes)}


def _finalize_pcs(ops):
    out = []
    pc = 0
    for it in ops:
        x = dict(it)
        x["pc"] = pc
        x["end"] = pc + 4
        out.append(x)
        pc += 4
    return out


# ==============================================================================
# X86-64 KERNEL GENERATORS (pure GNU/Clang .intel_syntax noprefix)
# ABI: uint64_t k_NAME(uint64_t iters /* rdi */, uint8_t *buf /* rsi */)
# ==============================================================================

def _x86_loop(name, kind, init_asm, body_ops, tail_asm=None, epilogue_asm=None, div=1.0):
    loop_ops = list(body_ops) + [
        op("add64_ri", "alu", "add rcx, 1", ["rcx"], ["rcx", "zf", "cf"]),
        op("cmp64_rr", "alu", "cmp rcx, rdi", ["rcx", "rdi"], ["zf", "cf"]),
        op("jb", "branch", "jb 1b", ["cf"], []),
    ]
    return {
        "name": name, "kind": kind, "div": div,
        "init": list(init_asm) + ["xor ecx, ecx"],
        "loop_ops": _finalize_pcs(loop_ops),
        "epilogue": list(epilogue_asm or []) + ["ret"],
    }


def build_x86_kernels(include_avx512=True):
    lat_probes = []
    lat_probes.append(_x86_loop(
        "lat_add_gpr", "lat", ["mov rax, 1", "mov rdx, 3"],
        [op("add64_rr", "alu", "add rax, rdx", ["rax", "rdx"], ["rax", "zf", "cf"]) for _ in range(16)],
        div=16.0,
    ))
    lat_probes.append(_x86_loop(
        "lat_or_gpr", "lat", ["mov rax, 1", "mov rdx, 3"],
        [op("or64_rr", "alu", "or rax, rdx", ["rax", "rdx"], ["rax", "zf", "cf"]) for _ in range(16)],
        div=16.0,
    ))
    lat_probes.append(_x86_loop(
        "lat_vpaddd_xmm", "lat",
        ["vmovdqu xmm0, xmmword ptr [rsi]", "vmovdqu xmm1, xmmword ptr [rsi + 16]"],
        [op("paddd128", "vec_alu", "vpaddd xmm0, xmm0, xmm1", ["v0", "v1"], ["v0"]) for _ in range(16)],
        epilogue_asm=["vmovdqu xmmword ptr [rsi + 1024], xmm0", "vzeroupper"], div=16.0,
    ))
    lat_probes.append(_x86_loop(
        "lat_vpxor_xmm", "lat",
        ["vmovdqu xmm0, xmmword ptr [rsi]", "vmovdqu xmm1, xmmword ptr [rsi + 16]"],
        [op("pxor128", "vec_alu", "vpxor xmm0, xmm0, xmm1", ["v0", "v1"], ["v0"]) for _ in range(16)],
        epilogue_asm=["vmovdqu xmmword ptr [rsi + 1024], xmm0", "vzeroupper"], div=16.0,
    ))
    if include_avx512:
        lat_probes.append(_x86_loop(
            "lat_vpaddd_zmm", "lat",
            ["vmovdqu32 zmm0, zmmword ptr [rsi]", "vmovdqu32 zmm8, zmmword ptr [rsi + 64]"],
            [op("paddd512", "wide_alu", "vpaddd zmm0, zmm0, zmm8", ["v0", "v8"], ["v0"]) for _ in range(16)],
            epilogue_asm=["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"], div=16.0,
        ))
        lat_probes.append(_x86_loop(
            "lat_vpxord_zmm", "lat",
            ["vmovdqu32 zmm0, zmmword ptr [rsi]", "vmovdqu32 zmm8, zmmword ptr [rsi + 64]"],
            [op("pxord512", "wide_xor", "vpxord zmm0, zmm0, zmm8", ["v0", "v8"], ["v0"]) for _ in range(16)],
            epilogue_asm=["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"], div=16.0,
        ))
        lat_probes.append(_x86_loop(
            "lat_vprord_zmm", "lat",
            ["vmovdqu32 zmm0, zmmword ptr [rsi]"],
            [op("prord512", "wide_shift", "vprord zmm0, zmm0, 7", ["v0"], ["v0"]) for _ in range(16)],
            epilogue_asm=["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"], div=16.0,
        ))

    eval_k = []
    # Targeted probes
    eval_k.append(_x86_loop(
        "probe_fusion_cmp", "probe", ["xor eax, eax"],
        [op("add64_ri", "alu", "add rax, 1", ["rax"], ["rax", "zf", "cf"])],
    ))
    eval_k.append({
        "name": "probe_nofusion_sep", "kind": "probe", "div": 1.0,
        "init": ["xor eax, eax", "xor ecx, ecx"],
        "loop_ops": _finalize_pcs([
            op("add64_ri", "alu", "add rcx, 1", ["rcx"], ["rcx", "zf", "cf"]),
            op("cmp64_rr", "alu", "cmp rcx, rdi", ["rcx", "rdi"], ["zf", "cf"]),
            op("lea", "lea", "lea rax, [rax + 1]", ["rax"], ["rax"]),
            op("jb", "branch", "jb 1b", ["cf"], []),
        ]),
        "epilogue": ["ret"],
    })
    mov_pairs = [("rax", "r8"), ("rdx", "r9"), ("r8", "r10"), ("r9", "r11"), ("r10", "rax"), ("r11", "rdx")] * 2
    eval_k.append(_x86_loop(
        "probe_mov_elim", "probe", ["mov r8, 1", "mov r9, 2", "mov r10, 3", "mov r11, 4"],
        [op("mov64_rr", "mov_rr", f"mov {d}, {s}", [s], [d]) for d, s in mov_pairs],
        epilogue_asm=["mov rax, r8"],
    ))
    alu_regs = ["rax", "rdx", "r8", "r9", "r10", "r11"] * 2
    eval_k.append(_x86_loop(
        "probe_alu_wide", "probe",
        ["xor eax, eax", "xor edx, edx", "xor r8d, r8d", "xor r9d, r9d", "xor r10d, r10d", "xor r11d, r11d"],
        [op("add64_ri", "alu", f"add {r}, {i+1}", [r], [r, "zf", "cf"]) for i, r in enumerate(alu_regs)],
    ))
    eval_k.append(_x86_loop(
        "probe_dep_chain", "probe", ["mov rax, 1"],
        [op("add64_ri", "alu", f"add rax, {2*i+1}", ["rax"], ["rax", "zf", "cf"]) for i in range(10)],
    ))
    ld_regs = ["rax", "rdx", "r8", "r9", "rax", "rdx", "r8", "r9"]
    eval_k.append(_x86_loop(
        "probe_load_mix", "probe", ["xor eax, eax", "xor edx, edx", "xor r8d, r8d", "xor r9d, r9d"],
        [op("add64_rm", "alu_mem", f"add {r}, qword ptr [rsi + {i*8}]", [r, "rsi"], [r, "zf", "cf"]) for i, r in enumerate(ld_regs)],
    ))
    eval_k.append(_x86_loop(
        "probe_self_mov_dep", "probe", ["mov rax, 1", "mov rdx, 2"],
        [
            op("shr64_ri", "alu", "shr rdx, 1", ["rdx"], ["rdx", "zf", "cf"]),
            op("mov64_rr", "mov_rr", "mov rdx, rdx", ["rdx"], ["rdx"]),
            op("add64_rr", "alu", "add rdx, rax", ["rdx", "rax"], ["rdx", "zf", "cf"]),
            op("shl64_ri", "alu", "shl rdx, 1", ["rdx"], ["rdx", "zf", "cf"]),
        ],
    ))
    # Real SSE2/AVX BLAKE3 G-round (using VEX vpxor so it runs on Zen 3 as well as Zen 4/5/ICL)
    g_ops = []
    for (m_reg, r1, r2) in (("xmm4", 16, 12), ("xmm5", 8, 7)):
        g_ops += [
            op("paddd128", "vec_alu", f"vpaddd xmm0, xmm0, {m_reg}", ["v0", f"v{m_reg[3:]}"], ["v0"]),
            op("paddd128", "vec_alu", "vpaddd xmm0, xmm0, xmm1", ["v0", "v1"], ["v0"]),
            op("pxor128", "vec_alu", "vpxor xmm3, xmm3, xmm0", ["v3", "v0"], ["v3"]),
            op("mov128", "vec_alu", "vmovdqa xmm6, xmm3", ["v3"], ["v6"]),
            op("pslld128", "vec_shift", f"vpslld xmm3, xmm3, {32-r1}", ["v3"], ["v3"]),
            op("psrld128", "vec_shift", f"vpsrld xmm6, xmm6, {r1}", ["v6"], ["v6"]),
            op("por128", "vec_alu", "vpor xmm3, xmm3, xmm6", ["v3", "v6"], ["v3"]),
            op("paddd128", "vec_alu", "vpaddd xmm2, xmm2, xmm3", ["v2", "v3"], ["v2"]),
            op("pxor128", "vec_alu", "vpxor xmm1, xmm1, xmm2", ["v1", "v2"], ["v1"]),
            op("mov128", "vec_alu", "vmovdqa xmm6, xmm1", ["v1"], ["v6"]),
            op("pslld128", "vec_shift", f"vpslld xmm1, xmm1, {32-r2}", ["v1"], ["v1"]),
            op("psrld128", "vec_shift", f"vpsrld xmm6, xmm6, {r2}", ["v6"], ["v6"]),
            op("por128", "vec_alu", "vpor xmm1, xmm1, xmm6", ["v1", "v6"], ["v1"]),
        ]
    eval_k.append(_x86_loop(
        "real_sse2_g_round", "real",
        [f"vmovdqu xmm{i}, xmmword ptr [rsi + {i*16}]" for i in range(6)],
        g_ops, epilogue_asm=["vmovdqu xmmword ptr [rsi + 1024], xmm0", "vzeroupper"],
    ))

    if include_avx512:
        z_init = [f"vmovdqu32 zmm{i}, zmmword ptr [rsi + {(i%8)*64}]" for i in range(17)]
        eval_k.append(_x86_loop(
            "probe_zmm_vpaddd", "probe512", z_init[:9],
            [op("paddd512", "wide_alu", f"vpaddd zmm{i%8}, zmm{i%8}, zmm8", [f"v{i%8}", "v8"], [f"v{i%8}"]) for i in range(16)],
            epilogue_asm=["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"],
        ))
        eval_k.append(_x86_loop(
            "probe_zmm_vpxord", "probe512", z_init[:9],
            [op("pxord512", "wide_xor", f"vpxord zmm{i%8}, zmm{i%8}, zmm8", [f"v{i%8}", "v8"], [f"v{i%8}"]) for i in range(16)],
            epilogue_asm=["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"],
        ))
        eval_k.append(_x86_loop(
            "probe_zmm_vprord", "probe512", z_init[:8],
            [op("prord512", "wide_shift", f"vprord zmm{i%8}, zmm{i%8}, 7", [f"v{i%8}"], [f"v{i%8}"]) for i in range(16)],
            epilogue_asm=["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"],
        ))
        # 4-column BLAKE3 ZMM half-round (56 ops)
        g4 = []
        for (r1, r2) in ((16, 12), (8, 7)):
            for c in range(4):
                g4.append(op("paddd512", "wide_alu", f"vpaddd zmm{c}, zmm{c}, zmm{4+c}", [f"v{c}", f"v{4+c}"], [f"v{c}"]))
            for c in range(4):
                g4.append(op("paddd512", "wide_alu", f"vpaddd zmm{c}, zmm{c}, zmm16", [f"v{c}", "v16"], [f"v{c}"]))
            for c in range(4):
                g4.append(op("pxord512", "wide_xor", f"vpxord zmm{12+c}, zmm{12+c}, zmm{c}", [f"v{12+c}", f"v{c}"], [f"v{12+c}"]))
            for c in range(4):
                g4.append(op("prord512", "wide_shift", f"vprord zmm{12+c}, zmm{12+c}, {r1}", [f"v{12+c}"], [f"v{12+c}"]))
            for c in range(4):
                g4.append(op("paddd512", "wide_alu", f"vpaddd zmm{8+c}, zmm{8+c}, zmm{12+c}", [f"v{8+c}", f"v{12+c}"], [f"v{8+c}"]))
            for c in range(4):
                g4.append(op("pxord512", "wide_xor", f"vpxord zmm{4+c}, zmm{4+c}, zmm{8+c}", [f"v{4+c}", f"v{8+c}"], [f"v{4+c}"]))
            for c in range(4):
                g4.append(op("prord512", "wide_shift", f"vprord zmm{4+c}, zmm{4+c}, {r2}", [f"v{4+c}"], [f"v{4+c}"]))
        eval_k.append(_x86_loop(
            "probe_zmm_b3_g4", "real512", z_init,
            g4, epilogue_asm=["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"],
        ))

    # 10 reproducible randomized kernels
    rng = random.Random(20261126)
    gprs = ["rax", "rdx", "r8", "r9", "r10", "r11"]
    xmms = [f"xmm{i}" for i in range(8)]
    zmms = [f"zmm{i}" for i in range(8)]
    profs = [
        ("rand_00_scalar", "scalar", 12),
        ("rand_01_scalar_mem", "scalar_mem", 16),
        ("rand_02_sse", "sse", 18),
        ("rand_03_mixed", "mixed", 18),
        ("rand_04_avx512" if include_avx512 else "rand_04_scalar", "avx512" if include_avx512 else "scalar", 24),
        ("rand_05_scalar", "scalar", 18),
        ("rand_06_scalar_mem", "scalar_mem", 16),
        ("rand_07_sse", "sse", 24),
        ("rand_08_mixed", "mixed", 22),
        ("rand_09_avx512_mix" if include_avx512 else "rand_09_sse", "avx512_mix" if include_avx512 else "sse", 16),
    ]
    for name, prof, n_ops in profs:
        init = [f"mov {r}, {i+1}" for i, r in enumerate(gprs)]
        epi = []
        if prof in ("sse", "mixed"):
            init += [f"vmovdqu xmm{i}, xmmword ptr [rsi + {i*16}]" for i in range(8)]
            epi = ["vmovdqu xmmword ptr [rsi + 1024], xmm0", "vzeroupper"]
        elif prof in ("avx512", "avx512_mix"):
            init += [f"vmovdqu32 zmm{i}, zmmword ptr [rsi + {i*64}]" for i in range(8)]
            epi = ["vmovdqu32 zmmword ptr [rsi + 1024], zmm0", "vzeroupper"]
        body = []
        for _ in range(n_ops):
            k = prof
            if k == "mixed":
                k = rng.choice(["scalar", "scalar_mem", "sse"])
            elif k == "avx512_mix":
                k = rng.choice(["scalar", "avx512"])
            if k == "scalar":
                d, s = rng.choice(gprs), rng.choice(gprs)
                kind_op = rng.choice(["add", "sub", "or", "shl", "shr", "lea", "mov"])
                if kind_op == "add": body.append(op("add64_rr", "alu", f"add {d}, {s}", [d, s], [d, "zf", "cf"]))
                elif kind_op == "sub": body.append(op("sub64_rr", "alu", f"sub {d}, {s}", [d, s], [d, "zf", "cf"]))
                elif kind_op == "or": body.append(op("or64_rr", "alu", f"or {d}, {s}", [d, s], [d, "zf", "cf"]))
                elif kind_op == "shl": body.append(op("shl64_ri", "alu", f"shl {d}, {rng.choice([1,2,3,5])}", [d], [d, "zf", "cf"]))
                elif kind_op == "shr": body.append(op("shr64_ri", "alu", f"shr {d}, {rng.choice([1,2,3,5])}", [d], [d, "zf", "cf"]))
                elif kind_op == "lea": body.append(op("lea", "lea", f"lea {d}, [{s} + {rng.randint(1,32)}]", [s], [d]))
                else: body.append(op("mov64_rr", "mov_rr", f"mov {d}, {s}", [s], [d]))
            elif k == "scalar_mem":
                d = rng.choice(gprs)
                off = rng.randint(0, 15) * 8
                if rng.random() < 0.65:
                    body.append(op("add64_rm", "alu_mem", f"add {d}, qword ptr [rsi + {off}]", [d, "rsi"], [d, "zf", "cf"]))
                else:
                    body.append(op("mov64_rm", "load", f"mov {d}, qword ptr [rsi + {off}]", ["rsi"], [d]))
            elif k == "sse":
                d, s1, s2 = rng.choice(xmms), rng.choice(xmms), rng.choice(xmms)
                vd, vs1, vs2 = f"v{d[3:]}", f"v{s1[3:]}", f"v{s2[3:]}"
                kind_op = rng.choice(["vpaddd", "vpxor", "vpslld", "vpsrld", "vmovdqa"])
                if kind_op == "vpaddd": body.append(op("paddd128", "vec_alu", f"vpaddd {d}, {s1}, {s2}", [vs1, vs2], [vd]))
                elif kind_op == "vpxor": body.append(op("pxor128", "vec_alu", f"vpxor {d}, {s1}, {s2}", [vs1, vs2], [vd]))
                elif kind_op == "vpslld": body.append(op("pslld128", "vec_shift", f"vpslld {d}, {s1}, {rng.choice([3,7,12])}", [vs1], [vd]))
                elif kind_op == "vpsrld": body.append(op("psrld128", "vec_shift", f"vpsrld {d}, {s1}, {rng.choice([3,7,12])}", [vs1], [vd]))
                else: body.append(op("mov128", "vec_alu", f"vmovdqa {d}, {s1}", [vs1], [vd]))
            elif k == "avx512":
                d, s1, s2 = rng.choice(zmms), rng.choice(zmms), rng.choice(zmms)
                vd, vs1, vs2 = f"v{d[3:]}", f"v{s1[3:]}", f"v{s2[3:]}"
                kind_op = rng.choice(["vpaddd", "vpxord", "vprord", "vmovdqa32"])
                if kind_op == "vpaddd": body.append(op("paddd512", "wide_alu", f"vpaddd {d}, {s1}, {s2}", [vs1, vs2], [vd]))
                elif kind_op == "vpxord": body.append(op("pxord512", "wide_xor", f"vpxord {d}, {s1}, {s2}", [vs1, vs2], [vd]))
                elif kind_op == "vprord": body.append(op("prord512", "wide_shift", f"vprord {d}, {s1}, {rng.choice([7,8,12,16])}", [vs1], [vd]))
                else: body.append(op("mov512", "wide_mov", f"vmovdqa32 {d}, {s1}", [vs1], [vd]))
        eval_k.append(_x86_loop(name, f"rand:{prof}", init, body, epilogue_asm=epi))

    return lat_probes, eval_k


# ==============================================================================
# AARCH64 KERNEL GENERATORS (pure standard AArch64 assembly, ELF + Mach-O)
# ABI: uint64_t k_NAME(uint64_t iters /* x0 */, uint8_t *buf /* x1 */)
# Caller-saved registers only: x2..x15, v0..v7, v16..v24 (never callee-saved v8..v15!)
# ==============================================================================

def _arm_loop(name, kind, init_asm, body_ops, epilogue_asm=None, div=1.0):
    loop_ops = list(body_ops) + [
        op("add_rri", "alu", "add x2, x2, #1", ["x2"], ["x2"]),
        op("cmp_rr", "alu", "cmp x2, x0", ["x2", "x0"], ["zf", "cf"]),
        op("b_lo", "branch", "b.lo 1b", ["cf"], []),
    ]
    return {
        "name": name, "kind": kind, "div": div,
        "init": list(init_asm) + ["mov x2, #0"],
        "loop_ops": _finalize_pcs(loop_ops),
        "epilogue": list(epilogue_asm or []) + ["mov x0, x3", "ret"],
    }


def build_arm64_kernels():
    lat_probes = [
        _arm_loop("lat_add_gpr", "lat", ["mov x3, #1", "mov x4, #3"],
                  [op("add_rrr", "alu", "add x3, x3, x4", ["x3", "x4"], ["x3"]) for _ in range(16)], div=16.0),
        _arm_loop("lat_orr_gpr", "lat", ["mov x3, #1", "mov x4, #3"],
                  [op("orr_rrr", "alu", "orr x3, x3, x4", ["x3", "x4"], ["x3"]) for _ in range(16)], div=16.0),
        _arm_loop("lat_vec_add4s", "lat", ["ldr q0, [x1, #0]", "ldr q1, [x1, #16]"],
                  [op("add_4s", "vec_alu", "add v0.4s, v0.4s, v1.4s", ["v0", "v1"], ["v0"]) for _ in range(16)],
                  epilogue_asm=["str q0, [x1, #1024]"], div=16.0),
        _arm_loop("lat_vec_eor", "lat", ["ldr q0, [x1, #0]", "ldr q1, [x1, #16]"],
                  [op("eor_v", "vec_alu", "eor v0.16b, v0.16b, v1.16b", ["v0", "v1"], ["v0"]) for _ in range(16)],
                  epilogue_asm=["str q0, [x1, #1024]"], div=16.0),
        _arm_loop("lat_vec_ushr", "lat", ["ldr q0, [x1, #0]"],
                  [op("ushr_4s", "vec_shift", "ushr v0.4s, v0.4s, #7", ["v0"], ["v0"]) for _ in range(16)],
                  epilogue_asm=["str q0, [x1, #1024]"], div=16.0),
        _arm_loop("lat_vec_rev32", "lat", ["ldr q0, [x1, #0]"],
                  [op("rev32_8h", "vec_shuf", "rev32 v0.8h, v0.8h", ["v0"], ["v0"]) for _ in range(16)],
                  epilogue_asm=["str q0, [x1, #1024]"], div=16.0),
        _arm_loop("lat_vec_ext", "lat", ["ldr q0, [x1, #0]"],
                  [op("ext_4", "vec_shuf", "ext v0.16b, v0.16b, v0.16b, #4", ["v0"], ["v0"]) for _ in range(16)],
                  epilogue_asm=["str q0, [x1, #1024]"], div=16.0),
    ]

    eval_k = []
    eval_k.append(_arm_loop("probe_cmp_fusion", "probe", ["mov x3, #0"],
                            [op("add_rri", "alu", "add x3, x3, #1", ["x3"], ["x3"])]))
    eval_k.append({
        "name": "probe_nofusion_sep", "kind": "probe", "div": 1.0,
        "init": ["mov x3, #0", "mov x2, #0"],
        "loop_ops": _finalize_pcs([
            op("add_rri", "alu", "add x2, x2, #1", ["x2"], ["x2"]),
            op("cmp_rr", "alu", "cmp x2, x0", ["x2", "x0"], ["zf", "cf"]),
            op("add_rri", "alu", "add x3, x3, #1", ["x3"], ["x3"]),
            op("b_lo", "branch", "b.lo 1b", ["cf"], []),
        ]),
        "epilogue": ["mov x0, x3", "ret"],
    })
    mov_p = [("x8", "x4"), ("x9", "x5"), ("x4", "x6"), ("x5", "x7"), ("x6", "x8"), ("x7", "x9")] * 2
    eval_k.append(_arm_loop("probe_mov_elim", "probe", ["mov x4, #1", "mov x5, #2", "mov x6, #3", "mov x7, #4"],
                            [op("mov_rr", "mov_rr", f"mov {d}, {s}", [s], [d]) for d, s in mov_p]))
    alu_r = ["x3", "x4", "x5", "x6", "x7", "x8"] * 2
    eval_k.append(_arm_loop("probe_alu_wide", "probe", [f"mov {r}, #{i+1}" for i, r in enumerate(alu_r[:6])],
                            [op("add_rri", "alu", f"add {r}, {r}, #{i+1}", [r], [r]) for i, r in enumerate(alu_r)]))
    eval_k.append(_arm_loop("probe_dep_chain", "probe", ["mov x3, #1"],
                            [op("add_rri", "alu", f"add x3, x3, #{2*i+1}", ["x3"], ["x3"]) for i in range(10)]))
    ld_ops = []
    for base in (0, 32):
        for j, (dr, ar) in enumerate([("x7", "x3"), ("x8", "x4"), ("x9", "x5"), ("x10", "x6")]):
            ld_ops.append(op("ldr64", "load", f"ldr {dr}, [x1, #{base + j*8}]", ["x1"], [dr]))
        for dr, ar in [("x7", "x3"), ("x8", "x4"), ("x9", "x5"), ("x10", "x6")]:
            ld_ops.append(op("add_rrr", "alu", f"add {ar}, {ar}, {dr}", [ar, dr], [ar]))
    eval_k.append(_arm_loop("probe_load_mix", "probe", ["mov x3, #0", "mov x4, #0", "mov x5, #0", "mov x6, #0"], ld_ops))

    v8_init = [f"ldr q{i}, [x1, #{i*16}]" for i in range(8)] + ["ldr q16, [x1, #128]"]
    eval_k.append(_arm_loop("probe_vec_add_8reg", "probe", v8_init,
                            [op("add_4s", "vec_alu", f"add v{i%8}.4s, v{i%8}.4s, v16.4s", [f"v{i%8}", "v16"], [f"v{i%8}"]) for i in range(16)],
                            epilogue_asm=["str q0, [x1, #1024]"]))
    v16_regs = list(range(8)) + list(range(16, 24))
    v16_init = [f"ldr q{r}, [x1, #{(r%8)*16}]" for r in v16_regs] + ["ldr q24, [x1, #128]"]
    eval_k.append(_arm_loop("probe_vec_add_16reg", "probe", v16_init,
                            [op("add_4s", "vec_alu", f"add v{r}.4s, v{r}.4s, v24.4s", [f"v{r}", "v24"], [f"v{r}"]) for r in v16_regs],
                            epilogue_asm=["str q0, [x1, #1024]"]))
    eval_k.append(_arm_loop("probe_vec_shf_wide", "probe", v8_init[:8],
                            [op("ushr_4s", "vec_shift", f"ushr v{i}.4s, v{i}.4s, #7", [f"v{i}"], [f"v{i}"]) for i in range(8)] +
                            [op("shl_4s", "vec_shift", f"shl v{i}.4s, v{i}.4s, #3", [f"v{i}"], [f"v{i}"]) for i in range(8)],
                            epilogue_asm=["str q0, [x1, #1024]"]))
    eval_k.append(_arm_loop("probe_vec_ext_wide", "probe", v8_init[:8],
                            [op("ext_4", "vec_shuf", f"ext v{i}.16b, v{i}.16b, v{i}.16b, #4", [f"v{i}"], [f"v{i}"]) for i in range(8)] +
                            [op("ext_8", "vec_shuf", f"ext v{i}.16b, v{i}.16b, v{i}.16b, #8", [f"v{i}"], [f"v{i}"]) for i in range(8)],
                            epilogue_asm=["str q0, [x1, #1024]"]))

    # Real BLAKE3 NEON G-column (20 vector ops)
    g1_ops = [
        op("add_4s", "vec_alu", "add v0.4s, v0.4s, v1.4s", ["v0", "v1"], ["v0"]),
        op("add_4s", "vec_alu", "add v0.4s, v0.4s, v4.4s", ["v0", "v4"], ["v0"]),
        op("eor_v", "vec_alu", "eor v3.16b, v3.16b, v0.16b", ["v3", "v0"], ["v3"]),
        op("rev32_8h", "vec_shuf", "rev32 v3.8h, v3.8h", ["v3"], ["v3"]),
        op("add_4s", "vec_alu", "add v2.4s, v2.4s, v3.4s", ["v2", "v3"], ["v2"]),
        op("eor_v", "vec_alu", "eor v1.16b, v1.16b, v2.16b", ["v1", "v2"], ["v1"]),
        op("ushr_4s", "vec_shift", "ushr v6.4s, v1.4s, #12", ["v1"], ["v6"]),
        op("shl_4s", "vec_shift", "shl v1.4s, v1.4s, #20", ["v1"], ["v1"]),
        op("orr_v", "vec_alu", "orr v1.16b, v1.16b, v6.16b", ["v1", "v6"], ["v1"]),
        op("add_4s", "vec_alu", "add v0.4s, v0.4s, v1.4s", ["v0", "v1"], ["v0"]),
        op("add_4s", "vec_alu", "add v0.4s, v0.4s, v5.4s", ["v0", "v5"], ["v0"]),
        op("eor_v", "vec_alu", "eor v3.16b, v3.16b, v0.16b", ["v3", "v0"], ["v3"]),
        op("ushr_4s", "vec_shift", "ushr v6.4s, v3.4s, #8", ["v3"], ["v6"]),
        op("shl_4s", "vec_shift", "shl v3.4s, v3.4s, #24", ["v3"], ["v3"]),
        op("orr_v", "vec_alu", "orr v3.16b, v3.16b, v6.16b", ["v3", "v6"], ["v3"]),
        op("add_4s", "vec_alu", "add v2.4s, v2.4s, v3.4s", ["v2", "v3"], ["v2"]),
        op("eor_v", "vec_alu", "eor v1.16b, v1.16b, v2.16b", ["v1", "v2"], ["v1"]),
        op("ushr_4s", "vec_shift", "ushr v6.4s, v1.4s, #7", ["v1"], ["v6"]),
        op("shl_4s", "vec_shift", "shl v1.4s, v1.4s, #25", ["v1"], ["v1"]),
        op("orr_v", "vec_alu", "orr v1.16b, v1.16b, v6.16b", ["v1", "v6"], ["v1"]),
    ]
    eval_k.append(_arm_loop("real_neon_b3_g1", "real", v8_init[:6], g1_ops, epilogue_asm=["str q0, [x1, #1024]"]))

    # 10 reproducible randomized AArch64 + NEON mixes
    rng = random.Random(20261126)
    gprs = ["x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10"]
    vecs = [f"v{i}" for i in range(8)]
    profs = [
        ("rand_00_scalar", "scalar", 12),
        ("rand_01_scalar_mem", "scalar_mem", 14),
        ("rand_02_neon", "neon", 14),
        ("rand_03_neon_heavy", "neon", 22),
        ("rand_04_mixed", "mixed", 18),
        ("rand_05_scalar", "scalar", 18),
        ("rand_06_scalar_mem", "scalar_mem", 16),
        ("rand_07_neon", "neon", 18),
        ("rand_08_mixed", "mixed", 24),
        ("rand_09_neon_mem", "neon_mem", 16),
    ]
    for name, prof, n_ops in profs:
        init = [f"mov {r}, #{i+1}" for i, r in enumerate(gprs)]
        epi = []
        if prof in ("neon", "mixed", "neon_mem"):
            init += [f"ldr q{i}, [x1, #{i*16}]" for i in range(8)]
            epi = ["str q0, [x1, #1024]"]
        body = []
        for _ in range(n_ops):
            k = prof
            if k == "mixed":
                k = rng.choice(["scalar", "scalar_mem", "neon", "neon_mem"])
            if k == "scalar":
                d, s1, s2 = rng.choice(gprs), rng.choice(gprs), rng.choice(gprs)
                kind_op = rng.choice(["add", "sub", "orr", "and", "lsl", "lsr", "mov"])
                if kind_op in ("lsl", "lsr"):
                    body.append(op(f"{kind_op}_ri", "alu", f"{kind_op} {d}, {s1}, #{rng.choice([1,2,3,5,7])}", [s1], [d]))
                elif kind_op == "mov":
                    body.append(op("mov_rr", "mov_rr", f"mov {d}, {s1}", [s1], [d]))
                else:
                    body.append(op(f"{kind_op}_rrr", "alu", f"{kind_op} {d}, {s1}, {s2}", [s1, s2], [d]))
            elif k == "scalar_mem":
                d = rng.choice(gprs)
                off = rng.choice([0, 8, 16, 24, 32, 40, 48, 56, 64, 72])
                if rng.random() < 0.72:
                    body.append(op("ldr64", "load", f"ldr {d}, [x1, #{off}]", ["x1"], [d]))
                else:
                    s = rng.choice(gprs)
                    body.append(op("add_rrr", "alu", f"add {d}, {d}, {s}", [d, s], [d]))
            elif k == "neon":
                d, s1, s2 = rng.choice(vecs), rng.choice(vecs), rng.choice(vecs)
                kind_op = rng.choice(["add4s", "eor", "orr", "ushr", "shl", "rev32", "ext", "uzp1"])
                if kind_op == "add4s": body.append(op("add_4s", "vec_alu", f"add {d}.4s, {s1}.4s, {s2}.4s", [s1, s2], [d]))
                elif kind_op in ("eor", "orr"): body.append(op(f"{kind_op}_v", "vec_alu", f"{kind_op} {d}.16b, {s1}.16b, {s2}.16b", [s1, s2], [d]))
                elif kind_op in ("ushr", "shl"): body.append(op(f"{kind_op}_4s", "vec_shift", f"{kind_op} {d}.4s, {s1}.4s, #{rng.choice([3,7,12,16])}", [s1], [d]))
                elif kind_op == "rev32": body.append(op("rev32_8h", "vec_shuf", f"rev32 {d}.8h, {s1}.8h", [s1], [d]))
                elif kind_op == "ext":
                    imm = rng.choice([4, 8, 12])
                    body.append(op(f"ext_{imm}", "vec_shuf", f"ext {d}.16b, {s1}.16b, {s2}.16b, #{imm}", [s1, s2], [d]))
                else:
                    body.append(op("uzp1_4s", "vec_shuf", f"uzp1 {d}.4s, {s1}.4s, {s2}.4s", [s1, s2], [d]))
            elif k == "neon_mem":
                vi = rng.randrange(8)
                off = rng.choice([0, 16, 32, 48, 64, 80, 96, 112])
                if rng.random() < 0.5:
                    body.append(op("ldr128", "vec_load", f"ldr q{vi}, [x1, #{off}]", ["x1"], [f"v{vi}"]))
                else:
                    s1 = rng.choice(vecs)
                    body.append(op("add_4s", "vec_alu", f"add v{vi}.4s, v{vi}.4s, {s1}.4s", [f"v{vi}", s1], [f"v{vi}"]))
        eval_k.append(_arm_loop(name, f"rand:{prof}", init, body, epilogue_asm=epi))

    return lat_probes, eval_k


def detect_host_info():
    mach = platform.machine().lower()
    target = "arm64" if mach in ("arm64", "aarch64") else "x86_64"
    os_name = platform.system()
    model = "unknown"
    avx512 = False
    if os_name == "Darwin":
        try:
            model = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        except Exception:
            pass
    elif os.path.exists("/proc/cpuinfo"):
        txt = open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace").read()
        m = re.search(r"^model name\s*:\s*(.+)$", txt, re.M)
        if m:
            model = m.group(1).strip()
        avx512 = "avx512f" in txt
    if model == "unknown" and os_name == "Linux":
        try:
            out = subprocess.check_output(["lscpu"], text=True)
            m = re.search(r"^Model name:\s*(.+)$", out, re.M)
            if m:
                model = m.group(1).strip()
        except Exception:
            pass
    return {"target": target, "os": os_name, "cpu_model": model, "avx512": avx512}


def run_llvm_mca(kernel, target, mcpu):
    if not mcpu:
        return None
    lines = [it["asm"] for it in kernel["loop_ops"]]
    if target == "x86_64":
        asm = ".intel_syntax noprefix\n" + "\n".join(l.replace("1b", "0") for l in lines) + "\n"
        cmd = ["llvm-mca", "-mtriple=x86_64-linux-gnu", f"-mcpu={mcpu}", "-iterations=100"]
    else:
        asm = "\n".join(l.replace("1b", "0") for l in lines) + "\n"
        cmd = ["llvm-mca", "-mtriple=aarch64-linux-gnu", f"-mcpu={mcpu}", "-iterations=100"]
    try:
        r = subprocess.run(cmd, input=asm, text=True, capture_output=True, timeout=10)
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"Total Cycles:\s+(\d+)", r.stdout)
    return float(m.group(1)) / 100.0 if m else None


def compile_and_measure(td, kernels, target, is_darwin, iters, rounds, core):
    pfx = "_" if is_darwin else ""
    asm_lines = []
    if target == "x86_64":
        asm_lines.append(".intel_syntax noprefix")
    asm_lines.append(".text")
    # 64-op serial calibration chains (1.000 cyc/op on every x86-64 and AArch64 core)
    if target == "x86_64":
        asm_lines += [
            ".p2align 6", f".globl {pfx}cal_add64", f"{pfx}cal_add64:",
            "  mov rax, 1", "  mov rdx, 3", "  xor ecx, ecx", "1:",
        ] + ["  add rax, rdx"] * 64 + [
            "  add rcx, 1", "  cmp rcx, rdi", "  jb 1b", "  ret",
            ".p2align 6", f".globl {pfx}cal_alt64", f"{pfx}cal_alt64:",
            "  mov rax, 1", "  mov rdx, 3", "  xor ecx, ecx", "1:",
        ] + ["  or rax, rdx"] * 64 + [
            "  add rcx, 1", "  cmp rcx, rdi", "  jb 1b", "  ret",
        ]
    else:
        asm_lines += [
            ".p2align 6", f".globl {pfx}cal_add64", f"{pfx}cal_add64:",
            "  mov x3, #1", "  mov x4, #3", "  mov x2, #0", "1:",
        ] + ["  add x3, x3, x4"] * 64 + [
            "  add x2, x2, #1", "  cmp x2, x0", "  b.lo 1b", "  mov x0, x3", "  ret",
            ".p2align 6", f".globl {pfx}cal_alt64", f"{pfx}cal_alt64:",
            "  mov x3, #1", "  mov x4, #3", "  mov x2, #0", "1:",
        ] + ["  orr x3, x3, x4"] * 64 + [
            "  add x2, x2, #1", "  cmp x2, x0", "  b.lo 1b", "  mov x0, x3", "  ret",
        ]

    for k in kernels:
        asm_lines.append(".p2align 6")
        asm_lines.append(f".globl {pfx}k_{k['name']}")
        asm_lines.append(f"{pfx}k_{k['name']}:")
        for l in k["init"]:
            asm_lines.append("  " + l)
        asm_lines.append("1:")
        for it in k["loop_ops"]:
            asm_lines.append("  " + it["asm"])
        for l in k["epilogue"]:
            asm_lines.append("  " + l)
    asm_path = os.path.join(td, "kernels.S")
    with open(asm_path, "w", encoding="utf-8") as f:
        f.write("\n".join(asm_lines) + "\n")

    decls = "\n".join(f"extern uint64_t k_{k['name']}(uint64_t, uint8_t *);" for k in kernels)
    tbl = "\n".join(f'  {{"{k["name"]}", k_{k["name"]}}},' for k in kernels)
    c_src = f"""#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#if defined(__APPLE__)
#include <pthread/qos.h>
#else
#include <linux/perf_event.h>
#include <sched.h>
#include <sys/ioctl.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

extern uint64_t cal_add64(uint64_t, uint8_t *);
extern uint64_t cal_alt64(uint64_t, uint8_t *);
{decls}

typedef uint64_t (*kfn_t)(uint64_t, uint8_t *);
struct K {{ const char *name; kfn_t fn; }};
static struct K KERNELS[] = {{
{tbl}
}};

static inline uint64_t now_ns(void) {{
#if defined(__APPLE__)
    return clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
#endif
}}

static int cmp_d(const void *a, const void *b) {{
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}}

#if !defined(__APPLE__)
static int open_perf_cycles(void) {{
    struct perf_event_attr pe;
    memset(&pe, 0, sizeof(pe));
    pe.type = PERF_TYPE_HARDWARE;
    pe.size = sizeof(pe);
    pe.config = PERF_COUNT_HW_CPU_CYCLES;
    pe.disabled = 1;
    pe.exclude_kernel = 1;
    pe.exclude_hv = 1;
    int fd = (int)syscall(__NR_perf_event_open, &pe, 0, -1, -1, 0);
    if (fd >= 0) {{
        uint64_t c = 0;
        ioctl(fd, PERF_EVENT_IOC_RESET, 0);
        ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);
        volatile uint64_t s = cal_add64(2000, NULL);
        (void)s;
        ioctl(fd, PERF_EVENT_IOC_DISABLE, 0);
        if (read(fd, &c, sizeof(c)) != sizeof(c) || c < 10000) {{
            close(fd);
            return -1;
        }}
    }}
    return fd;
}}
#endif

static double ref_ns_per_cyc(uint8_t *buf, uint64_t cal_iters) {{
    volatile uint64_t sink = 0;
    uint64_t t0 = now_ns();
    sink ^= cal_add64(cal_iters, buf);
    uint64_t dt = now_ns() - t0;
    (void)sink;
    return (double)dt / ((double)cal_iters * 64.0);
}}

int main(void) {{
    uint8_t *buf = NULL;
    if (posix_memalign((void **)&buf, 64, 4096) != 0) return 2;
    for (int i = 0; i < 4096; i++) buf[i] = (uint8_t)((i * 37 + 13) & 0xff);

    int use_pmu = 0, pmu_fd = -1;
#if defined(__APPLE__)
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
#else
    cpu_set_t set; CPU_ZERO(&set); CPU_SET({core}, &set);
    sched_setaffinity(0, sizeof(set), &set);
    pmu_fd = open_perf_cycles();
    if (pmu_fd >= 0) use_pmu = 1;
#endif

    volatile uint64_t sink = 0;
    uint64_t cal_iters = 50000;
    sink ^= cal_add64(cal_iters, buf);
    sink ^= cal_alt64(cal_iters, buf);
    double ratios[9];
    for (int r = 0; r < 9; r++) {{
        uint64_t t0 = now_ns(); sink ^= cal_add64(cal_iters, buf); uint64_t dt_a = now_ns() - t0;
        uint64_t t1 = now_ns(); sink ^= cal_alt64(cal_iters, buf); uint64_t dt_b = now_ns() - t1;
        ratios[r] = (double)dt_b / (double)dt_a;
    }}
    qsort(ratios, 9, sizeof(double), cmp_d);
    printf("META mode=%s freq_ghz=%.3f alt_over_add=%.4f\\n",
           use_pmu ? "perf_event_open" : "ref_add64_clock",
           1.0 / ref_ns_per_cyc(buf, cal_iters), ratios[4]);

    int n = (int)(sizeof(KERNELS) / sizeof(KERNELS[0]));
    uint64_t iters = {iters}ULL;
    int rounds = {rounds};
    for (int i = 0; i < n; i++) {{
        sink ^= KERNELS[i].fn(iters / 4, buf);
        double vals[64];
        for (int r = 0; r < rounds; r++) {{
#if !defined(__APPLE__)
            if (use_pmu) {{
                uint64_t c = 0;
                ioctl(pmu_fd, PERF_EVENT_IOC_RESET, 0);
                ioctl(pmu_fd, PERF_EVENT_IOC_ENABLE, 0);
                sink ^= KERNELS[i].fn(iters, buf);
                ioctl(pmu_fd, PERF_EVENT_IOC_DISABLE, 0);
                if (read(pmu_fd, &c, sizeof(c)) != sizeof(c)) c = 0;
                vals[r] = (double)c / (double)iters;
                continue;
            }}
#endif
            double a = 0.0, b = 0.0;
            uint64_t dt = 0;
            for (int tries = 0; tries < 8; tries++) {{
                a = ref_ns_per_cyc(buf, cal_iters);
                uint64_t t0 = now_ns();
                sink ^= KERNELS[i].fn(iters, buf);
                dt = now_ns() - t0;
                b = ref_ns_per_cyc(buf, cal_iters);
                double diff = a > b ? (a - b) : (b - a);
                if (diff <= 0.008 * a) break;
            }}
            vals[r] = ((double)dt / (double)iters) / (0.5 * (a + b));
        }}
        qsort(vals, rounds, sizeof(double), cmp_d);
        printf("RESULT %s %.4f %.4f %.4f\\n", KERNELS[i].name, vals[rounds/2], vals[0], vals[rounds-1]);
    }}
    (void)sink;
    return 0;
}}
"""
    c_path = os.path.join(td, "harness.c")
    with open(c_path, "w", encoding="utf-8") as f:
        f.write(c_src)
    bin_path = os.path.join(td, "harness")
    subprocess.run(["cc", "-O2", c_path, asm_path, "-o", bin_path], check=True)
    r = subprocess.run([bin_path], capture_output=True, text=True, check=True)
    meta, res = {}, {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == "META":
            for kv in parts[1:]:
                k, v = kv.split("=", 1)
                meta[k] = v
        elif parts and parts[0] == "RESULT":
            res[parts[1]] = (float(parts[2]), float(parts[3]), float(parts[4]))
    return meta, res


def main():
    host = detect_host_info()
    ap = argparse.ArgumentParser(description="Standalone CPU pipeline probe & simulator validator.")
    ap.add_argument("--arch", default=None, choices=sorted(sim.PIPELINES.keys()),
                    help="Simulator profile to compare against (auto-selected if omitted)")
    ap.add_argument("--no-avx512", action="store_true", help="Skip AVX-512 kernels on x86-64")
    ap.add_argument("--run", action="store_true", help="Compile and run hardware measurement on this CPU")
    ap.add_argument("--iters", type=int, default=60000)
    ap.add_argument("--rounds", type=int, default=11)
    ap.add_argument("--core", type=int, default=1)
    ap.add_argument("--mcpu", default=None, help="Optional llvm-mca -mcpu flag")
    ap.add_argument("--json-out", default=None, help="Write sanitized PR-ready JSON results file")
    args = ap.parse_args()

    target = host["target"]
    if args.arch is None:
        arch = "apple_m" if (target == "arm64" and host["os"] == "Darwin") else (
            "ampere1" if target == "arm64" else ("zen5" if host["avx512"] else "zen3")
        )
    else:
        arch = args.arch

    if target == "x86_64":
        use_512 = host["avx512"] and (not args.no_avx512)
        lat_k, eval_k = build_x86_kernels(include_avx512=use_512)
    else:
        lat_k, eval_k = build_arm64_kernels()

    meta, meas = {}, {}
    if args.run:
        with tempfile.TemporaryDirectory() as td:
            meta, meas = compile_and_measure(
                td, lat_k + eval_k, target, host["os"] == "Darwin", args.iters, args.rounds, args.core
            )

    print(f"CPU Pipeline Probe — target={target}, sim_arch={arch}, model={host['cpu_model']}")
    if meta:
        print(f"Counter meta: {meta}")

    lat_out = {}
    if meas:
        print("\n=== Exact Instruction Latency Probes (cycles / instruction) ===")
        for k in lat_k:
            med, lo, hi = meas[k["name"]]
            v = med / k["div"]
            lat_out[k["name"]] = round(v, 3)
            print(f"  {k['name']:18s}  {v:5.3f} cyc/op  (min={lo/k['div']:.3f}, max={hi/k['div']:.3f})")

    print(f"\n{'Kernel':22s} {'Kind':12s} {'Ins':>3s} {'Sim':>7s} {'MCA':>7s} {'Meas':>7s} {'Sim%':>7s}  Bottleneck")
    print("-" * 86)
    sim_e, mca_e, recs = [], [], []
    for k in eval_k:
        s = sim.simulate_loop(k["loop_ops"], arch, iterations=16)
        pred = s["cycles_per_iter"]
        mca = run_llvm_mca(k, target, args.mcpu) if args.mcpu else None
        mv = meas[k["name"]][0] if k["name"] in meas else None
        es = f"{100.0*(pred-mv)/mv:+6.1f}%" if mv else "    n/a"
        ms = f"{mca:7.2f}" if mca is not None else "    n/a"
        mvs = f"{mv:7.2f}" if mv is not None else "    n/a"
        print(f"{k['name']:22s} {k['kind']:12s} {len(k['loop_ops']):3d} {pred:7.2f} {ms} {mvs} {es}  {s['bottleneck']}")
        if mv is not None:
            sim_e.append(abs(100.0 * (pred - mv) / mv))
            if mca is not None:
                mca_e.append(abs(100.0 * (mca - mv) / mv))
        recs.append({
            "name": k["name"], "kind": k["kind"], "loop_insns": len(k["loop_ops"]),
            "sim": round(pred, 3), "sim_bottleneck": s["bottleneck"],
            "llvm_mca": round(mca, 3) if mca is not None else None,
            "measured": round(mv, 4) if mv is not None else None,
        })

    def _sum(es):
        w10 = sum(1 for e in es if e <= 10.0)
        w15 = sum(1 for e in es if e <= 15.0)
        return {
            "n": len(es), "within_10pct": w10, "within_10pct_rate": round(100.0 * w10 / len(es), 1),
            "within_15pct": w15, "median_abs_err_pct": round(statistics.median(es), 1),
            "mean_abs_err_pct": round(statistics.mean(es), 1),
        }

    if meas:
        print(f"\n=== Accuracy Summary ({len(eval_k)} kernels) ===")
        for label, es in [("sim.py  ", sim_e)] + ([("llvm-mca", mca_e)] if mca_e else []):
            st = _sum(es)
            print(f"  {label}: {st['within_10pct']:2d}/{st['n']} ({st['within_10pct_rate']:5.1f}%) <=10% | "
                  f"{st['within_15pct']:2d}/{st['n']} <=15% | med {st['median_abs_err_pct']:4.1f}% | mean {st['mean_abs_err_pct']:4.1f}%")

    if args.json_out:
        out = {
            "cpu_model": host["cpu_model"],
            "target": target,
            "os": host["os"],
            "sim_arch": arch,
            "date_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
            "meta": meta,
            "latencies": lat_out,
            "sim_accuracy": _sum(sim_e) if sim_e else None,
            "llvm_mca_accuracy": _sum(mca_e) if mca_e else None,
            "records": recs,
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
            f.write("\n")
        print(f"\nWrote sanitized results to {args.json_out}")


if __name__ == "__main__":
    main()
