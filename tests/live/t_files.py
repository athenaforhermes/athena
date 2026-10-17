"""
What a file's bytes are, before anyone renders them.

Two answers live here, and both were wrong in the same direction: the reader
decided "text or binary" by looking for a NUL byte, and the watcher answered an
empty path with `Path.home()`.

The first is a real file loss on this machine. UTF-16 is what Windows writes on
its own — PowerShell's `>` redirect, `wsl --list --verbose`, some .ini and .log
files — and every one of those is full of NUL bytes, so every one of them
previewed as "binary, nothing to show". The same file was searchable by `/search`,
which had decoded UTF-16 from the day it was written, and unreadable by `/read`,
which had not. One route asking the question properly is not a licence for the
other to guess.

The second is the audit's find: a caller who named no folder got a fingerprint of
a folder they never named, because the fallback was the person's own home rather
than Athena's. Nothing was leaked by a fingerprint, but it was an answer to a
question nobody asked.

So this file checks the sniffing table (the marks, the NUL pattern, the wrong
encoding that still decodes), the route that uses it (text, encoding, truncation,
and a real binary), and the watcher's fallback. Everything runs against a
synthetic home: the browser's own `enforce` is left in place, which is the point
— the sandbox is exercised, not bypassed.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")

PASS = FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name}" + (f"  -- {detail}" if detail else ""))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


browser = load("athena_files_under_test", PLUGIN / "dashboard" / "files.py")

# ------------------------------------------------------------------ a home

TMP = Path(tempfile.mkdtemp(prefix="athena-files-")).resolve()
HOME = TMP / "athena"
(HOME / "logs").mkdir(parents=True, exist_ok=True)

# The browser resolves its own home from a settings file it ships next to. Pinned
# here to a synthetic one instead, so these checks cannot read or write the real
# Athena's folders — and `_fixed_drives` is emptied for the same reason: a real
# `C:` in the roots would make an out-of-sandbox path look legal.
browser._athena_home = lambda: HOME
browser._fixed_drives = lambda: []

WSL = "wsl --list --verbose\n  * Ubuntu    Running    2\n"
browser_home_files = {
    # What PowerShell's `>` writes: UTF-16 little-endian with a mark.
    "ps.log": WSL.encode("utf-16"),
    # The same, from a tool that writes UTF-16 with no mark — the case the NUL
    # pattern has to catch on its own.
    "wsl.log": WSL.encode("utf-16-le"),
    "be.ini": "key=value\n[section]\n".encode("utf-16-be"),
    "plain.txt": "hello world\n".encode("utf-8"),
    "accented.txt": "caf\u00e9 na\u00efve \u2014 dash\n".encode("utf-8"),
    # A real binary: every byte value, so any encoding "succeeds" and only the
    # NUL pattern can refuse it.
    "blob.bin": bytes(range(256)) * 8,
    "small.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
}
for name, blob in browser_home_files.items():
    (HOME / name).write_bytes(blob)

# Long enough to be cut by a 1 KB read, which lands the cut mid-character on any
# UTF-16 file: 900 two-byte characters, then a byte more.
(HOME / "logs" / "long.log").write_bytes(("\u00e9" * 900 + "z").encode("utf-16-le"))

ReadIn = browser.ReadIn
WatchIn = browser.WatchIn


def body(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


def read(name: str, max_kb: int = 64) -> dict:
    return body(browser.read_text(ReadIn(path=str(HOME / name), max_kb=max_kb)))


sniff = browser.sniff_text

# ------------------------------------------------------- the sniffing table

# The regression itself, stated first: a UTF-16 file is text, and it used to be
# reported as binary.
got = sniff(browser_home_files["wsl.log"])
check("a UTF-16LE file is text, not binary", got is not None and got[1] == "utf-16-le",
      f"got {got if got is None else got[1]!r}")
check("its text is the file's text", got is not None and got[0] == WSL,
      f"got {got[0]!r}" if got else "None")

got = sniff(browser_home_files["ps.log"])
check("a mark is believed, and not left in the text",
      got is not None and got[1] == "utf-16-le" and got[0] == WSL and "\ufeff" not in got[0],
      f"got {got!r}")

got = sniff(browser_home_files["be.ini"])
check("big-endian NULs are read as big-endian", got is not None and got[1] == "utf-16-be",
      f"got {got if got is None else got[1]!r}")

check("UTF-8 is still UTF-8", sniff("caf\u00e9\n".encode("utf-8")) == ("caf\u00e9\n", "utf-8"))
check("a mark on UTF-8 is stripped, not shown",
      sniff("\ufeffkey=value\n".encode("utf-8-sig")) == ("key=value\n", "utf-8-sig"))

# The fallback is named rather than silent: a caller can tell "this decoded as
# UTF-8" from "this decoded only because latin-1 accepts anything".
check("latin-1 is the named last resort, not a silent one",
      sniff(b"caf\xe9\n") == ("caf\u00e9\n", "latin-1"))

check("every byte value is binary, whatever it happens to decode as",
      sniff(browser_home_files["blob.bin"]) is None)
check("a PNG header is binary", sniff(browser_home_files["small.png"]) is None)

# Binary has to stay binary: the loose direction of a sniffer is the dangerous
# one, and "NULs in a pattern" must not become "any file with a NUL".
check("an even-length binary is not mistaken for 16-bit text",
      sniff(bytes([0, 1]) * 200) is None)

# A read stops at a byte cap, and on UTF-16 that cut lands mid-character. That is
# the reader's cut, not the file's problem, so the odd trailing byte is dropped.
half = "abcdef".encode("utf-16-le")[:-1]
check("a cap that split a character still reads", sniff(half) == ("abcde", "utf-16-le"))

check("an empty file is empty text, not binary", sniff(b"") == ("", "utf-8"))
check("a file shorter than the pattern needs is not guessed at", sniff(b"\x00") is None)

# ------------------------------------------------------------- through /read

r = read("wsl.log")
check("/read answers a UTF-16 file with text", r.get("is_binary") is False and r.get("text") == WSL,
      f"got {r}")
check("/read says which encoding it used", r.get("encoding") == "utf-16-le", f"got {r.get('encoding')!r}")
check("/read is not truncated when the file fits", r.get("truncated") is False)

r = read("ps.log")
check("/read of a marked file shows no mark", r.get("text") == WSL, f"got {r.get('text')!r}")

r = read("plain.txt")
check("/read of UTF-8 is unchanged", r.get("text") == "hello world\n" and r.get("encoding") == "utf-8")

r = read("blob.bin")
check("/read refuses a real binary",
      r.get("is_binary") is True and "text" not in r, f"got {r}")

r = read("accented.txt")
check("/read keeps accents intact", r.get("text", "").startswith("caf\u00e9 na\u00efve"),
      f"got {r.get('text')!r}")

r = read("logs/long.log", max_kb=1)
check("/read cuts a long UTF-16 file without calling it binary",
      r.get("is_binary") is False and r.get("truncated") is True, f"got {r}")
check("the cut text is what the bytes said",
      r.get("text", "").startswith("\u00e9\u00e9") and len(r.get("text", "")) in (512, 513),
      f"len {len(r.get('text', ''))}")

# ------------------------------------------------------------- the sandbox

# `enforce` is what makes the missing folder in the audit harmless, so it is
# exercised rather than stubbed: a path outside the roots is refused, and the
# refusal is an HTTP error rather than a body the caller has to inspect.
outside = TMP / "outside.txt"
outside.write_text("not mine\n", encoding="utf-8")
try:
    browser.read_text(ReadIn(path=str(outside), max_kb=4))
    refused = False
except Exception:
    refused = True
check("a path outside the roots is refused, not read", refused)

# ------------------------------------------------------------- the watcher

r = body(browser.watch_dir(WatchIn(path="")))
check("/watch with no path answers about Athena's home",
      r.get("ok") is True and Path(r.get("path", "")) == HOME, f"got {r}")

r = body(browser.watch_dir(WatchIn(path=str(HOME / "plain.txt"))))
check("/watch on a file is refused as a folder",
      r.get("error") == "not a folder" and r.get("ok") is None, f"got {r}")

before = body(browser.watch_dir(WatchIn(path=str(HOME))))["fingerprint"]
(HOME / "added.txt").write_text("x\n", encoding="utf-8")
after = body(browser.watch_dir(WatchIn(path=str(HOME))))["fingerprint"]
check("the fingerprint moves when the folder does", before != after,
      f"{before!r} -> {after!r}")

# The fingerprint is a fingerprint, not a listing: it says "something changed"
# without carrying names, and that is what makes it cheap enough to poll.
check("the fingerprint carries no file names",
      "added.txt" not in json.dumps(body(browser.watch_dir(WatchIn(path=str(HOME))))))

print(f"\nt_files: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + ", ".join(FAILURES))
sys.exit(1 if FAIL else 0)
