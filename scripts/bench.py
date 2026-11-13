#!/opt/homebrew/bin/python3 -I
"""Throughput of rayserial over a pty, VM (`ray run`) vs native (./rayserial).

Rates are rayserial's own numbers (from opening the port to the last octet,
startup excluded). The pty itself is the ceiling: a Python→Python transfer
over the same kind of pair is measured first.

Every process is our own child; the boards end by --exit-idle/--for or
through their Popen handle.

usage: bench.py [--mib N] [--runs N]
"""

import argparse
import hashlib
import os
import pty
import random
import re
import select
import statistics
import subprocess
import termios
import threading
import time
import tty

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "bench")
ENGINES = {
    "VM": ["ray", "run", os.path.join(ROOT, "src", "main.ray")],
    "native": [os.path.join(ROOT, "rayserial")],
}
RATE = re.compile(rb"(\d+) bytes in (\d+) ms")


def mbs(n, secs):
    return n / secs / 1e6 if secs > 0 else 0.0


def rate_of(stderr):
    m = RATE.search(stderr)
    if not m:
        raise RuntimeError(f"no rate in {stderr!r}")
    return int(m.group(1)), int(m.group(2)) / 1000.0


def wait_raw(fd):
    end = time.time() + 20
    while time.time() < end and termios.tcgetattr(fd)[3] & termios.ICANON:
        time.sleep(0.005)


def python_baseline(data):
    m, s = pty.openpty()
    tty.setraw(s)
    got = bytearray()

    def reader():
        while len(got) < len(data):
            got.extend(os.read(s, 65536))

    t = threading.Thread(target=reader)
    t0 = time.time()
    t.start()
    os.write(m, data)
    t.join()
    secs = time.time() - t0
    os.close(m)
    os.close(s)
    assert bytes(got) == data
    return mbs(len(data), secs)


def capture_from_python(engine, data):
    m, s = pty.openpty()
    path = os.ttyname(s)
    target = os.path.join(OUT, "cap.bin")
    p = subprocess.Popen(engine + ["capture", path, "-o", target, "--bytes", str(len(data)), "--for", "120"],
                         stderr=subprocess.PIPE)
    wait_raw(m)
    os.write(m, data)
    _, err = p.communicate(timeout=180)
    os.close(m)
    os.close(s)
    assert open(target, "rb").read() == data, "capture mismatch"
    n, secs = rate_of(err)
    return mbs(n, secs)


def send_to_python(engine, data):
    m, s = pty.openpty()
    path = os.ttyname(s)
    src = os.path.join(OUT, "send.bin")
    open(src, "wb").write(data)
    p = subprocess.Popen(engine + ["send", path, src], stderr=subprocess.PIPE)
    got = bytearray()
    while len(got) < len(data):
        r, _, _ = select.select([m], [], [], 30)
        if not r:
            break
        got.extend(os.read(m, 65536))
    _, err = p.communicate(timeout=180)
    os.close(m)
    os.close(s)
    assert bytes(got) == data, "send mismatch"
    n, secs = rate_of(err)
    return mbs(n, secs)


def board(engine, *args):
    p = subprocess.Popen(engine + ["fake-board", "--for", "300"] + list(args), stdout=subprocess.PIPE)
    port = p.stdout.readline().split()[1].decode()
    return p, port


def send_to_sink(client, board_engine, data):
    p, port = board(board_engine, "--mode", "sink", "--exit-idle", "1500")
    src = os.path.join(OUT, "sink.bin")
    open(src, "wb").write(data)
    r = subprocess.run(client + ["send", port, src], capture_output=True, timeout=300)
    out, _ = p.communicate(timeout=60)
    assert hashlib.sha256(data).hexdigest().encode() in out, f"sink sha mismatch {out!r}"
    n, secs = rate_of(r.stderr)
    return mbs(n, secs)


def capture_from_source(client, board_engine, n):
    p, port = board(board_engine, "--mode", "source", "--bytes", str(n), "--exit-idle", "1500")
    target = os.path.join(OUT, "src.bin")
    r = subprocess.run(client + ["capture", port, "-o", target, "--bytes", str(n), "--send", "go", "--for", "300"],
                       capture_output=True, timeout=300)
    out, _ = p.communicate(timeout=60)
    sha = hashlib.sha256(open(target, "rb").read()).hexdigest()
    assert sha.encode() in out, "source sha mismatch"
    m, secs = rate_of(r.stderr)
    return mbs(m, secs)


def echo_roundtrip(client, board_engine, data):
    p, port = board(board_engine, "--mode", "echo", "--exit-idle", "2500")
    src = os.path.join(OUT, "echo.bin")
    rx = os.path.join(OUT, "echo_rx.bin")
    open(src, "wb").write(data)
    r = subprocess.run(client + ["send", port, src, "--rx", rx, "--rx-idle", "500", "--chunk", "512"],
                       capture_output=True, timeout=300)
    p.communicate(timeout=60)
    assert open(rx, "rb").read() == data, "echo mismatch"
    n, secs = rate_of(r.stderr)
    return mbs(n, secs)


def best(f, runs):
    vals = [f() for _ in range(runs)]
    return max(vals), statistics.median(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mib", type=int, default=4)
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    data = random.Random(1).randbytes(a.mib * 1024 * 1024)
    small = data[: 1024 * 1024]
    nat = ENGINES["native"]
    rows = []
    b, m = best(lambda: python_baseline(data), a.runs)
    rows.append(("pty ceiling: Python → Python", f"{a.mib} MiB", f"{m:.1f}", f"{m:.1f}"))
    for label, size, fn in [
        ("Python peer → `capture`", f"{a.mib} MiB", lambda e: capture_from_python(e, data)),
        ("`send` → Python peer", f"{a.mib} MiB", lambda e: send_to_python(e, data)),
        ("`send` → fake-board sink (native board)", f"{a.mib} MiB", lambda e: send_to_sink(e, nat, data)),
        ("fake-board source (native) → `capture`", f"{a.mib} MiB", lambda e: capture_from_source(e, nat, len(data))),
        ("`send --rx` ↔ echo board (native), 512-octet chunks", "1 MiB", lambda e: echo_roundtrip(e, nat, small)),
        ("same board in the same engine: `send` → sink", f"{a.mib} MiB", lambda e: send_to_sink(e, e, data)),
    ]:
        vals = {}
        for name, eng in ENGINES.items():
            _, med = best(lambda: fn(eng), a.runs)
            vals[name] = med
        rows.append((label, size, f"{vals['VM']:.1f}", f"{vals['native']:.1f}"))
        print(rows[-1], flush=True)
    print(f"\n| Transfer | Size | VM (MB/s) | native (MB/s) |\n|---|---|---|---|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")
    print(f"\nmedian of {a.runs} runs; MB = 10^6 octets; rates from rayserial's own timer")


if __name__ == "__main__":
    main()
