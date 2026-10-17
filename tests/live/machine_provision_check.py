"""End-to-end proof that provisioning a machine works: create, fetch, import, boot, exec.

Runs in its own process, so the job thread lives here and the script waits for it. Not part
of the suite: it downloads a few hundred megabytes and creates a real distro, which is a
side effect a test suite has no business having.
"""
import importlib.util
import json
import pathlib
import sys
from pathlib import Path
import time

PLUG = pathlib.Path(__file__).resolve().parents[0]
while not (PLUG / "plugin.yaml").is_file():
    PLUG = PLUG.parent
sys.path.insert(0, str(PLUG / "dashboard"))

spec = importlib.util.spec_from_file_location("athena_plugin_probe", str(PLUG / "__init__.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["athena_plugin_probe"] = mod
spec.loader.exec_module(mod)

api = mod._load_api()
host = api.machines_host
m = api.athena_machines

if "--qemu" in sys.argv:
    """The live QEMU half: fetch a small ISO, boot it, read its console, snapshot it,
    destroy it. Run this on a machine that has QEMU — the argv is proved without QEMU by
    `t_machines.py`, and this proves the rest of it."""
    import subprocess
    print("home  :", host.home)
    print("qemu  :", json.dumps(host.qemu())[:200])
    if not m.qemu_exe():
        print("\nQEMU is not installed on this machine, so this mode cannot run here.")
        print("Set it up from the Machines tab (or " +
              str(host.qemu().get('install_command') or 'winget install SoftwareFreedomConservancy.QEMU') +
              ") and run this again. Nothing was changed.")
        sys.exit(2)

    NAME = "qemu-alpha"
    ISO = ("https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/"
           "alpine-virt-3.20.3-x86_64.iso")
    if m.load_spec(host.home, NAME) is None:
        s = m.MachineSpec(name=NAME, provider="qemu", os="linux",
                          image={"kind": "iso", "url": ISO}, cpus=2, memory_mb=2048,
                          disk_gb=4, display="panel", ports=[{"host": 8099, "guest": 80}],
                          ssh={"port": 2299})
        print("create:", json.dumps(host.create(s))[:200])
    else:
        s = m.load_spec(host.home, NAME)
        print("machine already existed")
    mdir = m.machine_dir(host.home, NAME)

    res = host.start(NAME)
    print("start :", json.dumps(res)[:300])
    if res.get("job"):
        deadline = time.time() + 1800
        while time.time() < deadline:
            time.sleep(5)
            j = api._JOBS.get(res["job"]) or {}
            if j.get("state") in ("done", "failed"):
                print("job   :", j.get("state"), "| error:", j.get("error"))
                for line in (j.get("log") or [])[-6:]:
                    print("   log:", line)
                break
        else:
            print("!! still preparing after 30 minutes")

    print("\n== is it a running machine ==")
    print("process alive:", m.qemu_running(mdir), "| host state:", host.state(s))
    print("argv was:")
    for a in m.qemu_argv(s, mdir):
        print("   ", a)

    print("\n== its console (the guest's own boot output) ==")
    log = mdir / "console.log"
    found = ""
    deadline = time.time() + 180
    while time.time() < deadline:
        if log.is_file() and log.stat().st_size > 0:
            found = log.read_text(errors="replace")
            if "Linux version" in found or "Alpine" in found:
                break
        time.sleep(5)
    print((found or "(nothing yet)")[:1200])

    print("\n== its screen, read the way the panel reads it ==")
    shot = {}
    deadline = time.time() + 120
    while time.time() < deadline:
        shot = host.screen(NAME, save=True)
        if shot.get("image"):
            break
        time.sleep(4)
    print("screen :", json.dumps({k: v for k, v in shot.items() if k != "image"})[:260])
    if shot.get("image"):
        png = Path(str(shot.get("saved") or (mdir / "screen.png")))
        print("  saved:", png.name, png.stat().st_size // 1024, "KB",
              "| a real framebuffer, not a placeholder:",
              (shot.get("width"), shot.get("height")) not in ((640, 480), (0, 0)),
              "| first bytes:", shot["image"][:22])

    print("\n== a keystroke, into that screen ==")
    typed = host.screen_input(NAME, [{"kind": "key", "key": "Enter"}])
    print("  ->", json.dumps(typed)[:200])
    again = host.screen(NAME)
    print("  the screen answered:", again.get("ok"), "| size now:",
          (again.get("width"), again.get("height")))

    # Out to a window and back, which is a restart twice, and the restart is what used to
    # fail: the previous QEMU had not let go of the guest's console file yet, so the boot
    # died with `could not connect serial device to character backend` — a machine the
    # someone watching it saw as "it crashed". Both moves have to leave it running.
    print("\n== out to a window of its own, and back into the panel ==")
    # The machine was made by a job, and that job is still starting and stopping it. Two
    # actors on one machine is the fight this step exists to avoid, so wait for the job
    # rather than pushing a restart into the middle of it.
    waited = time.time()
    while api._job_running(f"machine-{NAME}") and time.time() - waited < 300:
        time.sleep(2)
    print("  the job that made it is finished:", not api._job_running(f"machine-{NAME}"),
          f"| waited {int(time.time() - waited)}s")
    out = host.set_display(NAME, "window")
    print("  out   :", json.dumps({k: v for k, v in out.items() if k != "log"})[:320])
    print("  running still:", m.qemu_running(mdir),
          "| fell back to a headless boot:", out.get("display_fallback"))
    time.sleep(3)
    back = host.set_display(NAME, "panel")
    print("  back  :", json.dumps({k: v for k, v in back.items() if k != "log"})[:320])
    print("  running still:", m.qemu_running(mdir),
          "| fell back to a headless boot:", back.get("display_fallback"))
    live = host.screen(NAME)
    print("  screen:", live.get("ok"), (live.get("width"), live.get("height")),
          "| the screen is in the panel again:", m.load_spec(host.home, NAME).display)

    print("\n== a command in it is refused, with the way in ==")
    r = host.exec_(NAME, "uname -a")
    print("  ->", json.dumps(r)[:400])

    print("\n== stop, snapshot, destroy ==")
    print("stop    :", json.dumps(host.stop(NAME))[:200])
    print("stopped :", m.qemu_running(mdir) is False)
    snap = host.snapshot(NAME)
    print("snapshot:", json.dumps(snap)[:200])
    if snap.get("job"):
        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(3)
            j = api._JOBS.get(snap["job"]) or {}
            if j.get("state") in ("done", "failed"):
                print("  state:", j.get("state"), "| error:", j.get("error"))
                print("  result:", json.dumps(j.get("result"))[:400])
                break
    export = mdir / "exports"
    print("exports :", [f"{f.name} {f.stat().st_size // 1048576} MB"
                        for f in sorted(export.glob("*")) if f.is_file()])
    print("delete  :", json.dumps(host.delete(NAME, unregister=True))[:300])
    print("gone    :", not mdir.exists(), "| still in the list:",
          [r["name"] for r in host.list()["machines"]])
    sys.exit(0)

print("home :", host.home)
print("wsl  :", json.dumps({k: v for k, v in host.wsl().items() if k != "distros"})[:200])
print("setup:", json.dumps(host.setup()["needs_setup"])[:300])

NAME = "ubuntu-dev"
if m.load_spec(host.home, NAME) is None:
    preset = m.PRESETS["ubuntu-24.04"]
    s = m.MachineSpec(name=NAME, provider="wsl", os="ubuntu-24.04",
                      image=dict(preset["image"]), cpus=4, memory_mb=8192, disk_gb=40)
    print("create:", json.dumps(host.create(s))[:200])
else:
    s = m.load_spec(host.home, NAME)
    print("machine already existed")

res = host.start(NAME)
print("start:", json.dumps(res)[:300])

if res.get("job"):
    jid = res["job"]
    last = ""
    deadline = time.time() + 1800
    while time.time() < deadline:
        time.sleep(5)
        j = api._JOBS.get(jid) or {}
        prog = j.get("progress") or {}
        label = prog.get("label") or ""
        state = j.get("state")
        if label != last:
            print(f"  [{state}] {label}")
            last = label
        if state in ("done", "failed"):
            print("job state:", state, "| error:", j.get("error"))
            print("result:", json.dumps(j.get("result"))[:400])
            for line in (j.get("log") or [])[-8:]:
                print("   log:", line)
            break
    else:
        print("!! provisioning still running after 30 minutes")

print("\n== exec ==")
for cmd in ("uname -srm", ". /etc/os-release && echo \"$PRETTY_NAME\"", "whoami", "nproc"):
    r = host.exec_(NAME, cmd, timeout=180)
    print(f"$ {cmd}\n  -> ok={r.get('ok')} {r.get('output', '').strip()[:200]}")

print("\n== list ==")
print(json.dumps(host.list(), indent=1)[:900])
