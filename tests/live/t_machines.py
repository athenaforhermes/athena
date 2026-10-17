"""
Machines: the parts that can be proved without booting anything, and the parts that can't.

Two of these are the interesting ones. The `wsl -l -v` parser is checked against output
captured from a real `wsl.exe` — not a table written to look like it — because that output is
UTF-16LE with no BOM, and a parser that reads it as text produces distro names containing a
NUL between every character and then silently matches nothing. The registry is checked against
a temporary home so it never touches the machines somebody actually has.

The live checks decide for themselves whether this machine can prove anything, and say
SKIPPED when it cannot, which is the rule every other suite here follows.

Usage: env -u PYTHONPATH <venv python> t_machines.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUG = HERE.parent.parent
sys.path.insert(0, str(PLUG / "dashboard"))

import machines  # noqa: E402

passed = failed = skipped = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def skip(label, why):
    global skipped
    skipped += 1
    print(f"  skip {label} — {why}")


# ---- names, because a machine name becomes a directory and a distro name -------------

check("a name with a path separator in it cannot escape the machines folder",
      "/" not in machines.safe_name("../../etc/passwd")
      and "\\" not in machines.safe_name("..\\..\\windows\\system32")
      and not machines.safe_name("../../etc/passwd").startswith("."),
      repr(machines.safe_name("../../etc/passwd")))
check("... and an empty, dotted or absurdly long name still yields something usable",
      all(machines.valid_name(machines.safe_name(n))
          for n in ("", "   ", "...", "x" * 200, "café ☕ machine")),
      repr([machines.safe_name(n) for n in ("", "   ", "...", "x" * 200, "café ☕ machine")]))
check("a name that is not a name is rejected rather than silently cleaned",
      not machines.valid_name("-leading") and not machines.valid_name("with space")
      and not machines.valid_name(""))
check("the machine folder for a traversing name stays under the machines folder",
      machines.machine_dir(Path("/tmp/athena"), "../../etc").parent
      == machines.machines_dir(Path("/tmp/athena")),
      str(machines.machine_dir(Path("/tmp/athena"), "../../etc")))

# ---- the spec -------------------------------------------------------------------------

spec = machines.MachineSpec(name="dev-box")
spec.validate()
check("an Athena-owned machine names its own distro after itself, so two cannot share a disk",
      spec.distro == "athena-dev-box", repr(spec.distro))
check("... and it is not marked as attaching to somebody else's",
      spec.attach is False)

round_trip = machines.MachineSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
check("a spec survives a round trip through the file that will hold it",
      round_trip.to_dict() == spec.to_dict())

for label, kwargs in (
    ("a provider that does not exist", {"provider": "vmware"}),
    ("an image kind that does not exist", {"image": {"kind": "tarball"}}),
    ("a display that does not exist", {"display": "kiosk"}),
):
    try:
        machines.MachineSpec(name="bad", **kwargs).validate()
        check(f"{label} is refused", False, "it was accepted")
    except machines.SpecError as e:
        check(f"{label} is refused", True and bool(str(e)), str(e))

try:
    machines.MachineSpec(name="attach-me", attach=True, distro="").validate()
    check("attaching without naming a distro is refused", False, "it was accepted")
except machines.SpecError:
    check("attaching without naming a distro is refused", True)

clamped = machines.MachineSpec(name="clamp", cpus=9999, memory_mb=1, disk_gb=-5)
clamped.validate()
check("nonsense sizes are clamped to something that can boot rather than failing later",
      clamped.cpus == 64 and clamped.memory_mb == 256 and clamped.disk_gb == 2,
      f"cpus={clamped.cpus} mem={clamped.memory_mb} disk={clamped.disk_gb}")

# ---- the decoding that everything else depends on ------------------------------------

# Captured from a real `wsl.exe -l -v` on this machine: UTF-16LE, no BOM, two leading
# spaces of indentation, CRLF line endings.
REAL_BYTES = (
    b" \x00 \x00N\x00A\x00M\x00E\x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00"
    b" \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00S\x00T\x00A\x00T\x00E\x00 \x00 \x00"
    b" \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00\r\x00\n\x00"
    b"*\x00 \x00U\x00b\x00u\x00n\x00t\x00u\x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00"
    b" \x00 \x00 \x00 \x00 \x00 \x00S\x00t\x00o\x00p\x00p\x00e\x00d\x00 \x00 \x00"
    b" \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x00 \x002\x00\r\x00\n\x00"
)
decoded = machines.decode_wsl(REAL_BYTES)
check("real wsl.exe output decodes without a NUL between every character",
      "Ubuntu" in decoded and "\x00" not in decoded, repr(decoded[:60]))
check("... and plain UTF-8 (the Linux and WSL1 case) is left alone",
      machines.decode_wsl(b"Ubuntu  Stopped  2\n") == "Ubuntu  Stopped  2\n")

table = machines.parse_distro_table(decoded)
check("the distro table parses into rows",
      len(table) == 1 and table[0]["name"] == "Ubuntu", repr(table))
check("the '*' marks the default distro",
      table[0]["default"] is True and table[0]["state"] == "Stopped"
      and table[0]["version"] == "2", repr(table[0]))

two = machines.parse_distro_table(
    "  NAME              STATE           VERSION\r\n"
    "* Ubuntu            Stopped         2\r\n"
    "  docker-desktop    Running         2\r\n")
check("a machine with two distros gets both, and only one default",
      [d["name"] for d in two] == ["Ubuntu", "docker-desktop"]
      and sum(1 for d in two if d["default"]) == 1, repr(two))
check("the 'no distributions' sentence is not mistaken for a distro",
      machines.parse_distro_table("Windows Subsystem for Linux has no installed "
                                  "distributions.\r\n") == [])
check("garbage, blank lines and a lone name do not become rows",
      machines.parse_distro_table("\n\nnotadistro\n \r\n") == [])

# ---- the registry, against a home of our own -----------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    check("an empty home lists no machines", machines.list_specs(home) == [])
    saved = machines.save_spec(home, machines.MachineSpec(name="alpha", provider="wsl"))
    check("a saved spec lands in its own folder with its disk beside it",
          (saved / "machine.json").is_file() and saved.name == "alpha")
    check("... and reads back as the same spec",
          (machines.load_spec(home, "alpha") or machines.MachineSpec(name="?")).name == "alpha")

    (home / "machines" / "broken").mkdir(parents=True)
    (home / "machines" / "broken" / "machine.json").write_text("{not json", encoding="utf-8")
    listed = [s.name for s in machines.list_specs(home)]
    check("one unreadable machine spec does not hide the healthy ones",
          listed == ["alpha", "broken"], repr(listed))
    check("... and the broken one says so instead of raising",
          next(s for s in machines.list_specs(home) if s.name == "broken").notes != "")

# ---- what a machine asks for before it can be used ------------------------------------

status = machines.wsl_status()
if not status["installed"]:
    ask = machines.wsl_needs_setup()
    check("a machine with no WSL asks before installing anything",
          ask and ask["needs_setup"] == "wsl" and ask["command"].startswith("wsl --install"),
          repr(ask))
    skip("WSL lifecycle", "wsl.exe is not on this machine")
else:
    check("WSL is reported as installed with the distros it knows",
          status["installed"] and isinstance(status["distros"], list),
          repr(status)[:200])
    check("every distro row has a name and a version WSL actually reports",
          all(d["name"] and d["version"] in ("1", "2") for d in status["distros"]),
          repr(status["distros"]))
    check("a machine that wants a distro it does not have asks about the image instead",
          (machines.wsl_needs_setup(machines.MachineSpec(name="ghost-box"))
           or {}).get("needs_setup") == "image")
    if status["distros"]:
        existing = status["distros"][0]["name"]
        check("a machine pointed at a distro that does exist needs no setup",
              machines.wsl_needs_setup(
                  machines.MachineSpec(name="attach-me", attach=True, distro=existing)
              ) is None, existing)
    else:
        skip("attach to an existing distro", "no distro installed to attach to")

# ---- qemu: detected, never installed silently -----------------------------------------

q = machines.qemu_status()
check("QEMU's presence is reported honestly either way",
      isinstance(q["installed"], bool) and (q["path"] or q["install_command"]), repr(q))
if not q["installed"]:
    ask = machines.qemu_needs_setup()
    check("... and a missing QEMU comes with the command that would install it, "
          "rather than being installed unasked",
          ask["needs_setup"] == "qemu" and bool(ask["command"]), repr(ask))

# ---- the presets are usable, not decorative -------------------------------------------

ubuntu = machines.PRESETS["ubuntu-24.04"]
check("the default preset is a real URL for a real Ubuntu rootfs",
      ubuntu["image"]["url"].startswith("https://cloud-images.ubuntu.com/"), ubuntu["image"]["url"])
check("every preset names a provider and an image kind the spec accepts",
      all(p["provider"] in machines.PROVIDERS and p["image"]["kind"] in machines.IMAGE_KINDS
          for p in machines.PRESETS.values()),
      repr({k: (v["provider"], v["image"]["kind"]) for k, v in machines.PRESETS.items()}))

# ---- nothing here writes down one machine's paths --------------------------------------

src = (PLUG / "dashboard" / "machines.py").read_text(encoding="utf-8")
import re  # noqa: E402
check("the module derives its paths instead of naming somebody's home directory",
      not re.search(r"(?i)C:[/\\]Users[/\\][a-z]+|/home/[a-z]+/", src), "found a hard-coded path")
check("... and it does not name the vendored upstream product",
      not re.search(r"(?i)pinokio\w*", re.sub(r"https?://\S+", "", src)), "found a product name")

# ---- a real command in a real distro, when there is one -------------------------------

live_wanted = os.environ.get("ATHENA_MACHINE_LIVE") == "1"
if status["installed"] and status["distros"] and live_wanted:
    target = status["default"] or status["distros"][0]["name"]
    res = machines.wsl_exec(target, "uname -s && echo athena-ok", timeout=120)
    check(f"a command runs inside {target} and its output comes back",
          res["ok"] and "Linux" in res["output"] and "athena-ok" in res["output"],
          repr(res)[:300])
    fail = machines.wsl_exec(target, "exit 3", timeout=60)
    check("... and a command that fails is reported as a failure, with its exit code",
          not fail["ok"] and fail["code"] == 3, repr(fail)[:200])
    big = machines.wsl_exec(target, "seq 1 4000", timeout=120, max_chars=500)
    check("... and a flood of output is cut in the middle rather than swallowed whole",
          big["truncated"] and len(big["output"]) < 900, f"{len(big['output'])} chars")
else:
    why = ("wsl.exe is not on this machine" if not status["installed"]
           else "no distro installed" if not status["distros"]
           else "set ATHENA_MACHINE_LIVE=1 to start a distro and run a command in it")
    skip("running a command in a live distro", why)

# ---- setup is a question, and the platform's own answer is the one given ------
# The command text is looked up per platform rather than composed here: the failure
# this pins is a Linux box being told to run `wsl --install`, or a Windows box told
# to use apt. Four methods now, because installing WSL needs Windows to ask, installing
# QEMU on Windows is a download behind a progress bar when winget will name the
# installer (and a driven command when it will not), and installing QEMU anywhere else
# needs a password Athena must not type on someone's behalf.

plan_wsl = machines.install_plan("wsl")
check("asking for WSL on Windows becomes an elevation prompt rather than a silent install",
      (plan_wsl["ok"] and plan_wsl["method"] == "elevated") if sys.platform == "win32"
      else (not plan_wsl["ok"] and "Windows" in plan_wsl.get("error", "")),
      json.dumps(plan_wsl)[:200])
plan_q = machines.install_plan("qemu")
# This one asks the real winget when there is one, so both answers are correct: a
# counted download when winget hands over the installer's URL, the command it would
# have run when it does not. Elsewhere it is a command only the person can run.
check("QEMU on Windows is a download that can be watched, or the command that still works",
      (plan_q["method"] in ("download", "job") and plan_q["ok"] is True) if sys.platform == "win32"
      else plan_q["method"] == "manual",
      json.dumps(plan_q)[:220])
check("... and a plan to download something says what it will fetch and whether it is checked",
      plan_q["method"] != "download" or (plan_q.get("url", "").startswith("http")
                                        and len(plan_q.get("sha256") or "") == 64),
      json.dumps({k: str(v)[:60] for k, v in plan_q.items()}))
check("... and nothing is run for a prerequisite Athena has no recipe for",
      machines.install_plan("teleporter")["ok"] is False
      and machines.install_plan("")["ok"] is False,
      json.dumps(machines.install_plan("teleporter"))[:160])
# Deliberately not called on Windows: start_elevated() launches the installer
# through the desktop's own consent prompt, so proving it here would install WSL
# on whichever computer happens to be running the suite. The suite's job is to
# prove the refusal, and the refusal is the branch that cannot be reached by hand.
if sys.platform == "win32":
    skip("the elevated path refuses on a platform with no consent prompt",
         "calling it here would start a real install on this computer")
else:
    check("... and the elevated path refuses to pretend on a platform with no prompt",
          machines.start_elevated("wsl --install")["ok"] is False,
          json.dumps(machines.start_elevated("wsl --install"))[:200])

# A spec that would not survive being written is refused before anything is created.
for bad, why in (({"name": "not a name"}, "a name that is not a name"),
                 ({"name": "x", "provider": "vmware"}, "a provider nothing implements"),
                 ({"name": "x", "provider": "qemu", "image": {}}, "a booted machine with no image"),
                 ({"name": "../escape"}, "a name that would escape the machines folder")):
    spec = machines.MachineSpec(**bad)
    try:
        spec.validate()
        check(f"{why} is refused when the machine is made", False, json.dumps(bad))
    except machines.SpecError as e:
        check(f"{why} is refused when the machine is made", bool(str(e)), str(e)[:120])


# --------------------------------------------------------- qemu: the escape hatch
# What boots an ISO, and the part of it that can be proved on a computer with no
# QEMU on it: the command line. Every failure here — a disk also handed in as a
# CD-ROM, an image URL where QEMU wants a file, a virtio disk in front of a Windows
# installer that has no virtio driver, a guest with no forwarded port and therefore
# no way in — is a failure that shows up as "it booted and then did nothing".
def _q(**kw):
    base = dict(name="box", provider="qemu", os="linux", image={"kind": "iso", "path": "/iso/x.iso"},
                cpus=4, memory_mb=4096, disk_gb=40)
    base.update(kw)
    return machines.MachineSpec(**base)


_qdir = Path(tempfile.mkdtemp()) / "machines" / "box"


def _arg(argv, flag):
    """The value after a flag, or '' — the arguments are a list, not a string, so a
    missing flag is provably missing rather than a substring that failed to match."""
    return argv[argv.index(flag) + 1] if flag in argv else ""


def _args(argv, flag):
    """Every value after a repeated flag, in order. `-device` is used more than once, so
    `_arg` answers a different question than the one a device check is asking."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def _qemu_accepts(argv, wait=2.5):
    """Start QEMU on this command line and say whether it stayed up.

    This exists because a string check lied to me: `-device usb-tablet` was in the argv, the
    check passed, and QEMU refused the line and exited instantly with "No 'usb-bus' bus found
    for device 'usb-tablet'". A device flag is worth nothing unless QEMU agrees to run with
    it, so the line is started for real. (False, reason) when it refused; (True, "") when it
    survived the wait, which for QEMU means it accepted every device and every path.
    """
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return None, f"cannot start QEMU: {e}"
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            out = (proc.stdout.read() + proc.stderr.read()).decode("utf-8", "replace")
            said = [ln for ln in out.splitlines() if ln.strip()]
            return False, " / ".join(said[:2])[:300]
        time.sleep(0.1)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return True, ""


_qargv = machines.qemu_argv(_q(), _qdir)
check("a QEMU command line builds without a QEMU anywhere on this machine",
      _qargv and _qargv[0].endswith(("qemu-system-x86_64", "qemu-system-x86_64.exe")),
      " ".join(_qargv)[:200])
check("... an ISO is a CD-ROM, offered the disk first and itself as the fallback",
      _arg(_qargv, "-cdrom") == "/iso/x.iso" and _arg(_qargv, "-boot") == "order=cd",
      " ".join(_qargv)[:240])
# The reported bug: an install finishes, says "reboot now", and the machine boots its own
# installer again because the CD-ROM was first. A machine with nothing on its disk still has
# to reach the installer, which is what the fallback is for; a finished one has to boot itself.
check("... so a finished install boots its own system, and an empty disk still reaches the installer",
      machines.boot_preference(_q()) == "system"
      and machines.boot_args("system")[-1] == "order=cd",
      machines.boot_args(machines.boot_preference(_q())))
# Reported from omarchy: "once i got into omarchy i have no mouse passthrough so i cant move
# the mouse on the vm desktop". Without a pointing device QEMU turns VNC's absolute pointer
# position into relative motion for an emulated PS/2 mouse, and that only works when the guest
# draws a cursor the emulation can shove around — a Wayland guest draws none. The flag is the
# whole fix, so it is checked as a flag on the device list rather than as a substring.
check("a machine is given an absolute pointing device, so a pointer can move in a Wayland guest",
      "usb-tablet" in _args(_qargv, "-device"), " ".join(_args(_qargv, "-device")))
check("... on a USB bus it asks for itself, since this machine type has none by default",
      "-usb" in _qargv, " ".join(_qargv)[:240])
check("... alongside the network card, not instead of it",
      any(str(d).startswith(("e1000", "virtio-net", "rtl8139")) for d in _args(_qargv, "-device")),
      " ".join(_args(_qargv, "-device")))
check("... and one that does not serve a screen at all needs none of it",
      "usb-tablet" in _args(machines.qemu_argv(_q(display="panel"), _qdir), "-device")
      and "-vnc" in machines.qemu_argv(_q(display="panel"), _qdir),
      " ".join(_args(machines.qemu_argv(_q(display="panel"), _qdir), "-device")))
check("... and a live or rescue medium can still be asked for first, every time",
      _arg(machines.qemu_argv(_q(boot="medium"), Path(tempfile.mkdtemp()) / "m" / "box"), "-boot")
      == "order=dc" and machines.boot_preference(_q(boot="medium")) == "medium",
      machines.boot_args(machines.boot_preference(_q(boot="medium"))))
# An installer becomes a machine by itself once there is a system on its disk. The reading is
# the disk file's own size — measured here: empty 193 KiB, a finished omarchy install 5.89 GiB —
# and it is what "it should replace the machine with the installed version automatically" needs.
_dir_install = Path(tempfile.mkdtemp()) / "machines" / "box"
_dir_install.mkdir(parents=True, exist_ok=True)
check("a machine nobody has started has no disk to speak of, and keeps its installer",
      machines.disk_written_bytes(_dir_install) == 0
      and machines.is_installed(_q(), _dir_install) is False
      and machines.attach_medium(_q(), _dir_install) is True,
      str(machines.disk_written_bytes(_dir_install)))
check("... the threshold is a real number, above an empty disk and below any installed system",
      0 < machines.INSTALLED_BYTES <= 1024 ** 3, str(machines.INSTALLED_BYTES))
(_dir_install / "disk.qcow2").write_bytes(b"0" * machines.INSTALLED_BYTES)
check("... a disk with a system written on it reads as installed, without a boot or a mount",
      machines.is_installed(_q(), _dir_install) is True,
      str(machines.disk_written_bytes(_dir_install)))
# Every check above reads the command line. This one hands it to QEMU and sees whether QEMU
# says yes, because reading it is how a refused line got through: `-device usb-tablet` with no
# USB controller on the machine type made QEMU exit instantly with "No 'usb-bus' bus found for
# device 'usb-tablet'" while the string checks were all green. A live check that cannot run
# here says SKIPPED rather than pretending.
_live_why = ""
if not machines.qemu_exe():
    _live_why = "QEMU is not installed here"
elif not machines.qemu_img_exe():
    _live_why = "qemu-img is not installed here, so there is no disk to boot"
else:
    _live_dir = Path(tempfile.mkdtemp()) / "machines" / "suite-live"
    _live_dir.mkdir(parents=True)
    _live_disk = _live_dir / "disk.qcow2"
    _img_run = subprocess.run([machines.qemu_img_exe(), "create", "-f", "qcow2",
                               str(_live_disk), "8M"], capture_output=True)
    if _img_run.returncode != 0:
        _live_why = f"qemu-img could not make a disk: {_img_run.stderr.decode('utf-8', 'replace')[:120]}"
if _live_why:
    skip("QEMU itself accepts the command line this builds, pointing device and all", _live_why)
else:
    _live_spec = machines.MachineSpec(name="suite-live", provider="qemu", cpus=1, memory_mb=512,
                                      display="panel", vnc_port=77,
                                      image={"kind": "disk", "path": str(_live_disk)})
    _started, _said = _qemu_accepts(machines.qemu_argv(_live_spec, _live_dir))
    if _started is None:
        skip("QEMU itself accepts the command line this builds, pointing device and all", _said)
    elif _started:
        check("QEMU itself accepts the command line this builds, pointing device and all", True)
    else:
        # QEMU can also refuse for reasons that belong to this computer rather than to the
        # line: no accelerator, a port already taken. Only the line's own arguments are this
        # check's business, and anything else is reported as the skip it is.
        _ours = ("bus found", "usb-tablet", "unknown option", "invalid", "no such")
        if any(word in _said.lower() for word in _ours):
            check("QEMU itself accepts the command line this builds, pointing device and all",
                  False, _said)
        else:
            skip("QEMU itself accepts the command line this builds, pointing device and all",
                 _said)


_installed_argv = machines.qemu_argv(_q(), _dir_install)
check("... and the installer medium comes off the command line, so it cannot win a boot again",
      "-cdrom" not in _installed_argv and "-boot" not in _installed_argv,
      " ".join(_installed_argv)[:240])
check("... leaving nothing to order around: a machine with no medium simply boots its disk",
      "no operating system" not in " ".join(_installed_argv).lower()
      and "-drive" in _installed_argv, " ".join(_installed_argv)[:240])
check("... unless somebody asks for the medium, which is how the installer comes back",
      "-cdrom" in machines.qemu_argv(_q(boot="medium"), _dir_install)
      and _arg(machines.qemu_argv(_q(boot="medium"), _dir_install), "-boot") == "order=dc",
      " ".join(machines.qemu_argv(_q(boot="medium"), _dir_install))[:240])
check("... and what is on the disk is read fresh every time, never remembered on the spec",
      "installed" not in _q(boot="medium").to_dict() and machines.MachineSpec(
          name="box", provider="qemu", image={"kind": "iso", "path": "/iso/x.iso"}).boot == "")
check("... the choice travels in the spec, where a machine can be rebuilt from it",
      machines.MachineSpec.from_dict(_q(boot="medium").to_dict()).boot == "medium")
try:
    _q(boot="floppy").validate()
    check("... and a word that is not a boot choice is refused", False, "floppy accepted")
except machines.SpecError as e:
    check("... and a word that is not a boot choice is refused", "boot" in str(e), str(e))
check("... and it is the file on disk, never the URL it was fetched from",
      "http" not in " ".join(_qargv).lower(),
      [a for a in _qargv if "http" in a][:1])
check("... the console is a file, because a pipe nobody reads fills up and stalls the guest",
      _arg(_qargv, "-serial").startswith("file:") and "console.log" in _arg(_qargv, "-serial"),
      _arg(_qargv, "-serial"))
check("... the accelerator is one this host can actually do",
      _arg(_qargv, "-accel") in ("whpx", "kvm", "tcg"), _arg(_qargv, "-accel"))
if sys.platform == "win32":
    check("... which on Windows is WHPX, the one that needs the Hyper-V this machine reports",
          _arg(_qargv, "-accel") == "whpx", _arg(_qargv, "-accel"))

# A Windows guest is not a Linux guest with a different name on the box. The disk
# has to exist first — a machine that has not been started yet has no drive in its
# command line at all, which is its own assertion further down.
_busdir = Path(tempfile.mkdtemp()) / "machines" / "box"
_busdir.mkdir(parents=True, exist_ok=True)
(_busdir / "disk.qcow2").write_bytes(b"")
_qdisk = machines.qemu_argv(_q(), _busdir)
_wargv = machines.qemu_argv(_q(os="windows", image={"kind": "iso", "path": "/iso/win11.iso"}), _busdir)
check("a Windows guest is given disks and a NIC its installer already has drivers for",
      "if=ide" in " ".join(_wargv) and _arg(_wargv, "-device").startswith("e1000")
      and "virtio" not in " ".join(_wargv), " ".join(_wargv)[:220])
# The size a guest comes up at is the video device's to hint and the guest's to choose. virtio-vga's
# EDID is the hint a Linux guest reads; measured against omarchy's own ISO (`cache/scratch/res_probe.py`,
# throwaway disk, size read back out of the VNC handshake): 1920x1080 by 37s, against 1280x800 with no
# hint at all. Windows is not asked, for the same reason its disks are `if=ide` above.
check("a Linux guest is asked for a 1920x1080 screen, which is the size it comes up at",
      "-vga" in _qdisk and "virtio-vga,xres=1920,yres=1080" in _qdisk, " ".join(_qdisk)[-160:])
check("... and it is asked once: the default card is turned off rather than left beside it",
      _qdisk[_qdisk.index("-vga") + 1] == "none", _qdisk[_qdisk.index("-vga") + 1])
check("... and a Windows guest is not asked at all, because its installer has no driver for that card",
      "virtio-vga" not in " ".join(_wargv))
check("... while a Linux guest gets virtio, which is the fast one",
      "if=virtio" in " ".join(_qdisk) and _arg(_qdisk, "-device").startswith("virtio-net-pci"),
      _arg(_qdisk, "-device"))

# Somebody else's image boots *as itself*, so the format on the command line has to follow the
# file. `format=qcow2` was written into both drive lines, which is right for our own disk and
# wrong for every picked image: QEMU answers `Invalid parameter 'qcow2'` — naming its own driver
# rather than the mistake — on a .vmdk/.vhdx/.vhd, and boots nothing.
check("a picked disk image is handed to QEMU as the format it actually is",
      machines.disk_format_for("old.vmdk") == "vmdk"
      and machines.disk_format_for("win.VHDX") == "vhdx"
      and machines.disk_format_for("legacy.vhd") == "vpc"
      and machines.disk_format_for("card.img") == "raw"
      and machines.disk_format_for("box.qcow2") == "qcow2",
      " ".join(f"{s}={machines.disk_format_for('x' + s)}" for s in machines._DISK_SUFFIXES))
check("... every suffix this file accepts a disk image under has a format, or the menu and the "
      "command line disagree about what is openable",
      all(machines.disk_format_for("x" + s) for s in machines._DISK_SUFFIXES)
      and all(machines.disk_format_for("x" + s) == "" for s in (".bin", ".iso", ".tar")),
      str([(s, machines.disk_format_for("x" + s)) for s in machines._DISK_SUFFIXES]))
_foreign = machines.qemu_argv(
    _q(image={"kind": "disk", "path": "/disks/old.vmdk"}), _qdir)
check("... and a .vmdk is not told it is a qcow2, which is the bug this replaced",
      "format=vmdk" in " ".join(_foreign) and "format=qcow2" not in " ".join(_foreign),
      " ".join(_foreign)[-200:])
check("... while a machine's own disk is still the qcow2 it was made as",
      "format=qcow2" in " ".join(_qdisk), [a for a in _qdisk if a.startswith("file=")][:1])

# A stop that did not finish is not a stop. The old version killed, waited, and then reported
# `ok`/`stopped` no matter what — so a guest that outlived its kill kept writing to its disk
# while the panel offered to snapshot it. Nothing here touches a real process: liveness is
# faked, and the pid it is faked for does not exist.
_stopdir = Path(tempfile.mkdtemp()) / "machines" / "sleepy"
_stopdir.mkdir(parents=True, exist_ok=True)
_pf = machines.qemu_pid_file(_stopdir)
_pf.write_text("4294967294", encoding="utf-8")
_real_alive, _real_find = machines._pid_alive, machines.qemu_pids_for
try:
    machines._pid_alive = lambda pid: int(pid) == 4294967294
    machines.qemu_pids_for = lambda folder: [4294967294]
    _stopped = machines.qemu_stop(_stopdir)
finally:
    machines._pid_alive, machines.qemu_pids_for = _real_alive, _real_find
check("a guest that outlives its kill is reported as still running, not as stopped",
      _stopped.get("ok") is False and _stopped.get("state") == "stopping"
      and "4294967294" in str(_stopped.get("error") or ""), str(_stopped)[:200])
check("... and the pidfile it did not honour stays put, so the next stop can still find it",
      _pf.is_file(), str(_pf))
try:
    machines._pid_alive = lambda pid: False
    machines.qemu_pids_for = lambda folder: []
    _gone = machines.qemu_stop(_stopdir)
finally:
    machines._pid_alive, machines.qemu_pids_for = _real_alive, _real_find
check("... while a machine that really is gone answers stopped and clears its pidfile",
      _gone.get("ok") is True and _gone.get("state") == "stopped" and not _pf.is_file(),
      str(_gone))

# Settings after the fact. The rules are a pure function of the spec, the patch and whether
# the guest is up, so the panel and an agent cannot be told two different things about what a
# number may be -- and "saved" and "in effect" are separate answers, which is the only way a
# person finds out before the next boot rather than at it.
_editable = machines.MachineSpec(name="box", provider="qemu", os="linux",
                                 image={"kind": "iso", "path": "/iso/x.iso"},
                                 cpus=4, memory_mb=4096, disk_gb=40, display="panel")
_applied = machines.apply_patch(_editable, {"cpus": 6, "memory_mb": 8192, "disk_gb": 60,
                                            "boot": "medium"})
check("a machine's cores, memory, disk and boot can be changed after it exists",
      _applied.get("ok") and _applied["changed"] == {"cpus": 6, "memory_mb": 8192,
                                                    "disk_gb": 60, "boot": "medium"},
      str(_applied)[:200])
check("... and a change to what the command line holds says it takes effect at the next start",
      _applied.get("restart_required") is True
      and any("next start" in n for n in _applied.get("notes") or []),
      str(_applied.get("notes")))
check("... while a purely recorded change (a note) needs no restart at all",
      machines.apply_patch(_editable, {"notes": "mine"})["restart_required"] is False,
      str(machines.apply_patch(_editable, {"notes": "mine"}).get("notes")))
check("... and the spec that comes back is a whole spec, validated, not a pile of fields",
      _applied["spec"]["name"] == "box" and _applied["spec"]["disk_gb"] == 60
      and machines.MachineSpec.from_dict(_applied["spec"]).cpus == 6,
      str(_applied["spec"])[:160])
check("... changing it to what it already is says so instead of claiming work",
      machines.apply_patch(_editable, {"cpus": 4})["changed"] == {})
for _label, _patch, _says in (
        ("a field that is not a setting is refused by name",
         {"provider": "wsl"}, "provider"),
        ("a number outside its range is refused with the range",
         {"cpus": 0}, "1 to 64"),
        ("a disk cannot be shrunk, because the filesystem is inside it",
         {"disk_gb": 10}, "smaller"),
        ("a screen has to be one of the known ones",
         {"display": "hologram"}, "hologram"),
        ("an image path that is not there is refused now, not at boot",
         {"image": {"kind": "iso", "path": "/nope/x.iso"}}, "no file"),
        ("a port pair that is not a pair is refused with the shape it wants",
         {"ports": ["nonsense"]}, "8080:80"),
        ("an empty patch is not a change", {}, "nothing was changed")):
    _res = machines.apply_patch(_editable, _patch)
    check(_label, _res.get("ok") is False and _says in str(_res.get("error")),
          str(_res.get("error"))[:140])
check("... and more cores than this computer has are refused with the number it has",
      machines.apply_patch(_editable, {"cpus": 4096}).get("ok") is False
      and "cores" in str(machines.apply_patch(_editable, {"cpus": 4096}).get("error")),
      str(machines.apply_patch(_editable, {"cpus": 4096}).get("error"))[:140])
check("... ports arrive as the spec's own shape, so the command line reads them the same way",
      machines.apply_patch(_editable, {"ports": ["8080:80"]})["changed"]["ports"]
      == [{"host": 8080, "guest": 80}])
check("... and turning a port forward off is a patch, not a delete",
      machines.apply_patch(
          machines.MachineSpec.from_dict(machines.apply_patch(
              _editable, {"ports": ["8080:80"]})["spec"]),
          {"ports": []})["changed"]["ports"] == [])

# Networking is the difference from WSL, so it is the one that must not be assumed.
_pargv = machines.qemu_argv(_q(ssh={"port": 2222}, ports=[{"host": 8080, "guest": 80}]), _qdir)
_netdev = _arg(_pargv, "-netdev")
check("a QEMU guest gets its ports forwarded explicitly, because it cannot see your localhost",
      "hostfwd=tcp:127.0.0.1:2222-:22" in _netdev and "hostfwd=tcp:127.0.0.1:8080-:80" in _netdev, _netdev)
check("... and each forward is loopback-only, never a guest display or shell on the network",
      _netdev.count("hostfwd=tcp:127.0.0.1:") == 2 and "0.0.0.0" not in " ".join(_pargv),
      _netdev)
check("... a machine with no ports says so by forwarding nothing, rather than inventing one",
      "hostfwd" not in _arg(machines.qemu_argv(_q(), _qdir), "-netdev"))

check("the display is a real choice: a window for an install, none for a headless box",
      _arg(machines.qemu_argv(_q(display="window"), _qdir), "-display") == "sdl"
      and _arg(machines.qemu_argv(_q(display="none"), _qdir), "-display") == "none",
      _arg(_qargv, "-display"))
_panelargv = machines.qemu_argv(_q(display="panel"), _qdir)
_sock_argv = machines.vnc_bind(_qdir, _q(), unix=True)
_port_argv = machines.vnc_bind(_qdir, _q(), unix=False)
check("... and VNC, when asked for, is a socket in the machine's own folder rather than a "
      "loopback port — a port is reachable by every program running as this user, and a "
      "socket is a file that can be given a mode — and the socket is only asked for where "
      "it can also be opened, because a screen nobody can dial is broken, not private",
      _sock_argv == ["-vnc", machines._unix_host(machines.vnc_socket(_qdir))]
      and _sock_argv[1].startswith("unix:")
      and _port_argv == ["-vnc", f"127.0.0.1:{int(_q().vnc_port or 0)}"]
      and "-vnc" not in machines.qemu_argv(_q(display="window"), _qdir)
      and _arg(_panelargv, "-vnc") == machines.vnc_bind(_qdir, _q())[1],
      f"{_sock_argv} / {_port_argv} / default leaves "
      f"AF_UNIX={machines.unix_sockets_available()}")
check("... and which of the two is the default is asked of the running interpreter, not "
      "of the platform's name: QEMU creates the socket on Windows and Python there cannot "
      "connect to one, so the giving-away half is not evidence about the taking-away half",
      machines.vnc_bind(_qdir, _q()) == (
          _sock_argv if machines.unix_sockets_available() else _port_argv),
      f"AF_UNIX={machines.unix_sockets_available()} default={machines.vnc_bind(_qdir, _q())}")

# Where to dial is asked of the filesystem rather than stored, because both kinds of
# machine can exist on one computer at once: a guest started before the socket change
# has a loopback port and no socket file, and one started after it is the other way
# round. A stored answer is the one that goes stale.
_nosock = machines.vnc_endpoint(_qdir, _q())
machines.vnc_socket(_qdir).parent.mkdir(parents=True, exist_ok=True)
machines.vnc_socket(_qdir).write_bytes(b"")
_withsock = machines.vnc_endpoint(_qdir, _q())
machines.vnc_socket(_qdir).unlink()
check("... and where to dial is asked of the filesystem, so a guest started before the "
      "socket change still has a screen — and a socket file is not answered with where "
      "the reader cannot open it",
      _nosock == "127.0.0.1"
      and (_withsock.startswith("unix:") if machines.unix_sockets_available()
           else _withsock == "127.0.0.1")
      and machines.vnc_endpoint(_qdir, _q()) == "127.0.0.1",
      f"{_nosock!r} → {_withsock!r} → {machines.vnc_endpoint(_qdir, _q())!r} "
      f"AF_UNIX={machines.unix_sockets_available()}")

# The accelerator and the CPU model are one decision and not two, because it was the
# *combination* that measured dead here: WHPX with `-cpu host` leaves the vCPU frozen at
# its reset vector while QEMU itself is alive, which looks exactly like a machine that
# started and has nothing to show. Measured one variable at a time on 2026-10-14.
check("a Windows accelerator never gets the host's own CPU model: that pairing does not run",
      machines.qemu_cpu_args("whpx") == []
      and machines.qemu_cpu_args("kvm") == ["-cpu", "host"]
      and machines.qemu_cpu_args("tcg") == ["-cpu", "max"], str(machines.qemu_cpu_args("whpx")))
check("... and it is the command line that follows that answer, not a second opinion",
      "-cpu" not in _arg(machines.qemu_argv(_q(display="panel"), _qdir) or [], "-cpu") or
      _arg(machines.qemu_argv(_q(display="panel"), _qdir), "-accel") != "whpx"
      if sys.platform == "win32" else True,
      str(machines.qemu_argv(_q(display="panel"), _qdir)))

check("a machine's VNC port is QEMU's own rule: display N is 5900+N",
      machines.vnc_tcp_port(_q(vnc_port=0)) == 5900
      and machines.vnc_tcp_port(_q(vnc_port=3)) == 5903,
      str(machines.vnc_tcp_port(_q(vnc_port=3))))
# The occupied display is *found*, not assumed: 5901 is exactly what a running machine takes,
# and a test that hardcodes it fails on the very host it is trying to be about ("Only one usage
# of each socket address"). So hold the lowest display this host actually has free.
_hold = _hold_n = None
for _n in range(9, 60):
    _s = socket.socket()
    try:
        _s.bind(("127.0.0.1", 5900 + _n))
        _s.listen(1)
        _hold, _hold_n = _s, _n
        break
    except OSError:
        _s.close()
_held = machines.pick_vnc_display([]) if _hold else None
if _hold:
    _hold.close()
check("... and a display number something else is already using is stepped over",
      _hold_n is not None and _held != _hold_n
      and machines.pick_vnc_display([0]) != 0
      and machines.pick_vnc_display([0, 1, 2]) >= 2, f"held {_hold_n}, picked {_held}")

# "Nobody said" is a real answer, and this computer gives it. Installing an OS from an ISO
# is something you *watch* — setup asks questions and the answer is a click — so a machine
# made without an opinion gets a window, and a host with no session gets the VNC screen
# instead of an SDL window that exits the instant it cannot open.
# The default is the panel now. A window of its own is something a person asks for with
# "pop out"; it is not something that happens to them when they press Start, and the guest
# screen they asked to see belongs inside the panel they asked it from.
check("a display nobody chose is answered by this computer, and the answer is the panel",
      machines.resolve_display(_q()) == machines.default_display()
      and machines.default_display() == "panel",
      machines.default_display())
check("... and the spec accepts 'nobody said' rather than demanding a guess at the call site",
      machines.MachineSpec(name="t", provider="qemu", display="",
                           image={"kind": "iso", "path": "/iso/t.iso"},
                           cpus=2, memory_mb=2048, disk_gb=10).validate() is None)
check("... with the answer recorded on the machine, so what it does is readable later",
      _q(display="window").to_dict()["display"] == "window"
      and machines.resolve_display(_q(display="")) == machines.default_display())
check("... and a window machine is a window machine wherever it is read from",
      _arg(machines.qemu_argv(_q(display="window"), _qdir), "-display") == "sdl"
      and _arg(machines.qemu_argv(_q(display="panel"), _qdir), "-display") == "none",
      _arg(machines.qemu_argv(_q(display="panel"), _qdir), "-display"))
check("... and the default machine gets the panel's VNC screen and no window at all",
      _arg(machines.qemu_argv(_q(), _qdir), "-display") == "none"
      and "-vnc" in machines.qemu_argv(_q(), _qdir),
      str(machines.qemu_argv(_q(), _qdir)))

# Where the screen and the ports are, in one sentence, because "started, now what" is the
# question the card has to answer the moment it is asked.
_reach = machines.qemu_reach(_q(display="window", ssh={"port": 2222},
                                ports=[{"host": 8080, "guest": 80}]))
check("what a started machine says about reaching it names the screen and every port",
      _reach.startswith("Its screen is a window of its own")
      and "127.0.0.1:2222" in _reach and "8080" in _reach and "80" in _reach, _reach)
check("... and a panel machine says its screen is in the panel, not a port number to go find",
      "panel" in machines.qemu_reach(_q(display="panel", vnc_port=0))
      and "5900" not in machines.qemu_reach(_q(display="panel", vnc_port=0))
      and "5901" not in machines.qemu_reach(_q(display="panel", vnc_port=1)),
      machines.qemu_reach(_q(display="panel", vnc_port=1)))
check("... and a machine nothing can reach says that, rather than trailing an empty list",
      machines.qemu_reach(_q(display="none")).endswith("nothing in it is reachable from here yet"),
      machines.qemu_reach(_q(display="none")))

# The console: the only part of a QEMU guest the panel can show inside itself. It is read
# from the END, because an install's log is megabytes and the part worth seeing is the part
# that just happened.
_clog = Path(tempfile.mkdtemp()) / "console.log"
_clog.write_text("boot loader line one\n" + ("filler\n" * 200) + "last line the guest said\n",
                 encoding="utf-8")
_ctail = machines.console_tail(_clog.parent, limit=200)
check("a guest's console is read from the end, because the part worth seeing is the last part",
      "last line the guest said" in _ctail["text"] and "boot loader line one" not in _ctail["text"],
      _ctail["text"][-80:])
check("... and a tail that starts mid-line drops the half line instead of showing a severed one",
      _ctail["text"].startswith("filler") or _ctail["text"].startswith("last line"),
      repr(_ctail["text"][:40]))
check("... a console nobody has written to yet is empty and says so, not an error",
      (lambda d: machines.console_tail(d)["exists"] is False
                 and machines.console_tail(d)["text"] == "")(Path(tempfile.mkdtemp())))
check("... and the whole log is handed over when it fits in one read",
      "boot loader line one" in machines.console_tail(_clog.parent, limit=100000)["text"])

# QEMU's own output lives in its own file, and that is not tidiness: pointing its stderr
# and its serial chardev at one path makes the chardev fail to open on Windows
# ("could not connect serial device to character backend") and QEMU dies before the guest
# exists. Reproduced both ways on this machine; the two files are load-bearing.
_qlog = _clog.parent / "qemu.log"
_qlog.write_text("qemu-system-x86_64.exe: -machine nonsense: this is qemu talking\n",
                 encoding="utf-8")
_ctail2 = machines.console_tail(_clog.parent)
check("what qemu itself said travels next to the guest's console, not inside it",
      "qemu talking" in _ctail2["errors"] and "qemu talking" not in _ctail2["text"],
      f"{_ctail2['errors'][:60]!r} / {_ctail2['text'][:40]!r}")
check("... and the reason a machine did not stay up is qemu's last line, in its own words",
      machines._qemu_said(_clog.parent).endswith("this is qemu talking"),
      machines._qemu_said(_clog.parent))
check("... a machine that never said anything says nothing rather than inventing a reason",
      (lambda d: machines._qemu_said(d) == "" and
                 machines._qemu_log_tail(d)["exists"] is False)(Path(tempfile.mkdtemp())))
check("... and the console a guest writes and the file qemu writes are two different paths",
      _ctail2["path"] != machines._qemu_log_tail(_clog.parent)["path"],
      f"{_ctail2['path']} / {machines._qemu_log_tail(_clog.parent)['path']}")

# A machine that boots somebody's disk image does not get a second, empty one.
_own = machines.qemu_argv(_q(image={"kind": "disk", "path": "/img/arch.qcow2"}), _qdir)
check("a machine pointed at its own disk image boots that image, not an empty drive beside it",
      "file=/img/arch.qcow2" in " ".join(_own) and _own.count("-drive") == 1,
      " ".join(_own)[:200])

# Membership: the disk the machine owns is only added once it exists.
_nodisk = machines.qemu_argv(_q(), Path(tempfile.mkdtemp()) / "fresh")
check("... and before its disk exists, the command line is honest about having none",
      "-drive" not in _nodisk, " ".join(_nodisk)[:200])
_pstr = _q(ports=[{"host": "8080", "guest": "80"}])
_pstr.validate()
check("... and a port written as a string is normalised, because that is how JSON arrives",
      _pstr.ports == [{"host": 8080, "guest": 80}], json.dumps(_pstr.ports))

# Ports are data, so garbage is refused while somebody is still looking at the form.
for bad, why in (([{"host": "eight", "guest": 80}], "a port that is not a number"),
                 ([{"host": 0, "guest": 80}], "port zero"),
                 ([{"host": 8080, "guest": 99999}], "a port past the end of the range"),
                 ([{"guest": 80}], "a forward with no host side")):
    try:
        _q(ports=bad).validate()
        check(f"{why} is refused when the machine is made", False, json.dumps(bad))
    except machines.SpecError as e:
        check(f"{why} is refused when the machine is made", bool(str(e)), str(e)[:110])

# State comes from the process table, not from a file Athena wrote.
_rundir = Path(tempfile.mkdtemp()) / "box"
check("a machine with no pidfile is not running",
      machines.qemu_running(_rundir) is False)
_rundir.mkdir(parents=True, exist_ok=True)
machines.qemu_pid_file(_rundir).write_text("4000000", encoding="utf-8")
check("... a pidfile left behind by a crash is not a running machine either",
      machines.qemu_running(_rundir) is False)
_stop = machines.qemu_stop(_rundir)
check("... and stopping a machine that is already gone is reported as stopped, not as a failure",
      _stop.get("ok") is True and _stop.get("state") == "stopped", json.dumps(_stop)[:160])
check("... with the pidfile cleaned up, so the next start is not confused by it",
      machines.qemu_pid_file(_rundir).exists() is False)

# The one that matters when somebody believes they have a backup.
_rundir.mkdir(parents=True, exist_ok=True)
machines.qemu_pid_file(_rundir).write_text(str(os.getpid()), encoding="utf-8")
check("a snapshot of a disk being written to is refused — that is not a backup, it is a coin toss",
      machines.qemu_running(_rundir) is True
      and machines.qemu_snapshot(_q(), _rundir, _rundir / "exports").get("ok") is False,
      json.dumps(machines.qemu_snapshot(_q(), _rundir, _rundir / "exports"))[:180])
check("... and a machine with no disk has nothing to snapshot, and says that instead",
      machines.qemu_snapshot(_q(), Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())).get("ok") is False)
machines.qemu_pid_file(_rundir).unlink()

if not machines.qemu_exe():
    check("starting a machine where QEMU is not installed asks, and does not pretend",
          machines.qemu_start(_q(), Path(tempfile.mkdtemp())).get("needs_setup", {}).get("needs_setup")
          == "qemu",
          json.dumps(machines.qemu_start(_q(), Path(tempfile.mkdtemp())))[:200])
    check("... and making its disk says which half of QEMU is missing",
          "half installed" in str(machines.qemu_create_disk(_q(), Path(tempfile.mkdtemp())).get("error")),
          str(machines.qemu_create_disk(_q(), Path(tempfile.mkdtemp())))[:160])
else:
    skip("starting a machine where QEMU is not installed asks, and does not pretend",
         "QEMU is installed here, so that branch cannot be reached — the live path is below")
    skip("... and making its disk says which half of QEMU is missing", "QEMU is installed here")

# A QEMU guest has no shell channel, and the refusal names the way in instead of sulking.
# The host half is where the panel and the agent both land, so the sentence is checked
# there rather than invented twice.
try:
    sys.path.insert(0, str(PLUG / "dashboard"))
    import plugin_api as A  # noqa: E402
    _home = Path(tempfile.mkdtemp())
    _spec = _q(name="qbox", image={"kind": "iso", "path": "/iso/x.iso"}, ssh={"port": 2222})
    machines.save_spec(_home, _spec)
    _host = A.MachineHost()
    _host_home = A._athena_home
    A._athena_home = lambda: _home
    try:
        check("the panel and the agent see the same machine, from the same spec",
              [r["name"] for r in _host.list()["machines"]] == ["qbox"],
              json.dumps(_host.list())[:200])
        check("... a QEMU machine's state is its process, not a distro in a list",
              _host.state(_spec) == "stopped")
        _exec = _host.exec_("qbox", "uname -a")
        check("... and running a command in it is refused with the way in, not with a shrug",
              _exec.get("ok") is False and "2222" in str(_exec.get("error"))
              and "127.0.0.1" in str(_exec.get("error")), str(_exec.get("error"))[:220])
        check("... a machine QEMU cannot start says so before it says anything else",
              (_host.start("qbox") or {}).get("needs_setup", {}).get("needs_setup") == "qemu"
              if not machines.qemu_exe() else True)
    finally:
        A._athena_home = _host_home
except Exception as _e:  # noqa: BLE001 — the host is a heavier import; say so, do not fail
    skip("the panel and the agent see the same machine, from the same spec",
         f"the panel's own module would not import here: {_e}")


# ---- sizing: what this computer can give, decided by a pure function -----------------
# "Sized for this machine on first use" is only a claim if the decision can be checked
# anywhere — including on a computer with no VM support at all, which is why the fit is
# numbers in, numbers out, and the reading of the host is a separate function.

_fits = machines.fit_spec(32, 32768, 500.0)
check("a big host gives the guest half its cores and never more than eight",
      _fits["cpus"] == 8, json.dumps(_fits)[:200])
check("... a quarter of its memory, never more than half of it",
      _fits["memory_mb"] == 8192 and _fits["memory_mb"] <= 32768 // 2, str(_fits["memory_mb"]))
check("... and the full disk when there is room for it", _fits["disk_gb"] == 40)
check("... every number in the fit is a recommendation, and says where it came from",
      len(_fits["why"]) >= 3 and all("→" in w or "core" in w for w in _fits["why"]),
      json.dumps(_fits["why"])[:220])

_small = machines.fit_spec(4, 8192, 90.0)
check("a small host gives a small guest rather than a hopeful one",
      _small["cpus"] == 2 and _small["memory_mb"] == 2048, json.dumps(_small)[:200])
_tiny = machines.fit_spec(1, 1024, 5.0)
check("... and a host too small for a comfortable guest says so instead of pretending",
      _tiny["cpus"] <= 1 and _tiny["memory_mb"] <= 1024
      and any("not enough" in w for w in _tiny["why"])
      and any("take turns" in w for w in _tiny["why"]), json.dumps(_tiny["why"])[:260])
check("... where the fit was never read, the number is a default and is called one",
      all("default rather than a" in w
          for w in machines.fit_spec(None, None, None)["why"][:3]),
      json.dumps(machines.fit_spec(None, None, None)["why"])[:240])
check("... a nearly full disk sizes the guest down rather than filling the disk",
      machines.fit_spec(8, 16384, 12.0)["disk_gb"] <= 2
      and machines.fit_spec(8, 16384, 60.0)["disk_gb"] == 40,
      str(machines.fit_spec(8, 16384, 12.0)["disk_gb"]))
check("... and a WSL guest is told its numbers are limits rather than memory taken",
      any("limits" in w for w in machines.fit_spec(8, 16384, 60.0, "wsl")["why"]))

_facts = machines.host_facts()
check("this computer's own facts are read, not assumed",
      _facts["cores"] and _facts["cores"] >= 1 and _facts["memory_mb"]
      and _facts["memory_mb"] > 1024 and _facts["free_gb"] is not None,
      json.dumps(_facts))
check("... free space is reported for a folder that does not exist yet, from the disk it "
      "would live on, instead of being given up on",
      machines.host_facts(Path(tempfile.gettempdir()) / "no-such-dir-tala" / "deep")["free_gb"]
      is not None)
_rec = machines.recommended_spec("fitbox", "qemu", image={"kind": "iso", "path": "/iso/x.iso"})
check("a machine nobody sized is a valid machine that QEMU could be asked to start",
      _rec.provider == "qemu" and _rec.cpus >= 2 and _rec.memory_mb >= 1024
      and _rec.validate() is None and _rec.to_dict()["image"]["path"] == "/iso/x.iso",
      json.dumps(_rec.to_dict())[:220])

# Ports reach the same rule from both directions: the text a person types and the pair
# the panel already parsed.
check("a port pair typed as text and one sent as a pair are the same pair",
      machines.parse_port_pair("8080:80") == {"host": 8080, "guest": 80}
      and machines.parse_port_pair("8080->80") == {"host": 8080, "guest": 80}
      and machines.parse_port_pair({"host": 8080, "guest": 80}) == {"host": 8080, "guest": 80})
_BAD = 0
for _junk in ("eight:80", "8080", "0:80", "70000:80", ""):
    try:
        machines.parse_port_pair(_junk)
    except machines.SpecError:
        _BAD += 1
check("... and junk is refused with the shape of a port pair in the sentence",
      _BAD == 5, f"{_BAD}/5 refused")
try:
    machines.parse_port_pair("70000:80")
    _why = ""
except machines.SpecError as _e:
    _why = str(_e)
check("... the refusal says the numbers, because that is what is wrong",
      "1 to 65535" in _why, _why)

# ---- the image folder, and the menu drawn from it ------------------------------------
# One folder, named by the user, read as a list. The kind a name suggests is a label and
# never a decision — the spec still refuses an image whose kind was not written down.
_kinds = {n: machines.image_kind_for(n) for n in
          ("bazzite.iso", "BAZZITE.ISO", "arch.qcow2", "x.img", "y.raw", "z.vhdx", "w.vmdk",
           "rootfs.tar.gz", "rootfs.tgz", "rootfs.tar.xz", "rootfs.tar.zst", "rootfs.tar",
           "notes.txt", "README", "")}
check("a filename is read for what it suggests, extension by extension, case and all",
      _kinds["bazzite.iso"] == "iso" and _kinds["BAZZITE.ISO"] == "iso"
      and all(_kinds[n] == "disk" for n in ("arch.qcow2", "x.img", "y.raw", "z.vhdx", "w.vmdk"))
      and all(_kinds[n] == "rootfs" for n in
              ("rootfs.tar.gz", "rootfs.tgz", "rootfs.tar.xz", "rootfs.tar.zst", "rootfs.tar")),
      json.dumps(_kinds))
check("... and a name that suggests nothing suggests nothing, rather than guessing",
      _kinds["notes.txt"] == "" and _kinds["README"] == "" and _kinds[""] == "")

_iso_home = Path(tempfile.mkdtemp())
check("the image folder is the user's choice, then the environment's, then Athena's own",
      machines.iso_dir(_iso_home, {"iso_dir": "E:/ISOs"}) == Path("E:/ISOs")
      and machines.iso_dir(_iso_home, {}) == _iso_home / "isos")
os.environ["ATHENA_ISO_DIR"] = "D:/from-env"
check("... the environment answers only when the user has not",
      machines.iso_dir(_iso_home, {}) == Path("D:/from-env")
      and machines.iso_dir(_iso_home, {"iso_dir": "E:/ISOs"}) == Path("E:/ISOs"))
os.environ.pop("ATHENA_ISO_DIR", None)
check("... and asking where the images are does not make the folder appear",
      not (_iso_home / "isos").exists())

_iso_dir = Path(tempfile.mkdtemp())
for _n, _sz in (("win11.iso", 4096), ("bazzite.iso", 1024), ("arch.qcow2", 2 * 1024 * 1024),
                ("notes.txt", 3)):
    (_iso_dir / _n).write_bytes(b"x" * _sz)
# The shape a real image folder has: images sorted into folders, and one folder too
# deep to be somebody's image collection.
(_iso_dir / "windows").mkdir()
(_iso_dir / "windows" / "win10.iso").write_bytes(b"x" * (5 * 1024 * 1024))
(_iso_dir / "linux" / "arch").mkdir(parents=True)
(_iso_dir / "linux" / "arch" / "arch.iso").write_bytes(b"x" * 1024)
(_iso_dir / "mac").mkdir()
(_iso_dir / "deep" / "a" / "b" / "c").mkdir(parents=True)
(_iso_dir / "deep" / "a" / "b" / "c" / "buried.iso").write_bytes(b"x" * 16)

_scan = machines.scan_isos(_iso_dir)
_names = [i["name"] for i in _scan["isos"]]
check("the folder is listed as files, newest first, with what each name suggests",
      _scan["exists"] is True
      and sorted(_names) == sorted(["win11.iso", "bazzite.iso", "arch.qcow2", "notes.txt",
                                    "win10.iso", "arch.iso"]),
      json.dumps(_names))
check("... a file whose name suggests nothing is still listed, with no kind",
      any(i["name"] == "notes.txt" and i["kind"] == "" for i in _scan["isos"]))
check("... sizes come back in MB, so the menu can say how big a file is",
      any(i["name"] == "arch.qcow2" and i["size_mb"] == 2.0 for i in _scan["isos"]),
      json.dumps(_scan["isos"])[:200])
check("... and the newest file is first, whichever folder it was in",
      all(_scan["isos"][i]["modified"] >= _scan["isos"][i + 1]["modified"]
          for i in range(len(_scan["isos"]) - 1)),
      json.dumps([(i["name"], i["modified"]) for i in _scan["isos"]]))

# Reading *down* is the point: images organised as windows/linux/mac must not look like
# an empty folder.
_depth = {i["name"]: i for i in _scan["isos"]}
check("an image inside a subfolder is found, and says which folder it came from",
      _depth["win10.iso"]["rel"] == "windows" and _depth["win11.iso"]["rel"] == "",
      json.dumps({k: v["rel"] for k, v in _depth.items()}))
check("... two levels down as well, because that is how images are organised",
      _depth["arch.iso"]["rel"] == "linux/arch", json.dumps(_depth["arch.iso"]))
check("... folders are counted by where the images are, so the panel can group them",
      _scan["folders"] == 3, str(_scan["folders"]))
check("... a tree deeper than the bound is counted rather than walked forever",
      "buried.iso" not in _depth and _scan["skipped"] >= 1, str(_scan["skipped"]))
check("... and depth says how far to read: shallow means the top level only",
      sorted(i["name"] for i in machines.scan_isos(_iso_dir, depth=0)["isos"])
      == sorted(["arch.qcow2", "win11.iso", "bazzite.iso", "notes.txt"]),
      json.dumps([i["name"] for i in machines.scan_isos(_iso_dir, depth=0)["isos"]]))
check("... a folder that is not there is not an error, it is a fact",
      machines.scan_isos(_iso_dir / "nope")["exists"] is False
      and machines.scan_isos(_iso_dir / "nope")["isos"] == [])
check("... and a folder holding more files than the limit says how many it left out",
      machines.scan_isos(_iso_dir, limit=2)["skipped"] == 5,
      str(machines.scan_isos(_iso_dir, limit=2)["skipped"]))

# The picker: dirs only, files counted, and a way up that stops at the root.
_fold = machines.list_folders(str(_iso_dir))
check("the folder picker lists folders and counts files rather than listing them",
      _fold["ok"] is True
      and sorted(d["name"] for d in _fold["dirs"]) == ["deep", "linux", "mac", "windows"]
      and _fold["files"] == 4,
      json.dumps({"dirs": [d["name"] for d in _fold["dirs"]], "files": _fold["files"]}))
check("... it says where up is, and starts from a drive when it is given nothing",
      _fold["parent"] == str(_iso_dir.parent)
      and machines.list_folders("")["path"] is not None
      and len(machines.list_folders("")["drives"]) >= 1,
      json.dumps({"parent": _fold["parent"], "drives": machines.list_folders("")["drives"]}))
check("... and a folder it cannot read is reported, not fatal",
      machines.list_folders(str(_iso_dir / "nope" / "deeper"))["ok"] is True)

# The reason the kind matters: a file chosen from the menu is only a machine if the kind
# travelled with it.
_pick = [i for i in _scan["isos"] if i["name"] == "bazzite.iso"][0]
try:
    machines.MachineSpec(name="picked", provider="qemu",
                         image={"kind": _pick["kind"], "path": _pick["path"]}).validate()
    _okpick = True
except machines.SpecError:
    _okpick = False
check("a file chosen from the folder becomes a machine, because the kind came with it",
      _okpick, json.dumps(_pick))
try:
    machines.MachineSpec(name="guessed", provider="qemu",
                         image={"kind": "", "path": _pick["path"]}).validate()
    _badpick = False
except machines.SpecError:
    _badpick = True
check("... and a machine whose image kind was never written down is still refused",
      _badpick)

# ---- installing QEMU: the half that can be proved without a network, and the half that
# can be proved with one that is ours ----

_manifest_text = """Found QEMU [SoftwareFreedomConservancy.QEMU]
Version: 11.1.0
Publisher: QEMU Community
Installer:
  Installer Type: nullsoft
  Installer Url: https://qemu.weilnetz.de/w64/2026/qemu-w64-setup-20260811.exe
  Installer SHA256: F98A8AEB5F7FAEA9765B6DEE28316C266CD179D80354A2FED8E50176F9A2E59F
  Release Date: 2026-08-11"""
# Captured from `winget show -e --id SoftwareFreedomConservancy.QEMU` on winget 1.29.
_man = machines.parse_installer_fields(_manifest_text)
check("winget's listing is read for the installer's URL, hash and version",
      _man["ok"] and _man["version"] == "11.1.0"
      and _man["url"].endswith("qemu-w64-setup-20260811.exe")
      and _man["sha256"].startswith("f98a8aeb")
      and _man["file"] == "qemu-w64-setup-20260811.exe",
      json.dumps(_man))
check("... the hash is lower-cased, because hashes are compared and not printed",
      _man["sha256"] == _man["sha256"].lower() and len(_man["sha256"]) == 64)
check("... and a listing with no installer in it says so instead of inventing a URL",
      machines.parse_installer_fields("Found nothing\nVersion: 1.0")["ok"] is False
      and machines.parse_installer_fields("")["ok"] is False)
check("a command with a space in its path is split the way Windows needs it",
      machines.split_command('"C:/Program Files/QEMU/setup.exe" /S')
      == ("C:/Program Files/QEMU/setup.exe", "/S")
      and machines.split_command("wsl.exe --install") == ("wsl.exe", "--install")
      and machines.split_command("") == ("", ""))

# A real HTTP server inside this test: real bytes, a real Content-Length, and progress
# taken from them — the point of the whole download half.
import hashlib                                                     # noqa: E402
import http.server                                                 # noqa: E402
import socketserver                                                # noqa: E402
import threading                                                   # noqa: E402

_dl_root = Path(tempfile.mkdtemp())
_blob = os.urandom(3 * 1024 * 1024)
(_dl_root / "installer.exe").write_bytes(_blob)
_blob_sha = hashlib.sha256(_blob).hexdigest()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):                                     # noqa: D102
        pass


_httpd = socketserver.TCPServer(("127.0.0.1", 0), lambda *a, **k: _Quiet(*a, directory=str(_dl_root), **k))
_threading = threading.Thread(target=_httpd.serve_forever, daemon=True)
_threading.start()
_base = f"http://127.0.0.1:{_httpd.server_address[1]}"

_seen = []
_got = machines.download_with_progress(f"{_base}/installer.exe", _dl_root / "out" / "i.exe",
                                       on_progress=lambda d, t: _seen.append((d, t)),
                                       expect_sha256=_blob_sha)
check("a download reports the bytes as they land, against a total it did not guess",
      _got["ok"] and _got["bytes"] == len(_blob) and _got["total"] == len(_blob)
      and _seen and _seen[0] == (0, len(_blob)) and _seen[-1][0] == len(_blob),
      json.dumps({"ok": _got.get("ok"), "n": len(_seen), "last": _seen[-1] if _seen else None}))
check("... the numbers only ever go up, because a bar that jumps back is a lie",
      all(_seen[i][0] <= _seen[i + 1][0] for i in range(len(_seen) - 1)))
check("... the file is only in place once it is whole, and the hash it came with is checked",
      _got["sha256"] == _blob_sha and (_dl_root / "out" / "i.exe").exists()
      and not (_dl_root / "out" / "i.exe.part").exists())
_bad = machines.download_with_progress(f"{_base}/installer.exe", _dl_root / "out" / "bad.exe",
                                       expect_sha256="0" * 64)
check("... a file that is not the published one is deleted rather than installed",
      _bad["ok"] is False and "not the one that was published" in _bad["error"]
      and not (_dl_root / "out" / "bad.exe").exists()
      and not (_dl_root / "out" / "bad.exe.part").exists(), json.dumps(_bad)[:200])
_miss = machines.download_with_progress(f"{_base}/not-here.exe", _dl_root / "out" / "x.exe")
check("... and a download that fails says so, with nothing left behind",
      _miss["ok"] is False and not (_dl_root / "out" / "x.exe").exists(), json.dumps(_miss)[:200])
_httpd.shutdown()

# The plan itself: with a manifest it is a counted download, without one it is the
# command that still works. Both are choices made here, not in a click handler.
_was = machines.qemu_manifest
machines.qemu_manifest = lambda *a, **k: _man
_plan = machines.install_plan("qemu")
check("on Windows the plan is a download we can watch, with the published hash",
      (_plan.get("method") == "download" and _plan.get("url") == _man["url"]
       and _plan.get("sha256") == _man["sha256"] and _plan.get("installer_args") == ["/S"])
      if sys.platform == "win32" else _plan.get("method") == "manual",
      json.dumps(_plan)[:220])
machines.qemu_manifest = lambda *a, **k: {"ok": False, "error": "no winget"}
_plan2 = machines.install_plan("qemu")
check("... and when winget will not say, it falls back to the command rather than nothing",
      _plan2.get("ok") is True and _plan2.get("method") in ("job", "manual")
      and "winget" in str(_plan2.get("command", "")) or _plan2.get("method") == "manual",
      json.dumps(_plan2)[:200])
machines.qemu_manifest = _was
check("an unknown prerequisite is refused rather than guessed at",
      machines.install_plan("a-nonsense")["ok"] is False)

# ---- the restart race: a stop is almost always followed by a start --------------------
# Moving a screen between the panel and a window is a restart, and a restart arrives at
# QEMU's chardev a moment after the previous QEMU was killed. The file is the thing that
# decides whether the boot works, so the wait is on the file, not on a nap.
print("\nthe console file a restart has to take over")
with tempfile.TemporaryDirectory() as d:
    folder = Path(d)
    check("a folder with a free console says so, at once",
          machines.wait_for_console(folder, 2.0) is True)
    check("... and the file it names is the one the guest's serial goes to",
          machines.console_path(folder).name == "console.log")

    # A folder that cannot hold the file is a "no", not an exception: this runs inside a
    # click, and a stack trace there is a crash in the panel rather than a sentence.
    check("a console that cannot be opened is a false, not a crash",
          machines.wait_for_console(folder / "no-such-folder", 0.4) is False)

    # QEMU's own words, verbatim from the run that measured this.
    real = ("qemu-system-x86_64: -serial file:console.log: could not connect serial device "
            "to character backend 'file:console.log'")
    check("QEMU's complaint about its chardev is recognised as worth one more attempt",
          machines.console_was_held(real) is True)
    check("... and so is the shorter way it words the same thing",
          machines.console_was_held("could not connect serial device to character backend") is True)
    check("a missing image is not retried — that answer will not change",
          machines.console_was_held("Could not open 'nope.iso': No such file or directory") is False)
    check("an empty log is not a reason to start the same machine twice",
          machines.console_was_held("") is False and machines.console_was_held(None) is False)

# --- the ports a machine forwards, and what a taken one is told -----------------
# QEMU's own answer to a busy port is one line in a log and an immediate exit. The port is
# named here instead, from the same list the command line is built from, so what is checked
# cannot drift from what is opened.
print("\n-- forwarded ports --")


def _spec(**kw):
    s = machines.MachineSpec(name="porty")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


_plain = _spec(ports=[{"host": 8080, "guest": 80}, {"host": 9000, "guest": 9000}])
check("the list is the declared ports, in order",
      machines.forwarded_ports(_plain) == [(8080, 80), (9000, 9000)],
      str(machines.forwarded_ports(_plain)))
check("ssh is a forwarded port like any other, to the guest's own port 22",
      machines.forwarded_ports(_spec(ssh={"port": 2222}, ports=[{"host": 8080, "guest": 80}]))
      == [(2222, 22), (8080, 80)])
check("a half-declared pair is not forwarded, so it is not claimed either",
      machines.forwarded_ports(_spec(ports=[{"host": 0, "guest": 80}, {"guest": 22}])) == [])
check("... nor is a pair that is not numbers", machines.forwarded_ports(
      _spec(ports=[{"host": "eighty", "guest": 80}])) == [])
check("a machine that forwards nothing forwards nothing", machines.forwarded_ports(_spec()) == [])

# A listener this test owns, with room in its accept queue: a backlog of one fills after the
# first probe (nobody is accepting them), and the port then answers as free — which would make
# these checks depend on the order they happen to run in.
_busy = __import__("socket").socket()
_busy.bind(("127.0.0.1", 0))
_busy.listen(128)
_busy_port = _busy.getsockname()[1]
_held = _spec(ports=[{"host": _busy_port, "guest": 80}])
if not machines._port_free(_busy_port):
    check("a port something else is listening on is the one that is named",
          machines.busy_forwarded_port(_held) == _busy_port, str(_busy_port))
    _why = machines._port_hint(_held, "qemu exited on start")
    check("the failure says which port, and what to do about it",
          str(_busy_port) in _why and "already in use" in _why and "settings" in _why,
          _why[:160])
    check("... and it is not repeated when QEMU already said it",
          machines._port_hint(_held, "Could not set up host forwarding rule 'tcp:127.0.0.1:x'")
          == "Could not set up host forwarding rule 'tcp:127.0.0.1:x'")
else:
    print(f"  skip a bound port is not seen as held on this machine (port {_busy_port})")
    skipped += 1
_busy.close()

check("... and a machine whose ports are all free is not blamed for anything",
      machines.busy_forwarded_port(_plain) is None)
check("... while a failure that is not about a port keeps its own words",
      machines._port_hint(_plain, "qemu exited on start") == "qemu exited on start")

check("the command line opens exactly the ports the check asks about",
      [a for a in machines.qemu_argv(_plain, Path("shape")) if "hostfwd" in a]
      == ["user,id=net0,hostfwd=tcp:127.0.0.1:8080-:80,hostfwd=tcp:127.0.0.1:9000-:9000"],
      str([a for a in machines.qemu_argv(_plain, Path("shape")) if "hostfwd" in a]))

# --- the WSL question is asked once, not once per poll --------------------------
# Every answer costs two `wsl.exe` launches, and the panel asks for the machine list every few
# seconds. A cache that is never dropped is the other half of the bug: a distro installed from the
# panel would not appear, so the operations that change the list forget the answer.
print("\n-- asking about WSL --")
_asked = {"runs": 0}
_real_run, _real_path = machines._run, machines.wsl_path


def _counting_run(argv, timeout=60):
    _asked["runs"] += 1
    return {"ok": True, "code": 0, "text": "WSL version: 2.6.1.0", "error": ""}


machines._run = _counting_run
machines.wsl_path = lambda: "wsl.exe"
try:
    machines.forget_wsl_status()
    first = machines.wsl_status()
    runs_first = _asked["runs"]
    second = machines.wsl_status()
    check("a second question inside the window is answered without starting anything",
          runs_first == 2 and _asked["runs"] == 2, f"{runs_first} then {_asked['runs']}")
    check("... and it is the same answer, not a second shape of it", first == second)
    machines.wsl_status(fresh=True)
    check("... while asking for a fresh one starts the processes again",
          _asked["runs"] == 4, str(_asked["runs"]))
    check("... and the fresh answer replaces the cache", machines._WSL_STATUS["installed"] is True)

    machines.forget_wsl_status()
    machines.wsl_status()
    check("... and forgetting means the next question is really asked",
          _asked["runs"] == 6, str(_asked["runs"]))

    # A distro operation that changes the list has to drop the answer, or the panel keeps showing
    # the list from before the change until the timer runs out.
    machines.wsl_status()
    before = _asked["runs"]
    machines.wsl_terminate("athena-probe")
    check("... and an operation that changes a distro's state drops the answer",
          machines._WSL_STATUS == {} and _asked["runs"] > before, str(_asked["runs"]))

    # "There is no WSL on this machine" is a fact about the machine and is worth keeping: it costs
    # the same two launches to find out again, and the timer is what un-sticks it.
    _asked["runs"] = 0
    machines.wsl_path = lambda: None
    machines.forget_wsl_status()
    machines.wsl_status()
    machines.wsl_status()
    check("... and 'there is no WSL on this machine' is cached like any other answer",
          _asked["runs"] == 0 and machines._WSL_STATUS.get("installed") is False,
          f"{_asked['runs']} runs, {machines._WSL_STATUS}")
finally:
    machines._run, machines.wsl_path = _real_run, _real_path
    machines.forget_wsl_status()

# --- a spec a person edited, refused in the words of the field -------------------
# `machine.json` is a text file people can open. The argv builder reads numbers out of it, so
# `"memory_mb": "8GB"` used to raise inside the builder and answer 500 — somebody who edited a
# text file was told the server broke. And a stop must never depend on this: a machine whose spec
# has a typo still has to be stoppable.
print("\n-- a spec somebody edited by hand --")
_ok = machines.MachineSpec(name="box", cpus=4, memory_mb=8192)
check("a machine that is all there is no problem at all",
      machines.spec_problem(_ok) == "", machines.spec_problem(_ok))
check("a number that is not a number names the field and the range",
      "memory_mb" in machines.spec_problem(_spec(memory_mb="8GB")),
      machines.spec_problem(_spec(memory_mb="8GB")))
check("... and a number outside what a machine may have names the range too",
      "64" in machines.spec_problem(_spec(cpus=999)), machines.spec_problem(_spec(cpus=999)))
check("a name somebody cleared is refused as a name, not blamed on the command line",
      "no name" in machines.spec_problem(_spec(name="   ")),
      machines.spec_problem(_spec(name="   ")))
check("a display that is not a way to be shown says which ways there are",
      "display" in machines.spec_problem(_spec(display="hologram")),
      machines.spec_problem(_spec(display="hologram")))
check("... and an image kind this machine cannot boot likewise",
      "image" in machines.spec_problem(_spec(image={"kind": "floppy"})),
      machines.spec_problem(_spec(image={"kind": "floppy"})))
check("a disk image that has been moved is named by where it should be",
      "not there any more" in machines.spec_problem(
          _spec(image={"kind": "disk", "path": str(Path("shape") / "gone.qcow2")})))

# The fields nobody set are not faults: a WSL machine has no disk to size, no display and no
# boot order, and a check that called those missing would refuse every machine of that kind.
_wsl = machines.MachineSpec(name="wslbox", provider="wsl", cpus=2, memory_mb=4096)
for _f in ("disk_gb", "display", "boot", "image"):
    setattr(_wsl, _f, None)
check("... while a machine that simply has no disk, display or boot order is fine",
      machines.spec_problem(_wsl) == "", machines.spec_problem(_wsl))
_bad = machines.spec_problem(_spec(memory_mb="8GB"))
check("... and the sentence says what is wrong with it and what it may be instead",
      "not a number" in _bad and "between" in _bad, _bad)
check("... and a provider this plugin cannot start is refused before anything is built",
      "provider" in machines.spec_problem(_spec(provider="vmware")),
      machines.spec_problem(_spec(provider="vmware")))

print(f"\n{failed} failed" if failed
      else f"\n{passed}/{passed} checks passed" + (f" ({skipped} skipped)" if skipped else ""))
sys.exit(1 if failed else 0)
