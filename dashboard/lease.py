"""Who is driving a machine's screen: a person at the panel, or an agent acting for one.

Until this existed, both could drive one guest and nothing arbitrated. An agent typing an
installer's answers and a person moving the mouse at the same panel were two writers on one
keyboard: the agent's "Enter" arrived after the person clicked something else, and the guest
acted on a combination neither of them meant. Nobody was lying about it — there was simply
no record of who had the keyboard, so there was nothing to refuse.

So there is a record now, one per machine, and it is deliberately not the screen connection:

  * A connection can be reaped (`screen.SESSION_IDLE_SECONDS`) or die with the guest, and a
    person who is still sitting there watching the installer should not lose the keyboard
    because a socket aged out. The hold is about the machine, not about the socket.
  * A person's input always goes through, and takes the hold. Someone at the panel is the
    owner of this computer; there is no version of this where their keystroke is dropped and
    an agent's is not.
  * While a person holds it, an agent's input is refused — `human_has_control`, in those
    words, with how long they have had it and how to get it back. Not queued, not sent
    anyway: the guest would act on either. This is the same shape as every other refusal in
    this plugin (a nameless field, a path outside the roots, a licence outside the fence):
    the caller is told why, and what to do instead.
  * The hold expires on its own after `HOLD_SECONDS` with nobody touching the machine at all,
    because a person who closed the panel and walked away should not leave an agent locked
    out of a guest for the rest of the afternoon. Ten minutes is longer than any hand-back
    should need and shorter than a coffee.
  * `force` releases a person's hold and says so in the answer, because the agent here is the
    person's own tool and the alternative — an agent that must wake somebody up to press Enter
    — is worse. It is the one escape hatch, and it is reported rather than silent.

The clock is a parameter everywhere so this is testable without sleeping: the whole point of
an expiry is a thing that only happens ten minutes in.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# The two names a holder can have. Not an enum: they are printed in refusals, and the panel
# reads them, so they are strings that a person can read back.
PERSON = "person"
AGENT = "agent"

# How long a hold lasts with nobody touching the machine at all. See the module docstring.
HOLD_SECONDS = 600.0

_HOLDS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _stamp(seconds: float) -> str:
    """A wall-clock time a person can read, for the panel — not what the expiry is decided on."""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):  # pragma: no cover — defensive
        return ""


def _live(machine: str, now: float) -> Optional[Dict[str, Any]]:
    """The hold on a machine, if there is one that has not lapsed. Never touches the lock."""
    hold = _HOLDS.get(machine)
    if not hold:
        return None
    if now - float(hold.get("seen") or 0.0) >= HOLD_SECONDS:
        return None
    return hold


def owner(machine: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Who holds a machine's screen right now, or None if nobody does.

    The answer is a copy, so a caller cannot hold this module's state by accident, and it is
    the shape the panel and the tool both report: who, since when, how long, and whether it is
    about to lapse.
    """
    moment = time.monotonic() if now is None else float(now)
    with _LOCK:
        hold = _live(str(machine or ""), moment)
        if hold is None:
            return None
        out = dict(hold)
    out["for_seconds"] = round(moment - float(out.get("since") or moment), 1)
    out["expires_in_seconds"] = round(HOLD_SECONDS - (moment - float(out.get("seen") or moment)), 1)
    out.pop("seen", None)
    return out


def state(now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Every live hold, by machine — what a panel draws an indicator from."""
    moment = time.monotonic() if now is None else float(now)
    with _LOCK:
        machines = [k for k in _HOLDS if _live(k, moment)]
    return {m: owner(m, moment) for m in sorted(machines) if owner(m, moment)}


def refusal(machine: str, hold: Dict[str, Any]) -> Dict[str, Any]:
    """The answer to an agent that tried to type while a person had the keyboard.

    Every field is here because a caller needs it: the code to branch on, the sentence to show,
    who has it and for how long, when it lapses by itself, and the two ways to get it back.
    """
    who = str(hold.get("who") or PERSON)
    seconds = int(float(hold.get("for_seconds") or 0))
    for_text = f"{seconds}s" if seconds < 120 else f"{round(seconds / 60)}m"
    return {
        "ok": False,
        "error": "human_has_control",
        "machine": machine,
        "who": who,
        "since": hold.get("since_wall") or "",
        "for_seconds": hold.get("for_seconds"),
        "expires_in_seconds": hold.get("expires_in_seconds"),
        "release_with": ("the Release control in this machine's Screen tab, or "
                         "op=type with force=true"),
        "note": (f"a person has been using {machine}'s screen for {for_text}, so this "
                 f"keystroke was not sent: they and I would be two hands on one keyboard, "
                 f"and the guest acts on whatever arrives. It frees itself in "
                 f"{int(float(hold.get('expires_in_seconds') or 0))}s without anyone "
                 f"touching it, or take it now with force=true."),
    }


def check(machine: str, who: str = AGENT, now: Optional[float] = None,
          force: bool = False) -> Optional[Dict[str, Any]]:
    """Whether `who` may type at this machine's screen. None means yes.

    An answer means no, and it is the whole refusal (`refusal()`), ready to be handed back.
    A person is never refused: their keystroke outranks any agent's, which is the point.
    """
    want = str(who or AGENT).strip().lower()
    moment = time.monotonic() if now is None else float(now)
    name = str(machine or "")
    with _LOCK:
        hold = _live(name, moment)
        if hold is None or want != AGENT or str(hold.get("who")) == AGENT:
            return None
        if force:
            _HOLDS.pop(name, None)
            return None
        live = dict(hold)
    live["for_seconds"] = round(moment - float(live.get("since") or moment), 1)
    live["expires_in_seconds"] = round(HOLD_SECONDS - (moment - float(live.get("seen") or moment)), 1)
    return refusal(name, live)


def hold(machine: str, who: str = PERSON, note: str = "",
         now: Optional[float] = None) -> Dict[str, Any]:
    """Note that `who` is driving this machine's screen, and answer with the new state.

    Called on every input rather than on the first one, because "still driving" is what the
    expiry is measured from — a person typing for two minutes must not have their keyboard
    handed out from under them at the ten-minute mark of a single keystroke.
    """
    moment = time.monotonic() if now is None else float(now)
    name = str(machine or "")
    want = str(who or PERSON).strip().lower()
    if not name:
        return {"ok": False, "error": "a hold needs a machine"}
    with _LOCK:
        was = _live(name, moment)
        record = dict(was or {})
        if was is None or str(record.get("who")) != want:
            # A change of hands: the clock starts here, and the wall-clock stamp is kept
            # because "since 15:04" is what a person reads, not a monotonic float.
            record = {"who": want, "since": moment, "since_wall": _stamp(time.time()),
                      "takes": int(record.get("takes") or 0) + 1}
        record["seen"] = moment
        if note:
            record["note"] = str(note)
        _HOLDS[name] = record
    return owner(name, moment) or {}


def release(machine: str, now: Optional[float] = None) -> Dict[str, Any]:
    """Hand the keyboard back, whoever holds it. Called by the panel's own control.

    Unconditional on purpose: this is the person's escape hatch from an agent that is typing,
    and asking them to prove who they are at their own desk is not a security boundary, it is
    an annoyance.
    """
    moment = time.monotonic() if now is None else float(now)
    name = str(machine or "")
    with _LOCK:
        had = _HOLDS.pop(name, None)
    if not had:
        return {"ok": True, "machine": name, "held": None, "note": "nobody was holding it"}
    return {"ok": True, "machine": name, "held": {"who": had.get("who"),
                                                  "since": had.get("since_wall")},
            "now": owner(name, moment)}


def forget(machine: Optional[str] = None) -> None:
    """Drop the record for one machine, or for all of them. For a deleted machine and tests."""
    with _LOCK:
        if machine is None:
            _HOLDS.clear()
        else:
            _HOLDS.pop(str(machine), None)
