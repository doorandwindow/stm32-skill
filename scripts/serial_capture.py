#!/usr/bin/env python3
"""Capture N seconds of UART output into a UTF-8 text file.

Why this exists: an AI agent cannot watch an interactive serial terminal.
This script is the bridge: it opens a COM port, records everything that
arrives during a fixed time window, writes it to a file, and echoes a short
summary so the agent can Read the results like any other log.

Features an agent relies on:
  * port auto-detection by VID:PID (CH340/CH9102/FT232/CP210x/ST-LINK VCP),
    because COM numbers drift across replugs
  * friendly diagnosis when the port is busy (who else holds it?)
  * optional relative timestamps on every line
  * exit code tells the story: 0 = captured, 2 = could not open / no port found

Examples:
  python serial_capture.py --out uart.log                     # auto-find CH340, 3 s @115200
  python serial_capture.py --port COM18 --seconds 5 --ts --json --out run.log
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

# (vid, pid, label) tried in order when --port is not given.
KNOWN_UARTS = [
    (0x1A86, 0x7523, "CH340"),      # ALIENTEK board onboard USB-UART
    (0x1A86, 0xE024, "CH9102"),
    (0x0483, 0x5740, "ST-LINK VCP"),
    (0x0403, 0x6001, "FT232"),
    (0x10C4, 0xEA60, "CP210x"),
]


def lock_path(port_name: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in port_name.lower())
    return os.path.join(tempfile.gettempdir(), f"stm32_serial_capture_{safe}.lock")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = os.popen(f'tasklist /FI "PID eq {pid}" /NH').read()
        return str(pid) in out
    except Exception:
        return False


def check_lock(port_name: str) -> tuple[int, float] | None:
    """Return (pid, age_seconds) of a *live* lock held by someone else, else None."""
    lp = lock_path(port_name)
    if not os.path.exists(lp):
        return None
    try:
        txt = open(lp, encoding="utf-8").read().strip()
        pid_str, _, ts_str = txt.partition(":")
        ts = float(ts_str) if ts_str else 0.0
        pid = int(pid_str)
        age = time.time() - ts
        me = os.getpid()
        if pid != me and pid_alive(pid) and age < 600:
            return pid, age
    except Exception:
        pass
    # stale/dead -> clean up quietly
    try:
        os.remove(lp)
    except OSError:
        pass
    return None


def write_lock(port_name: str) -> None:
    lp = lock_path(port_name)
    try:
        with open(lp, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}:{time.time()}")
    except OSError:
        pass


def remove_lock(port_name: str) -> None:
    try:
        os.remove(lock_path(port_name))
    except OSError:
        pass


def pick_port(args_port: str | None, vid: int, pid: int) -> tuple[str, str] | None:
    """Return (device, description) or None. Prints the port table on failure."""
    import serial.tools.list_ports as lp_mod

    ports = list(lp_mod.comports())
    if args_port:
        for p in ports:
            if p.device.upper() == args_port.upper():
                return p.device, p.description
        return None
    # exact VID:PID match first (most specific)
    for p in ports:
        if p.vid == vid and p.pid == pid:
            return p.device, p.description
    # fall back through the known-USB-UART list
    for kvid, kpid, label in KNOWN_UARTS:
        for p in ports:
            if p.vid == kvid and p.pid == kpid:
                return p.device, p.description
    return None


def print_port_table() -> None:
    import serial.tools.list_ports as lp_mod

    print("[i] available COM ports:")
    for p in lp_mod.comports():
        vid = f"{p.vid:04X}" if p.vid else "----"
        pid = f"{p.pid:04X}" if p.pid else "----"
        print(f"    {p.device:<8} VID:PID={vid}:{pid}  {p.description}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="COM port, e.g. COM18 (default: auto-detect)")
    ap.add_argument("--vid", type=lambda x: int(x, 16), default=0x1A86, help="target vendor id, hex (default 1A86 = CH340)")
    ap.add_argument("--pid", type=lambda x: int(x, 16), default=0x7523, help="target product id, hex (default 7523)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--out", required=True, help="output log file (UTF-8)")
    ap.add_argument("--ts", action="store_true", help="prefix each line with seconds-since-start")
    ap.add_argument("--no-dtr", dest="dtr", action="store_false", default=False,
                    help="assert DTR on open (default OFF: DTR can reset some boards)")
    ap.add_argument("--rts", action="store_true", default=False, help="assert RTS on open")
    ap.add_argument("--echo", type=int, default=30, metavar="N", help="print last N lines to stdout (default 30, 0=none)")
    ap.add_argument("--json", action="store_true", help="append a JSON summary line '=== CAPTURE_JSON === {...}'")
    args = ap.parse_args()

    import serial
    from serial.tools import list_ports  # noqa: F401  (ensures backend present)

    result = {"port": None, "description": None, "baud": args.baud, "seconds": args.seconds,
              "out": args.out, "opened": False, "lines": 0, "bytes": 0, "error": None}

    # --- resolve port -----------------------------------------------------
    hit = pick_port(args.port, args.vid, args.pid)
    if hit is None:
        result["error"] = ("requested port not found" if args.port
                           else f"no matching USB-UART found (tried VID:PID {args.vid:04X}:{args.pid:04X}, then known list)")
        print(f"[!] {result['error']}")
        print_port_table()
        _emit(result, args)
        return 2
    device, desc = hit
    result["port"], result["description"] = device, desc

    # --- busy check -------------------------------------------------------
    holder = check_lock(device)
    busy_hint = None
    try:
        ser = serial.Serial(device, args.baud, timeout=0.1)
    except serial.SerialException as e:
        msg = str(e)
        if holder:
            busy_hint = f"our own previous capture (pid={holder[0]}, {holder[1]:.0f}s ago) may still hold the port; stop it or wait"
        elif "Access is denied" in msg or "PermissionError" in msg:
            others = [p.device for p in list_ports.comports()]
            busy_hint = ("port is open by another program - usual suspects: "
                         "VSCode Serial Monitor (bottom status bar), PuTTY/MobaXterm, "
                         "a leftover python serial process")
        result["error"] = f"cannot open {device}: {msg}"
        print(f"[!] {result['error']}")
        if busy_hint:
            print(f"[i] {busy_hint}")
        result["hint"] = busy_hint
        print_port_table()
        _emit(result, args)
        return 2

    write_lock(device)
    started = time.time()
    n_lines = n_bytes = 0
    try:
        ser.reset_input_buffer()
        ser.dtr = args.dtr
        ser.rts = args.rts
        result["opened"] = True
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# === capture start  {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                     f"{device}@{args.baud}  window={args.seconds}s ===\n")
            pending = b""
            while True:
                now = time.time()
                left = args.seconds - (now - started)
                if left <= 0:
                    break
                chunk = ser.read(max(1, min(1024, ser.in_waiting or 64)))
                if chunk:
                    n_bytes += len(chunk)
                    pending += chunk
                    while b"\n" in pending:
                        raw, _, pending = pending.partition(b"\n")
                        line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
                        n_lines += 1
                        stamp = f"[{now - started:7.2f}] " if args.ts else ""
                        fh.write(f"{stamp}{line}\n")
                        fh.flush()
                # flush partial last line at deadline handled below
            if pending.strip():
                line = pending.rstrip(b"\r").decode("utf-8", errors="replace")
                n_lines += 1
                fh.write(f"{'[' + format(time.time() - started, '.2f') + '] ' if args.ts else ''}{line}\n")
            fh.write(f"\n# === capture end  {n_lines} lines, {n_bytes} bytes, "
                     f"{time.time() - started:.2f}s ===\n")
    finally:
        try:
            ser.close()
        finally:
            remove_lock(device)

    elapsed = time.time() - started
    print(f"[+] captured {n_lines} lines / {n_bytes} bytes in {elapsed:.2f}s from {device} ({desc}) -> {args.out}")
    if n_lines == 0:
        print("[!] port opened but NOTHING arrived - MCU may be silent (crashed early, wrong baud, TX wired elsewhere); see troubleshooting.md")

    tail: list[str] = []
    if args.echo > 0:
        with open(args.out, encoding="utf-8", errors="replace") as fh:
            rows = fh.read().splitlines()
        body = [r for r in rows if not r.startswith("#")]
        tail = body[-args.echo:]
        print(f"--- last {len(tail)} lines ---")
        for r in tail:
            print(r)
    result.update(lines=n_lines, bytes=n_bytes, elapsed=round(elapsed, 2), tail=tail)
    _emit(result, args)
    return 0


def _emit(result: dict, args) -> None:
    if args.json:
        print("=== CAPTURE_JSON === " + json.dumps({k: v for k, v in result.items()}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
