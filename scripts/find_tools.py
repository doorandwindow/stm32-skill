#!/usr/bin/env python3
"""Locate STM32 embedded toolchain binaries on Windows.

Search order per tool:
  1. STM32Cube VS Code extension bundles:  %LOCALAPPDATA%\\stm32cube\\bundles\\<bundle>\\<version>\\bin\\<exe>
     (multiple versions may coexist; highest version wins)
  2. STM32CubeCLT:                          C:\\ST\\STM32CubeCLT*\\...
  3. System PATH (shutil.which)

Prints a JSON object mapping logical tool names -> absolute paths (or null).
Exit 0 if at least the essential tools were found, 1 otherwise.

Essential tools : cmake, programmer_cli        (build + flash need both)
Optional tools  : ninja, arm_none_eabi_gcc, arm_none_eabi_size,
                  arm_none_eabi_gdb, stlink_gdbserver
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys


def ver_key(ver: str):
    """Sort key tolerant of versions like '4.0.1+st.3', '2.22.0'."""
    parts = re.split(r"[.+_-]", ver)
    key = []
    for p in parts:
        key.append((0, int(p)) if p.isdigit() else (1, p))
    return key


def newest_version_dir(bundle_root: str) -> str | None:
    """Return the subdirectory of bundle_root with the highest version name."""
    if not os.path.isdir(bundle_root):
        return None
    subs = []
    for name in os.listdir(bundle_root):
        full = os.path.join(bundle_root, name)
        if os.path.isdir(full) and re.match(r"^[0-9]", name):
            subs.append((ver_key(name), full))
    if not subs:
        # some bundles sit directly in bin/ without a version dir
        if os.path.isdir(os.path.join(bundle_root, "bin")):
            return bundle_root
        return None
    subs.sort(key=lambda t: t[0])
    return subs[-1][1]


def bundle_tool(localappdata: str, bundle: str, exe: str) -> str | None:
    root = os.path.join(localappdata, "stm32cube", "bundles", bundle)
    vdir = newest_version_dir(root)
    if vdir:
        cand = os.path.join(vdir, "bin", exe)
        if os.path.isfile(cand):
            return cand
    return None


def clt_tool(patterns: list[str], rel: list[str]) -> str | None:
    """Find a tool inside STM32CubeCLT installs."""
    for pat in patterns:
        for base in sorted(glob.glob(pat), reverse=True):
            for r in rel:
                cand = os.path.join(base, *r.split("/"))
                if os.path.isfile(cand):
                    return cand
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="also write the result JSON to this file")
    args = ap.parse_args()

    lad = os.environ.get("LOCALAPPDATA", "")
    notes: list[str] = []

    st_roots = sorted(glob.glob(r"C:\ST\STM32CubeCLT*"), reverse=True)

    tools: dict[str, str | None] = {}

    # ---- programmer (essential) ----
    tools["programmer_cli"] = (
        bundle_tool(lad, "programmer", "STM32_Programmer_CLI.exe")
        or clt_tool(st_roots, ["STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe"])
        or shutil.which("STM32_Programmer_CLI")
    )

    # ---- cmake (essential) ----
    tools["cmake"] = (
        bundle_tool(lad, "cmake", "cmake.exe")
        or clt_tool(st_roots, ["CMake/bin/cmake.exe"])
        or shutil.which("cmake")
    )

    # ---- everything else is best-effort ----
    tools["ninja"] = (
        bundle_tool(lad, "ninja", "ninja.exe")
        or clt_tool(st_roots, ["Ninja/ninja.exe"])
        or shutil.which("ninja")
    )
    gnu_bins = [
        bundle_tool(lad, "gnu-tools-for-stm32", f"arm-none-eabi-{t}.exe") for t in ("gcc", "g++", "size")
    ]
    if any(gnu_bins):
        base = os.path.dirname(gnu_bins[0])
        tools["arm_none_eabi_gcc"] = os.path.join(base, "arm-none-eabi-gcc.exe")
        tools["arm_none_eabi_gpp"] = os.path.join(base, "arm-none-eabi-g++.exe")
        tools["arm_none_eabi_size"] = os.path.join(base, "arm-none-eabi-size.exe")
        tools["_gcc_bin_dir"] = base
    else:
        cc = shutil.which("arm-none-eabi-gcc")
        if cc:
            tools["_gcc_bin_dir"] = os.path.dirname(os.path.abspath(cc))
            tools["arm_none_eabi_gcc"] = cc
        else:
            tools["_gcc_bin_dir"] = None
        tools.setdefault("arm_none_eabi_gcc", cc)

    tools["arm_none_eabi_gdb"] = (
        bundle_tool(lad, "gnu-gdb-for-stm32", "arm-none-eabi-gdb.exe")
        or clt_tool(st_roots, ["GNU-tools-for-STM32/bin/arm-none-eabi-gdb.exe"])
        or shutil.which("arm-none-eabi-gdb")
    )
    tools["stlink_gdbserver"] = (
        bundle_tool(lad, "stlink-gdbserver", "ST-LINK_gdbserver.exe")
        or clt_tool(st_roots, ["STM32CubeProgrammer/bin/ST-LINK_gdbserver.exe"])
    )

    essential_missing = [t for t in ("cmake", "programmer_cli") if not tools.get(t)]

    result = {
        "ok": not essential_missing,
        "tools": {k: v.replace("\\", "/") if isinstance(v, str) else None for k, v in tools.items()},
        "missing_essential": essential_missing,
        "notes": [],
    }
    if lad and os.path.isdir(os.path.join(lad, "stm32cube", "bundles")):
        result["notes"].append("source: LOCALAPPDATA stm32cube bundles")
    elif st_roots:
        result["notes"].append("source: STM32CubeCLT under C:\\ST")

    out = json.dumps(result, indent=2)
    print(out)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(out)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
