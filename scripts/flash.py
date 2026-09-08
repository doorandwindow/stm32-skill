#!/usr/bin/env python3
"""Flash an ELF/Hex/BIN to an STM32 over ST-Link SWD using STM32CubeProgrammer CLI.

Wraps the raw CLI so the agent gets a human verdict instead of exit-code archaeology:
  * finds STM32_Programmer_CLI automatically (Cube VSCode bundles / CubeCLT / PATH)
  * classifies the failure (ST-Link absent? target won't sync? read protection?)
  * saves the full CLI transcript to a log file

Examples:
  python flash.py                          # newest *.elf under ./build/*/
  python flash.py --elf build/Debug/fw.elf --no-reset
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from find_tools import bundle_tool, clt_tool  # type: ignore
except Exception:  # running standalone without sibling
    def bundle_tool(*a, **k):
        return None
    def clt_tool(*a, **k):
        return None


def locate_cli(explicit: str | None) -> str | None:
    if explicit and os.path.isfile(explicit):
        return explicit
    lad = os.environ.get("LOCALAPPDATA", "")
    c = bundle_tool(lad, "programmer", "STM32_Programmer_CLI.exe")
    if c:
        return c
    roots = sorted(glob.glob(r"C:\ST\STM32CubeCLT*"), reverse=True)
    c = clt_tool(roots, ["STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"])
    if c:
        return c
    return None


def locate_elf(project_dir: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.isabs(explicit) or os.path.exists(explicit) else os.path.join(project_dir, explicit)
    hits: list[tuple[float, str]] = []
    for pat in ("build/*/*.elf", "*.elf"):
        for p in glob.glob(os.path.join(project_dir, pat)):
            try:
                hits.append((os.path.getmtime(p), p))
            except OSError:
                pass
    if not hits:
        return None
    return sorted(hits)[-1][1]


# substrings -> meaning (checked against combined CLI output, case-insensitive)
FAILURE_TABLE = [
    (("no st-link", ),              "ST-Link 没被识别:检查 USB 线/换口,stlink-usb-driver 驱动是否装好"),
    (("target no sync", "cannot connect", "no target connected", "connect failed"),
                                    "SWD 连不上目标:确认 ST-Link 与板子连接、板子有电、PA13/PA14 未被占用;mode=UR(复位下连)通常可救已跑飞的板子"),
    (("read protected", "rdp", "mem management error", "not readable"),
                                    "芯片读保护(RDP)或扇区异常,需全片擦除解锁(会清掉所有程序):STM32_Programmer_CLI.exe -c port=SWD mode=UR -e all 后重试"),
    (("firmware out of date", ),    "ST-Link 固件过旧:运行 STM32_Programmer_CLI.exe --upgrade 或 stlink-upgrader"),
    (("no debug session", "usb device unavailable", "another application"),
                                    "ST-Link 被其它程序占用(GDB server、另一个烧录进程),关掉后重试"),
]


def classify(output: str, rc: int) -> tuple[bool, str]:
    low = output.lower()
    if rc == 0 and ("download verified successfully" in low or "verified successfully" in low or "file download complete" in low):
        if "error" not in low or "0 error" in low:
            return True, ""
    for needles, hint in FAILURE_TABLE:
        for n in needles:
            if n in low:
                return False, hint
    return False, f"STM32_Programmer_CLI 返回码 {rc},输出中没有明确的成功标志"


def run_flash(cli: str, elf: str, mode: str = "UR", verify: bool = True,
              reset: bool = True, log_file: str | None = None) -> dict:
    cmd = [cli, "-c", f"port=SWD mode={mode}", "-w", elf]
    if verify:
        cmd += ["-v"]
    if reset:
        cmd += ["-rst"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ok, hint = classify(output, proc.returncode)
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "w", encoding="utf-8", newline="\n") as f:
                f.write("# cmd: " + " ".join(cmd) + "\n\n" + output)
        except OSError:
            pass
    # pull a couple of informative lines for the summary
    interesting = []
    for ln in output.splitlines():
        s = ln.strip()
        if re.match(r"^(Memory readback|External Flash|File download complete|Download verified|Writing|Verification)", s, re.I) \
           or "erasing" in s.lower():
            interesting.append(s[:120])
        if len(interesting) >= 5:
            break
    return {"ok": ok, "rc": proc.returncode, "cmd": cmd, "elf": elf,
            "hint": hint, "log": log_file, "summary_lines": interesting}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--elf", help="firmware file (.elf/.hex/.bin); default: newest under build/*/")
    ap.add_argument("--cli", help="explicit STM32_Programmer_CLI.exe path")
    ap.add_argument("--mode", default="UR", choices=["UR", "normal"], help="connect-under-reset (UR, default) or normal")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--log", help="where to write the CLI transcript (default: <elf dir>/flash_last.log)")
    ap.add_argument("--json", action="store_true", help="print machine-readable '=== FLASH_JSON === {...}' line")
    args = ap.parse_args()

    cli = locate_cli(args.cli)
    if not cli:
        print("[!] 找不到 STM32_Programmer_CLI.exe —— 请安装 STM32CubeCLT 或带 programmer bundle 的 STM32Cube VSCode 扩展,"
              "或用 --cli 指定路径")
        _emit(None, args, ok=False, err="programmer_cli not found")
        return 1

    elf = locate_elf(os.getcwd(), args.elf)
    if not elf:
        print("[!] 找不到固件文件:用 --elf 指定 .elf/.hex,或先完成构建(build 目录里没有任何 *.elf)")
        _emit(cli, args, ok=False, err="elf not found")
        return 1
    print(f"[i] flashing {elf}")

    res = run_flash(cli, elf, mode=args.mode, verify=not args.no_verify,
                    reset=not args.no_reset, log_file=args.log)
    if res["ok"]:
        print(f"[+] 烧录并校验成功{'(未复位)' if args.no_reset else ',目标已复位'}")
    else:
        print(f"[!] 烧录失败 (rc={res['rc']})")
        if res["hint"]:
            print(f"[i] {res['hint']}")
    for s in res["summary_lines"]:
        print(f"    · {s}")
    if res["log"]:
        print(f"[i] full log: {res['log']}")
    _emit(cli, args, **{"ok": res["ok"], "res": res})
    return 0 if res["ok"] else 1


def _emit(cli, args, ok: bool, res=None, err=None):
    if getattr(args, "json", False):
        payload = {"ok": ok, "stage": "flash"}
        if err:
            payload["error"] = err
        if res:
            payload["result"] = res
        print("=== FLASH_JSON === " + json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
