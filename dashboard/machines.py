"""Machines — a real OS Athena can start, stop, hand a command to, and show you.

Why this exists: half of what people want to try is not native to the machine
Hermes runs on. An app that only installs on Ubuntu, a build that needs Arch, a
"does this work on Windows" question that should not be answered by guesswork.

Two providers, one interface:

    wsl    the default on Windows. A real Linux kernel, usually already
           installed, a fraction of the cost of a full VM, and — the decisive
           part — the guest SHARES YOUR LOCALHOST, so a service started inside it
           is reachable from Hermes at 127.0.0.1, which is what the app runner
           already assumes.
    qemu   the escape hatch: an Omarchy or Bazzite ISO, a Windows installer, a
           .qcow2 you already have.

Machines are CLAIMABLE, not shared. Athena creates its own distro or its own
disk under `<athena home>/machines/<name>/` so that an agent can be handed a
shell without that shell being somebody's development environment. Pointing a
machine at an EXISTING distro is supported and is an explicit choice
(`attach: true`) — it is the one configuration where an agent command can touch
work you care about, so it is never what you get by accident.

Nothing here installs anything. `wsl --install` needs elevation and usually a
reboot, and QEMU is a system package; both are reported as `needs_setup` with
the exact command so a person (or an agent that has been told to ask) decides.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
PROVIDERS = ("wsl", "qemu")
IMAGE_KINDS = ("rootfs", "iso", "disk")
# `""` is not a fourth kind of display: it is "nobody said", which this computer
# answers with `default_display()`. A spec saved from the panel always carries the
# answer, so what a machine does is readable months later without re-deciding.
DISPLAYS = ("", "panel", "window", "none")
# Where a machine with a medium attached starts. `""` is "nobody said", which this computer
# answers with `boot_preference()`: the disk first, the medium as the fallback. That is what
# makes an installer ISO a machine that becomes its own computer — and what stops an installed
# machine from booting back into the installer because a flag nobody set still said so.
BOOTS = ("", "system", "medium")

# When is an old installer no longer an installer? When there is a system on the disk. Measured
# on this host, 2026-10-14: a never-started 40 GB qcow2 is 193 KiB, a booted live session leaves
# a few MiB behind, and omarchy's finished install was 5.89 GiB. 256 MiB sits above "this disk is
# still empty" and below anything a person would call installed, which is the whole job of a
# threshold — and because it can be wrong, the panel says what it decided and offers the way back.
INSTALLED_BYTES = 256 * 1024 * 1024

# One place that decides what a machine can be made of. The Ubuntu entry is the
# default because it is the image most apps assume, and it is a real URL that was
# checked rather than remembered.
PRESETS: Dict[str, Dict[str, Any]] = {
    "ubuntu-24.04": {
        "label": "Ubuntu 24.04",
        "provider": "wsl",
        "image": {
            "kind": "rootfs",
            "url": "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/"
                   "ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz",
        },
        "notes": "The default. A real Ubuntu, Athena-owned, about 340 MB to fetch once.",
    },
    "debian": {
        "label": "Debian",
        "provider": "wsl",
        "image": {"kind": "rootfs", "url": ""},
        "notes": "Supply a Debian WSL rootfs tarball, or drag one in from the panel.",
    },
    "arch": {
        "label": "Arch",
        "provider": "wsl",
        "image": {"kind": "rootfs", "url": ""},
        "notes": "A rootfs tarball from the Arch WSL images. The honest base for testing "
                 "anything Arch-shaped.",
    },
    "custom-iso": {
        "label": "Boot an ISO or disk image",
        "provider": "qemu",
        "image": {"kind": "iso", "path": ""},
        "notes": "Omarchy, Bazzite, a Windows installer, or a qcow2/vhdx you already have.",
    },
}

_INSTALL_COMMANDS = {
    "win32": "winget install -e --id SoftwareFreedomConservancy.QEMU",
    "linux": "sudo apt install qemu-system-x86 qemu-utils",
    "darwin": "brew install qemu",
}

QEMU_WINGET_ID = "SoftwareFreedomConservancy.QEMU"


class SpecError(ValueError):
    """A machine spec that would not work if it were saved."""


# --------------------------------------------------------------------------- paths
#
# Derived, never written down. This module deliberately does not import
# plugin_api: plugin_api imports machines for its routes and tools, and a cycle
# between the two would make the import order load-bearing, which is how a plugin
# ends up working only in the process that imported it first. Callers pass the
# home in; `default_home()` resolves the same folder for anything running outside
# the engine (the test harness, a machine driven from a shell).

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def default_home() -> Path:
    """Athena's home, resolved the way plugin_api.py resolves it — one answer, not two.

    ATHENA_HOME wins (the test harness and anything running outside the engine sets
    it), then a relocated home from settings.json, then the default inside Hermes'
    data directory. Callers inside the plugin pass `engine.home` in instead; this
    exists so a machine started from outside the engine lands in the same place.
    """
    override = os.environ.get("ATHENA_HOME")
    if override:
        return Path(override)
    home = PLUGIN_DIR.parent.parent / "athena"
    for candidate in (home / "settings.json", PLUGIN_DIR / "settings.json"):
        try:
            value = (json.loads(candidate.read_text(encoding="utf-8")) or {}).get("home")
            if value:
                home = Path(str(value).replace("\\", "/"))
                break
        except Exception:  # noqa: BLE001 — an unreadable settings file is not a reason to fail
            continue
    try:
        return home.resolve()
    except Exception:  # noqa: BLE001
        return home


def machines_dir(home: Path) -> Path:
    return Path(home) / "machines"


def machine_dir(home: Path, name: str) -> Path:
    return machines_dir(home) / safe_name(name)


def forgotten_dir(home: Path) -> Path:
    """Where a machine goes when it is forgotten rather than destroyed.

    Forgetting used to delete the folder for every kind of machine, which made
    "Forget it (keeps the distro)" a promise the code did not keep on a QEMU machine:
    the disk file went with the folder. A separate place outside `machines/` is what
    lets the distro stay and Athena stop managing it, and it is scanable, so the panel
    can list what is in there and offer it back.
    """
    return Path(home) / "forgotten"


def forgotten_machine_dir(home: Path, name: str) -> Path:
    return forgotten_dir(home) / safe_name(name)


def forgotten_machines(home: Path) -> List[Dict[str, Any]]:
    """What Athena has been asked to forget, with what is still on the disk.

    Read from the folders rather than remembered in a file: a record of a machine that
    is no longer there is a record that can be wrong, and the folder is the machine.
    """
    out: List[Dict[str, Any]] = []
    root = forgotten_dir(home)
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        spec = None
        try:
            spec = load_spec(home, folder.name) if (folder / "machine.json").is_file() else None
            if spec is None:
                # A forgotten folder is not under `machines/`, so it is read directly.
                raw = json.loads((folder / "machine.json").read_text(encoding="utf-8"))
                spec = MachineSpec(**raw) if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001 — an unreadable one is still listed, by name
            spec = None
        disk = folder / "disk.qcow2"
        try:
            size = disk.stat().st_size if disk.is_file() else 0
        except OSError:
            size = 0
        out.append({
            "name": spec.name if spec is not None else folder.name,
            "summary": spec.summary() if spec is not None else "a machine Athena cannot read",
            "provider": getattr(spec, "provider", "") or "",
            "home": str(folder),
            "disk_bytes": size,
            "keeps": ("the distro on that disk" if size else "nothing but its folder"),
        })
    return out


def restore_machine(home: Path, name: str) -> Dict[str, Any]:
    """Bring a forgotten machine back, whole. The folder moves; nothing is copied."""
    src = forgotten_machine_dir(home, name)
    if not src.is_dir():
        return {"ok": False, "error": f"nothing has been forgotten under the name {name!r}"}
    dest = machine_dir(home, name)
    if dest.exists():
        return {"ok": False,
                "error": f"there is already a machine called {name!r} — take it away first, "
                         f"or bring this one back under another name"}
    try:
        machines_dir(home).mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"it could not be moved back: {e}"}
    return {"ok": True, "name": name, "home": str(dest)}


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    """A machine name that is safe as a directory and as a WSL distro name.

    WSL rejects a distro name with a path separator; it will happily accept a
    name that is merely ugly, so the ugly ones are cleaned here rather than
    argued about by two different callers.
    """
    cleaned = _SAFE.sub("-", str(name or "").strip()).strip(".-")
    return cleaned[:40] or "machine"


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", str(name or "")))


# --------------------------------------------------------------------------- spec

@dataclass
class MachineSpec:
    """Everything needed to rebuild this machine, and nothing that describes its disk.

    The disk is 40 GB of somebody's life; the spec is two hundred bytes of YAML.
    Keeping them apart is what makes Reset a real operation rather than a promise.
    """
    name: str
    provider: str = "wsl"
    os: str = ""
    image: Dict[str, Any] = field(default_factory=dict)
    distro: str = ""
    attach: bool = False
    cpus: int = 4
    memory_mb: int = 4096
    disk_gb: int = 40
    ssh: Dict[str, Any] = field(default_factory=dict)
    ports: List[Dict[str, Any]] = field(default_factory=list)
    vnc_port: int = 0
    # Empty means "this computer decides" — see `default_display()`. Anything that
    # saves a spec through `save_spec` records the answer, so a machine made from the
    # panel never carries an empty one.
    display: str = ""
    # `system` starts on what the disk holds; `medium` starts on the CD-ROM again. Empty
    # means the disk first with the medium as a fallback, so a machine with nothing on its
    # disk still lands in the installer.
    boot: str = ""
    notes: str = ""
    created: str = ""
    schema: int = SCHEMA_VERSION

    # ---------------------------------------------------------------- shape
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MachineSpec":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in dict(data or {}).items() if k in known})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        """Raise SpecError with a sentence a person can act on."""
        if not valid_name(self.name):
            raise SpecError("a machine name is letters, digits, dots, dashes or underscores, "
                            "starting with a letter or digit, up to 40 characters")
        if self.provider not in PROVIDERS:
            raise SpecError(f"provider {self.provider!r} is not one of {', '.join(PROVIDERS)}")
        kind = str((self.image or {}).get("kind") or "")
        if kind and kind not in IMAGE_KINDS:
            raise SpecError(f"image kind {kind!r} is not one of {', '.join(IMAGE_KINDS)}")
        if self.display not in DISPLAYS:
            raise SpecError(f"display {self.display!r} is not one of {', '.join(DISPLAYS)}")
        if str(self.boot or "") not in BOOTS:
            raise SpecError(f"boot {self.boot!r} is not one of "
                            f"{', '.join(b or 'disk-then-medium' for b in BOOTS)}")
        if self.provider == "wsl":
            if not self.attach and not self.distro:
                # Athena-owned machines get their own distro name, derived from the
                # machine name so two machines can never share one root filesystem.
                self.distro = f"athena-{safe_name(self.name)}"
            if self.attach and not self.distro:
                raise SpecError("a machine that attaches to an existing distro has to name it")
        if self.provider == "qemu":
            # QEMU has no rootfs to import: it boots an ISO or a disk, so a machine
            # with neither is a machine that can never start. Refusing it here is
            # what makes "give Athena an ISO instead of WSL" a field rather than a
            # promise — the failing case is the one where the field was forgotten.
            image = self.image or {}
            if not (image.get("path") or image.get("url")):
                raise SpecError("a QEMU machine needs an image: an ISO or a disk to boot, "
                                "as image.path or image.url")
            if not kind:
                raise SpecError("a QEMU machine's image needs a kind: iso or disk")
            # Ports are how anything in a QEMU guest becomes reachable: the guest has
            # no view of this computer's localhost. A malformed pair would become a
            # silently missing `hostfwd` — a machine that boots and then looks dead —
            # so it is refused while somebody is still looking at the form.
            normal: List[Dict[str, int]] = []
            for entry in (self.ports or []):
                try:
                    # A number written as a string is accepted and normalised rather
                    # than refused — JSON from a form arrives that way, and the value
                    # is unambiguous. Junk is refused, because junk is unambiguous too:
                    # it is not a port.
                    host, guest = int(entry.get("host")), int(entry.get("guest"))
                except (AttributeError, TypeError, ValueError):
                    raise SpecError(f"a port must be {{host: <number>, guest: <number>}}, "
                                    f"and this one is {entry!r}") from None
                if not (1 <= host <= 65535 and 1 <= guest <= 65535):
                    raise SpecError(f"port numbers run from 1 to 65535, and this pair is "
                                    f"{host} -> {guest}")
                normal.append({"host": host, "guest": guest})
            self.ports = normal
        try:
            self.cpus = max(1, min(64, int(self.cpus)))
            self.memory_mb = max(256, min(262144, int(self.memory_mb)))
            self.disk_gb = max(2, min(4096, int(self.disk_gb)))
        except (TypeError, ValueError):
            raise SpecError("cpus, memory_mb and disk_gb are numbers")

    # ---------------------------------------------------------------- helpers
    @property
    def is_image_boot(self) -> bool:
        return str((self.image or {}).get("kind") or "") in ("iso", "disk")

    def image_ref(self) -> str:
        img = self.image or {}
        return str(img.get("path") or img.get("url") or "")

    def summary(self) -> str:
        if self.provider == "wsl":
            what = f"existing distro {self.distro}" if self.attach else f"distro {self.distro}"
        else:
            what = self.image_ref() or "no image yet"
        return f"{self.provider} · {what}"


# --------------------------------------------------------------------------- registry

def list_specs(home: Path) -> List[MachineSpec]:
    out: List[MachineSpec] = []
    root = machines_dir(home)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        spec_file = d / "machine.json"
        if not spec_file.is_file():
            continue
        try:
            out.append(MachineSpec.from_dict(json.loads(spec_file.read_text(encoding="utf-8"))))
        except Exception:  # noqa: BLE001 — a broken spec must not hide the healthy ones
            out.append(MachineSpec(name=d.name, notes="machine.json could not be read"))
    return out


def load_spec(home: Path, name: str) -> Optional[MachineSpec]:
    spec_file = machine_dir(home, name) / "machine.json"
    if not spec_file.is_file():
        return None
    return MachineSpec.from_dict(json.loads(spec_file.read_text(encoding="utf-8")))


def save_spec(home: Path, spec: MachineSpec) -> Path:
    spec.validate()
    d = machine_dir(home, spec.name)
    d.mkdir(parents=True, exist_ok=True)
    if not spec.created:
        import datetime
        spec.created = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    (d / "machine.json").write_text(json.dumps(spec.to_dict(), indent=2) + "\n", encoding="utf-8")
    return d


# --------------------------------------------------------------------------- host facts
#
# What this computer is, and what a guest can honestly be given. Split in two on
# purpose: `host_facts()` reads the machine, `fit_spec()` decides — and the decision
# is a pure function, so "the recommended settings for this machine" is a claim that
# can be checked anywhere, including on a computer with no VM support at all.

def _total_memory_mb() -> Optional[int]:
    """Total RAM in MB, or None. psutil when it is there, the platform's own call when not."""
    try:
        import psutil  # noqa: PLC0415 — a missing psutil must not break import of this module
        return int(psutil.virtual_memory().total // (1024 * 1024)) or None
    except Exception:  # noqa: BLE001
        pass
    try:
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys // (1024 * 1024)) or None
        else:
            page = os.sysconf("SC_PAGE_SIZE")
            pages = os.sysconf("SC_PHYS_PAGES")
            return int(page * pages // (1024 * 1024)) or None
    except Exception:  # noqa: BLE001
        return None


def _free_memory_mb() -> Optional[int]:
    """Memory this host could actually hand to a guest right now, or None.

    Deliberately not ``total - used``: the number that decides whether a guest boots is
    what the OS can free up without swapping, which is ``available``, not the arithmetic
    remainder. A machine at 90% of a 64 GB box is fine; one at 90% of a 16 GB box is not,
    and only the free figure tells them apart.
    """
    try:
        import psutil  # noqa: PLC0415 — as above, a missing psutil must not break the module
        return int(psutil.virtual_memory().available // (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    try:
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys // (1024 * 1024))
        else:
            page = os.sysconf("SC_PAGE_SIZE")
            avail = os.sysconf("SC_AVPHYS_PAGES")
            return int(page * avail // (1024 * 1024))
    except Exception:  # noqa: BLE001
        return None


def host_facts(path: Optional[Path] = None) -> Dict[str, Any]:
    """Cores, total memory and free space for this computer.

    A fact that cannot be read is ``None``, never a plausible default: a made-up
    "8 cores" would size somebody's machine from a number nobody measured.
    """
    cores = None
    try:
        import psutil  # noqa: PLC0415
        cores = int(psutil.cpu_count(logical=True) or 0) or None
    except Exception:  # noqa: BLE001
        cores = os.cpu_count() or None
    if cores is None:
        cores = os.cpu_count() or None
    free_gb = None
    try:
        probe = Path(path) if path else Path.home()
        # A folder that does not exist yet has no free space to report; the disk it
        # would be created on does, so walk up to something real rather than giving
        # up and calling the answer unknown.
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free_gb = round(shutil.disk_usage(str(probe)).free / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        free_gb = None
    # `memory_free_mb` rides along with `memory_mb` because the two together are the only
    # honest way to answer "can a guest run now": a host with 64 GB total and 900 MB free
    # will fail to boot the guest that `memory_mb` alone recommends.
    return {"cores": cores, "memory_mb": _total_memory_mb(),
            "memory_free_mb": _free_memory_mb(), "free_gb": free_gb,
            "platform": sys.platform}


def fit_spec(cores: Optional[int], memory_mb: Optional[int], free_gb: Optional[float],
             provider: str = "qemu") -> Dict[str, Any]:
    """The settings that suit a host of this size, with the reasoning attached.

    Pure: numbers in, numbers out. The rules are conservative on purpose — a guest
    that takes the whole machine is a guest that makes the machine unusable while it
    runs, which is how people learn to distrust VMs:

    * CPUs: half the cores, 2 to 8, always leaving the host at least one.
    * Memory: a quarter of RAM, 2 GB to 16 GB, never more than half, and never so
      much that the host is left under 2 GB.
    * Disk: 40 GB when there is room for it, smaller when the disk is nearly full,
      and never more than what is free once 10 GB is left behind.

    A number that cannot be read is dropped from the calculation rather than
    invented, and the ``why`` list says which parts are recommendations and which
    parts are just what the machine says.
    """
    why: List[str] = []
    cpus = 4
    if cores:
        cpus = max(2, min(8, int(cores) // 2))
        cpus = min(cpus, max(2, int(cores) - 1))
        cpus = min(cpus, max(1, int(cores)))
        why.append(f"{int(cores)} cores here → {cpus} for the guest, leaving the host "
                   f"{max(0, int(cores) - cpus)}")
    else:
        why.append(f"could not read the core count, so {cpus} is a default rather than a "
                   f"recommendation")
    mem = 4096
    if memory_mb:
        mem = max(2048, min(16384, int(memory_mb) // 4))
        mem = min(mem, max(1024, int(memory_mb) // 2))
        mem = min(mem, max(1024, int(memory_mb) - 2048))
        mem = int(mem // 512 * 512)
        why.append(f"{round(int(memory_mb) / 1024)} GB of RAM here → {round(mem / 1024)} GB "
                   f"for the guest, leaving the host at least 2 GB")
    else:
        why.append(f"could not read the total memory, so {round(mem / 1024)} GB is a default "
                   f"rather than a recommendation")
    disk = 40
    if free_gb is not None:
        free = float(free_gb)
        room = max(2, int(free - 10))
        disk = min(40, room)
        if disk < 40:
            why.append(f"{free} GB free here → a {disk} GB disk, because 10 GB stays free")
        else:
            why.append(f"{free} GB free here → the full 40 GB disk fits")
    else:
        why.append(f"could not read the free space, so {disk} GB is a default rather than a "
                   f"recommendation")
    if memory_mb and mem < 2048:
        why.append(f"this computer has {round(int(memory_mb) / 1024)} GB of RAM, which is not "
                   f"enough to run a guest comfortably — this is the largest the host can "
                   f"spare, not a recommendation")
    if cores and cpus >= int(cores):
        why.append(f"with only {int(cores)} core(s) there is nothing to leave behind — the "
                   f"guest and everything else here will take turns")
    if provider == "wsl":
        why.append("a WSL machine shares this computer's memory and CPU, so these are limits "
                   "on what the guest may use rather than memory taken at boot")
    else:
        why.append("a QEMU guest is given memory and cores up front, so these are amounts "
                   "taken while it runs")
    return {"cpus": cpus, "memory_mb": mem, "disk_gb": max(2, disk), "provider": provider,
            "why": why, "host": {"cores": cores, "memory_mb": memory_mb, "free_gb": free_gb}}


def recommended_spec(name: str, provider: str = "qemu", path: Optional[Path] = None,
                     **overrides: Any) -> MachineSpec:
    """A machine sized for this computer, so first use is a form that is already right."""
    facts = host_facts(path)
    fit = fit_spec(facts.get("cores"), facts.get("memory_mb"), facts.get("free_gb"), provider)
    data = {"name": name, "provider": provider, "cpus": fit["cpus"],
            "memory_mb": fit["memory_mb"], "disk_gb": fit["disk_gb"]}
    data.update(overrides)
    return MachineSpec.from_dict(data)


# --------------------------------------------------------------------------- editing
#
# What a machine's settings may be changed to *after* it exists. Sections 1-3 of the spec
# are the shape of the thing (a name, a provider, an OS) and none of them is editable: a
# machine is its own disk and its own distro, and swapping either under it makes a machine
# whose stored spec no longer describes what is on the disk. Everything below is a knob.

# The bounds are here, not in the panel: a number typed into a box and a number sent by an
# agent are the same question, and the answer has to be the same one. Wide on purpose — a
# 256 GB machine is somebody's real machine — but bounded, because `-m 0` and `-smp 9999`
# are not settings, they are a QEMU that will not start.
LIMITS = {
    "cpus": (1, 64),
    "memory_mb": (512, 262144),
    "disk_gb": (4, 4096),
}

def spec_problem(spec: "MachineSpec") -> str:
    """Why this spec cannot become a command line, or "" if it can.

    `machine.json` is a file a person can edit, and `qemu_argv` reads numbers straight out of it —
    so `"memory_mb": "8GB"`, a name somebody cleared, or an image path that has been moved raised
    inside the argv builder and came back as a 500 with a traceback in the log. Somebody who edited
    a text file was told the server broke. This asks the same questions first, and answers with a
    sentence that names the field and what it may be. A field nobody set is not a fault: a WSL
    machine has no disk to size.
    """
    if not str(getattr(spec, "name", "") or "").strip():
        return ("this machine has no name, and a name is what its folder, its window and its "
                "record are called")
    provider = str(getattr(spec, "provider", "") or "")
    if provider not in ("qemu", "wsl"):
        return f"{provider!r} is not a provider this machine knows how to start"
    for field, (lo, hi) in LIMITS.items():
        raw = getattr(spec, field, None)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return (f"{field} is {raw!r}, which is not a number — edit it to a whole number "
                    f"between {lo} and {hi}")
        if value < lo or value > hi:
            return (f"{field} is {value}, which is outside what a machine may have "
                    f"({lo} to {hi})")
    where = str(getattr(spec, "display", "") or "")
    if where not in DISPLAYS:
        return f"display is {where!r}, which is not a way this machine can be shown"
    boot = str(getattr(spec, "boot", "") or "")
    if boot not in BOOTS:
        return f"boot is {boot!r}, which is not somewhere this machine can start from"
    image = getattr(spec, "image", None) or {}
    if not isinstance(image, dict):
        return "the image section is not a set of settings — it should be an object with a kind"
    kind = str(image.get("kind") or "")
    if kind and kind not in IMAGE_KINDS:
        return (f"{kind!r} is not a kind of image this machine can boot "
                f"(one of: {', '.join(IMAGE_KINDS)})")
    if kind == "disk" and image.get("path"):
        path = Path(str(image["path"]))
        if not path.is_file():
            return f"the image this machine boots is not there any more: {path}"
    return ""


# Fields that change the command line, and therefore only take effect at the next boot.
NEEDS_RESTART = ("cpus", "memory_mb", "disk_gb", "display", "boot", "image")

# The settings a person may change from the panel. Everything absent here is refused by
# `apply_patch` with a sentence naming what it may be changed to.
PATCHABLE = ("cpus", "memory_mb", "disk_gb", "display", "boot", "image", "ports", "ssh",
             "notes")


def _check_cpu_topology(spec: "MachineSpec", patch: Dict[str, Any]) -> Optional[str]:
    """Refuse a cores/memory pair this computer cannot give, in the words of the answer.

    `fit_spec` already does this arithmetic when a machine is *made*; a machine being edited
    has to ask the same question, or the panel offers a number that fails at the next start.
    """
    cpus = int(patch.get("cpus", spec.cpus))
    memory = int(patch.get("memory_mb", spec.memory_mb))
    facts = host_facts(machines_dir(Path.home()))
    fit = fit_spec(facts.get("cores"), facts.get("memory_mb"), facts.get("free_gb"),
                   spec.provider)
    if cpus > int(fit.get("cpus") or 0):
        return (f"this computer has {fit.get('cpus')} usable cores for a "
                f"{spec.provider} machine, and {cpus} was asked for")
    if memory > int(fit.get("memory_mb") or 0):
        return (f"this computer can lend a {spec.provider} machine about "
                f"{fit.get('memory_mb')} MB of memory, and {memory} was asked for")
    return None


def apply_patch(spec: "MachineSpec", patch: Dict[str, Any],
                running: bool = False) -> Dict[str, Any]:
    """A machine's spec with the changes applied, and what those changes cost.

    Pure apart from the host's own capacity check: no file is written, nothing is started,
    and the answer says **how much of it needs a reboot to be true**. That is the whole
    point of doing this here rather than in the route — a setting that is recorded but not
    in effect is the kind of thing a panel shows as done, and a person only discovers at the
    next boot, as a machine that came up with the old numbers.

    Refusals (returned as `ok: False` with `error`): a field that is not a setting, a value
    out of bounds, a display/boot/image kind that is not one of the known ones, a port pair
    that is not a port pair, more cores or memory than this computer has, and a disk that
    would get *smaller* — shrinking a filesystem from outside the guest is how a disk is
    destroyed, so the only way down is Reset.
    """
    unknown = sorted(set(patch or {}) - set(PATCHABLE))
    if unknown:
        return {"ok": False, "error": f"{', '.join(unknown)} "
                                     f"{'is not a setting' if len(unknown) == 1 else 'are not settings'} "
                                     f"on a machine — changeable: {', '.join(PATCHABLE)}"}
    if not patch:
        return {"ok": False, "error": "nothing was changed"}

    values: Dict[str, Any] = {}
    for field in ("cpus", "memory_mb", "disk_gb"):
        if field not in patch:
            continue
        try:
            value = int(patch[field])
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{field} is a whole number, and this is "
                                         f"{patch[field]!r}"}
        low, high = LIMITS[field]
        if not (low <= value <= high):
            unit = {"cpus": "cores", "memory_mb": "MB of memory", "disk_gb": "GB"}[field]
            return {"ok": False, "error": f"{field} runs from {low} to {high} {unit}, "
                                         f"and {value} is outside that"}
        values[field] = value

    if "disk_gb" in values and values["disk_gb"] < int(spec.disk_gb):
        return {"ok": False, "error": f"a disk cannot be made smaller ({spec.disk_gb} GB is "
                                     f"on it) — a growing disk is resized, and a smaller one "
                                     f"means a new machine"}

    if "display" in patch:
        display = str(patch["display"] or "")
        if display not in DISPLAYS:
            return {"ok": False, "error": f"display {display!r} is not one of "
                                         f"{', '.join(d or 'auto' for d in DISPLAYS)}"}
        values["display"] = display

    if "boot" in patch:
        boot = str(patch["boot"] or "")
        if boot not in BOOTS:
            return {"ok": False, "error": f"boot {boot!r} is not one of "
                                         f"{', '.join(b or 'disk-then-medium' for b in BOOTS)}"}
        values["boot"] = boot

    if "image" in patch:
        image = patch["image"]
        if image is None:
            values["image"] = {}
        elif isinstance(image, dict):
            kind = str(image.get("kind") or "")
            if kind and kind not in IMAGE_KINDS:
                return {"ok": False, "error": f"image kind {kind!r} is not one of "
                                             f"{', '.join(IMAGE_KINDS)}"}
            path = str(image.get("path") or "")
            if path and not Path(path).exists():
                # A path that is not there is refused here rather than at boot: "the machine
                # started on nothing" is a much later sentence than "that file is not there".
                return {"ok": False, "error": f"there is no file at {path}"}
            if not path and not image.get("url") and kind:
                return {"ok": False, "error": f"an image of kind {kind} needs a path or a url"}
            values["image"] = {k: v for k, v in dict(image).items()
                               if k in ("kind", "path", "url", "name")}
        else:
            return {"ok": False, "error": "image is an object with kind/path/url, not "
                                         f"{type(image).__name__}"}

    if "ports" in patch:
        if patch["ports"] is None:
            values["ports"] = []
        elif isinstance(patch["ports"], (list, tuple)):
            ports: List[Dict[str, int]] = []
            for entry in patch["ports"]:
                try:
                    ports.append(parse_port_pair(entry))
                except SpecError as e:
                    return {"ok": False, "error": str(e)}
            values["ports"] = ports
        else:
            return {"ok": False, "error": "ports is a list of 8080:80 pairs"}

    if "ssh" in patch:
        if patch["ssh"] is None or patch["ssh"] is False:
            values["ssh"] = {}
        elif isinstance(patch["ssh"], dict):
            payload = dict(patch["ssh"])
            if payload:
                try:
                    port = int(payload.get("port") or 0)
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"an ssh port is a number, and this is "
                                                 f"{payload.get('port')!r}"}
                if not (1 <= port <= 65535):
                    return {"ok": False, "error": f"an ssh port runs from 1 to 65535, and "
                                                 f"{port} is outside that"}
                payload["port"] = port
            values["ssh"] = payload
        else:
            return {"ok": False, "error": "ssh is an object with a port, or nothing to turn "
                                         "it off"}

    if "notes" in patch:
        values["notes"] = str(patch["notes"] or "")

    wrong = _check_cpu_topology(spec, values)
    if wrong:
        return {"ok": False, "error": wrong}

    changed = {k: v for k, v in values.items() if getattr(spec, k, None) != v}
    if not changed:
        return {"ok": True, "changed": {}, "restart_required": False,
                "spec": spec.to_dict(), "notes": ["nothing was different"]}

    new = replace(spec, **changed)
    try:
        new.validate()
    except SpecError as e:
        return {"ok": False, "error": str(e)}

    restart = sorted(k for k in changed if k in NEEDS_RESTART)
    said = []
    if restart:
        said.append(("this machine is running: " if running else "")
                    + f"{', '.join(restart)} take{'s' if len(restart) == 1 else ''} effect "
                      + ("after a restart" if running else "at the next start"))
    if "disk_gb" in changed:
        said.append(f"the disk is grown to {changed['disk_gb']} GB"
                    + (" when it is next started" if running else " now"))
    return {"ok": True, "changed": {k: v for k, v in changed.items()},
            "restart_required": bool(restart) or "disk_gb" in changed, "spec": new.to_dict(),
            "notes": said}


def grow_disk(folder: Path, spec: "MachineSpec") -> Dict[str, Any]:
    """Grow the disk file to the spec's size, or say why it was left alone.

    Growing only, and only ever from outside a running guest: `qemu-img resize` on a disk a
    guest is writing to is a torn filesystem. A smaller number never reaches here —
    `apply_patch` refuses it — because the only non-destructive way to give the space back is
    the guest's own partition tools.
    """
    img = qemu_img_exe()
    disk = Path(folder) / "disk.qcow2"
    if not disk.is_file():
        return {"ok": True, "resized": False, "note": "this machine has no disk yet"}
    if not img:
        return {"ok": False, "error": "qemu-img is not here, so the disk could not be grown"}
    res = subprocess.run([img, "resize", str(disk), f"{int(spec.disk_gb)}G"],
                         capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        return {"ok": False, "error": "could not grow the disk: "
                                     + ((res.stderr or res.stdout or "").strip()
                                        or f"qemu-img exited {res.returncode}")}
    return {"ok": True, "resized": True, "bytes": disk.stat().st_size,
            "note": f"the disk file now holds {int(spec.disk_gb)} GB — the guest has to "
                    f"grow its own filesystem into it before the space is usable"}


# --------------------------------------------------------------------------- images
#
# Where the ISOs come from. One folder, pointed at by the user (no drive-wide scan —
# that is somebody's whole disk read to answer a question about one folder), plus an
# inbox inside Athena's home so there is somewhere to put files on day one.

ISO_DIR_SETTING = "iso_dir"

_ISO_SUFFIXES = (".iso",)
_DISK_SUFFIXES = (".qcow2", ".img", ".raw", ".vhd", ".vhdx", ".vmdk")

# What QEMU calls each of those on a `-drive` line. `.img` is raw by convention: a disk
# image someone intends to boot is a byte-for-byte image, and the alternative would be to
# let QEMU guess -- see `disk_format_for`.
_DISK_FORMATS = {".qcow2": "qcow2", ".img": "raw", ".raw": "raw",
                 ".vhd": "vpc", ".vhdx": "vhdx", ".vmdk": "vmdk"}


def _format_arg(path: Path | str) -> str:
    """`,format=…` for a `-drive` line, or "" when the name does not say what it is."""
    fmt = disk_format_for(path)
    return f",format={fmt}" if fmt else ""


def disk_format_for(path: Path | str) -> str:
    """The QEMU format name for a disk image file, or "" when the name does not say.

    A picked image boots *as itself*, so the format has to follow the file: `-drive
    format=qcow2` on somebody's `.vmdk` is a machine that will not start, and the error
    QEMU gives ("Invalid parameter 'qcow2'") names the driver rather than the mistake.
    The suffix is the only evidence available before QEMU opens the file, and every
    suffix this file accepts a disk image under is listed here.

    An unknown suffix answers "" — the caller then leaves `format=` off entirely and
    lets QEMU probe the image, which is the only remaining honest option: guessing a
    format from nothing writes the wrong driver into the command line.
    """
    return _DISK_FORMATS.get(Path(str(path or "")).suffix.lower(), "")
_ROOTFS_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.zst", ".tar.bz2", ".tbz2",
                    ".tar", ".wsl")


_PORT_PAIR = re.compile(r"^\s*(\d{1,5})\s*(?:->|[:>=-])\s*(\d{1,5})\s*$")


def parse_port_pair(text: Any) -> Dict[str, int]:
    """``"8080:80"`` → ``{"host": 8080, "guest": 80}``, or SpecError.

    The panel has a parser for the text somebody types and the API has one for the
    JSON that arrives; this is the one place the *rule* lives, so the two cannot
    disagree about what a port pair is. Anything that is not two numbers 1..65535
    around a separator is refused with the numbers in the sentence.
    """
    if isinstance(text, dict):
        return {"host": int(text.get("host")), "guest": int(text.get("guest"))}
    match = _PORT_PAIR.match(str(text or ""))
    if not match:
        raise SpecError(f"a port pair looks like 8080:80 (host:guest), and this is "
                        f"{str(text or '').strip()!r}")
    host, guest = int(match.group(1)), int(match.group(2))
    if not (1 <= host <= 65535 and 1 <= guest <= 65535):
        raise SpecError(f"port numbers run from 1 to 65535, and this pair is {host} -> {guest}")
    return {"host": host, "guest": guest}


def image_kind_for(name: str) -> str:
    """The kind a filename *suggests* — iso, disk, rootfs, or "" when it suggests nothing.

    A suggestion, not a fact, and never written into a spec without being shown: the
    menu labels a file with this, the form's kind field is set from it in front of the
    person choosing, and the spec still refuses an image with no kind. Guessing right
    most of the time is how a machine ends up booting nothing.
    """
    low = str(name or "").strip().lower()
    for suffix, kind in ((_ISO_SUFFIXES, "iso"), (_DISK_SUFFIXES, "disk"),
                         (_ROOTFS_SUFFIXES, "rootfs")):
        if any(low.endswith(s) for s in suffix):
            return kind
    return ""


# An image is a body of bytes that becomes the system a machine boots, so the one door it
# may come through from the network is a TLS fetch. `image.path` is the other door and it
# is a file a person picked; a URL that is really a local path, a UNC share, an `http://`
# mirror or a `file://` reference is refused *by name*, not silently fetched — "the bytes
# changed in transit" is not something this machine can notice afterwards.
_IMAGE_URL_OK = re.compile(r"^https://[^/\s]+(?::\d{1,5})?(?:/[^\s]*)?$", re.I)


def image_url_problem(url: str) -> str:
    """Why this image URL may not be fetched — or ``""`` when it may. Pure.

    Three shapes are named separately, because they need three different sentences: a
    scheme that is not https (an image nobody verified), no scheme at all (a file this
    computer already has, which belongs in ``image.path``), and an https URL with nothing
    to fetch from.
    """
    text = str(url or "").strip()
    if not text:
        return "an image URL cannot be empty"
    if _IMAGE_URL_OK.match(text):
        return ""
    if "://" in text:
        scheme = text.split("://", 1)[0].strip().lower()
        if scheme != "https":
            return (f"a machine image may only be fetched over https, and this URL is "
                    f"{scheme}://… — what arrives over anything else can be swapped in "
                    f"transit, and it becomes the system this machine boots")
    if text.lower().startswith("https:"):
        return f"{text!r} is not an URL an image can be fetched from"
    return (f"a machine image may only be fetched over https, and {text!r} is not an URL "
            f"at all — a file this computer already has goes in the machine's image path")


# A filename for a fetched image is derived, never taken. The last segment of a URL is text
# somebody else chose: `..`, an empty segment, a name carrying `/` or `\`, a Windows drive
# letter, a name Windows refuses to create. `Path(folder) / "image" / ".."` is the machine's
# own folder and `../../..` is somebody's home, so the segment is reduced to a plain name
# here — and the caller still refuses anything that would land outside `<machine>/image/`.
_IMAGE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._+-]")
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def image_filename(url: str, fallback: str = "image") -> str:
    """The local filename to fetch a URL's image into — a plain name. Pure.

    Everything that is not a letter, a digit or ``._+-`` becomes ``_``; the result is
    stripped of leading and trailing dots and dashes (so `..`, `.hidden` and `name.` are
    names, not paths); a reserved Windows device name is prefixed; and an empty result —
    including a URL that names no path at all, like `https://host` — is the fallback. The
    answer is always one path segment, and never `.` or `..`.
    """
    text = str(url or "").split("#", 1)[0].split("?", 1)[0]
    rest = text.split("://", 1)[1] if "://" in text else text
    # Drop `host[:port]`: what is left is the path, and a URL with none of it names nothing.
    path = rest.split("/", 1)[1] if "/" in rest else ("" if "://" in text else rest)
    tail = path.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = _IMAGE_NAME_CHARS.sub("_", tail.strip()).strip("._-")[:120]
    if not name:
        return str(fallback or "image")
    if name.upper().split(".", 1)[0] in _RESERVED_NAMES or not name[0].isalnum():
        name = f"image_{name}"
    return name.strip("._-") or str(fallback or "image")


def iso_dir(home: Path, settings: Optional[Dict[str, Any]] = None) -> Path:
    """The folder ISOs live in: the user's choice, or Athena's own inbox.

    Three answers, in order, and no others: the ``iso_dir`` setting (what the panel
    writes), ``ATHENA_ISO_DIR`` (the harness and anything driven from a shell), then
    ``<home>/isos``. Nothing is created here — asking where something is must not
    make a folder appear.
    """
    chosen = str((settings or {}).get(ISO_DIR_SETTING) or "").strip()
    if chosen:
        return Path(chosen.replace("\\", "/")).expanduser()
    env = str(os.environ.get("ATHENA_ISO_DIR") or "").strip()
    if env:
        return Path(env.replace("\\", "/")).expanduser()
    return Path(home) / "isos"


# How deep the image folder is read. People organise images as `windows/`,
# `linux/`, `mac/` and often a version below that; a tree five deep is somebody
# else's directory structure, not an image folder.
ISO_DEPTH = 3


def scan_isos(folder: Path, limit: int = 200, depth: int = ISO_DEPTH) -> Dict[str, Any]:
    """What is in the image folder, subfolders included, newest first.

    Read *down* through subfolders: a listing that stopped at the top level would
    show an empty folder to somebody with six images inside `windows/` and `linux/`.
    Depth is a bound rather than a guess (see ``ISO_DEPTH``).

    Each file carries the folder it lives in as ``rel`` (relative, ``""`` at the
    top), so the panel can group images by folder, and the kind its *name* suggests.
    Files only — a folder is counted, never returned as an image. A folder that
    cannot be read is reported as an error and skipped, not fatal.
    """
    folder = Path(folder)
    out: Dict[str, Any] = {"dir": str(folder), "exists": folder.is_dir(), "isos": [],
                           "folders": 0, "skipped": 0, "limit": limit, "depth": int(depth)}
    if not folder.is_dir():
        return out
    found: List[Any] = []          # (mtime, path, rel-folder)
    stack: List[Any] = [(folder, 0)]
    while stack:
        cur, level = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = sorted(list(it), key=lambda e: e.name)
        except OSError as e:
            out.setdefault("errors", []).append(f"could not read {cur}: {e}")
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if level + 1 <= int(depth):
                        stack.append((Path(entry.path), level + 1))
                    else:
                        out["skipped"] += 1
                    continue
                st = entry.stat()
            except OSError:
                continue
            rel = os.path.relpath(cur, folder)
            found.append((st.st_mtime, Path(entry.path),
                          "" if rel == "." else rel.replace("\\", "/")))
    found.sort(key=lambda row: row[0], reverse=True)
    for mtime, path, rel in found[:limit]:
        try:
            st = path.stat()
        except OSError:
            continue
        out["isos"].append({
            "name": path.name,
            "path": str(path),
            "rel": rel,
            "kind": image_kind_for(path.name),
            "size_mb": round(st.st_size / (1024 * 1024), 1),
            "modified": int(st.st_mtime),
        })
    if len(found) > limit:
        out["skipped"] += len(found) - limit
    # How many folders the *listed* images live in — what the panel groups by, and
    # not the same as how many folders were walked past.
    out["folders"] = len({str(i.get("rel") or "") for i in out["isos"]})
    return out


def drives() -> List[str]:
    """The drives (or `/`) to start a folder picker from — a fact, not a guess.

    `psutil` when it is importable, the root otherwise. Never a hard-coded `C:`:
    this file ships to Linux as well.
    """
    out: List[str] = []
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            mount = str(part.mountpoint or "")
            if mount and mount not in out:
                out.append(mount)
    except Exception:  # noqa: BLE001 — no psutil is a reason to fall back, not to fail
        out = []
    if not out:
        out = ["/"] if sys.platform != "win32" else []
    return out


def list_folders(path: Optional[str] = None, home: Optional[Path] = None) -> Dict[str, Any]:
    """The folders inside one folder, so an image folder can be picked instead of typed.

    Directories only — files are counted, never listed, because the question is
    "which folder" and a folder of forty ISOs is not the answer to it. `parent` is
    ``None`` at a drive root (and at `/`), so the panel stops offering a way up
    rather than drawing a button that does nothing. A folder that cannot be read
    comes back as an error with `ok: false` — an unreadable folder is a fact worth
    showing, not a crash.
    """
    win = sys.platform == "win32"
    here = str(path or "").strip()
    if not here:
        here = str(home or "") or (drives()[0] if drives() else ("C:/" if win else "/"))
    here = here.replace("\\", "/")
    try:
        cur = Path(here).expanduser()
        if not cur.is_dir():
            cur = cur.parent if cur.parent.is_dir() else Path.home()
    except Exception:  # noqa: BLE001
        cur = Path.home()
    out: Dict[str, Any] = {"ok": True, "path": str(cur), "sep": "/" if not win else "\\",
                           "windows": win, "dirs": [], "files": 0, "parent": None,
                           "drives": drives()}
    try:
        with os.scandir(cur) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        out["dirs"].append({"name": entry.name, "path": str(Path(entry.path))})
                    else:
                        out["files"] += 1
                except OSError:
                    continue
    except OSError as e:
        out["ok"] = False
        out["error"] = f"could not read {cur}: {e}"
    out["dirs"].sort(key=lambda d: str(d["name"]).lower())
    parent = cur.parent
    out["parent"] = None if parent == cur else str(parent)
    return out


# --------------------------------------------------------------------------- wsl

def wsl_path() -> Optional[str]:
    """The `wsl.exe` to drive, or None. Windows only — Linux runs machines as containers."""
    if sys.platform != "win32":
        return None
    found = shutil.which("wsl") or shutil.which("wsl.exe")
    if found:
        return found
    for cand in (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe",
                 Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Sysnative" / "wsl.exe"):
        if cand.is_file():
            return str(cand)
    return None


def decode_wsl(blob: bytes) -> str:
    """Decode `wsl.exe` output, which is UTF-16LE on Windows and plain text elsewhere.

    Checked rather than assumed: `wsl.exe -l -v` comes back as UTF-16LE with NO
    byte-order mark and two spaces of indentation before the header. Decoding it
    as text leaves a NUL between every character, so distro names read as
    `U\\0b\\0u\\0n\\0t\\0u\\0` and every name comparison quietly fails.
    """
    if not blob:
        return ""
    if blob[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return blob.decode("utf-16", errors="replace")
    # No BOM: UTF-16LE means every second byte is NUL in ASCII-range output.
    sample = blob[:400]
    if len(sample) >= 4 and sample[1::2].count(0) > len(sample[1::2]) * 0.7:
        return blob.decode("utf-16-le", errors="replace")
    return blob.decode("utf-8", errors="replace")


def parse_distro_table(text: str) -> List[Dict[str, Any]]:
    """Parse `wsl -l -v` into [{name, state, version, default}].

    `*` marks the default distro, columns are space-padded, and the state is one
    word — so splitting on whitespace and reading three fields is enough.

    The version column is what tells a row from a sentence. With no distros
    installed, `wsl -l -v` prints "Windows Subsystem for Linux has no installed
    distributions." — which splits into more than three words and yields a distro
    called `Windows` unless something rejects it. A real row always carries a
    version, and a version is always 1 or 2, so that is the invariant.
    """
    out: List[Dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        line = raw.replace("\r", "").rstrip()
        if not line.strip():
            continue
        if re.match(r"^\s*NAME\s+STATE\s+VERSION\s*$", line):
            continue
        default = line.lstrip().startswith("*")
        parts = line.lstrip("*").split()
        if len(parts) < 3:
            continue
        name, state, version = parts[0], parts[1], parts[2]
        if version not in ("1", "2") or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            continue
        out.append({"name": name, "state": state, "version": version, "default": default})
    return out


def _run(argv: List[str], timeout: int = 60) -> Dict[str, Any]:
    creation = 0
    if sys.platform == "win32":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           creationflags=creation, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return {"ok": False, "code": 127, "text": "", "error": f"{argv[0]} was not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "text": "",
                "error": f"timed out after {timeout}s: {' '.join(argv[:3])}…"}
    text = (decode_wsl(p.stdout) + decode_wsl(p.stderr)).strip()
    return {"ok": p.returncode == 0, "code": p.returncode, "text": text,
            "error": "" if p.returncode == 0 else text}


_WSL_STATUS: Dict[str, Any] = {}
_WSL_STATUS_AT: float = 0.0
# How long an answer about WSL is kept. Short enough that a distribution somebody installs by
# hand turns up on its own, long enough that a panel polling the machine list is not starting two
# processes every few seconds to be told the same thing.
WSL_STATUS_TTL = 15.0


def forget_wsl_status() -> None:
    """Throw the cached answer away, after anything that changes the list.

    Called by the operations that add or remove a distribution *and* by the ones that
    could not be checked: an import that timed out may still have landed, so the next
    question is asked fresh rather than answered from before it started.
    """
    global _WSL_STATUS, _WSL_STATUS_AT
    _WSL_STATUS, _WSL_STATUS_AT = {}, 0.0


def wsl_status(timeout: int = 30, fresh: bool = False) -> Dict[str, Any]:
    """Whether WSL is here, what version, and which distros it knows.

    Cached for `WSL_STATUS_TTL` seconds. This answer costs two `wsl.exe` launches — `--version`
    and `-l -v` — and launching a process on Windows is not free: the panel asks for the machine
    list every few seconds, and paying two process starts per poll for an answer that changes
    about once a week is work nobody asked for. `fresh=True` (and `forget_wsl_status()`) is the
    way to insist; a plain call is allowed to be a few seconds old.

    Deliberately three separate answers: WSL can be installed with no distro at
    all (the `--no-distribution` state a fresh machine is in), and "installed but
    empty" needs a different sentence on screen than "not installed".
    """
    global _WSL_STATUS, _WSL_STATUS_AT
    if _WSL_STATUS and not fresh and (time.time() - _WSL_STATUS_AT) < WSL_STATUS_TTL:
        return dict(_WSL_STATUS)
    out = _wsl_status_now(timeout)
    _WSL_STATUS, _WSL_STATUS_AT = dict(out), time.time()
    return out


def _wsl_status_now(timeout: int) -> Dict[str, Any]:
    exe = wsl_path()
    if not exe:
        return {"installed": False, "version": "", "distros": [], "default": "",
                "reason": "wsl.exe is not on this machine"}
    ver = _run([exe, "--version"], timeout=timeout)
    listing = _run([exe, "-l", "-v"], timeout=timeout)
    distros = parse_distro_table(listing["text"]) if listing.get("text") else []
    if not distros and listing.get("text"):
        # `wsl -l -v` on a machine with no distros prints a sentence, not a table.
        reason = listing["text"]
    elif not distros:
        reason = "WSL is installed but has no distributions yet"
    else:
        reason = ""
    version = ""
    if ver.get("text"):
        m = re.search(r"WSL version:\s*([0-9][\w.\-+]*)", ver["text"])
        version = m.group(1) if m else ""
    return {"installed": True, "version": version, "distros": distros,
            "default": next((d["name"] for d in distros if d["default"]), ""),
            "reason": reason}


def pick_distro(spec: MachineSpec, status: Optional[Dict[str, Any]] = None) -> str:
    """Which distro a spec means, falling back to the default one only when attaching."""
    status = status or wsl_status()
    if spec.distro:
        return spec.distro
    if spec.attach:
        return status.get("default") or ""
    return f"athena-{safe_name(spec.name)}"


def wsl_needs_setup(spec: Optional[MachineSpec] = None,
                    status: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The question to ask before a machine can be used, or None if nothing is missing.

    This is the shape the panel renders as a Setup card and the agent is told to
    ask about. It carries the command rather than running it: `wsl --install`
    needs elevation and usually a reboot, and a plugin that silently asks for
    admin is a plugin nobody should trust.
    """
    status = status or wsl_status()
    if not status.get("installed"):
        return {
            "needs_setup": "wsl",
            "question": "WSL is not installed on this machine. Set it up now?",
            "detail": "Windows needs the WSL feature enabled. It requires administrator "
                      "rights and usually a reboot, so it is not done for you without asking.",
            "command": "wsl --install --no-distribution",
            "then": "Reboot if Windows asks, then come back — the machine starts on its own.",
        }
    wanted = pick_distro(spec or MachineSpec(name="machine"), status) if spec else ""
    if spec and wanted and not any(d["name"] == wanted for d in status.get("distros", [])):
        if spec.attach:
            return {
                "needs_setup": "distro",
                "question": f"There is no WSL distro called {wanted!r}. Which one did you mean?",
                "detail": "This machine attaches to a distro you already have, so it has to "
                          "name one that exists.",
                "command": "", "distros": [d["name"] for d in status.get("distros", [])],
            }
        return {
            "needs_setup": "image",
            "question": f"Machine {spec.name!r} has no distro yet. Fetch and import "
                        f"{wanted!r}?",
            "detail": "This creates an Athena-owned WSL distro from the image in the spec.",
            "command": "", "distro": wanted,
        }
    return None


# --------------------------------------------------------------------------- qemu

def qemu_status() -> Dict[str, Any]:
    """Whether QEMU is here. Not installed silently: it is a system package."""
    exe = shutil.which("qemu-system-x86_64")
    if not exe and sys.platform == "win32":
        cand = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "qemu" / \
            "qemu-system-x86_64.exe"
        if cand.is_file():
            exe = str(cand)
    if not exe:
        return {"installed": False, "path": "", "version": "",
                "install_command": _INSTALL_COMMANDS.get(sys.platform, "install qemu"),
                "reason": "qemu-system-x86_64 is not on this machine"}
    return {"installed": True, "path": exe, "version": "", "install_command": "", "reason": ""}


def qemu_needs_setup() -> Dict[str, Any]:
    return {
        "needs_setup": "qemu",
        "question": "QEMU is not installed. Install it now?",
        "detail": "QEMU is what boots an ISO or a disk image. It is a system package of "
                  "about a gigabyte, so it is installed only when you say so.",
        "command": _INSTALL_COMMANDS.get(sys.platform, "install qemu"),
        "then": "Come back and start the machine again.",
    }


# ------------------------------------------------------- qemu, the escape hatch
#
# This is the half that answers questions WSL cannot: does the Bazzite image boot,
# what does Omarchy's installer actually do, does this work on Windows. Any of those
# needs a machine with a BIOS and a CD-ROM, which is what QEMU is.
#
# What is pure, and why: `qemu_argv()` builds the whole command line without running
# anything. Booting a guest is the one part of this file that cannot be proved on a
# computer with no QEMU on it — so the part that *can* be proved is the part that is
# wrong most often. A missing `-netdev`, a disk also handed in as a CD-ROM, an
# accelerator this host cannot do, a console written to a pipe with no reader: all of
# those are command-line mistakes, and all of them are caught here.
#
# A QEMU guest does NOT share this computer's localhost the way a WSL guest does, so
# anything it serves has to be forwarded explicitly (`hostfwd`) or it is unreachable
# from Hermes. Saying otherwise would be a lie the first time somebody tried to open
# the result.
#
# Snapshots refuse while the guest is up, on purpose: a copy of a disk being written
# is not a snapshot, it is a coin toss that looks like a backup.

def qemu_exe() -> str:
    """The emulator, or "" — `qemu_status()` owns the search, so it is asked once."""
    return str(qemu_status().get("path") or "")


def qemu_img_exe() -> str:
    """`qemu-img` sits beside the emulator on every install seen; PATH is the backstop."""
    exe = qemu_exe()
    if exe:
        p = Path(exe)
        cand = p.with_name("qemu-img" + (".exe" if p.suffix.lower() == ".exe" else ""))
        if cand.is_file():
            return str(cand)
    return shutil.which("qemu-img") or ""


def qemu_accel() -> str:
    """The accelerator this host can really use, in QEMU's own spelling.

    Windows gets `whpx` (it needs Hyper-V, which is why the system tab reports it);
    Linux gets `kvm` when /dev/kvm is actually usable, because a kvm flag on a host
    without kvm is an immediate refusal. Everything else — and every host where the
    probe is uncertain — gets `tcg`: the slow interpreter, which always works, which
    is the one property a fallback must have.
    """
    if sys.platform == "win32":
        return "whpx"
    if sys.platform.startswith("linux"):
        try:
            if os.access("/dev/kvm", os.R_OK | os.W_OK):
                return "kvm"
        except Exception:  # noqa: BLE001 — an unreadable /dev/kvm is simply no kvm
            pass
        return "tcg"
    return "tcg"


def _qemu_path(value: Any) -> str:
    """QEMU takes native paths; forward slashes are safe on both platforms."""
    return str(value).replace("\\", "/")


def has_desktop() -> bool:
    """Whether this host can put a window on a screen.

    Installing an OS from an ISO is something you *watch* — setup asks questions and
    the answer is a click — so the default machine gets a real QEMU window. A host
    with no session (a server, a container, a service account) has no screen to put one
    on, and QEMU's SDL refusing to open a window is an immediate exit that reads as
    "the machine will not start". So the question is asked rather than assumed.
    """
    if sys.platform in ("win32", "darwin"):
        return True
    if not sys.platform.startswith("linux"):
        return False
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                or str(os.environ.get("XDG_SESSION_TYPE", "")).lower() in ("x11", "wayland"))


def default_display() -> str:
    """What `display: ""` means on this computer: the screen inside the panel.

    Athena shows a guest's screen in the panel, so that is what a machine gets unless its
    owner says otherwise. A window of its own is a thing somebody asks for, not a thing
    that happens to them.
    """
    return "panel"


def boot_preference(spec: "MachineSpec") -> str:
    """Which of the two a machine with a medium attached starts on: `system` or `medium`.

    The default is the disk first and the medium second, and the reason is a person, not a
    principle: an install that has just finished says "reboot now", and a machine that boots
    its own installer again at that moment looks like the install was lost. `-boot order=c,d`
    asks the firmware for the disk and falls through to the CD-ROM when there is nothing
    bootable on it (`order=cd`), so an empty machine still lands in the installer and a finished
    one starts its own system. `medium` forces the CD-ROM first (`order=dc`), which is what a
    live or rescue image wants every time.
    """
    return "medium" if str(getattr(spec, "boot", "") or "").strip().lower() == "medium" else "system"


def disk_written_bytes(folder: Path) -> int:
    """How much of this machine's disk file exists. A qcow2 grows as it is written to, so the
    file's own size is a reading of "something is on that disk" that needs no guest, no mount
    and no parsing — and 0 for a machine that has never started."""
    disk = Path(folder) / "disk.qcow2"
    try:
        return disk.stat().st_size if disk.is_file() else 0
    except OSError:
        return 0


def is_installed(spec: "MachineSpec", folder: Path) -> bool:
    """Whether this machine's disk holds an installed system rather than a booted medium.

    Derived from the file every time, never stored: a spec that said `installed: true` would
    still say it after the disk was deleted, and a fact that outlives its cause is worse than
    no fact. Only a QEMU machine with an ISO can be in this state at all — a machine that boots
    a disk image it was given has no installer to outgrow.
    """
    if str(getattr(spec, "provider", "")) != "qemu":
        return False
    if str(((spec.image or {}).get("kind")) or "") != "iso":
        return False
    return disk_written_bytes(folder) >= INSTALLED_BYTES


def attach_medium(spec: "MachineSpec", folder: Path) -> bool:
    """Whether this machine's ISO goes on the command line at all.

    An installed machine that nobody has asked to boot the medium does not get one: the whole
    point of the setting is that a finished install is a computer, not an installer that has
    one. A machine that is still empty keeps its medium, because that is how it gets an
    operating system in the first place; and `boot: "medium"` puts the medium back for a live
    or rescue image — an explicit ask is never overruled by a guess about a file's size.
    """
    if str(((spec.image or {}).get("kind")) or "") != "iso":
        return False
    if not (spec.image or {}).get("path"):
        return False
    if boot_preference(spec) == "medium":
        return True
    return not is_installed(spec, folder)


def boot_args(preference: str) -> List[str]:
    """The `-boot` arguments for a preference. One place, so the order is provable.

    The letters concatenate and the order is the order they are tried in: `cd` is the disk and
    then the CD-ROM, `dc` the other way round. Measured on a real QEMU 10.2 here, 2026-10-14:
    `-boot order=c,d` is a syntax error ("Expected '=' after parameter 'd'") — the comma
    separates parameters, not devices — so `order=cd` is the form that boots.
    """
    return ["-boot", "order=dc"] if str(preference) == "medium" else ["-boot", "order=cd"]


def resolve_display(spec: "MachineSpec") -> str:
    """The display this machine will really use, with `""` answered by this host."""
    return str(spec.display or "") or default_display()


# A guest's screen size is *not* a setting here, and that was measured rather than assumed: a
# machine started with `-global VGA.edid=on -global VGA.xres=1280 -global VGA.yres=800` came up at
# 640x384 and stayed there for the two minutes it was watched, where the same machine with nothing
# stated reached 1280x800 by itself in 25 seconds. QEMU's xres/yres are the *initial* VGA mode
# rather than a hint to the guest, so stating one is not a bigger screen, it is a different
# machine — sometimes one that does not get as far. What a guest is at is the guest's business and
# it is reported by the frame's own size; the panel follows it, which is in screen.py.


VNC_BASE = 5900


def vnc_tcp_port(spec: "MachineSpec") -> int:
    """The TCP port QEMU's VNC server listens on for this machine.

    The spec carries QEMU's own display *number*, and the port is always that number plus
    5900 — QEMU's rule, not Athena's, kept in one place so the panel, the console and the
    argv all mean the same screen when they say ":1".
    """
    return VNC_BASE + int(spec.vnc_port or 0)


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Whether anything is already listening, asked cheaply and locally."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex((host, int(port))) != 0
    except OSError:
        return False


def pick_vnc_display(taken: Iterable[int] = (), span: int = 16) -> int:
    """The first VNC display number this host can actually bind.

    Every machine that shows its screen in the panel needs its own, and QEMU's port for
    display N is 5900+N: two machines quietly sharing display :0 would mean one of them
    starting and the other's screen never appearing. Both kinds of collision are checked —
    a number another machine already holds, and a port something else on this computer
    happens to be using.
    """
    used = set()
    for value in taken or ():
        try:
            used.add(int(value))
        except (TypeError, ValueError):
            continue
    for n in range(0, max(1, int(span))):
        if n in used:
            continue
        if _port_free(VNC_BASE + n):
            return n
    return 0


def qemu_cpu_args(accel: str) -> List[str]:
    """The `-cpu` this host and accelerator can actually execute, not the fastest one on paper.

    `-cpu host` hands the guest the host's own CPU model, which is the right answer under
    KVM on Linux. Under WHPX on this Windows build it is not: the vCPU never advances, and
    a live QEMU with a guest frozen at its reset vector reports the "guest has not
    initialized the display (yet)" placeholder forever. Measured here, 2026-10-14, one
    variable at a time:

        -accel whpx -cpu host    → 640x480 placeholder, "WHPX: Unexpected VP exit code 4"
        -accel whpx -cpu max     → same
        -accel whpx              → boots (SeaBIOS 720x400 within seconds)
        -accel whpx -cpu qemu64  → boots
        -accel tcg -cpu max      → boots

    So WHPX gets no `-cpu` at all and QEMU picks its own model — still accelerated, which
    matters because the alternative is emulation on one core.

    (Kept as a function of the accelerator alone: what changed the answer was the
    combination, and this is the only place the combination is decided.)
    """
    if accel == "kvm":
        return ["-cpu", "host"]
    if accel == "tcg":
        return ["-cpu", "max"]
    return []


def qemu_reach(spec: "MachineSpec") -> str:
    """Where the guest's screen and services are, as one sentence for a card.

    Says where the screen is, because "is it open, and where" is the first question
    somebody asks after pressing Start. The panel is the answer by default now, so the
    sentence names the panel first and only mentions a VNC address for a machine that
    runs one.
    """
    display = resolve_display(spec)
    ports = []
    ssh_port = int((spec.ssh or {}).get("port") or 0)
    if ssh_port:
        ports.append(f"ssh 127.0.0.1:{ssh_port}")
    for entry in (spec.ports or []):
        try:
            host, guest = int(entry.get("host") or 0), int(entry.get("guest") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if host and guest:
            ports.append(f"127.0.0.1:{host} \u2192 {guest}")
    if display == "window":
        head = "Its screen is a window of its own, opened when it starts"
    elif display == "none":
        head = "No screen: this one runs headless"
    else:
        head = "Its screen is here in the panel, live while it runs"
    return head + (f"; reachable at {', '.join(ports)}" if ports else
                   "; nothing in it is reachable from here yet")


def vnc_socket(folder: Path) -> Path:
    """Where a machine's VNC socket lives — inside the machine's own folder.

    In the folder rather than in a shared temp directory so that it is created, and
    vanishes, with the machine it belongs to: a socket left behind by a crash is a
    socket `vnc_bind` would otherwise have to be careful about reusing.
    """
    return Path(folder) / "vnc.sock"


# The mode the socket is given the moment QEMU has made it. 0600, not 0660: the guest
# display belongs to the person who started the machine, and there is no group of
# people who should be able to watch it.
VNC_SOCKET_MODE = 0o600


def unix_sockets_available() -> bool:
    """Can *this process* connect to a unix socket? Asked once, at the point of use.

    A feature test, not a platform test, and the difference is not academic. QEMU accepts
    `-vnc unix:` on Windows and creates the socket, and that is what got measured before
    this shipped — the giving-away half. The taking-away half is Python, and on this box
    `socket.AF_UNIX` does not exist at all (`uv`-managed CPython 3.11.15 on Windows), so
    the guest came up with a socket nobody could dial and the panel showed an empty screen:
    a failure that looks like the guest is broken and is really the reader.

    So neither spelling is assumed. `hasattr` on the interpreter that is actually running
    decides, which means Windows keeps the loopback port — the honest trade, and the
    weakness the threat model already names — while Linux and macOS get the socket.
    """
    return hasattr(socket, "AF_UNIX")


def vnc_bind(folder: Path, spec: "MachineSpec", unix: Optional[bool] = None) -> List[str]:
    """The `-vnc` arguments for this machine: a socket, or a loopback port.

    This used to be `127.0.0.1:<port>` with a comment saying "loopback only — a guest
    display is not for the LAN". That is true and beside the point. The thing a loopback
    port is reachable by is not the LAN, it is **every other program running as this
    user**, and a guest's screen is a live feed of whatever that guest is doing —
    passwords typed into it included. A unix socket is a file, and a file can be given a
    mode; that is the whole difference.

    Where the socket can be dialled, it is what gets used. Where it cannot — Windows, as
    measured — the port is kept, because a screen that cannot be read is not a more
    secure screen, it is a broken one. `unix` is left overridable so the tests can pin
    both shapes; no caller passes it.
    """
    if unix is None:
        unix = unix_sockets_available()
    if unix:
        return ["-vnc", _unix_host(vnc_socket(folder))]
    return ["-vnc", f"127.0.0.1:{int(spec.vnc_port or 0)}"]


def tighten_vnc_socket(folder: Path, tries: int = 8, pause: float = 0.25) -> bool:
    """Give the VNC socket its mode, once QEMU has made it.

    QEMU creates the file, so it arrives with whatever the platform's default says —
    measured `-rw-r--r--` on this machine, which is world-readable and is precisely
    what moving off a loopback port was meant to stop. There is a gap between the
    process starting and the socket existing, hence the retry: a mode set on a file
    that is not there yet does nothing at all, silently, and the socket stays readable
    by every account on the box while looking like it was handled.

    Best-effort, and it says so by returning a bool nobody is required to check. On
    Windows a mode is advisory and the ACL is what actually decides access, so this is
    the POSIX half of the guarantee, not the whole of it. A failure must not fail a
    start: a guest running with a slightly generous socket beats a guest that did not
    start, and this is called on the path where a person just pressed Start.
    """
    sock = vnc_socket(folder)
    for _ in range(max(1, tries)):
        try:
            os.chmod(sock, VNC_SOCKET_MODE)
            return True
        except FileNotFoundError:
            time.sleep(pause)
        except OSError:
            return False
    return False


def vnc_endpoint(folder: Path, spec: "MachineSpec") -> str:
    """Where to dial this machine's screen: its socket if it has one, its address if not.

    Asked of the filesystem rather than of a flag or a version number, because both
    kinds of machine can exist on one computer at once: a guest started before the
    socket change is listening on a loopback port and has no socket file, and one
    started after it is the other way round. The socket existing is the fact that
    settles which, and a stored answer would be the one that goes stale.

    The socket only wins if it can also be *opened*. A socket file left in the folder by
    a guest that QEMU started with `-vnc unix:` on a machine whose Python cannot dial one
    is a socket that exists and leads nowhere — and answering with it turns a readable
    screen into an empty one, so the availability test is part of the question rather
    than a detail of the caller.
    """
    sock = vnc_socket(folder)
    if not unix_sockets_available():
        return "127.0.0.1"
    try:
        if sock.exists():
            return _unix_host(sock)
    except OSError:  # noqa: PERF203 — an unreadable folder is not a reason to guess
        pass
    return "127.0.0.1"


def _unix_host(sock: Path) -> str:
    """The `unix:`-prefixed name `screen._open` understands, spelled once."""
    return "unix:" + _qemu_path(Path(sock))


def qemu_argv(spec: "MachineSpec", folder: Path) -> List[str]:
    """The full command line for one machine. Pure: nothing runs, nothing is touched.

    `folder` is the machine's own directory, where its disk, console log and pidfile
    live. Every argument that a person might need to change is read from the spec, so
    the same spec always produces the same machine.
    """
    exe = qemu_exe() or "qemu-system-x86_64"
    image = dict(spec.image or {})
    kind = str(image.get("kind") or "")
    disk = Path(folder) / "disk.qcow2"

    # A Windows guest gets IDE disks and an e1000 NIC, and this is not a detail: an
    # installer has no virtio driver, so a Windows ISO shown a virtio disk sees a
    # machine with no hard drive and refuses to install. Linux guests get virtio,
    # which is dramatically faster. The split is decided from `os` so that "a Windows
    # VM" is a field somebody sets rather than a combination they have to know.
    windows = str(spec.os or "").lower().startswith("win")
    bus = "ide" if windows else "virtio"
    nic = "e1000" if windows else "virtio-net-pci"

    accel = qemu_accel()
    argv = [exe, "-name", f"athena-{safe_name(spec.name)}"]
    argv += ["-machine", "q35", "-accel", accel]
    argv += qemu_cpu_args(accel)
    argv += ["-m", str(int(spec.memory_mb)), "-smp", str(int(spec.cpus))]

    # The disk. Every machine gets one, because an installer with nowhere to install
    # to is a demo and not a machine — except the case where the machine IS somebody
    # else's disk image, which then boots read-write as itself.
    own_disk = not (kind == "disk" and image.get("path"))
    if not own_disk:
        # Somebody else's image, booted read-write as itself: the format is the file's own.
        argv += ["-drive", "file=" + _qemu_path(image["path"]) + f",if={bus}"
                 + _format_arg(image["path"])]
    elif disk.is_file():
        # Our own disk is always the qcow2 `qemu_create_disk` makes, and it asks the same
        # question rather than asserting it: one place knows what a file is.
        argv += ["-drive", "file=" + _qemu_path(disk) + f",if={bus}"
                 + _format_arg(disk)]

    # An ISO is a CD-ROM, and where the machine starts is `boot_preference()`: the disk
    # first with the medium as a fallback, so a machine with nothing installed still lands
    # in the installer and a machine with an installed system boots it. Booting the medium
    # first is what makes a finished install look lost ("reboot now" — into the installer),
    # so it is the non-default that has to be asked for.
    if kind == "iso":
        # `path`, never `url`: by the time a command line exists the image has been
        # fetched, and handing QEMU an https:// URL would produce a guest that boots
        # nothing at all — the kind of failure that reads as "QEMU is broken".
        src = image.get("path") or ""
        if src and attach_medium(spec, disk.parent):
            argv += ["-cdrom", _qemu_path(src)]
            argv += boot_args(boot_preference(spec))

    # User-mode networking with one forwarded port per declared port. This is the
    # only way in from the host: a QEMU guest cannot see 127.0.0.1 of this computer.
    fwd = [f"hostfwd=tcp:127.0.0.1:{host}-:{guest}"
           for host, guest in forwarded_ports(spec)]
    argv += ["-netdev", "user,id=net0" + ("," + ",".join(fwd) if fwd else ""),
             "-device", f"{nic},netdev=net0"]

    # Input: an absolute pointing device, not a PS/2 mouse. VNC sends the pointer's
    # position, and without a tablet QEMU has to turn that into relative motion for an
    # emulated PS/2 mouse — which only works when the guest draws a cursor the emulation can
    # push around. A Wayland guest draws none, so the pointer arrives and never moves, which
    # is exactly what was reported from omarchy. `usb-tablet` is the device every remote
    # console uses for this; VirtualBox spells the same thought `--mouse usbtablet`. The
    # keyboard needs no equivalent: the emulated PS/2 keyboard was never the problem.
    #
    # `-usb` is not optional and is not decoration: a tablet hangs off a USB bus, and this
    # machine type has no USB controller until it is asked for one. Without it QEMU refuses
    # the command line outright — "-device usb-tablet: No 'usb-bus' bus found for device
    # 'usb-tablet'" — and exits before booting anything. So a check that only reads the
    # command line passes happily while QEMU would refuse to start the machine at all. The
    # string only says what was asked for.
    argv += ["-usb", "-device", "usb-tablet"]

    # A guest's screen size is the guest's to choose, but it takes its idea of what is available
    # from the video device's EDID, and a Linux guest reads `virtio-vga`'s. Asking for 1920x1080
    # there is what makes a guest come up at a real desktop size instead of the 1280x800 the
    # default std VGA leads it to. Measured, not assumed (`cache/scratch/res_probe.py` boots
    # omarchy's own ISO on a throwaway disk and reads the size back out of the VNC handshake, so
    # it never touches a machine anyone is using):
    #
    #   default std VGA ............................ 1280x800 by 39s
    #   -global VGA.xres=1920/yres=1080 (reverted) .. 640x384, and stuck there for two minutes
    #   -vga none -device virtio-vga,xres/yres ...... 1920x1080 by 37s
    #
    # The reverted knob was the *other* mechanism: `VGA.xres` is an initialisation hint to the
    # emulated std VGA, which is not a mode a Wayland guest takes. The device's own EDID is a mode
    # it does. It is still only a hint — a guest that wants another mode sets one and keeps it,
    # which is why this is a default and not a setting. `-vga none` is not optional: without it
    # QEMU adds its own std VGA alongside and the guest is offered two cards, only one of which
    # carries this hint.
    #
    # And it is Linux-only for the same reason the disks above are `if=ide` for Windows: a
    # Windows installer has no virtio-gpu driver either, so asking for one is asking for a blank
    # screen. Windows picks its own size, which is the correct answer for a guest that cannot be
    # told.
    if not windows:
        argv += ["-vga", "none", "-device", "virtio-vga,xres=1920,yres=1080"]

    # Display: `window` is a real QEMU window (what installing from an ISO used to
    # need), `none` is headless, `panel` is headless plus the VNC server the console
    # attaches to. `vnc_bind` decides what that server listens on — a socket with a
    # mode rather than a loopback port, which is the difference between "not on the
    # LAN" and "not readable by every other program you are running".
    display = resolve_display(spec)
    if display == "window":
        # SDL, not GTK: it is what a Windows QEMU is built with, and it falls back to
        # the platform's own window handling rather than needing a GTK runtime.
        argv += ["-display", "sdl"]
    else:
        argv += ["-display", "none"]
        if display == "panel":
            argv += vnc_bind(folder, spec)

    # The console log is a FILE, never a pipe: a pipe with no reader fills up and the
    # guest stalls, which is indistinguishable from a guest that hung.
    argv += ["-serial", f"file:{_qemu_path(Path(folder) / 'console.log')}"]
    return argv


def qemu_pid_file(folder: Path) -> Path:
    return Path(folder) / "qemu.pid"


def console_path(folder: Path) -> Path:
    """The guest's serial output. QEMU is given this file as a chardev, so it is a
    resource two QEMUs cannot share at once."""
    return Path(folder) / "console.log"


def wait_for_console(folder: Path, timeout: float = 5.0) -> bool:
    """Wait until the guest's console file can be opened again, and say whether it could.

    QEMU's serial chardev takes that file in a way that a second open fails, and a failed
    open is *fatal*: `could not connect serial device to character backend`, then QEMU
    exits before the guest exists. A stop kills the process, but Windows releases the
    handle a moment later — so a restart that begins the instant after a stop died on its
    first try. Measured 2026-10-14 on a real machine: moving a screen out to a window died
    this way, and the headless fallback boot 1.5 s later succeeded with the same argv's
    console line — the race, not the display and not the ISO. The open below is the same
    resource QEMU needs, so waiting on it is waiting on the real thing rather than a nap.
    """
    path = console_path(folder)
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        try:
            with open(path, "ab"):
                pass
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.2)


def console_was_held(why: str) -> bool:
    """Is this QEMU's own complaint that the console file was still held?

    A pure reading of one string, because it decides whether a second attempt is honest:
    this one is worth retrying (the holder is a process that just died), and a bad
    argument or a missing image is not — retrying those just makes the panel take longer to
    say the same true thing.
    """
    text = str(why or "").lower()
    return "could not connect serial device" in text or "character backend" in text


def _pid_alive(pid: int) -> bool:
    """Liveness without spawning a process per poll (psutil is already a dependency)."""
    try:
        import psutil
        return bool(psutil.pid_exists(int(pid)))
    except Exception:  # noqa: BLE001 — a missing psutil must not read as "dead"
        pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                                 capture_output=True, text=True, timeout=20).stdout or ""
            return str(int(pid)) in out
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def qemu_running(folder: Path) -> bool:
    """A pidfile is a claim; a live process is the fact — and both are checked.

    A stale pidfile after a crash is the normal case, not the exception. A panel that
    believed the file would offer "Stop" for a machine that is already gone, and an
    agent would be told work was running when nothing was.
    """
    pf = qemu_pid_file(folder)
    if not pf.is_file():
        return False
    try:
        pid = int((pf.read_text(encoding="utf-8") or "0").strip() or 0)
    except (TypeError, ValueError):
        return False
    return pid > 0 and _pid_alive(pid)


def qemu_create_disk(spec: "MachineSpec", folder: Path) -> Dict[str, Any]:
    """Make the disk. The one thing that must exist before a guest can boot."""
    img = qemu_img_exe()
    if not img:
        return {"ok": False, "error": "qemu-img is not here — QEMU is only half installed"}
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    disk = folder / "disk.qcow2"
    if disk.is_file() and disk.stat().st_size > 0:
        return {"ok": True, "disk": str(disk), "created": False}
    res = subprocess.run([img, "create", "-f", "qcow2", str(disk), f"{int(spec.disk_gb)}G"],
                         capture_output=True, text=True, timeout=300)
    if res.returncode != 0 or not disk.is_file():
        return {"ok": False, "error": "could not create the disk: "
                                     + ((res.stderr or res.stdout or "").strip() or
                                        f"qemu-img exited {res.returncode}")}
    return {"ok": True, "disk": str(disk), "created": True}


def qemu_prepare_image(spec: "MachineSpec", folder: Path, fetch=None,
                       log=None) -> Dict[str, Any]:
    """Turn an image URL into a local file, once, in the machine's own folder.

    Fetched on first start rather than at create time: a machine can be described
    before the network is willing, and a 2 GB ISO that somebody merely *named* should
    not start downloading because they typed a name. `fetch(url, dest)` is supplied by
    the caller, because progress reporting belongs to whoever owns the job — this
    function owns only the decision of what to fetch and when to reuse it.

    It also owns the two rules about *which* URL and *what name*, because this is the only
    place a machine image is fetched: https only (`image_url_problem`), and the local file
    named from the URL rather than by it (`image_filename`), inside `<folder>/image/` and
    never outside it.
    """
    image = dict(spec.image or {})
    url = str(image.get("url") or "")
    if not url:
        return {"ok": True, "path": str(image.get("path") or ""), "fetched": False}
    problem = image_url_problem(url)
    if problem:
        return {"ok": False, "error": problem, "url": url}
    root = Path(folder) / "image"
    local = root / image_filename(url)
    if root.resolve() not in local.resolve().parents:
        return {"ok": False, "error": f"the name this image URL implies would not land "
                                      f"inside {root}", "url": url}
    if local.is_file() and local.stat().st_size > 0:
        if log:
            log(f"image already here ({local.stat().st_size // (1 << 20)} MB), not fetching it twice")
        spec.image = dict(image, path=str(local))
        return {"ok": True, "path": str(local), "fetched": False}
    if fetch is None:
        return {"ok": False, "error": "this machine points at an image by URL and the caller "
                                     "gave nothing to fetch it with"}
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        fetch(url, local)
    except Exception as e:  # noqa: BLE001 — a failed download is a sentence, not a traceback
        return {"ok": False, "error": f"could not fetch the image: {e}"}
    if not local.is_file() or local.stat().st_size == 0:
        return {"ok": False, "error": f"the download left nothing at {local}"}
    # The spec keeps its URL and gains the path: a machine rebuilt from this spec on a
    # machine with no network still finds the image it already has.
    spec.image = dict(image, path=str(local))
    return {"ok": True, "path": str(local), "fetched": True}


def _port_hint(spec: "MachineSpec", why: str) -> str:
    """Why the machine did not start, with a taken port named if that is what it was."""
    text = why or "qemu exited on start"
    if re.search(r"host forwarding|address already in use|Could not set up", text, re.I):
        return text                      # QEMU already said it; do not say it twice
    busy = busy_forwarded_port(spec)
    if busy is None:
        return text
    return (text + f" — and port {busy} is already in use on this computer, which is where "
                   f"this machine's forwarded port would land. Close whatever is listening "
                   f"on {busy}, or change the port in the machine's settings.")


def forwarded_ports(spec: "MachineSpec") -> List[Tuple[int, int]]:
    """Every (host, guest) port QEMU is asked to forward, in the order they are declared.

    One reader for this list, because two things need it and they must not disagree: the
    command line that opens the forwards, and the diagnosis below that has to name a port when
    the guest will not start. A port that is declared but never forwarded is a machine that
    answers nothing, and a port checked but not forwarded is a wrong answer about a machine
    that is fine.
    """
    pairs: List[Tuple[int, int]] = []
    ssh_port = int((spec.ssh or {}).get("port") or 0)
    if ssh_port:
        pairs.append((ssh_port, 22))
    for extra in (spec.ports or []):
        try:
            host, guest = int(extra.get("host") or 0), int(extra.get("guest") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if host and guest:
            pairs.append((host, guest))
    return pairs


def busy_forwarded_port(spec: "MachineSpec") -> Optional[int]:
    """The first declared host port something else already holds, or None.

    QEMU's own answer to a taken port is a line in its log and an immediate exit: `Could not
    set up host forwarding rule`. That is a true sentence and a useless one -- it names neither
    the port nor the program, and on a first boot it reads as "this machine will not start".
    """
    for host, _guest in forwarded_ports(spec):
        if not _port_free(host):
            return host
    return None


def qemu_start(spec: "MachineSpec", folder: Path, log=None) -> Dict[str, Any]:
    """Boot the machine, detached, and say whether it is actually still up.

    The last part matters. QEMU exits immediately on a bad argument list and that is
    the common case on a first boot, so "started" is claimed only after a short wait
    proves the process is still alive — with the tail of its own console log as the
    explanation when it is not.
    """
    exe = qemu_exe()
    if not exe:
        return {"ok": False, "needs_setup": qemu_needs_setup()}
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if qemu_running(folder):
        return {"ok": True, "already": True, "state": "running"}

    got = qemu_prepare_image(spec, folder, fetch=(lambda u, d: _span_fetch(u, d, log)), log=log)
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error") or "the image could not be prepared"}

    if not (dict(spec.image or {}).get("kind") == "disk" and dict(spec.image or {}).get("path")):
        made = qemu_create_disk(spec, folder)
        if not made.get("ok"):
            return {"ok": False, "error": made.get("error")}

    # QEMU's own talk goes to a file of its own. It must NOT be the console log: the
    # chardev below opens that same path for the guest's serial output, and a second
    # write handle on a file the child already holds makes the open fail on Windows --
    # `could not connect serial device to character backend` -- and QEMU exits before the
    # guest exists, which reads as a machine that will not start at all. Measured, both
    # orders: one file for both = dead on start; separate files = boots.
    qlog = folder / "qemu.log"

    def boot(candidate: "MachineSpec"):
        argv = qemu_argv(candidate, folder)
        if log:
            log(qemu_describe(candidate, folder))
        why = ""
        # Twice at most, and only for one reason: a restart arrives here moments after a
        # stop killed the previous guest, whose handle on the console file is still being
        # released. Anything else QEMU says is the real answer the first time.
        for attempt in (1, 2):
            if not wait_for_console(folder, 5.0) and log:
                log("the console file is still held — starting anyway and reading what QEMU says")
            try:
                with open(qlog, "ab", buffering=0) as out:
                    proc = subprocess.Popen(
                        argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        cwd=str(folder),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        if sys.platform == "win32" else 0,
                        start_new_session=sys.platform != "win32")
            except Exception as e:  # noqa: BLE001 — report it instead of raising at a click
                return None, f"qemu would not start: {e}"
            qemu_pid_file(folder).write_text(str(proc.pid), encoding="utf-8")
            # Only a panel display has a VNC socket to tighten, and only where the socket
            # spelling was actually used — asking otherwise spends the retry loop waiting
            # for a file this machine was never going to make, on the path where somebody
            # is standing there waiting for a screen.
            if resolve_display(candidate) == "panel" and unix_sockets_available():
                tighten_vnc_socket(folder)
            time.sleep(1.5)
            if proc.poll() is None:
                return proc, ""
            why = _qemu_said(folder)
            if attempt == 1 and console_was_held(why):
                if log:
                    log("the console file was still held by the machine that just stopped — trying once more")
                time.sleep(1.2)
                continue
            break
        qemu_pid_file(folder).unlink(missing_ok=True)
        return None, ("qemu exited on start" + (f" — {why}" if why else ""))

    proc, why = boot(spec)

    # A window is the nicest way to watch an install and the first thing to go wrong:
    # a QEMU built without SDL, a Windows session over RDP, a bare X server with no
    # display to open on — all exit immediately, and every one of them would otherwise
    # read as "this machine will not start". So a machine that asked for a window and
    # died gets one more try headless with VNC, and says so.
    fell_back = ""
    if proc is None and resolve_display(spec) == "window":
        try:
            plain = replace(spec, display="panel")
        except Exception:  # noqa: BLE001 — a spec that cannot be copied keeps its first answer
            plain = None
        if plain is not None:
            if log:
                log("no window could be opened — starting it headless with VNC instead")
            proc, why2 = boot(plain)
            if proc is None:
                why = why2 or why
            else:
                fell_back = ("a window could not be opened on this computer, so the "
                             "machine is running headless on VNC instead")

    if proc is None:
        qemu_pid_file(folder).unlink(missing_ok=True)
        return {"ok": False, "state": "stopped",
                "error": _port_hint(spec, why)}

    out = {"ok": True, "state": "running", "pid": proc.pid,
           "display": resolve_display(spec) if not fell_back else "panel",
           "report": qemu_describe(spec, folder), "reach": qemu_reach(spec)}
    if fell_back:
        out["display_fallback"] = fell_back
        out["reach"] = qemu_reach(replace(spec, display="panel"))
    return out


def _qemu_log_tail(folder: Path, limit: int = 4000) -> Dict[str, Any]:
    """What QEMU itself said — its own stderr, in a file of its own.

    Kept apart from the guest's console on purpose: it must be, or QEMU cannot start (see
    the note in `qemu_start`). It is where an argument error, a missing accelerator or a
    crash lands, so it is also what explains a start that did not happen.
    """
    folder = Path(folder)
    path = folder / "qemu.log"
    if not path.is_file():
        return {"exists": False, "path": str(path), "text": ""}
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            raw = fh.read()
    except OSError as e:
        return {"exists": True, "path": str(path), "text": "", "error": str(e)}
    text = raw.decode("utf-8", errors="replace")
    if size > limit and "\n" in text:
        text = text.split("\n", 1)[1]
    return {"exists": True, "path": str(path), "text": text, "bytes": size}


def _qemu_said(folder: Path) -> str:
    """One line naming why QEMU did not stay up, for the note a person reads.

    QEMU's own words first: the error is the last line it printed, and it already names
    the argument and the reason. The guest's console is only a fallback, for the case
    where the failure happened after the guest had said something.
    """
    for src in (_qemu_log_tail(folder, limit=2000), console_tail(folder, limit=2000)):
        lines = [ln.strip() for ln in str(src.get("text") or "").splitlines() if ln.strip()]
        if lines:
            return lines[-1][:400]
    return ""


def console_tail(folder: Path, limit: int = 8000) -> Dict[str, Any]:
    """The tail of a machine's console log — what the guest has said so far.

    A QEMU guest's serial output is the only thing that speaks before the OS is up:
    the boot loader, the installer's first screen, a kernel panic. It is read from the
    END of the file, because the part that matters is the part that just happened, and
    the whole log of an install is megabytes.

    QEMU's *own* output travels alongside as `errors` — not folded into the text, because
    "the guest said this" and "QEMU said this" are different claims and the second one is
    what explains a machine that never got as far as speaking.
    """
    folder = Path(folder)
    path = folder / "console.log"
    errors = _qemu_log_tail(folder, limit=min(int(limit or 8000), 8000))
    if not path.is_file():
        return {"ok": True, "exists": False, "path": str(path), "text": "",
                "bytes": 0, "running": qemu_running(folder), "errors": errors.get("text") or ""}
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            raw = fh.read()
    except OSError as e:
        return {"ok": False, "error": f"the console log could not be read: {e}",
                "path": str(path), "text": "", "bytes": 0, "running": qemu_running(folder),
                "errors": errors.get("text") or ""}
    # The tail can start mid-line and mid-character; both are cosmetic here, and both
    # are cheaper than reading megabytes to throw almost all of it away.
    text = raw.decode("utf-8", errors="replace")
    if size > limit and "\n" in text:
        # Only when the read really was a tail: the first line of a partial read starts
        # in the middle of a line, and half a line at the top of a console is noise.
        text = text.split("\n", 1)[1]
    return {"ok": True, "exists": True, "path": str(path), "text": text,
            "bytes": size, "shown": len(raw), "running": qemu_running(folder),
            "errors": errors.get("text") or ""}


def _span_fetch(url: str, dest: Path, log=None) -> None:
    """Default image fetch for callers that have no job to report into.

    One downloader in this file: `download_with_progress` owns the bytes, the `.part`
    rename and the hash check; this only turns its reporting into log lines. It raises
    on failure, because its caller is a machine being provisioned — a machine that
    quietly has no disk is worse than one that stopped with a reason.
    """
    def _say(done: int, total: int) -> None:
        if log and total:
            log(f"  downloaded {done // (1 << 20)} MB of {total // (1 << 20)} MB")

    res = download_with_progress(url, dest, on_progress=_say)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "the image did not arrive")


def qemu_pids_for(folder: Path) -> List[int]:
    """Every live QEMU whose command line names this machine's folder.

    The pidfile is what `qemu_start` wrote; this is what is *true*. They disagree in the
    two cases that matter — a pidfile deleted by a half-finished stop while the process
    is still up, and a crash that left the file behind — and a stop has to be able to
    answer "is this machine still running?" from something other than its own record.

    The match is on the folder path, which appears in the argv of every guest this file
    starts (`cwd` plus the disk, console and pid paths all live in it). Narrow on
    purpose: a QEMU belonging to somebody else must never be killed by name.
    """
    want = str(Path(folder).resolve()).lower()
    out: List[int] = []
    try:
        import psutil
    except Exception:  # noqa: BLE001 — without psutil there is nothing to scan
        return out
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            argv = proc.info.get("cmdline") or []
        except Exception:  # noqa: BLE001 — a process that vanished mid-scan
            continue
        if not argv:
            continue
        blob = " ".join(str(a) for a in argv).lower()
        if "qemu-system" in blob and want in blob:
            out.append(int(proc.info.get("pid") or 0))
    return [pid for pid in out if pid > 0]


def qemu_stop(folder: Path) -> Dict[str, Any]:
    """Stop the guest, and say whether it stopped.

    `ok` means *stopped* — the state the caller asked for — not "the request was
    accepted". A machine that was already stopped answers ok, because it is in that
    state; a machine that is still holding its disk answers `ok: False` with
    `state: "stopping"` and keeps its pidfile, so the next stop still has something to
    work with and nothing downstream mistakes it for a disk that is at rest.

    That last part is the whole reason this is not a one-line kill: the pidfile is
    removed only once the process is gone, and "gone" is asked of the process table, not
    of the kill that was just issued. A snapshot, a disk read or a restart taken on the
    word of an unverified stop is how a live guest's disk ends up being copied.
    """
    folder = Path(folder)
    pf = qemu_pid_file(folder)
    pid, how = 0, ""
    if pf.is_file():
        try:
            pid = int((pf.read_text(encoding="utf-8") or "0").strip() or 0)
        except (TypeError, ValueError):
            pid = 0
    found = qemu_pids_for(folder)
    # `found` first: a machine whose pidfile went missing while its guest is still up is
    # running, and this must not answer "stopped" because the *file* says nothing.
    if not found and (pid <= 0 or not _pid_alive(pid)):
        # No usable pidfile and nothing in the process table: it is not running.
        if pf.is_file() and pid <= 0:
            how = "the pidfile was unusable, and no guest of this machine was running"
        else:
            how = "it was not running"
        if pf.is_file() and not found:
            pf.unlink(missing_ok=True)
        return {"ok": True, "state": "stopped", "how": how}
    if pid <= 0 or not _pid_alive(pid):
        # The record was wrong and the process table says otherwise: stop the process we
        # can actually see rather than trusting the file.
        pid = found[0]
        how = "the pidfile did not name it, so it was found by its own command line"
    elif not how:
        how = "terminated"
    if how == "terminated":
        if sys.platform == "win32":
            res = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                 capture_output=True, text=True, timeout=60)
            if res.returncode != 0 and _pid_alive(pid):
                how = "taskkill refused it"
        else:
            try:
                os.kill(pid, 15)
                for _ in range(20):
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.25)
                if _pid_alive(pid):
                    os.kill(pid, 9)
                    how = "killed after it would not stop on its own"
            except OSError:
                pass
    # Wait for it to actually be gone before saying it is stopped. A stop is almost always
    # followed by a start — that is what moving a screen does — and returning while the
    # dying process still holds its files is what made those restarts fail on the first
    # try. Bounded, and reported as prose when it had to be waited for.
    for _ in range(25):
        if not _pid_alive(pid) and not qemu_pids_for(folder):
            break
        time.sleep(0.2)
    else:
        how += " (it took a moment to let go)"
    still = [p for p in qemu_pids_for(folder)] or ([pid] if _pid_alive(pid) else [])
    if still:
        # Not stopped, and the file that says how to stop it stays where it is.
        return {"ok": False, "state": "stopping", "pid": still[0],
                "how": how or "asked to terminate",
                "error": f"the guest is still running (pid {still[0]}) after waiting "
                         f"{25 * 0.2:.0f}s — its disk is being written to, so nothing here "
                         f"may be copied or started over it. Try stopping it again."}
    pf.unlink(missing_ok=True)
    return {"ok": True, "state": "stopped", "how": how}

def qemu_snapshot(spec: "MachineSpec", folder: Path, exports: Path, log=None) -> Dict[str, Any]:
    """A stopped disk, copied whole. Refused while the guest is up — and that refusal
    is the feature: a copy of a file being written is a coin toss that looks like a
    backup, and it is discovered when it is restored, which is the worst moment.
    """
    folder, exports = Path(folder), Path(exports)
    if qemu_running(folder):
        return {"ok": False, "error": "stop the machine first — its disk is being written to "
                                     "right now, and a copy of that is not a snapshot"}
    disk = folder / "disk.qcow2"
    if not disk.is_file():
        return {"ok": False, "error": "this machine has no disk yet — nothing to snapshot"}
    exports.mkdir(parents=True, exist_ok=True)
    dest = exports / f"{safe_name(spec.name)}-{time.strftime('%Y%m%d-%H%M%S')}.qcow2"
    if log:
        log(f"copying {disk.stat().st_size // (1 << 20)} MB")
    shutil.copy2(disk, dest)
    return {"ok": True, "export": str(dest), "bytes": dest.stat().st_size}


def qemu_describe(spec: "MachineSpec", folder: Path = Path(".")) -> str:
    """One line about what is booting, read from the spec and the host.

    Never from the guest: a guest that has not finished booting cannot answer, and the
    silence would be reported as an empty machine.
    """
    image = dict(spec.image or {})
    kind = str(image.get("kind") or "disk")
    src = str(image.get("path") or image.get("url") or "")
    what = Path(src).name if src else "no image"
    return (f"qemu {qemu_accel()} · {kind} {what} · {spec.cpus} cores · "
            f"{spec.memory_mb} MB · {resolve_display(spec)} display")



# --------------------------------------------------------------------------- setup

# How QEMU is asked about, and where its installer is fetched from. `winget show`
# hands over the direct URL and the hash, which is what makes a real progress bar
# possible: see `qemu_manifest`.
QEMU_WINGET_ID = "SoftwareFreedomConservancy.QEMU"


def parse_installer_fields(text: str) -> Dict[str, Any]:
    """The installer URL, hash and version out of `winget show` output. Pure.

    `winget show` prints `  Installer Url: https://…` inside its `Installer:` block,
    one `key: value` per line. Nothing here talks to the network, so the rule that
    reads it can be tested against a captured listing instead of against winget.
    """
    out: Dict[str, Any] = {}
    for line in str(text or "").splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        k = key.strip().lower()
        v = value.strip()
        if not v:
            continue
        if k == "installer url":
            out["url"] = v
        elif k == "installer sha256":
            out["sha256"] = v.lower()
        elif k == "installer type":
            out["type"] = v
        elif k == "version" and "version" not in out:
            out["version"] = v
    if out.get("url"):
        out["file"] = out["url"].rstrip("/").split("/")[-1]
    out["ok"] = bool(out.get("url"))
    return out


def qemu_manifest(timeout: int = 60) -> Dict[str, Any]:
    """What winget knows about QEMU's installer: URL, hash, version.

    Why this exists: `winget install` prints **nothing measurable** when its output is
    a pipe — verified on winget 1.29, where a piped download prints five lines and no
    percentage or byte count. So an install driven that way can say what it is doing
    but never how far along it is, and a progress bar fed from it would be an
    animation pretending to be information. `winget show`, by contrast, hands over the
    installer's own URL, which `download_with_progress` can fetch with a known total.

    A failure here is not fatal — `install_plan` falls back to winget as the
    installer, which still works, just without a bar.
    """
    if sys.platform != "win32":
        return {"ok": False, "error": "winget is Windows' package manager"}
    try:
        proc = subprocess.run(["winget", "show", "-e", "--id", QEMU_WINGET_ID],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not ask winget about QEMU: {e}"}
    man = parse_installer_fields((proc.stdout or "") + "\n" + (proc.stderr or ""))
    if not man.get("ok"):
        man.setdefault("error", "winget did not name an installer URL for QEMU")
    return man


def download_with_progress(url: str, dest: Path, on_progress: Any = None,
                           expect_sha256: str = "", timeout: int = 60,
                           chunk: int = 262144) -> Dict[str, Any]:
    """Fetch a file, reporting bytes as they land, and check the hash it came with.

    Written as a download *we* can see, rather than handing the URL to an installer
    that downloads silently: the point is a progress bar that means something.

    Rules kept here: the bytes land in `dest.part` and are renamed only once the
    whole file is there (a half-downloaded installer must never look complete); the
    hash, when one was given, is verified **before** the rename, and a mismatch is a
    refusal rather than a warning; and progress is reported from real bytes, never
    from a timer, so a stalled download looks stalled.
    """
    from urllib.request import Request, urlopen   # local: the module imports it lazily too

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dest) + ".part")
    digest = hashlib.sha256()
    done = 0
    total = 0
    started = time.time()
    last = 0.0
    try:
        req = Request(url, headers={"User-Agent": "athena"})
        with urlopen(req, timeout=timeout) as r, open(part, "wb") as fh:
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            if on_progress is not None:
                on_progress(0, total)
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                digest.update(buf)
                done += len(buf)
                now = time.time()
                # Four updates a second is smoother than the eye needs and cheap
                # enough that a 200 MB file does not flood the job's progress.
                if on_progress is not None and (now - last >= 0.25):
                    last = now
                    on_progress(done, total)
    except Exception as e:  # noqa: BLE001 — every failure here is a report, not a crash
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"could not download {url}: {e}", "bytes": done,
                "total": total}
    got = digest.hexdigest()
    if expect_sha256 and got.lower() != str(expect_sha256).strip().lower():
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        return {"ok": False, "bytes": done, "total": total, "sha256": got,
                "error": f"the file that arrived is not the one that was published "
                         f"(expected {str(expect_sha256)[:16]}…, got {got[:16]}…) — deleted it "
                         f"rather than installing it"}
    try:
        os.replace(part, dest)
    except OSError as e:
        return {"ok": False, "error": f"could not put the download in place: {e}",
                "bytes": done, "total": total}
    if on_progress is not None:
        on_progress(done, total or done)
    return {"ok": True, "path": str(dest), "bytes": done, "total": total, "sha256": got,
            "seconds": round(time.time() - started, 1)}


def install_plan(what: str) -> Dict[str, Any]:
    """How a missing prerequisite gets installed — decided here, not in a click handler.

    Three different answers, and they are different on purpose:

      * `elevated` — Windows' own RunAs dialog does the asking. Athena starts
        `wsl.exe --install`; Windows asks for permission; the person answers. The
        elevation prompt is the consent, which is why this is not run silently.
      * `job` — a plain command that takes minutes rather than permissions. It runs
        with the rest of the long work, watchable.
      * `manual` — a command that needs `sudo` and a terminal. Nothing is run:
        command strings are shown for a person to run themselves, because a plugin
        with no way to talk to you must not pretend it installed something.

    The command text comes from the platform table above, never from a guess, so a
    Linux box is never told to run `wsl --install`.
    """
    what = str(what or "").strip().lower()
    if what == "wsl":
        if sys.platform != "win32":
            return {"ok": False, "error": "WSL is a Windows feature — there is nothing to "
                                          "install on this platform"}
        return {"ok": True, "what": "wsl", "command": "wsl --install --no-distribution",
                "method": "elevated",
                "note": "Windows will ask for permission in a moment. Approve it, reboot if "
                        "it says to, then start the machine again."}
    if what == "qemu":
        command = _INSTALL_COMMANDS.get(sys.platform, "")
        if not command:
            return {"ok": False, "error": f"no known way to install QEMU on {sys.platform}"}
        if sys.platform == "win32":
            man = qemu_manifest()
            if man.get("ok"):
                return {"ok": True, "what": "qemu", "command": command, "method": "download",
                        "url": man["url"], "sha256": man.get("sha256", ""),
                        "file": man.get("file") or "qemu-w64-setup.exe",
                        "version": man.get("version", ""), "installer_args": ["/S"],
                        "note": f"QEMU {man.get('version') or 'installer'} is fetched here first, "
                                f"about 200 MB, so you can watch it download \u2014 then Windows "
                                f"asks for permission to install it."}
            return {"ok": True, "what": "qemu", "command": command, "method": "job",
                    "note": "winget is fetching QEMU; it is about a gigabyte. winget prints no "
                            "percentage when its output is not a console, so this one can say what "
                            "it is doing but not how far along it is."}
        return {"ok": True, "what": "qemu", "command": command, "method": "manual",
                "note": "This one needs your password, so it is yours to run: copy it into a "
                        "terminal, then start the machine again."}
    return {"ok": False, "error": f"nothing is known about installing {what!r}"}


def split_command(command: str) -> Any:
    """`"C:/Program Files/QEMU/qemu-w64-setup.exe" /S` → (exe, args). Pure.

    Windows' ShellExecuteW wants the program and its arguments as two strings, so a
    path with a space in it has to survive the split. Quoted first token wins;
    anything else splits on whitespace.
    """
    text = str(command or "").strip()
    if not text:
        return ("", "")
    if text[0] in ("\"", "'"):
        quote = text[0]
        end = text.find(quote, 1)
        if end > 0:
            return (text[1:end], text[end + 1:].strip())
    parts = text.split(None, 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def start_elevated(command: str) -> Dict[str, Any]:
    """Hand an installer to Windows' RunAs so the consent prompt is Windows' own.

    ShellExecuteW returns immediately with a code above 32 once the request is
    accepted — it is the *prompt* that came back, not the install. Saying so is the
    honest wording: the note the caller shows is about permission being asked, not
    about WSL being ready.

    The command is the caller's, split by `split_command` — an earlier version
    hard-coded `wsl.exe` here and so silently ignored whatever it was asked to run.
    """
    if sys.platform != "win32":
        return {"ok": False, "error": "only Windows has an elevation prompt to use"}
    exe, args = split_command(command)
    if not exe:
        return {"ok": False, "error": "nothing to run"}
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not ask Windows to install: {e}"}
    if int(rc) <= 32:
        return {"ok": False, "error": f"Windows refused to start the installer (code {rc}) — "
                                      f"run `{command}` in a terminal as administrator"}
    return {"ok": True, "started": True, "command": command}


# --------------------------------------------------------------------------- wsl verbs

# ------------------------------------------------------------------- closing the host
#
# A distro Athena owns runs code an agent asked for. WSL's defaults hand that code the
# Windows host: every drive is mounted under /mnt, and `interop` lets a Linux process run
# any .exe with the host's own rights — `notepad.exe` in a shell is a program opened as
# you, and `/mnt/c/Users/you` is your own folder with your own keys in it. Neither is
# needed to run an app, and both are closed here, in /etc/wsl.conf, BEFORE the first
# command runs in a distro Athena created or owns.
#
# `attach: true` is the other case and gets the opposite answer: a distro the person
# already had is theirs, not an execution surface for an agent, so a command in it is
# refused rather than hardened — writing /etc/wsl.conf into somebody's own Ubuntu would
# change a machine they use.
WSL_CONF_PATH = "/etc/wsl.conf"
WSL_CONF_MARKER = "# written by athena: this distro cannot reach the Windows host"
WSL_CONF_LINES = (
    "[automount]",
    "enabled=false",
    "[interop]",
    "enabled=false",
    "appendWindowsPath=false",
)


def wsl_conf_text() -> str:
    """What Athena writes into a distro it owns. Pure, so a test can compare the bytes."""
    return WSL_CONF_MARKER + "\n" + "\n".join(WSL_CONF_LINES) + "\n"


def spec_for_distro(distro: str, home: Optional[Path] = None) -> Optional["MachineSpec"]:
    """The registered machine that names this distro — or None when no machine does.

    `attach: true` wins when two machines name the same distro: the conservative answer is
    the one that refuses a command, never the one that runs it. The registry is read
    directly (a handful of `machine.json` files) rather than cached, because the answer
    gates a refusal — and a stale "we own that" is exactly the failure that matters.
    """
    want = str(distro or "").strip().lower()
    if not want:
        return None

    def naming(where: Path) -> List["MachineSpec"]:
        try:
            return [s for s in list_specs(where) if s.provider == "wsl"
                    and str(s.distro or "").strip().lower() == want]
        except Exception:  # noqa: BLE001 — an unreadable registry is not a reason to fail
            return []

    # The caller's home decides; `default_home()` is looked at as well, because `wsl_exec`
    # is also reached by a caller that passes no home at all and by one that passes a home
    # of its own. A spec found in that second home may only ever *refuse* — an attached
    # machine found elsewhere still means "somebody's own distro", while an owned one found
    # elsewhere is not enough to write into a disk this home does not name.
    named = naming(Path(home) if home else default_home())
    if not named and home:
        elsewhere = default_home()
        if elsewhere != Path(home):
            named = [s for s in naming(elsewhere) if s.attach]
    if not named:
        return None
    return next((s for s in named if s.attach), named[0])


def is_athena_distro(distro: str) -> bool:
    """Whether this distro name is one Athena gives its own machines. Pure.

    `pick_distro` names a distro Athena made `athena-<machine>`, and `wsl_import` is handed
    exactly that — so the prefix is Athena's own convention rather than a guess. It is the
    second, registry-independent signal for ownership: `spec_for_distro` reads a folder the
    engine chose, and if that lookup ever landed on a different home the answer would be
    "nobody said", which is the one answer that would let a command run in an Athena-owned
    distro without its host shell having been closed first.
    """
    return str(distro or "").strip().lower().startswith("athena-")


def distro_is_ours(distro: str, home: Optional[Path] = None) -> Optional[bool]:
    """True (Athena made it), False (somebody's own, attached), None (nobody said).

    Three answers rather than two, because they lead to three different things: harden it,
    refuse it, leave it exactly as found. A machine's own record decides first; with no
    machine naming it, Athena's naming convention still says whose it is; and a distro with
    neither signal is left alone — this module is also called against distros that are
    nobody's machine, and inventing an owner for those would harden a stranger's Linux.
    """
    spec = spec_for_distro(distro, home)
    if spec is not None:
        return not bool(spec.attach)
    return True if is_athena_distro(distro) else None


_HARDENED: set = set()


def _shq(text: str) -> str:
    """`'…'` for bash — a value put into a shell line is quoted, or it is not a value."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _harden_command() -> str:
    """The one shell line that writes /etc/wsl.conf and reads it back. Pure.

    `printf` is a bash builtin, so this needs nothing installed: a minimal rootfs may have
    no `tee` and a busybox may have a different `cat`, but the shell it was asked for is
    there by definition. The read-back rides in the same command, because "the file is
    there" has to be evidence rather than an exit code.
    """
    lines = " ".join(_shq(ln) for ln in wsl_conf_text().splitlines())
    return (f"umask 022; mkdir -p /etc; printf '%s\\n' {lines} > {WSL_CONF_PATH}; "
            f"cat {WSL_CONF_PATH}")


def wsl_harden(distro: str, home: Optional[Path] = None, timeout: int = 180,
               restart: bool = True, force: bool = False) -> Dict[str, Any]:
    """Close the host shell inside a distro Athena owns. Idempotent, proven by read-back.

    Written as **root**, before anything else runs in the distro, and with no `user` or
    `cwd` taken from the caller: this is Athena's own act on Athena's own machine. The file
    is read back and compared, so `ok` means the bytes are in place — an `ok` that only
    meant "wsl.exe exited 0" is the assumption this exists to replace.

    `restart` terminates the distro so the settings are in force for whatever runs next.
    WSL reads /etc/wsl.conf when a distro *starts*, so a distro that is already up — with
    interop on it — would otherwise stay reachable for exactly the command a reviewer came
    looking for. A terminate that fails is reported, not fatal: the next start applies the
    file anyway.
    """
    exe = wsl_path()
    if not exe:
        return {"ok": False, "error": "wsl.exe is not on this machine"}
    name = str(distro or "").strip()
    if not name:
        return {"ok": False, "error": "no distro to harden"}
    if not force and name.lower() in _HARDENED:
        return {"ok": True, "already": True, "distro": name, "path": WSL_CONF_PATH}
    res = _run([exe, "-d", name, "-u", "root", "--", "bash", "-lc", _harden_command()],
               timeout=timeout)
    if not res.get("ok") or WSL_CONF_MARKER not in (res.get("text") or ""):
        return {"ok": False, "distro": name, "path": WSL_CONF_PATH,
                "error": f"could not write {WSL_CONF_PATH} in {name}: "
                         + ((res.get("error") or res.get("text") or "").strip()
                            or f"wsl.exe exited {res.get('code')}")}
    _HARDENED.add(name.lower())
    ended = wsl_terminate(name) if restart else {}
    return {"ok": True, "distro": name, "path": WSL_CONF_PATH, "conf": wsl_conf_text(),
            "restarted": bool(ended.get("ok")), "restart_error": ended.get("error") or ""}


# What a command may be for a distro that is somebody else's to run anyway: being unable to
# ask it `uname -a` is a different kind of refusal from being unable to install into it.
_READ_ONLY_COMMANDS = frozenset({
    "[", ":", "basename", "cat", "cmp", "cut", "date", "df", "diff", "dirname", "du",
    "echo", "egrep", "exit", "false", "fgrep", "file", "find", "free", "getent", "grep",
    "groups", "head", "hostname", "id", "journalctl", "ls", "lsb_release", "lsblk", "lscpu",
    "lsmod", "md5sum", "nl", "nproc", "od", "printenv", "printf", "ps", "pwd", "readlink",
    "realpath", "rg", "seq", "sha1sum", "sha256sum", "sha512sum", "sleep", "sort", "stat",
    "strings", "tac", "tail", "test", "tr", "true", "type", "uname", "uniq", "uptime", "wc",
    "which", "whoami", "wslpath",
})
# Flags that turn a reader into a writer: `find` is the command that carries them.
_WRITE_FLAGS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf",
                "-fls")
_SPLIT_COMMANDS = re.compile(r"\|\||&&|[;|&\n]")


def is_read_only_command(command: str) -> bool:
    """Whether every part of this command only reads. Pure, and deliberately strict.

    Used for one thing: deciding whether a command in somebody else's distro may run. Yes
    only for a chain whose every command is on a list of readers, with no output
    redirection, no command substitution and no flag that turns a reader into a writer.
    Anything unrecognised is **not** read-only: the dangerous commands are the ones nobody
    thought of, so the answer for them has to be no.
    """
    text = str(command or "").strip()
    if not text or "`" in text or "$(" in text or "${" in text:
        return False
    # `2>/dev/null` is how a probe stays quiet, and it writes nothing.
    cleaned = re.sub(r"\d?>>?\s*(?:/dev/null|&1|&2)", " ", text)
    if any(ch in cleaned for ch in "<>"):
        return False
    parts = [p.strip() for p in _SPLIT_COMMANDS.split(cleaned)]
    if not parts or any(not p for p in parts):
        return False
    for part in parts:
        words = part.split()
        head = words[0] if words else ""
        # A path (`/usr/bin/ls`) and a wrapper (`env`, `xargs`, `sudo`) are both refused:
        # neither is on the list, and a leading path is how a list gets spoofed.
        if head not in _READ_ONLY_COMMANDS:
            return False
        if any(w in _WRITE_FLAGS for w in words[1:]):
            return False
    return True


def _wsl_refusal(distro: str, command: str, ours: Optional[bool], approved: bool,
                 home: Optional[Path]) -> Dict[str, Any]:
    """The answer that stops a command before it is sent, or `{}` to let it through."""
    if ours is not False or approved or is_read_only_command(command):
        return {}
    spec = spec_for_distro(distro, home)
    machine = f", attached to the machine {spec.name!r}" if spec else ""
    return {"ok": False, "code": None, "output": "", "truncated": False, "distro": distro,
            "command": command, "refused": "attached_distro", "attach": True,
            "error": (f"refusing to run a command in {distro}{machine}: it is a distro you "
                      f"own rather than one Athena made, so code in it can read and change "
                      f"the files you care about. Read-only commands (uname, ls, cat, df …) "
                      f"still run here, and a command you approve yourself runs as it is. "
                      f"Point a machine at your own distro if you want to see it on a card "
                      f"— make an Athena-owned machine when a command has to run somewhere "
                      f"of its own.")}


def wsl_exec(distro: str, command: str, user: str = "", cwd: str = "",
             timeout: int = 300, max_chars: int = 20000, home: Optional[Path] = None,
             owned: Optional[bool] = None, approved: bool = False) -> Dict[str, Any]:
    """Run a command inside a distro and bring back its output.

    `--` sends the rest of the line to Linux untouched, which is the difference
    between running `bash -lc "<your command>"` and having wsl.exe try to
    interpret your pipes itself.

    Two gates stand in front of that command, and both ask *whose* distro this is:

      * `owned=True` — Athena made it, so it is hardened first (`wsl_harden`) and the
        command then runs. A hardening that fails refuses the command: a distro whose host
        shell is open is the hole the gate exists to close, and running in it anyway would
        be reporting the fix and leaving it out.
      * `owned=False` — somebody's own distro, attached to a machine. Refused, in a
        sentence naming the machine, unless the command only reads
        (`is_read_only_command`) or the caller passes `approved=True`, which means a person
        said yes to *this* command — an agent does not get to pass that for itself.

    `owned=None` asks `distro_is_ours`, so a caller that has never heard of this rule — the
    panel's host, an agent tool, a shell doing bookkeeping — gets the same answer as one
    that passes it explicitly: a machine's own record first, then Athena's `athena-<machine>`
    naming convention. A distro with neither signal is left exactly as it was found —
    hardening and refusing are both decisions about a distro that is somebody's machine.
    """
    exe = wsl_path()
    if not exe:
        return {"ok": False, "error": "wsl.exe is not on this machine",
                "needs_setup": wsl_needs_setup()}
    ours = distro_is_ours(distro, home) if owned is None else bool(owned)
    refusal = _wsl_refusal(distro, command, ours, approved, home)
    if refusal:
        return refusal
    if ours:
        hard = wsl_harden(distro, home=home)
        if not hard.get("ok"):
            return {"ok": False, "code": None, "output": "", "truncated": False,
                    "distro": distro, "command": command, "refused": "wsl_conf",
                    "error": (f"refusing to run a command in {distro}: {hard.get('error')}. "
                              f"Athena owns this distro, and until {WSL_CONF_PATH} is in "
                              f"place anything running in it can reach this computer's "
                              f"drives and start its programs. Fix the distro, or point a "
                              f"machine at one you keep yourself with `attach`.")}
    argv = [exe, "-d", distro]
    if user:
        argv += ["-u", user]
    if cwd:
        argv += ["--cd", cwd]
    argv += ["--", "bash", "-lc", command]
    res = _run(argv, timeout=timeout)
    text = res.get("text") or ""
    truncated = len(text) > max_chars
    if truncated:
        keep = max_chars // 2
        text = (text[:keep] + f"\n… {len(text) - max_chars} characters cut from the middle …\n"
                + text[-keep:])
    return {"ok": bool(res.get("ok")), "code": res.get("code"), "output": text,
            "truncated": truncated, "error": "" if res.get("ok") else (res.get("error") or ""),
            "distro": distro, "command": command}


def wsl_terminate(distro: str, timeout: int = 60) -> Dict[str, Any]:
    exe = wsl_path()
    if not exe:
        return {"ok": False, "error": "wsl.exe is not on this machine"}
    res = _run([exe, "--terminate", distro], timeout=timeout)
    # The listing carries the state, so the cached answer is now wrong even though the list of
    # names is not. Forgetting is cheap; a card that says "Running" for a stopped distro is not.
    forget_wsl_status()
    return {"ok": bool(res.get("ok")), "distro": distro, "error": res.get("error") or ""}


def wsl_export(distro: str, dest: Path, timeout: int = 3600) -> Dict[str, Any]:
    """A snapshot of a distro, as a tarball. The cheap kind of snapshot: no hypervisor."""
    exe = wsl_path()
    if not exe:
        return {"ok": False, "error": "wsl.exe is not on this machine"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = _run([exe, "--export", distro, str(dest)], timeout=timeout)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "export failed"}
    size = dest.stat().st_size if dest.exists() else 0
    return {"ok": True, "path": str(dest), "bytes": size, "distro": distro}


def _harden_note(distro: str) -> Dict[str, Any]:
    """Close the host shell in a distro Athena just created, as part of the answer.

    Deliberately *not* fatal. The distro exists and is usable, and `wsl_exec` hardens (and
    refuses on failure) before anything runs in it — so a hardening that failed here is
    reported as `hardened: False` with the reason rather than turning a finished import
    into a failure and leaving half a machine with no way to finish it.
    """
    res = wsl_harden(distro)
    return {"hardened": bool(res.get("ok")), "wsl_conf": str(res.get("path") or WSL_CONF_PATH),
            "harden_error": "" if res.get("ok") else (res.get("error") or "")}


def wsl_import(name: str, rootfs: Path, location: Path, timeout: int = 3600) -> Dict[str, Any]:
    """Create a distro from a rootfs tarball, Athena-owned, in our own folder.

    `--install --from-file` is the modern path (it accepts a local file and can
    make a fixed-size disk); `--import` is kept as the fallback because it has
    worked on every WSL 2 release and this has to work on machines we cannot see.

    The moment a distro exists, its host shell is closed (`_harden_note`): WSL reads
    /etc/wsl.conf when a distro *starts*, so this is the only point where the settings can
    be in place for the very first command anybody runs in it.
    """
    exe = wsl_path()
    if not exe:
        return {"ok": False, "error": "wsl.exe is not on this machine",
                "needs_setup": wsl_needs_setup()}
    if not rootfs.is_file():
        return {"ok": False, "error": f"no rootfs image at {rootfs}"}
    location.mkdir(parents=True, exist_ok=True)
    modern = _run([exe, "--install", "--from-file", str(rootfs), "--name", name,
                   "--location", str(location), "--no-launch"], timeout=timeout)
    if modern.get("ok"):
        forget_wsl_status()
        return {"ok": True, "distro": name, "via": "install --from-file",
                **_harden_note(name)}
    legacy = _run([exe, "--import", name, str(location), str(rootfs)], timeout=timeout)
    if legacy.get("ok"):
        forget_wsl_status()
        return {"ok": True, "distro": name, "via": "import", **_harden_note(name)}
    # Both failing is normal on an old WSL, so report the modern attempt's message:
    # it is the one that names the actual problem (a feature not enabled, a file
    # that is not a rootfs) rather than the legacy fallback's generic complaint.
    # Forgotten even on failure: an import that timed out may still have landed, and a
    # cached "that distro does not exist" would then be the plug-in arguing with WSL.
    forget_wsl_status()
    return {"ok": False, "error": modern.get("error") or legacy.get("error") or "import failed",
            "detail": legacy.get("error") or ""}


def wsl_unregister(distro: str, timeout: int = 120) -> Dict[str, Any]:
    exe = wsl_path()
    if not exe:
        return {"ok": False, "error": "wsl.exe is not on this machine"}
    res = _run([exe, "--unregister", distro], timeout=timeout)
    forget_wsl_status()
    return {"ok": bool(res.get("ok")), "distro": distro, "error": res.get("error") or ""}
