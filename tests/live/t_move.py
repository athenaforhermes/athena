"""
Moving a screen is a restart, and a restart is where two actors must not meet.

Where a machine's screen lives is decided on QEMU's command line, so "out to a window" and
"back into the panel" are both a stop and a start. That is the whole point of this file: a
restart is not a setting, and the two ways it goes wrong were both measured on a real machine
rather than reasoned about.

  * The start raced the previous QEMU's hold on the guest's console file. QEMU exits with
    `could not connect serial device to character backend` and the machine is gone -- which,
    from the panel, is a machine that crashed. `wait_for_console` and `console_was_held` are
    the fix and are checked in t_machines.py against that exact wording.
  * A machine that is still being made has a job of its own, and that job starts and stops the
    guest as it works. Asking to move the screen in the middle of it put two actors on one
    machine: the job's own boot died with QEMU's "Ignoring request for interrupt vector 0" and
    the machine was left stopped. That is what this file pins down — the refusal, and that a
    refusal writes nothing, so a machine never ends up asking for a display it is not using.

Nothing here boots anything, and nothing here touches the machines somebody actually has: the
home is a temporary directory with one spec written into it.

Usage: env -u PYTHONPATH <venv python> t_move.py
"""
import importlib.util
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUG = HERE.parent.parent
DASH = PLUG / "dashboard"
sys.path.insert(0, str(DASH))

import machines  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


print("the restart a screen move is")

# Load the backend the way the plugin host does — by path, with its siblings found by name.
# A `from . import x` here resolves in-process and dies at call time in the host, which is
# how this path shipped broken once; t_screen.py guards the rule, this file just needs the
# module the way the host gets it.
_spec = importlib.util.spec_from_file_location("athena_move_api", str(DASH / "plugin_api.py"))
api = importlib.util.module_from_spec(_spec)
sys.modules["athena_move_api"] = api
_spec.loader.exec_module(api)
check("the backend loads by path, the way the plugin host loads it", True)


class TempHost(api.MachineHost):
    """A host whose home is a temporary directory, so this file cannot reach a real machine."""

    def __init__(self, home):
        super().__init__()
        self._home = Path(home)

    @property
    def home(self):
        return self._home


with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    spec = machines.MachineSpec(name="rig", provider="qemu", display="panel",
                                image={"kind": "iso", "path": "nope.iso"})
    machines.machine_dir(home, spec.name).mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, spec)
    host = TempHost(home)

    real_job_running = api._job_running
    try:
        api._job_running = lambda kind: True
        out = host.set_display("rig", "window")
        check("a machine that is still being made refuses to move its screen",
              out.get("ok") is False and "still being made" in str(out.get("error")), str(out)[:200])
        check("... and the sentence names the machine, because that is what the person asked about",
              "rig" in str(out.get("error")), str(out.get("error"))[:160])
        on_disk = machines.load_spec(home, "rig")
        check("... and a refusal writes nothing: the machine is not left asking for a display "
              "it is not using", on_disk.display == "panel", repr(on_disk.display))
        # A refusal is shaped like one: no half-answer carrying a display it did not apply,
        # which is what makes the panel able to show a sentence instead of a button that lied.
        check("... and a refusal is shaped like a refusal, not like a half-done move",
              not ({"display", "restarted", "vnc_port", "state"} & set(out)), str(out)[:200])
    finally:
        api._job_running = real_job_running

    # The job registry is the only thing that was faked: with it quiet, the same call is not
    # refused for that reason any more, which is what makes the check above a check.
    still_refused = host.set_display("rig", "holodeck")
    check("a display that does not exist is refused for its own reason, not the job's",
          still_refused.get("ok") is False and "still being made" not in str(still_refused.get("error")),
          str(still_refused)[:200])

    # What a machine starts on is decided on the same command line, so it is the same kind of
    # restart -- and it is the "reboot now" bug in one setting: a finished install must boot
    # itself, while an empty disk still has to reach the installer.
    real_job_running = api._job_running
    try:
        api._job_running = lambda kind: True
        out = host.set_boot("rig", "medium")
        check("a machine that is still being made refuses to change what it boots",
              out.get("ok") is False and "still being made" in str(out.get("error")), str(out)[:200])
        check("... and a refusal writes nothing: the spec still says what it said",
              machines.load_spec(home, "rig").boot == "", repr(machines.load_spec(home, "rig").boot))
    finally:
        api._job_running = real_job_running

    asked = host.set_boot("rig", "medium")
    check("the installer can be asked for first, and the answer says which order that is",
          asked.get("ok") is True and asked.get("boot") == "medium"
          and asked.get("boot_order") == "order=dc", str(asked)[:200])
    check("... and it is written down, so the next start boots that way",
          machines.load_spec(home, "rig").boot == "medium",
          repr(machines.load_spec(home, "rig").boot))
    back = host.set_boot("rig", "the disk")
    check("... and its own system can be asked for in the words a person would use",
          back.get("ok") is True and back.get("boot") == "system"
          and machines.load_spec(home, "rig").boot == "system", str(back)[:200])
    check("... and the command line the spec builds really does offer the disk first",
          machines.boot_args(machines.boot_preference(machines.load_spec(home, "rig")))
          == ["-boot", "order=cd"],
          machines.boot_args(machines.boot_preference(machines.load_spec(home, "rig"))))
    nonsense = host.set_boot("rig", "floppy")
    check("a boot choice that does not exist is refused for its own reason, not the job's",
          nonsense.get("ok") is False and "still being made" not in str(nonsense.get("error")),
          str(nonsense)[:200])
    check("... and a refusal leaves the machine booting what it was booting",
          machines.load_spec(home, "rig").boot == "system",
          repr(machines.load_spec(home, "rig").boot))

    plain = machines.MachineSpec(name="bare", provider="qemu",
                                 image={"kind": "disk", "path": "disk.qcow2"})
    machines.machine_dir(home, plain.name).mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, plain)
    check("a machine with nothing attached to boot from is told so, not silently changed",
          str(host.set_boot("bare", "medium").get("error") or "").find("medium") >= 0,
          str(host.set_boot("bare", "medium"))[:200])
    shell = machines.MachineSpec(name="sh", provider="wsl", distro="athena-sh")
    machines.machine_dir(home, shell.name).mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, shell)
    check("... and a machine whose shell is a shell, not a screen with a CD in it, is told so too",
          "CD-ROM" in str(host.set_boot("sh", "medium").get("error") or ""),
          str(host.set_boot("sh", "medium"))[:200])

    # "It should replace the machine with the installed version after it's installed
    # automatically." So: the first start after the install finishes adopts what is on the disk
    # -- writes `boot: system`, which is what takes the medium off the command line.
    fresh = machines.MachineSpec(name="inst", provider="qemu",
                                image={"kind": "iso", "path": "installer.iso"})
    fresh_dir = machines.machine_dir(home, fresh.name)
    fresh_dir.mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, fresh)
    check("a machine with an empty disk is not adopted as an installed one",
          host._adopt_installed(machines.load_spec(home, "inst")).get("adopted") is False
          and machines.load_spec(home, "inst").boot == "",
          repr(machines.load_spec(home, "inst").boot))
    (fresh_dir / "disk.qcow2").write_bytes(b"0" * machines.INSTALLED_BYTES)
    adopted = host._adopt_installed(machines.load_spec(home, "inst"))
    check("... but one whose disk now holds a system becomes that machine, on its own",
          adopted.get("adopted") is True
          and machines.load_spec(home, "inst").boot == "system", str(adopted)[:200])
    check("... and it says so in a sentence, so nobody has to notice that an ISO went quiet",
          "installed system" in str(adopted.get("note")) and adopted.get("disk_bytes", 0) > 0,
          str(adopted.get("note"))[:200])
    check("... and the medium really is off its command line from then on",
          "-cdrom" not in machines.qemu_argv(machines.load_spec(home, "inst"), fresh_dir),
          " ".join(machines.qemu_argv(machines.load_spec(home, "inst"), fresh_dir))[:200])

    # An explicit choice is never overruled by a guess about a file's size: somebody who asked
    # for the medium (a live or rescue image) keeps it, however much is on the disk.
    asked = machines.MachineSpec(name="live", provider="qemu", boot="medium",
                                 image={"kind": "iso", "path": "live.iso"})
    asked_dir = machines.machine_dir(home, asked.name)
    asked_dir.mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, asked)
    (asked_dir / "disk.qcow2").write_bytes(b"0" * machines.INSTALLED_BYTES)
    check("a machine that was asked to start on its medium keeps starting on its medium",
          host._adopt_installed(machines.load_spec(home, "live")).get("adopted") is False
          and machines.load_spec(home, "live").boot == "medium"
          and "-cdrom" in machines.qemu_argv(machines.load_spec(home, "live"), asked_dir),
          repr(machines.load_spec(home, "live").boot))

    # Two buttons, two different things done. "Forget it (keeps the distro)" promised to keep
    # a QEMU machine's distro and, until this, deleted the folder the distro was in.
    keep = machines.MachineSpec(name="keepme", provider="qemu",
                                image={"kind": "disk", "path": "given.qcow2"})
    keep_dir = machines.machine_dir(home, keep.name)
    keep_dir.mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, keep)
    (keep_dir / "disk.qcow2").write_bytes(b"0" * 4096)
    out = host.delete("keepme")
    check("forgetting a machine keeps its disk, which is where a QEMU distro lives",
          out.get("ok") is True and out.get("forgotten") is True
          and (machines.forgotten_machine_dir(home, "keepme") / "disk.qcow2").is_file(),
          str(out)[:220])
    check("... and it says where it went, rather than leaving the disk to be found again",
          str(out.get("kept_at") or "").endswith("keepme")
          and out.get("disk_bytes") == 4096 and "brought back" in str(out.get("note")),
          str(out.get("note"))[:200])
    check("... and it is gone from the list of machines", machines.load_spec(home, "keepme") is None)
    listed = host.forgotten()
    check("... and it is listed as forgotten, with what is still on its disk",
          listed.get("count") == 1 and listed["machines"][0]["name"] == "keepme"
          and listed["machines"][0]["disk_bytes"] == 4096
          and "distro" in listed["machines"][0]["keeps"], str(listed)[:220])
    back = host.restore("keepme")
    check("... and bringing it back moves the whole folder, disk included",
          back.get("ok") is True and (machines.machine_dir(home, "keepme") / "disk.qcow2").is_file()
          and machines.load_spec(home, "keepme") is not None
          and host.forgotten().get("count") == 0, str(back)[:220])
    check("... and it is a machine again, not a memory of one",
          back.get("machine") is not None and back["machine"].get("name") == "keepme",
          str(back.get("machine"))[:160])
    check("... and bringing back something nobody forgot says so",
          host.restore("never-was").get("ok") is False)
    check("... and a machine cannot be forgotten twice over its own folder",
          host.delete("keepme").get("ok") is True
          and host.delete("keepme").get("ok") is False, str(host.delete("keepme"))[:160])
    host.restore("keepme")

    gone = machines.MachineSpec(name="goner", provider="qemu",
                                image={"kind": "disk", "path": "given.qcow2"})
    gone_dir = machines.machine_dir(home, gone.name)
    gone_dir.mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, gone)
    (gone_dir / "disk.qcow2").write_bytes(b"0" * 4096)
    destroyed = host.delete("goner", unregister=True)
    # These routes match in the order they are written, so a literal path written after the
    # parameterised one is never reached: /machines/forgotten would answer "no machine called
    # 'forgotten'". The order is the property, so the order is what is checked.
    paths = [getattr(r, "path", "") for r in getattr(api, "router", None).routes] \
        if getattr(api, "router", None) is not None else []
    check("the list of forgotten machines answers before the route that would swallow it",
          bool(paths) and paths.index("/machines/forgotten") < paths.index("/machines/{name}"),
          str(paths[:8]))
    check("... and there is a way back for each one that was put aside",
          "/machines/forgotten/{name}/restore" in paths, str(paths[-6:]))

    # A route is a method *and* a path. The paths were checked and the methods were not, so the
    # panel's "Destroy it and its disk" sent POST to a DELETE-only route, came back 405, and the
    # machine was never destroyed while the person was told there had been an error. The panel
    # sends what it sends; every route it names has to answer that method.
    panel = (PLUG / "desktop" / "plugin.js").read_text(encoding="utf-8")
    pairs = set()
    for r in api.router.routes:
        p = getattr(r, "path", None)
        if p:
            for meth in (getattr(r, "methods", None) or []):
                pairs.add((meth.upper(), p))

    def _shape(path):
        p = re.sub(r"\$\{[^}]*\}", "{p}", path.split("?")[0])
        return p + "{p}" if p.endswith("/") else p   # '/schedules/' + encodeURIComponent(id)

    def _same_shape(pat, route):
        a = [x for x in pat.split("/") if x]
        b = [y for y in route.split("/") if y]
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            xp, yp = x == "{p}", y.startswith("{") and y.endswith("}")
            if xp and yp:
                continue
            if xp or yp or x != y:
                return False
        return True

    calls = []
    for m in re.finditer(r"(?:ctx\.rest|act|actBusy)\((['\"`])(.+?)\1", panel, re.S):
        raw = m.group(2)
        if "\n" in raw or raw.startswith("//"):
            continue
        tail = panel[m.end():m.end() + 300]
        mm = re.search(r"method:\s*'([A-Z]+)'", tail)
        # `act`/`actBusy` are the panel's verb helper and always POST; ctx.rest says its own.
        meth = (mm.group(1) if mm else "GET") if m.group(0).startswith("ctx.rest") else "POST"
        calls.append((meth, _shape(raw), raw))

    mismatched = []
    for meth, pat, raw in calls:
        here = sorted({p for (_, p) in pairs if _same_shape(pat, p)})
        if here and not any((meth, p) in pairs for p in here):
            mismatched.append(f"{meth} {raw} -> {', '.join(here)}")
    check("every route the panel calls answers the method the panel sends, not just the path",
          not mismatched, str(mismatched)[:300])
    check("... and that check can fail: a POST to the machine record is still a mismatch",
          any(_same_shape("/machines/{p}", p) for (_, p) in pairs)
          and ("POST", "/machines/{name}") not in pairs, "positive control")
    check("... and both machine verbs are reachable by the method the panel uses",
          ("POST", "/machines/{name}/forget") in pairs
          and ("POST", "/machines/{name}/destroy") in pairs, "forget/destroy")

    check("destroying a machine deletes the disk, and that is the only thing that does",
          destroyed.get("ok") is True and destroyed.get("unregistered") is True
          and not gone_dir.exists() and host.forgotten().get("count") == 0,
          str(destroyed)[:200])

    # A fetched image is written onto the spec, or the next start fetches all over again in a
    # job -- which is what made a screen move land in the middle of one. The URL stays, because
    # a machine rebuilt from this spec on a machine with no network still finds the file beside
    # it; what it gains is the path. It is an `https://` URL because that is the only scheme an
    # image may be fetched over -- the guard for that is asserted just below.
    url_spec = machines.MachineSpec(name="iso", provider="qemu",
                                    image={"kind": "iso", "name": "tiny.iso",
                                           "url": "https://example.invalid/tiny.iso"})
    machines.machine_dir(home, url_spec.name).mkdir(parents=True, exist_ok=True)
    machines.save_spec(home, url_spec)
    folder = machines.machine_dir(home, url_spec.name)

    def fake_fetch(url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"an iso, honestly")

    got = machines.qemu_prepare_image(url_spec, folder, fetch=fake_fetch)
    check("a fetch lands the image beside the machine and says so",
          got.get("ok") is True and got.get("fetched") is True
          and Path(got["path"]).name == "tiny.iso", str(got)[:160])
    machines.save_spec(home, url_spec)
    reloaded = machines.load_spec(home, url_spec.name)
    check("... and the path survives a save, so the next start is a boot and not a fetch",
          bool(reloaded.image.get("path")) and Path(reloaded.image["path"]).name == "tiny.iso",
          str(reloaded.image))
    check("... with the URL kept, for a machine rebuilt where there is no network",
          reloaded.image.get("url") == "https://example.invalid/tiny.iso", str(reloaded.image))
    again = machines.qemu_prepare_image(reloaded, folder, fetch=None)
    check("... so nothing is fetched twice: a second prepare reuses the file with no fetcher",
          again.get("ok") is True and again.get("fetched") is False, str(again)[:160])

    # An image becomes the system a machine boots, so it may only arrive over TLS. A non-https
    # URL is refused *by name* before the fetcher is reached -- "the bytes were swapped in
    # transit" is not something this machine can notice afterwards. The fetcher below records
    # every call it is handed, so "refused" is proven by the fetcher never seeing the URL at
    # all, not by the shape of the answer.
    plain_spec = machines.MachineSpec(name="plain-iso", provider="qemu",
                                      image={"kind": "iso", "name": "plain.iso",
                                             "url": "http://example.invalid/plain.iso"})
    plain_folder = machines.machine_dir(home, plain_spec.name)
    plain_folder.mkdir(parents=True, exist_ok=True)
    fetched_urls: list = []
    refused = machines.qemu_prepare_image(plain_spec, plain_folder,
                                          fetch=lambda url, dest: fetched_urls.append(url))
    check("an http:// image URL is refused rather than fetched",
          refused.get("ok") is False and refused.get("fetched") is not True, str(refused)[:200])
    check("... and the fetcher is never called with it, so nothing came over the wire",
          fetched_urls == [], str(fetched_urls))
    check("... and the refusal says why: an image may only be fetched over https",
          "https" in str(refused.get("error") or ""), str(refused.get("error"))[:200])
    check("... and no image file was left beside the machine",
          not (plain_folder / "image" / "plain.iso").exists())

    # And the guard is about jobs, not about machines: the state reading it sits next to still
    # answers honestly for a machine that never started.
    check("a stopped machine that never started still reads as stopped",
          host.state(machines.load_spec(home, "rig")) in ("stopped", "missing"),
          host.state(machines.load_spec(home, "rig")))

print(f"\n{passed} ok, {failed} failed")
sys.exit(1 if failed else 0)
