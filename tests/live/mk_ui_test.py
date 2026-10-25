"""
Build the headless UI harness: copy the merged plugin next to the driver and
give it the test surface the driver imports.

The driver `import plugin, { __fns } from './athena.mjs'`, and athena.mjs is a
copy -- so this is the step that keeps that copy honest. If it drifts, the whole
UI suite is testing last week's merge, which is worse than not running it.

The appended block is the only edit to the copy: everything the driver touches is
the module's own surface, not a re-implementation for the test.

Also runs an ES-module syntax check on the copy, because a copy that does not
parse would otherwise fail inside the driver with a confusing import error.

Usage: env -u PYTHONPATH <venv python> mk_ui_test.py
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path
import os

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUG = Path(str(HERMES_ROOT) + r"/plugins/athena")
# Node is needed only to `node --check` the generated module, so ask this
# machine for it the way the plugin does — ATHENA_NODE, then whatever `node-*`
# Hermes keeps for its own tooling, then PATH — instead of naming the one
# version that happened to exist on the machine this was written on.
def find_node() -> Path:
    override = os.environ.get("ATHENA_NODE")
    if override:
        return Path(override)
    tools = Path(os.environ.get("HERMES_TOOLS") or (HERMES_ROOT / "tools"))
    for d in sorted(tools.glob("node-*"), reverse=True):
        for cand in (d / "node.exe", d / "bin" / "node"):
            if cand.exists():
                return cand
    return Path("node")


NODE = find_node()
SRC = PLUG / "desktop" / "plugin.js"
OUT = Path(__file__).with_name("uitest") / "athena.mjs"

# What the driver reaches for by name. Keep in step with driver.mjs: an export
# listed here that the merge no longer defines fails loudly at import time,
# which is the point.
EXPORTS = {
    "actionFailure": "what a failed action says out loud, or nothing when it really succeeded",
    "AppCard": "an app's card: its state chip, the one button, and the menu behind",
    "appStateWords": "the one chip on a card: running \u00b7 :7860, runs here, nothing read yet",
    "appActions": "what a card offers as its button, its second, and its menu, by state",
    "keyBadge": "what an app asks for, as the one badge the card shows",
    "keysMatch": "the apps-tab filter for what runs here and what wants a key",
    "StoreCard": "a store card, whose install outlives it",
    "StoreTab": "the store's own argument, and the filter for what carries a sheet",
    "runDoneWords": "what a finished run says, artifact included",
    "runSecs": "how a run's length is worded: 8s, 4m 5s, 2h 3m",
    "jobPace": "what a running job says: working, quiet for 40s, no output for 6m",
    "jobGoneWords": "what a poll says about a job that vanished with the engine: the backend's sentence, or the restart",
    "updateAllWords": "what the shelf says when a shelf-wide update finishes: how many, and where the reasons are",
    "pollDelay": "how long the panel waits before asking /status again: a second while the engine is coming up, five when it is settled",
    "guestPoint": "where a click on the panel's picture lands inside the guest",
    "screenWords": "one line above the guest's screen: its size, the frame's size, when it was read",
    "screenLive": "whether the panel should keep asking for frames at all",
    "frameDelay": "how long to wait before the next frame: quick while it moves, slower when it does not",
    "kickDelay": "how long input waits for the picture back: leave a look that is already due alone, bring a later one in",
    "POINTER_MOVE_MS": "how often hovering may move the guest's pointer",
    "screenKey": "which key name the guest gets told about",
    "engineLine": "the header's one line about the engine: ready with its port, starting, or stopped",
    "Header": "the panel's own chrome: the engine line and the Start/Stop/Restart that act on it",
    "waited": "how long the wait has been, as a word the header can put after starting\u2026",
    "keyOwnerStep": "whether a key hands the keyboard back, or belongs to the guest \u2014 a value, so the gesture is provable",
    "dockNotes": "what the screen dock says about who holds the keyboard and how the pointer behaves",
    "keyboardButton": "the one click that changes who holds the keyboard",
    "keyboardNote": "the dock's line, whole",
    "leaseWords": "who is driving the guest's screen, and the one button that takes it back",
    "putBackPanel": "where an app has been, and what going back would cost",
    "costLine": "what the card has cost, in one sentence, with no claim it cannot back",
    "costDetail": "the small print: what was counted, what was not",
    "costSpan": "how a stretch of the card's time is worded: 1d 17h, 12m",
    "GpuCostCard": "the card's running total, and the price that prices it",
    "kindWords": "how a file kind is said out loud: an image, a 3D model",
    "acceptsPanel": "which apps here would take this file, and why the others would not",
    "SendToApp": "the bar under the file browser that hands a file to one of them",
    "madePanel": "what a run made, and who would take it on next",
    "repeatPanel": "what runs on its own, and whether it still will",
    "RunsTab": "the history of what has run here, with the repeats above it",
    "JOB_QUIET_HINTS": "what a silent job of each kind is usually doing",
    "machineWords": "the one chip on a machine: running, stopped, no distro yet",
    "shellTabs": "the tab bar: which tabs exist, and what a watched machine adds to them",
    "newMachineRow": "the first row of the make-a-machine form, drawn from what it is given",
    "machineFields": "a machine's settings as text a box can hold",
    "machineChanges": "only the differences between what was typed and what the machine is",
    "portPairs": "host:guest pairs, with the lines that are not pairs refused by name",
    "MachineScreenTab": "a machine's screen in its own tab, at the size of the pane",
    "watchMachine": "which machine's screen is open, if any",
    "GuestScreen": "the picture of a guest, and the input that goes back through it",
    "bootChoice": "where a machine with a medium attached starts, and the other way of it",
    "INPUT_ECHO_MS": "when to look after the person did something",
    "INPUT_BURST_MS": "how long a burst of typing keeps the fast cadence",
    "INPUT_FRAME_MS": "the fast cadence: just above the guest's own echo",
    "STILL_FRAME_MS": "nothing has happened for two frames",
    "MOVING_FRAME_MS": "something is happening that nobody asked for",
    "machineLine": "what a machine is, in one line: WSL, its distro, its cores",
    "machinePorts": "the ports a machine is reachable on, host\u2192guest",
    "parsePorts": "\"8080:80, 2222:22\" as the spec wants it, junk dropped",
    "isoWords": "the image folder as one line: where it is and what is in it",
    "isoLabel": "a file in the ISO menu: name, what the name suggests, how big",
    "isoOptions": "the ISO menu itself \u2014 the folder's files, plus \"somewhere else\"",
    "isoGroups": "images by the folder they live in, the top folder first",
    "isoGroupsShown": "which folders are showing: the top one open, subfolders shut until asked",
    "groupMap": "every folder open or every folder shut, for the one button that does both",
    "needList": "whatever a response says is missing, as a list of things to set up",
    "folderWords": "the folder browser's one line: where you are and what is in there",
    "isoPatch": "what picking a file fills in: path, kind, and a suggested guest OS",
    "fitWords": "the size this computer can give a guest, with the first reason",
    "needWords": "a missing WSL or QEMU as the question it turns into",
    "SetupCard": "the card that asks before anything is installed",
    "FolderPicker": "walking to the image folder instead of typing its path",
    "JobProgress": "the bar a job draws: bytes when there are bytes, a label when there are not",
    "MachineCard": "a machine's card: its chip, its verb, the console behind",
    "rigMatch": "the store filter for apps checked in from Windows on NVIDIA",
    "RunsRow": "one recorded run",
    "AutoTerm": "the auto-following output box",
    "installWrite": "write a store install's state",
    "installRead": "read a store install's state",
    "startInstall": "run an install with nothing mounted",
    "AppsTab": "the installed-apps tab, with its search and running list",
    "appMatches": "the apps-tab search filter, pure",
    "HealthReport": "the check list, and the button a step on one of them offers",
    "WeightShelf": "the shelf: what the disk holds, and pulling what it lacks",
    "NeedsYou": "what needs you: the report, in the System window",
    "needsPanel": "the report drawn from data, without a fetch behind it",
    "spotSelector": "the selector for the one thing the agent says it is touching, or nothing",
    "spotTab": "the tab that draws the kind of card an agent named, or ''",
}

TESTS = """
// ---- test surface ---------------------------------------------------------
// Appended by mk_ui_test.py. Named exports only: the default export stays the
// plugin itself, so the driver registers the same object the app loads.
export const __fns = {
%s
}
"""


def main():
    src = SRC.read_text(encoding="utf-8")

    # The registry is a dict literal, so a repeated key is not something Python
    # raises: the later line wins and the earlier one becomes dead text that still
    # looks alive. `screenLive` and `frameDelay` each appeared twice — 80 lines
    # naming 78 exports — and nothing counted either the lines or the names, so
    # the pair was invisible until a second pair of eyes read all 80.
    mine = Path(__file__).read_text(encoding="utf-8")
    block = mine.split("EXPORTS = {", 1)[1].split("\n}\n", 1)[0]
    written = re.findall(r'^\s{4}"([A-Za-z_]\w*)":', block, re.M)
    twice = sorted({n for n in written if written.count(n) > 1})
    if twice:
        print(f"FAIL  EXPORTS names {', '.join(twice)} twice — the later line wins and the earlier does nothing")
        return 1

    # Presence used to be a substring search across a 460 KB file, which passes
    # for a name that only appears in a comment, in a string, or in a nested
    # local. What the appended export block actually needs is exactly one
    # top-level definition of each name, so that is what gets counted.
    defs = {name: re.findall(
        rf"^(?:export\s+)?(?:async\s+)?(?:function|const|let|class)\s+{re.escape(name)}\b",
        src, re.M) for name in EXPORTS}
    missing = [n for n, f in defs.items() if not f]
    doubled = [n for n, f in defs.items() if len(f) > 1]
    if missing or doubled:
        if missing:
            print(f"FAIL  the merge no longer defines: {', '.join(missing)}")
        if doubled:
            print(f"FAIL  the merge defines these twice, so the export picks arbitrarily: {', '.join(doubled)}")
        print("      (update EXPORTS here and driver.mjs together, or restore the function)")
        return 1
    if "export default {" not in src:
        print("FAIL  the merge has no default export to register")
        return 1

    body = ",\n".join(f"  {name},   // {why}" for name, why in EXPORTS.items())
    OUT.write_text(src + TESTS % body, encoding="utf-8", newline="")
    print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes from {len(src):,})")

    # `node --check` treats .mjs as a module, so this catches a bad merge before
    # the driver does -- and the copy keeps its .mjs name so it does not.
    r = subprocess.run([str(NODE), "--check", str(OUT)], capture_output=True, text=True)
    if r.returncode != 0:
        print("FAIL  the copy does not parse as an ES module:")
        print("\n".join(r.stderr.splitlines()[:8]))
        return 1
    print("parses as an ES module")
    return 0


if __name__ == "__main__":
    sys.exit(main())
