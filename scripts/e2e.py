#!/opt/homebrew/bin/python3 -I
"""E2E suite for rayserial, with Python's stdlib as the independent oracle.

- termios: Python's termios.tcgetattr on the master of a pty it owns reads
  back what rayserial applied to the slave (the pair shares one termios).
- bytes: Python writes/reads the master as an independent peer.
- the fake board (rayserial fake-board) is the Arduino stand-in for the
  rest; the interactive terminal runs on a second pty that plays the
  user's terminal.

Every process started here is a child of this script (subprocess.Popen),
self-terminating (--for) and, at worst, stopped through its own Popen
handle. Nothing is ever killed by name or port.

usage: e2e.py --engine vm|native [-k FILTER]
"""

import argparse
import glob
import hashlib
import os
import pty
import random
import select
import subprocess
import sys
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "e2e")

# macOS <sys/termios.h>; Python's termios module lacks some of them.
CRTSCTS = getattr(termios, "CRTSCTS", 0x30000)

RESULTS = []
CHILDREN = []


def ok(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "ok  " if cond else "FAIL"
    print(f"{mark}  {name}" + (f"  -- {detail}" if detail and not cond else ""), flush=True)
    return cond


def cmd():
    if ENGINE == "native":
        return [os.path.join(ROOT, "rayserial")]
    return ["ray", "run", os.path.join(ROOT, "src", "main.ray")]


def spawn(args, **kw):
    p = subprocess.Popen(cmd() + args, cwd=ROOT, **kw)
    CHILDREN.append(p)
    return p


def run(args, timeout=60, stdin=None, input_bytes=None):
    p = subprocess.run(cmd() + args, cwd=ROOT, capture_output=True, timeout=timeout,
                       stdin=stdin, input=input_bytes)
    return p.returncode, p.stdout, p.stderr


def stop(p, grace=10):
    """Waits for our own child; SIGTERM through its Popen handle if needed."""
    try:
        return p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            return p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            return p.wait()


class Board:
    """A rayserial fake-board child; .port is its slave path."""

    def __init__(self, *args, secs=60):
        self.p = spawn(["fake-board", "--for", str(secs)] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        line = self.p.stdout.readline().decode()
        if not line.startswith("PORT "):
            raise RuntimeError(f"fake-board did not start: {line!r} {self.p.stderr.read()!r}")
        self.port = line.split()[1]

    def finish(self):
        """SIGTERM (graceful: the board prints its RX/TX line) and collect."""
        self.p.terminate()
        out, err = self.p.communicate(timeout=20)
        return out.decode(), err.decode()


def read_for(fd, secs):
    data = b""
    end = time.time() + secs
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
    return data


def read_until(fd, needle, secs=10, acc=b""):
    data = acc
    end = time.time() + secs
    while needle not in data and time.time() < end:
        r, _, _ = select.select([fd], [], [], max(0.0, end - time.time()))
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
    return data


def read_exact(fd, n, secs=30):
    data = bytearray()
    end = time.time() + secs
    while len(data) < n and time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            data += os.read(fd, n - len(data))
    return bytes(data)


def wait_raw(fd, secs=15):
    """Waits until the pty's termios shows raw mode (rayserial configured it)."""
    end = time.time() + secs
    while time.time() < end:
        if not (termios.tcgetattr(fd)[3] & termios.ICANON):
            return True
        time.sleep(0.01)
    return False


# --- termios oracle -----------------------------------------------------------

CASES = [
    (["-b", "115200"], 115200, termios.CS8, "none", 1, "none"),
    (["-b", "9600", "--data", "7", "--parity", "even"], 9600, termios.CS7, "even", 1, "none"),
    (["-b", "57600", "--parity", "odd", "--stop", "2", "--flow", "rtscts"], 57600, termios.CS8, "odd", 2, "rtscts"),
    (["-b", "19200", "--data", "5", "--flow", "xonxoff"], 19200, termios.CS5, "none", 1, "xonxoff"),
    (["-b", "300", "--data", "6", "--parity", "odd"], 300, termios.CS6, "odd", 1, "none"),
    (["-b", "250000"], 250000, termios.CS8, "none", 1, "none"),
    (["-b", "1000000"], 1000000, termios.CS8, "none", 1, "none"),
    (["-b", "74880"], 74880, termios.CS8, "none", 1, "none"),
]


def unpend(a):
    a = list(a)
    a[3] &= ~0x20000000
    return a


def check_attrs(a, baud, size, parity, stop_bits, flow):
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = a
    errs = []
    if cflag & termios.CSIZE != size:
        errs.append(f"CSIZE {cflag & termios.CSIZE:#x} != {size:#x}")
    if bool(cflag & termios.PARENB) != (parity != "none"):
        errs.append("PARENB")
    if parity != "none" and bool(cflag & termios.PARODD) != (parity == "odd"):
        errs.append("PARODD")
    if bool(cflag & termios.CSTOPB) != (stop_bits == 2):
        errs.append("CSTOPB")
    if (cflag & CRTSCTS == CRTSCTS) != (flow == "rtscts"):
        errs.append("CRTSCTS")
    if not (cflag & termios.CREAD) or not (cflag & termios.CLOCAL):
        errs.append("CREAD|CLOCAL")
    if bool(iflag & termios.IXON) != (flow == "xonxoff") or bool(iflag & termios.IXOFF) != (flow == "xonxoff"):
        errs.append("IXON/IXOFF")
    for name in ("ICRNL", "INLCR", "IGNCR", "ISTRIP", "BRKINT", "PARMRK"):
        if iflag & getattr(termios, name):
            errs.append(name)
    if oflag & termios.OPOST:
        errs.append("OPOST")
    for name in ("ICANON", "ECHO", "ECHONL", "ISIG", "IEXTEN"):
        if lflag & getattr(termios, name):
            errs.append(name)
    if cc[termios.VMIN] not in (1, b"\x01") or cc[termios.VTIME] not in (0, b"\x00"):
        errs.append(f"VMIN/VTIME {cc[termios.VMIN]!r}/{cc[termios.VTIME]!r}")
    if ispeed != baud or ospeed != baud:
        errs.append(f"speed {ispeed}/{ospeed} != {baud}")
    return errs


def test_termios_oracle():
    for args, baud, size, parity, stop_bits, flow in CASES:
        m, s = pty.openpty()
        path = os.ttyname(s)
        before = termios.tcgetattr(m)
        p = spawn(["capture", path, "-o", os.devnull, "--for", "2"] + args,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw = wait_raw(m)
        time.sleep(0.2)
        attrs = termios.tcgetattr(m)
        stop(p)
        after = termios.tcgetattr(m)
        label = " ".join(args)
        errs = check_attrs(attrs, baud, size, parity, stop_bits, flow) if raw else ["never went raw"]
        ok(f"termios oracle: {label}", not errs, ", ".join(errs))
        # PENDIN (0x20000000) is set by the macOS kernel itself when a tty
        # goes back to canonical mode: not part of what rayserial restores.
        ok(f"termios restored after exit: {label}", unpend(after) == unpend(before), f"{before} vs {after}")
        os.close(m)
        os.close(s)


# --- bytes with Python as the peer -----------------------------------------------


def test_bytes_python_peer():
    data = bytes(range(256)) * 4 + random.Random(7).randbytes(256 * 1024)
    # Python → rayserial capture
    m, s = pty.openpty()
    path = os.ttyname(s)
    target = os.path.join(OUT, "capture_all.bin")
    p = spawn(["capture", path, "-o", target, "--bytes", str(len(data)), "--for", "30"],
              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wait_raw(m)
    time.sleep(0.2)
    os.write(m, data)  # blocking master: waits for the reader
    code = stop(p, 30)
    got = open(target, "rb").read()
    ok("capture: 256 octet values ×4 + 256 KiB random from a Python peer", code == 0 and got == data,
       f"exit {code}, {len(got)} of {len(data)} bytes")
    os.close(m)
    os.close(s)
    # rayserial send → Python
    m, s = pty.openpty()
    path = os.ttyname(s)
    src = os.path.join(OUT, "send_all.bin")
    open(src, "wb").write(data)
    p = spawn(["send", path, src], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    got = read_exact(m, len(data))
    code = stop(p, 30)
    ok("send: the same bytes read by a Python peer", code == 0 and got == data,
       f"exit {code}, {len(got)} of {len(data)} bytes")
    # stdin as the source
    p = spawn(["send", path, "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(bytes(range(256)))
    p.stdin.close()
    got = read_exact(m, 256)
    code = stop(p, 30)
    ok("send -: stdin streamed to the port", code == 0 and got == bytes(range(256)))
    os.close(m)
    os.close(s)


# --- against the fake board -------------------------------------------------------

SCRIPT_OK = r"""# the board's happy path
timeout 3s
reset
expect "ready"
sendline led on
expect "LED ON"
sendline led?
expect "LED is ON"
send "pi"
send "ng\r"
expect "pong"
baud 9600
sendline baud?
expect "baud 9600"
expect /sensor n=\d+ temp=\d+\.\d hum=\d+/
reset 50ms
expect "ready"
sendline led?
expect "LED is OFF"
sendline boots?
expect /boots [3-9]/
log done
"""

SCRIPT_FAIL = """timeout 300ms
sendline ping
expect "pong"
expect "this never comes"
"""


def test_expect():
    b = Board("--sensor-ms", "300")
    sp = os.path.join(OUT, "ok.exp")
    open(sp, "w").write(SCRIPT_OK)
    code, out, err = run(["expect", b.port, sp, "--quiet"])
    ok("expect: reset/banner, led, ping split over two sends, baud change seen by the board, regex, second reset",
       code == 0 and b"script ok (9 expectation" in err, f"exit {code} {err[-300:]!r}")
    fp = os.path.join(OUT, "fail.exp")
    open(fp, "w").write(SCRIPT_FAIL)
    code, out, err = run(["expect", b.port, fp, "-q"])
    ok("expect: a missing answer fails with the line number and what came", code == 1
       and b"line 4: timeout (300 ms)" in err and b"last received" in err, f"exit {code} {err!r}")
    bad = os.path.join(OUT, "bad.exp")
    open(bad, "w").write("sendline x\nfrobnicate 3\n")
    code, out, err = run(["expect", b.port, bad])
    ok("expect: a bad script is refused before opening", code == 1 and b"line 2: unknown command" in err)
    b.finish()


def test_dump_binary():
    b = Board("--sensor-ms", "0")
    target = os.path.join(OUT, "dump.bin")
    code, out, err = run(["capture", b.port, "-o", target, "--send", "dump 1000\\r", "--idle", "600", "--for", "20"])
    got = open(target, "rb").read()
    body = bytes(i % 256 for i in range(1000))
    want = b"dump 1000\r\nBEGIN 1000\r\n" + body + b"\r\nEND\r\n"
    ok("capture --send: board's binary dump (0..255 ×) arrives byte-exact", code == 0 and got == want,
       f"{len(got)} bytes, first diff at {next((i for i in range(min(len(got), len(want))) if got[i] != want[i]), None)}")
    b.finish()


def test_echo_roundtrip():
    data = random.Random(11).randbytes(256 * 1024) + bytes(range(256))
    src = os.path.join(OUT, "echo_in.bin")
    rx = os.path.join(OUT, "echo_out.bin")
    open(src, "wb").write(data)
    b = Board("--mode", "echo")
    code, out, err = run(["send", b.port, src, "--rx", rx, "--rx-idle", "800", "--chunk", "512"], timeout=120)
    got = open(rx, "rb").read()
    ok("send --rx through the echo board: 256 KiB random + all octets round-trip", code == 0 and got == data,
       f"exit {code}, {len(got)} of {len(data)} {err[-200:]!r}")
    b.finish()


def board_sha(text, tag):
    for line in text.splitlines():
        if line.startswith(tag + " "):
            parts = line.split()
            return int(parts[1]), parts[4]
    return None, None


def test_sink_and_source():
    data = random.Random(5).randbytes(1024 * 1024)
    src = os.path.join(OUT, "sink_in.bin")
    open(src, "wb").write(data)
    b = Board("--mode", "sink")
    code, _, err = run(["send", b.port, src], timeout=120)
    out, _ = b.finish()
    n, sha = board_sha(out, "RX")
    ok("send → sink board: 1 MiB, sha256 equal to hashlib's", code == 0 and n == len(data)
       and sha == hashlib.sha256(data).hexdigest(), f"{n} {sha} {err[-200:]!r}")
    total = 1024 * 1024 + 77
    b = Board("--mode", "source", "--bytes", str(total))
    target = os.path.join(OUT, "source.bin")
    code, _, err = run(["capture", b.port, "-o", target, "--bytes", str(total), "--send", "go", "--for", "60"],
                       timeout=120)
    out, _ = b.finish()
    n, sha = board_sha(out, "TX")
    got = open(target, "rb").read()
    ok("capture ← source board: 1 MiB + 77, sha256 equal on both sides", code == 0 and n == total
       and len(got) == total and hashlib.sha256(got).hexdigest() == sha, f"{n} {len(got)}")


def test_slow_peer():
    data = random.Random(9).randbytes(150 * 1024)
    src = os.path.join(OUT, "slow_in.bin")
    open(src, "wb").write(data)
    b = Board("--mode", "sink", "--rate", "50000")
    t0 = time.time()
    code, _, err = run(["send", b.port, src], timeout=120)
    secs = time.time() - t0
    out, _ = b.finish()
    n, sha = board_sha(out, "RX")
    ok(f"slow peer (50 kB/s): send waits instead of losing data ({secs:.1f} s for 150 KiB)",
       code == 0 and n == len(data) and sha == hashlib.sha256(data).hexdigest() and secs >= 2.5,
       f"{n} {secs:.2f}s {err[-200:]!r}")


def test_timestamps():
    b = Board("--sensor-ms", "200")
    target = os.path.join(OUT, "stamped.txt")
    code, _, _ = run(["capture", b.port, "-o", target, "--for", "1.5", "--timestamps"])
    lines = [l for l in open(target, "rb").read().split(b"\n") if l.strip()]
    good = all(l.startswith(b"[20") and b"Z] sensor n=" in l for l in lines)
    ok(f"capture --timestamps: every line stamped ({len(lines)} lines)", code == 0 and len(lines) >= 4 and good,
       repr(lines[:3]))
    b.finish()


def test_piped_open():
    b = Board("--sensor-ms", "0")
    code, out, err = run(["open", b.port, "--for", "1.5", "--quiet"], input_bytes=b"ping\rled on\r")
    ok("open with piped stdin (no tty): keys go out, answers come back", code == 0
       and b"pong" in out and b"LED ON" in out, f"{code} {out!r} {err!r}")
    b.finish()


def test_interactive():
    b = Board("--sensor-ms", "0")
    um, us = pty.openpty()  # the user's terminal
    before = termios.tcgetattr(um)
    p = spawn(["open", b.port, "--crlf"], stdin=us, stdout=us, stderr=us, start_new_session=True)
    acc = read_until(um, b"Ctrl-A x quits", 20)
    ok("open: banner/status line on the user's terminal", b"115200 8N1" in acc, repr(acc[-200:]))
    a = termios.tcgetattr(um)
    raw = not (a[3] & (termios.ICANON | termios.ECHO | termios.ISIG)) and not (a[1] & termios.OPOST)
    ok("open: the local terminal is raw while the session runs (oracle: tcgetattr)", raw,
       f"lflag={a[3]:#x} oflag={a[1]:#x} iflag={a[0]:#x}")
    os.write(um, b"ping\r")
    acc = read_until(um, b"pong", 10)
    ok("open: typed line reaches the board, answer comes back", b"pong" in acc, repr(acc[-120:]))
    os.write(um, b"\x01h")
    acc = read_until(um, b"hex view on", 5)
    os.write(um, b"led?\r")
    acc = read_until(um, b"0d 0a", 5, b"")
    ok("open: Ctrl-A h shows the board's answer in hex", b"4c 45 44 20 69 73" in acc, repr(acc[-160:]))
    os.write(um, b"\x01h")
    read_until(um, b"hex view off", 5)
    os.write(um, b"\x01b9600\r")
    acc = read_until(um, b"*** baud 9600", 5)
    os.write(um, b"baud?\r")
    acc = read_until(um, b"baud 9600\r\n", 5, acc)
    ok("open: Ctrl-A b changes the speed (the board reads 9600 from termios)", acc.count(b"baud 9600") >= 2,
       repr(acc[-160:]))
    os.write(um, b"\x01p")
    acc = read_until(um, b"ready", 5)
    ok("open: Ctrl-A p pulses DTR and the board reboots (banner)", b"DTR pulsed" in acc and b"ready" in acc,
       repr(acc[-200:]))
    os.write(um, b"\x01r")
    acc = read_until(um, b"RTS", 5)
    ok("open: Ctrl-A r on a pty reports the missing modem ioctl", b"Inappropriate ioctl" in read_until(um, b"\r\n", 2, acc))
    os.write(um, b"\x01s")
    acc = read_until(um, b"received=", 5)
    ok("open: Ctrl-A s status from tcgetattr", b"9600 8N1" in acc, repr(acc[-200:]))
    os.write(um, b"\x01x")
    tail = read_until(um, b"bye", 5)
    code = stop(p, 10)
    after = termios.tcgetattr(um)
    ok("open: Ctrl-A x quits with exit 0", code == 0, f"exit {code} {tail!r}")
    ok("open: the local terminal is restored exactly (oracle: tcgetattr; PENDIN aside)",
       unpend(after) == unpend(before), f"{before} vs {after}")
    os.close(um)
    os.close(us)
    b.finish()


def test_board_port_goes_away():
    b = Board("--sensor-ms", "0", secs=2)
    t0 = time.time()
    code, out, err = run(["open", b.port, "--quiet", "--for", "20"], input_bytes=None, stdin=subprocess.DEVNULL)
    ok("open: the session ends when the board goes away", time.time() - t0 < 10, f"{code} {err!r}")
    b.finish()


def test_python_reads_fake_board():
    """The fake board seen by an independent client (Python's termios + os)."""
    b = Board("--sensor-ms", "0")
    fd = os.open(b.port, os.O_RDWR | os.O_NOCTTY)
    import tty
    tty.setraw(fd)
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, b"led on\r")
    acc = read_until(fd, b"LED ON\r\n", 5)
    ok("fake board answers an independent (Python) client", b"led on\r\nLED ON\r\n" in acc, repr(acc))
    # Python drops DTR the POSIX way (B0) → the board reboots
    a = termios.tcgetattr(fd)
    a[4] = a[5] = termios.B0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    time.sleep(0.1)
    a[4] = a[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, a)
    acc = read_until(fd, b"ready", 5)
    ok("fake board detects a B0 DTR drop from an independent client", b"ready" in acc, repr(acc))
    os.close(fd)
    b.finish()


def test_list_and_errors():
    code, out, err = run(["list", "--all"])
    mine = sorted(l.split("\t")[0] for l in out.decode().splitlines() if l.strip())
    pats = ["/dev/cu.*", "/dev/tty.usb*", "/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*", "/dev/rfcomm*"]
    oracle = sorted(set(p for pat in pats for p in glob.glob(pat)))
    ok(f"list --all equals a glob of /dev ({len(oracle)} ports)", code == 0 and mine == oracle, f"{mine} vs {oracle}")
    code, out, err = run(["open", "/dev/null"])
    ok("open /dev/null: refused, not a terminal", code == 1 and b"not a terminal device" in err, repr(err))
    code, out, err = run(["open", "/dev/cu.rayserial-missing"])
    ok("open of a missing port: strerror + errno", code == 1 and b"No such file or directory (errno 2)" in err,
       repr(err))
    code, out, err = run(["open", "/dev/null", "--parity", "mark"])
    ok("bad line option is refused", code == 1 and b"parity must be" in err, repr(err))


TESTS = [
    test_termios_oracle, test_bytes_python_peer, test_expect, test_dump_binary, test_echo_roundtrip,
    test_sink_and_source, test_slow_peer, test_timestamps, test_piped_open, test_interactive,
    test_board_port_goes_away, test_python_reads_fake_board, test_list_and_errors,
]


def main():
    global ENGINE
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["vm", "native"], default="vm")
    ap.add_argument("-k", default="")
    a = ap.parse_args()
    ENGINE = a.engine
    os.makedirs(OUT, exist_ok=True)
    if ENGINE == "native" and not os.path.exists(os.path.join(ROOT, "rayserial")):
        sys.exit("build ./rayserial first (make build)")
    t0 = time.time()
    try:
        for t in TESTS:
            if a.k and a.k not in t.__name__:
                continue
            try:
                t()
            except Exception as e:  # a crashed scenario is a failure, not an abort
                ok(t.__name__, False, f"{type(e).__name__}: {e}")
    finally:
        for p in CHILDREN:
            if p.poll() is None:
                stop(p, 5)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{ENGINE}: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed in {time.time() - t0:.0f} s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
