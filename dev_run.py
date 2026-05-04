"""Restart finance_bot.py when local source or finance_kb.json changes (development)."""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOT = ROOT / "finance_bot.py"
POLL_INTERVAL = 0.6


def _collect_watch_files():
    files = [p for p in ROOT.glob("*.py") if p.is_file()]
    kb = ROOT / "finance_kb.json"
    if kb.is_file():
        files.append(kb)
    return sorted(files)


def _mtime_snapshot(paths):
    snap = {}
    for p in paths:
        try:
            snap[p] = p.stat().st_mtime
        except OSError:
            continue
    return snap


def main():
    if not BOT.is_file():
        print("finance_bot.py not found next to dev_run.py")
        sys.exit(1)

    cmd = [sys.executable, str(BOT)]
    proc = subprocess.Popen(cmd)
    mtimes = _mtime_snapshot(_collect_watch_files())
    print("Watching for changes — Ctrl+C to stop.")

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            paths = _collect_watch_files()
            changed = False
            for p in paths:
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                old = mtimes.get(p)
                if old is None or m > old:
                    mtimes[p] = m
                    changed = True

            crashed = proc.poll() is not None

            if not changed and not crashed:
                continue

            if crashed and not changed:
                print("Bot exited unexpectedly — restarting...")

            if changed:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                print("File change detected — restarting bot...")

            proc = subprocess.Popen(cmd)

            # Avoid duplicate restart if mtime rounded within same poll window
            mtimes.update(_mtime_snapshot(paths))

    except KeyboardInterrupt:
        print("\nStopping...")
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
