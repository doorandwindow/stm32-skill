#!/usr/bin/env python3
"""One-shot embedded dev loop: configure -> build -> flash -> UART capture.

This is the agent-facing entry point: one invocation, structured JSON verdict
that names exactly which stage failed and where the logs live, so the calling
agent can jump straight to diagnosis instead of gluing four commands together.

Examples (run inside the firmware project directory):
  python devloop.py                                        # everything, defaults
  python devloop.py --preset Debug --seconds 4 --ts        # longer capture, timestamps
  python devloop.py --no-flash                             # build-only sanity pass
  python devloop.py --uart-port COM18                      # pin the serial port down

Final stdout line is:  === DEVLOOP_RESULT === { ...json... }
Stages report independently; the loop stops at the first failing stage but
previous stage artifacts (logs) are still reported.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

GCC_ERROR_RE = re.compile(
    r"^([^:\n]+\.(?:c|h|cpp|hpp|s|S)):(\d+)(?::(\d+))?:\s+(fatal\s+error|error|warning):\s+(.+)$",
    re.M,
)


def merge_env(tool_paths: dict) -> dict:
    """Prepend every located tool's bin dir to PATH so CMake/Ninja/GCC see each other."""
    env = dict(os.environ)
    bins = []
    for key in ("cmake", "ninja", "_gcc_bin_dir", "arm_none_eabi_gdb"):
        v = tool_paths.get(key)
        if v:
            d = os.path.dirname(v) if os.path.splitext(v)[1] == ".exe" else v
            if d and d not in bins:
                bins.append(d)
    if bins:
        env["PATH"] = ";".join(bins) + ";" + env.get("PATH", "")
    return env


def run(cmd, cwd, env, timeout, log_path) -> dict:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    with open(log_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# cmd: " + " ".join(cmd) + f"\n# cwd: {cwd}\n\n" + output)
    return {"rc": proc.returncode, "output": output, "log": log_path}


def parse_gcc_issues(text: str, limit: int = 20) -> list[str]:
    seen, out = set(), []
    for m in GCC_ERROR_RE.finditer(text):
        sev = m.group(4)
        if "warning" in sev:
            continue
        entry = f"{m.group(1)}:{m.group(2)}:{m.group(3) or 1}: {sev}: {m.group(5)[:160]}"
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def newest_elf(build_dir: str) -> str | None:
    hits = [(os.path.getmtime(p), p) for p in glob.glob(os.path.join(build_dir, "**", "*.elf"), recursive=True)]
    return sorted(hits)[-1][1] if hits else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=".", help="firmware project root holding CMakePresets.json")
    ap.add_argument("--preset", default="Debug", help="CMake preset name (default Debug)")
    ap.add_argument("--no-configure", action="store_true", help="never run the cmake configure step")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--no-flash", action="store_true")
    ap.add_argument("--no-capture", action="store_true")
    ap.add_argument("--config-only", action="store_true", help="stop right after a successful configure")
    # UART capture passthrough
    ap.add_argument("--uart-port"); ap.add_argument("--uart-baud", type=int, default=115200)
    ap.add_argument("--uart-vid", type=lambda x: int(x, 16), default=0x1A86)
    ap.add_argument("--uart-pid", type=lambda x: int(x, 16), default=0x7523)
    ap.add_argument("--seconds", type=float, default=3.0, help="UART capture window (default 3s)")
    ap.add_argument("--ts", action="store_true", help="timestamp captured UART lines")
    ap.add_argument("--flash-elf", help="explicit firmware path overriding auto-detection")
    ap.add_argument("--artifact-dir", help="where logs/result.json go (default <project>/build/devloop_last)")
    args = ap.parse_args()

    project = os.path.abspath(args.project)
    build_root = os.path.join(project, "build")
    art_dir = os.path.abspath(args.artifact_dir) if args.artifact_dir else os.path.join(build_root, "devloop_last")
    os.makedirs(art_dir, exist_ok=True)

    result: dict = {"ok": False, "stage": None, "project": project, "preset": args.preset,
                    "artifacts": {}, "hint": None}

    # ---------- tools ----------
    import shutil
    import find_tools as ft
    lad = os.environ.get("LOCALAPPDATA", "")
    tool_paths = {
        "cmake": ft.bundle_tool(lad, "cmake", "cmake.exe") or shutil.which("cmake"),
        "ninja": ft.bundle_tool(lad, "ninja", "ninja.exe") or shutil.which("ninja"),
        "_gcc_bin_dir": None,
        "programmer_cli": ft.bundle_tool(lad, "programmer", "STM32_Programmer_CLI.exe")
                          or getattr(ft, "clt_tool", lambda *a: None)(
                              sorted(glob.glob(r"C:\ST\STM32CubeCLT*"), reverse=True),
                              ["STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"]),
    }
    gccbin = ft.bundle_tool(lad, "gnu-tools-for-stm32", "arm-none-eabi-gcc.exe")
    tool_paths["_gcc_bin_dir"] = os.path.dirname(gccbin) if gccbin else (shutil.which("arm-none-eabi-gcc") or "")
    needs_build_or_flash = not (args.no_build and args.no_flash)
    missing_essential = [k for k in ("cmake", "programmer_cli")
                         if not tool_paths.get(k)] if needs_build_or_flash else []
    if missing_essential:
        # pure-capture runs may still proceed without a full toolchain
        result["stage"], result["hint"] = "tools", f"缺少关键工具: {missing_essential} — 单独运行 find_tools.py 看详情"
        print("=== DEVLOOP_RESULT === " + json.dumps(result, ensure_ascii=False))
        return 1

    env = merge_env({k: v for k, v in tool_paths.items()})
    build_dir = os.path.join(build_root, args.preset)

    # ---------- configure ----------
    needs_cfg = not os.path.isfile(os.path.join(build_dir, "build.ninja"))
    if needs_cfg and not args.no_configure:
        print(f"[devloop] configuring ({args.preset}) ...")
        r = run([tool_paths["cmake"], "-S", project, "--preset", args.preset],
                project, env, 300, os.path.join(art_dir, "configure.log"))
        result["artifacts"]["configure_log"] = r["log"]
        if r["rc"] != 0:
            result.update(stage="configure", hint="CMake configure 失败:常见于工具链不在 PATH(见 configure.log 开头的报错)、"
                                                 "或 CMakePresets.json/toolchain 文件问题")
            _finish(result, art_dir); return 1
    elif needs_cfg:
        result.update(stage="configure", hint="build 目录尚未配置(缺 build.ninja),但被 --no-configure 跳过")
        _finish(result, art_dir); return 1

    if args.config_only:
        result.update(ok=True, stage="configure")
        _finish(result, art_dir); return 0

    # ---------- build ----------
    elf = None
    if not args.no_build:
        print("[devloop] building ...")
        r = run([tool_paths["cmake"], "--build", "--preset", args.preset],
                project, env, 900, os.path.join(art_dir, "build.log"))
        result["artifacts"]["build_log"] = r["log"]
        issues = parse_gcc_issues(r["output"])
        if issues:
            result["artifacts"]["errors"] = issues
        if r["rc"] != 0:
            tail = [l for l in r["output"].splitlines() if l.strip()][-15:]
            result["artifacts"]["tail"] = tail
            hint_txt = ("编译失败——优先修复 errors 里列出的第一条(file:line),修完再跑一次;"
                        "undefined reference 多半是源文件没加进 CMakeLists")
            if not issues:
                hint_txt += ";没有解析出编译器报错时查 build.log 尾部(链接器错误/Region 溢出会打在 map 汇总里)"
            result.update(stage="build", hint=hint_txt)
            _finish(result, art_dir); return 1
        print("[devloop] build OK")

    # ---------- flash ----------
    if not args.no_flash:
        elf = args.flash_elf and os.path.abspath(args.flash_elf) or newest_elf(build_dir)
        if not elf:
            result.update(stage="flash", hint="找不到 *.elf(构建产物缺失?)")
            _finish(result, art_dir); return 1
        import flash as flash_mod
        pres = flash_mod.run_flash(tool_paths["programmer_cli"], elf, log_file=os.path.join(art_dir, "flash.log"))
        result["artifacts"]["flash_log"] = pres["log"]
        result["elf"] = elf
        if not pres["ok"]:
            result.update(stage="flash", hint=pres["hint"])
            _finish(result, art_dir); return 1
        print(f"[devloop] flashed {os.path.basename(elf)} (verified, MCU reset)")

    # ---------- capture ----------
    if not args.no_capture:
        uart_out = os.path.join(art_dir, "uart.log")
        cap_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "serial_capture.py"),
                   "--baud", str(args.uart_baud), "--seconds", str(args.seconds),
                   "--vid", f"{args.uart_vid:X}", "--pid", f"{args.uart_pid:X}",
                   "--out", uart_out, "--echo", "25", "--json"]
        if args.uart_port:
            cap_cmd += ["--port", args.uart_port]
        if args.ts:
            cap_cmd += ["--ts"]
        cap = subprocess.run(cap_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.seconds + 60)
        cout = (cap.stdout or "") + "\n" + (cap.stderr or "")
        result["artifacts"]["uart_log"] = uart_out
        mj = None
        for line in cout.splitlines():
            if line.startswith("=== CAPTURE_JSON === "):
                try:
                    mj = json.loads(line.split("=== CAPTURE_JSON === ", 1)[1])
                except json.JSONDecodeError:
                    mj = None
        if cap.returncode != 0 or (mj and not mj.get("opened")):
            result.update(stage="capture", hint=mj.get("hint") if mj and mj.get("hint") else (mj or {}).get("error", cout[-400:] if not mj else "串口打开失败"))
            if mj:
                result["capture"] = mj
            _finish(result, art_dir); return 1
        result["ok"] = True
        result["stage"] = "capture"
        if mj:
            result["capture"] = {"port": mj.get("port"), "description": mj.get("description"),
                                 "lines": mj.get("lines"), "bytes": mj.get("bytes"),
                                 "silent": (mj.get("lines") or 0) == 0}
            if (mj.get("lines") or 0) == 0:
                result["hint"] = ("端口打开成功但整窗无数据:MCU 可能启动早期就挂了(在时钟初始化前)"
                                  "、波特率不匹配、或程序本身不打日志。先用 LED 心跳/早期打印定位;详见 references/troubleshooting.md")
    else:
        result["ok"] = True
        result["stage"] = "flash" if not args.no_flash else "build"

    _finish(result, art_dir)
    return 0 if result["ok"] else 1


def _finish(result: dict, art_dir: str) -> None:
    rf = os.path.join(art_dir, "result.json")
    try:
        with open(rf, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        result["artifacts"]["result_json"] = rf
    except OSError:
        pass
    print("=== DEVLOOP_RESULT === " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if platform.system() != "Windows":
        print("[!] 当前脚本按 Windows + ST-Link 场景设计")
    sys.exit(main())
