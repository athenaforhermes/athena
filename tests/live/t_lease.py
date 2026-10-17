#!/usr/bin/env python3
"""Two hands on one keyboard, and the record that stops the second one.

A guest has one keyboard. A person clicking at the panel and an agent typing an installer's
answers are two writers on it, and the guest acts on whatever arrives, in the order it
arrives — which is how an agent's `Enter` lands after a person clicked something else and
the guest does something neither of them meant. Nothing was lying about this; there was no
record of who had the keyboard, so there was nothing to refuse.

`dashboard/lease.py` is that record, and it is checked here from both sides:

  * The module's own rules, driven with an explicit clock, because the interesting part of
    an expiry is a thing that only happens ten minutes in and a test that sleeps is a test
    nobody runs. A person is never refused; an agent is refused *while* a person holds it,
    in the words `human_has_control` with the two ways back; the hold renews on every
    keystroke rather than on the first one, so typing for two minutes does not hand the
    keyboard out at the ten-minute mark; `force` is the one escape hatch and it is reported.
  * The wiring, because a rule nothing consults is a rule that does not exist: the agent
    tool's `type` op has to arrive as `who="agent"`, the panel's door has to arrive as a
    person, the refusal has to land *before* anything is sent to the guest, and the frame
    the panel polls has to carry who is driving so the indicator has something to draw.

The check that matters most is the one about the guest: a refused keystroke must not reach
it. That is asserted against a fake screen that counts what it was given, because
`ok: false` in the answer and an event still on the wire is exactly the failure this file
exists to catch.

Run with `env -u PYTHONPATH -u ATHENA_HOME -u ATHENA_PORT` from this directory.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# the module, driven with an explicit clock
# ---------------------------------------------------------------------------
print("== dashboard/lease.py: who may type ==")
sys.path.insert(0, str(PLUGIN / "dashboard"))
import lease  # noqa: E402

lease.forget()
check("nobody holds a machine nobody has touched", lease.owner("rig") is None)
check("so an agent may type at it", lease.check("rig", lease.AGENT) is None)
check("and a person may too", lease.check("rig", lease.PERSON) is None)

hold = lease.hold("rig", lease.PERSON, now=1000.0)
check("a person's input takes the hold", hold.get("who") == lease.PERSON, str(hold))
check("it says since when, in wall-clock words a person reads",
      bool(hold.get("since_wall")), str(hold))
check("and it counts the hand-over", hold.get("takes") == 1, str(hold))

refused = lease.check("rig", lease.AGENT, now=1001.0)
check("an agent typing while a person holds it is refused", isinstance(refused, dict), str(refused))
check("refused in the words human_has_control", (refused or {}).get("error") == "human_has_control",
      str(refused))
check("the refusal names the machine", (refused or {}).get("machine") == "rig", str(refused))
check("it says who has it", (refused or {}).get("who") == lease.PERSON, str(refused))
check("it says how long they have had it", (refused or {}).get("for_seconds") is not None,
      str(refused))
check("it says when it frees itself", (refused or {}).get("expires_in_seconds") is not None,
      str(refused))
check("and it gives both ways to take it back",
      "Release" in str((refused or {}).get("release_with"))
      and "force" in str((refused or {}).get("release_with")), str(refused))
check("the sentence explains why rather than scolding",
      "two hands" in str((refused or {}).get("note")), str(refused))

check("a person is never refused, even while a person holds it",
      lease.check("rig", lease.PERSON, now=1001.0) is None)
check("a person takes it from an agent",
      lease.check("rig", lease.PERSON, now=1001.0) is None)

# Renewal: the expiry is measured from the last keystroke, not from the first one.
lease.forget()
lease.hold("rig", lease.PERSON, now=1000.0)
lease.hold("rig", lease.PERSON, now=1300.0)
check("a keystroke renews the hold rather than starting a new one",
      (lease.owner("rig", 1300.0) or {}).get("takes") == 1, str(lease.owner("rig", 1300.0)))
check("typing for five minutes does not hand the keyboard out at ten",
      lease.owner("rig", 1401.0) is not None)
check("and it does lapse ten minutes after the last keystroke",
      lease.owner("rig", 1901.0) is None)

# Expiry from a single hold, and what an agent sees after it.
lease.forget()
lease.hold("rig", lease.PERSON, now=0.0)
check("a hold with nobody touching the machine lapses on its own",
      lease.owner("rig", lease.HOLD_SECONDS) is None)
check("after it lapses an agent may type again",
      lease.check("rig", lease.AGENT, now=lease.HOLD_SECONDS) is None)

# force — the one escape hatch.
lease.forget()
lease.hold("rig", lease.PERSON, now=500.0)
check("force clears a person's hold", lease.check("rig", lease.AGENT, now=501.0, force=True) is None)
check("and the hold really is gone afterwards", lease.owner("rig", 501.0) is None)
check("force on a free machine changes nothing",
      lease.check("rig", lease.AGENT, now=501.0, force=True) is None)

# release — the panel's own control.
lease.forget()
lease.hold("rig", lease.PERSON, now=600.0)
back = lease.release("rig", now=601.0)
check("release hands the keyboard back", back.get("ok") and lease.owner("rig", 601.0) is None,
      str(back))
check("it reports who had it", (back.get("held") or {}).get("who") == lease.PERSON, str(back))
check("releasing a machine nobody held is not an error",
      lease.release("rig", now=601.0).get("ok") and lease.release("rig").get("held") is None,
      str(lease.release("rig")))

# state() — what the panel's indicator is drawn from.
lease.forget()
lease.hold("alpha", lease.AGENT, now=700.0)
lease.hold("beta", lease.PERSON, now=700.0)
live = lease.state(700.0)
check("state lists live holds by machine", sorted(live) == ["alpha", "beta"], str(sorted(live)))
check("a lapsed hold is not listed",
      lease.state(700.0 + lease.HOLD_SECONDS) == {}, str(lease.state(700.0 + lease.HOLD_SECONDS)))

held_copy = lease.owner("alpha", 700.0) or {}
held_copy["who"] = "somebody else"
check("owner() answers with a copy, so a caller cannot hold this module's state",
      (lease.owner("alpha", 700.0) or {}).get("who") == lease.AGENT, str(lease.owner("alpha", 700.0)))
check("a hold with no machine is refused rather than filed under nothing",
      lease.hold("", lease.PERSON).get("ok") is False, str(lease.hold("", lease.PERSON)))
lease.forget()
check("forget() drops every record", lease.state() == {}, str(lease.state()))


# ---------------------------------------------------------------------------
# the wiring: the tool, the panel, and the guest nobody refuses after the fact
# ---------------------------------------------------------------------------
print("\n== the tool and the panel take the same door ==")
sys.path.insert(0, str(PLUGIN / "dashboard"))
spec = importlib.util.spec_from_file_location("athena_plugin_lease", str(PLUGIN / "__init__.py"))
A = importlib.util.module_from_spec(spec)
sys.modules["athena_plugin_lease"] = A
spec.loader.exec_module(A)
api = A._load_api()

first = api._lease_module()
check("plugin_api loads the lease module", first is not None)
check("loading it twice is loading it once — one dictionary, not two",
      api._lease_module() is first)
check("and it is the file on disk, not a copy",
      Path(str(first.__file__)).name == "lease.py", str(getattr(first, "__file__", "")))

# A fake screen, so the claim "the guest was not sent to" can be counted rather than
# inferred from a return value.
class FakeScreen:
    def __init__(self):
        self.sent = []

    def session_for(self, name, host, port, drop=False):
        outer = self

        class Session:
            def input(self, events):
                outer.sent.append(list(events))
                return {"sent": len(events), "keys": len(events), "dropped": []}

        return Session()


fake_screen = FakeScreen()
real_screen = api.athena_screen
api.athena_screen = fake_screen

# A machine that says it is running and has a screen, without a hypervisor anywhere near it.
real_machines = api.athena_machines
api.athena_machines = SimpleNamespace(
    load_spec=lambda home, name: SimpleNamespace(provider="qemu", name=name, vnc_port=5959),
    machine_dir=lambda home, name: Path(home) / "machines" / name,
    qemu_running=lambda mdir: True,
    resolve_display=lambda s: "panel",
    vnc_tcp_port=lambda s: 5959,
    # The lease harness only cares that a keystroke reaches the right screen, so its
    # stand-in machine answers with the plain address: this stub exists to record what
    # the route dials, not to reproduce how a real one is named.
    vnc_endpoint=lambda folder, s: "127.0.0.1",
)

host = api.machines_host
first._HOLDS.clear()

r1 = host.screen_input("rig", [{"kind": "text", "text": "yes\n"}], who="agent")
check("an agent can type at a screen nobody is driving", r1.get("ok") is True, str(r1))
check("the guest got it", len(fake_screen.sent) == 1, str(fake_screen.sent))
check("and the answer says an agent is driving now",
      (r1.get("lease") or {}).get("who") == "agent", str(r1.get("lease")))

r2 = host.screen_input("rig", [{"kind": "key", "key": "Enter"}], who="person")
check("a person's keystroke goes through while an agent holds it", r2.get("ok") is True, str(r2))
check("and it takes the hold", (r2.get("lease") or {}).get("who") == "person", str(r2.get("lease")))
check("with the hand-over counted", (r2.get("lease") or {}).get("takes") == 2,
      str(r2.get("lease")))

before = len(fake_screen.sent)
r3 = host.screen_input("rig", [{"kind": "key", "key": "Escape"}], who="agent")
check("an agent is refused while a person holds the keyboard", r3.get("ok") is False, str(r3))
check("in the words human_has_control", r3.get("error") == "human_has_control", str(r3))
check("THE GUEST WAS NOT SENT TO — the refused keystroke never left",
      len(fake_screen.sent) == before, f"{before} -> {len(fake_screen.sent)}")
check("the refusal still carries the note and the ways back",
      bool(r3.get("note")) and bool(r3.get("release_with")), str(r3))

# The control. "Not sent to" is only evidence if something can be sent to: with no lease
# consulted, that same keystroke reaches the guest — which is exactly what every call did
# before this file existed, and what the refusal above is measured against.
_real_lease_module = api._lease_module
api._lease_module = lambda: None
try:
    loose = host.screen_input("rig", [{"kind": "key", "key": "Escape"}], who="agent")
finally:
    api._lease_module = _real_lease_module
check("control — with no lease consulted, the same keystroke DOES reach the guest",
      loose.get("ok") is True and len(fake_screen.sent) == before + 1,
      f"{loose} sent={len(fake_screen.sent)}")

r4 = host.screen_input("rig", [{"kind": "key", "key": "Escape"}], who="agent", force=True)
check("force gets the keystroke through", r4.get("ok") is True, str(r4))
check("and the answer says who has it now", (r4.get("lease") or {}).get("who") == "agent",
      str(r4.get("lease")))
check("a keyboard taken from a person is reported, not silent",
      "person" in str((r4.get("lease") or {}).get("note")), str(r4.get("lease")))

who = host.screen_lease("rig")
check("screen_lease reports the holder", (who.get("lease") or {}).get("who") == "agent", str(who))
check("and lists what else is held", "others" in who, str(who))
back_again = host.screen_lease("rig", release=True)
check("release is a route the panel can call", back_again.get("ok") is True, str(back_again))
check("afterwards nobody holds it", (back_again.get("lease")) is None, str(back_again))

# The tool: op=type has to arrive as an agent, op=lease has to reach the lease.
seen: list = []


class StubHost:
    def screen_input(self, name, events, who="person", force=False):
        seen.append(("type", who, force))
        return {"ok": True, "name": name, "sent": len(events), "lease": {"who": who}}

    def screen_lease(self, name, release=False):
        seen.append(("lease", release))
        return {"ok": True, "name": name, "lease": {"who": "name" if release else "person"},
                "others": {}}


api.machines_host = StubHost()


def tool(params: dict) -> dict:
    """An agent tool answers with a JSON string — decode it like the agent sees it."""
    import json
    return json.loads(A.handle_machines(params))


out = tool({"op": "type", "name": "rig", "events": [{"kind": "key", "key": "Enter"}]})
check("the agent tool's type op succeeds", out.get("success") is True, str(out)[:200])
check("and it arrives as an agent, which is what makes it refusable",
      seen and seen[-1] == ("type", "agent", False), str(seen))
out = tool({"op": "type", "name": "rig", "events": [], "force": True})
check("force travels from the tool", seen[-1] == ("type", "agent", True), str(seen))
out = tool({"op": "lease", "name": "rig"})
check("the lease op answers", out.get("success") is True and bool(out.get("lease")), str(out)[:200])
out = tool({"op": "lease", "name": "rig", "release": True})
check("and its release half does", seen[-1] == ("lease", True), str(seen))

schema = A.SCHEMAS["machines"]["parameters"]["properties"]
check("the tool's schema offers force, so an agent can ask for it",
      "force" in schema, sorted(schema))
check("and lease is an op it knows", "lease" in str(schema["op"].get("description")),
      str(schema["op"]))
check("the known ops are enforced against that list",
      tool({"op": "leasehold", "name": "rig"}).get("success") is False)
api.machines_host = host

# The routes: the panel's two calls have to exist, and the input door is the person's.
paths = {getattr(r, "path", "") for r in api.router.routes}
check("the panel has a route for who is driving", "/machines/{name}/lease" in paths, sorted(paths))
check("and one for handing it back", "/machines/{name}/lease/release" in paths, sorted(paths))
check("the panel's input door is the person's — it does not pass a `who`",
      "machines_host.screen_input(name, body.events))" in
      (PLUGIN / "dashboard" / "plugin_api.py").read_text(encoding="utf-8"))

# The frame. The panel polls `/screen` and draws its indicator from the same payload, so
# the lease has to ride along with the picture rather than cost a second request a second.
frame_src = api.machines_host.__class__.screen
check("the screen frame carries the lease for the panel's indicator",
      "self.screen_lease(name)" in
      __import__("inspect").getsource(frame_src), "screen() no longer reports the lease")

api.athena_screen = real_screen
api.athena_machines = real_machines
first.forget()

print(f"\n{PASS}/{PASS + FAIL} checks passed")
if FAILURES:
    print("failing: " + "; ".join(FAILURES))
sys.exit(1 if FAIL else 0)
