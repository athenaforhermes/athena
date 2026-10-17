"""Naming why an app stopped — the cause, and the one action that answers it.

A failed run leaves a log, and the log nearly always already says what happened:
in a sentence written by PyTorch, by Windows, or by ComfyUI's own folder lookup.
What the log cannot say is what to do about it, and that is the only part a person
cannot get from reading. So this reads the text and answers two things — the
cause, and the Athena-shaped action that addresses it.

Every pattern here was taken from output this machine actually produced, not from
memory (see `tests/live/t_triage.py`, which asserts against those captured
strings):

- `torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 111.76 GiB. GPU 0
  has a total capacity of 31.82 GiB of which 30.26 GiB is free.` — real, from the
  RTX 5090 through ComfyUI's own interpreter.
- `OSError: [WinError 10048] Only one usage of each socket address
  (protocol/network address/port) is normally permitted` — real, from two binds on
  Windows (a Linux box says `Address already in use` instead, so both are matched).
- `FileNotFoundError: Model in folder 'checkpoints' with filename 'x.safetensors'
  not found.` — ComfyUI's own sentence, read out of its source on disk.
- `Access to model meta-llama/Llama-2-7b is restricted. You must have access to it
  and be authenticated to access it.` — HuggingFace's real 401 body for a gated
  repository.

Two rules keep this honest. **Nothing is guessed from a symptom**: a match has to
name itself in the log, and when nothing matches this says so and hands back the
tail rather than inventing a cause. And **a cause without a fix is half an
answer**, so every entry carries the action, phrased as a call the caller can make
— and `fix.action` is deliberately one of a small set the panel already knows how
to perform (`point_models`, `stop_app`, `fix_app`, `credentials`, `shelf`).

Standard library only: this is loaded into the Hermes backend process, where a
missing import is a broken plugin.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# How much of a log is worth reading. A crashed app can leave megabytes, and the
# answer is always near the end; scanning from the tail keeps this cheap on a
# route the panel can call while a person is looking at it.
SCAN_BYTES = 400_000
TAIL_LINES = 12

# Where a line's meaning is decided. Ordered by specificity, not by likelihood:
# a log often carries several of these (a CUDA OOM inside a Gradio handler nests a
# traceback inside a traceback), and the most specific reading is the useful one.
_SIGS: List[Dict[str, Any]] = [
    {
        "id": "cuda_oom",
        "title": "The graphics card ran out of memory",
        "find": re.compile(
            r"torch\.OutOfMemoryError: CUDA out of memory"
            r"|CUDA out of memory"
            r"|CUDA_ERROR_OUT_OF_MEMORY"
            r"|CUBLAS_STATUS_ALLOC_FAILED"),
        "why": None,  # filled in below, it has numbers to extract
        "fix": None,
    },
    {
        "id": "missing_weights",
        "title": "A model file it needs is not where it is looking",
        "find": re.compile(r"Model in folder '(?P<folder>[^']+)' with filename "
                           r"'(?P<file>[^']+)' not found"),
        "why": None,
        "fix": None,
    },
    {
        "id": "missing_weight_file",
        "title": "A model file it needs is not where it is looking",
        "find": re.compile(r"(?:FileNotFoundError|No such file or directory|Error)"
                           r"[^\n]*?[\w./\\-]+\.(?:safetensors|ckpt|pth|pt|bin|gguf|onnx)"),
        "why": None,
        "fix": None,
    },
    {
        "id": "port_busy",
        "title": "Something is already listening on the port it wants",
        "find": re.compile(r"WinError 10048|Address already in use|EADDRINUSE"
                           r"|errno 10048|Failed to bind|is already in use"),
        "why": None,
        "fix": None,
    },
    {
        "id": "out_of_ram",
        "title": "The machine ran out of memory (not the card — the RAM)",
        # `\b` on both sides: "MemoryError" is a substring of PyTorch's own
        # "OutOfMemoryError", and matching that would read every CUDA OOM on this
        # machine as a RAM failure (a real defect this file's tests now hold down).
        "find": re.compile(r"WinError 1455|paging file is too small|\bMemoryError\b"
                           r"|Killed\s*$|WinError 8\b|Cannot allocate memory"),
        "why": None,
        "fix": None,
    },
    {
        "id": "out_of_disk",
        "title": "The disk filled up",
        "find": re.compile(r"WinError 112|not enough space on the disk"
                           r"|No space left on device|ENOSPC"),
        "why": None,
        "fix": None,
    },
    {
        "id": "missing_package",
        "title": "The app's environment is missing a package it imports",
        "find": re.compile(r"ModuleNotFoundError: No module named '(?P<mod>[^']+)'"
                           r"|ImportError: No module named (?P<mod2>[\w.]+)"),
        "why": None,
        "fix": None,
    },
    {
        "id": "gated_weights",
        "title": "That model is gated, and this token cannot reach it",
        "find": re.compile(r"You must have access to it and be authenticated"
                           r"|GatedRepoError|Cannot access gated repo"
                           r"|Access to model [^\n]+ is restricted"
                           r"|401 Client Error: Unauthorized"),
        "why": None,
        "fix": None,
    },
    {
        "id": "cuda_build_mismatch",
        "title": "Its PyTorch build does not match this card or driver",
        "find": re.compile(r"CUDA driver version is insufficient"
                           r"|no kernel image is available for execution"
                           r"|Torch not compiled with CUDA enabled"
                           r"|no CUDA-capable device is detected"),
        "why": None,
        "fix": None,
    },
    {
        "id": "network",
        "title": "It could not reach the network",
        "find": re.compile(r"Max retries exceeded|Failed to establish a new connection"
                           r"|Temporary failure in name resolution|SSLError"
                           r"|WinError 10054|Connection reset by peer"),
        "why": None,
        "fix": None,
    },
]

# Two signatures can describe one failure, and the less precise one is not extra
# information — it is the same reading the headline already gave. A generic
# `FileNotFoundError ... .safetensors` under a headline of ComfyUI's own sentence
# naming the folder and the file is noise, so it is folded away.
_ALIASES = {"missing_weight_file": "missing_weights"}

_GIB = re.compile(r"Tried to allocate ([\d.]+) (GiB|MiB)")
_CAP = re.compile(r"total capacity of ([\d.]+) GiB of which ([\d.]+) GiB is free")
_PORT = re.compile(r"port (\d{2,5})", re.IGNORECASE)


def _num(m: Optional[re.Match], group: int) -> Optional[float]:
    try:
        return float(m.group(group))
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _say_why(sig: Dict[str, Any], m: re.Match, text: str,
             ctx: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """The sentence a person reads, and the action that answers it.

    Returned together because they are one thought here: the numbers that make the
    cause specific are the same numbers that decide which fix is the right one. An
    out-of-memory that asked for more than the card has is not fixed by closing
    anything; one where the card was nearly full is.
    """
    running = [str(x) for x in (ctx.get("running") or [])]
    others = [a for a in running if a != ctx.get("app")]

    if sig["id"] == "cuda_oom":
        tried = _num(_GIB.search(text), 1)
        unit = (_GIB.search(text).group(2) if _GIB.search(text) else "GiB")
        cap = _CAP.search(text)
        total, free = _num(cap, 1), _num(cap, 2)
        want = f"{tried:.2f} {unit}" if tried else "more than the card has"
        if cap:
            why = (f"the work asked the card for {want}; it holds {total:.2f} GiB "
                   f"with {free:.2f} GiB free.")
        else:
            why = f"the work asked the card for {want}."
        # The distinction that changes the answer: a card that is mostly *busy* is
        # a different problem from a request that was simply too big.
        busy = cap and total and free is not None and free < total * 0.6
        if busy and others:
            # Two answers, because they are both correct and they differ in what they
            # cost: stopping the neighbour frees the card now and throws that work
            # away; waiting starts this one when the neighbour is done by itself.
            # Which is right depends on whether the neighbour is minutes from
            # finishing, which the log cannot know — so both are offered, ordered by
            # what is usually meant first.
            return (why + f" {', '.join(others)} {'is' if len(others) == 1 else 'are'} "
                    "holding the card, so this ran out beside them.",
                    {"action": "stop_app", "label": "Free the card",
                     "detail": f"stop {others[0]} and start this app again — the card "
                               "has the memory, something else was using it",
                     "apps": others,
                     "alt": {"action": "wait",
                             "label": "or wait for the card",
                             "detail": f"leave {others[0]} alone and start this one on "
                                       "its own when the card has room — the wait runs "
                                       "as a job you can watch and take back"}})
        if busy:
            return (why + " Something outside Athena is holding the card.",
                    {"action": "none", "label": "",
                     "detail": "check what else is using the GPU (another session, a "
                               "browser tab running a model) and try again"})
        return (why + " Nothing else is running here, so the request itself is larger "
                "than this card can hold.",
                {"action": "none", "label": "",
                 "detail": "ask for less in the app: a smaller batch, fewer frames, a "
                           "lower resolution, or a smaller quantised weight"})

    if sig["id"] in ("missing_weights", "missing_weight_file"):
        folder = (m.groupdict().get("folder") or "") if sig["id"] == "missing_weights" else ""
        name = m.groupdict().get("file") or ""
        if not name:
            found_file = re.search(r"[\w./\\-]+\.(?:safetensors|ckpt|pth|pt|bin|gguf|onnx)",
                                   m.group(0))
            name = found_file.group(0) if found_file else ""
        where = f"in its '{folder}' folder" if folder else "wherever it looks"
        why = (f"it went looking for {name or 'a weight file'} {where} and found nothing "
               "there. A missing weight is not breakage — the app is fine and has "
               "nothing to load.")
        return (why, {"action": "point_models",
                      "label": "Point this app at a folder that has it",
                      "detail": "this machine already knows which libraries hold "
                                "weights; if one of them has this file, pointing the "
                                "app at that folder is the whole fix, and if none "
                                "does the pull is the next step",
                      "file": name, "folder": folder})

    if sig["id"] == "port_busy":
        pm = _PORT.search(text)
        where = f"port {pm.group(1)}" if pm else "its port"
        why = (f"{where} was already taken when it tried to listen. Either this app is "
               "already running, or a process Athena did not start is holding it.")
        fix = ({"action": "stop_app", "label": "Stop this app and start it again",
                "detail": "if it was already running, one copy owns the port and the "
                          "second start is the failure; stopping clears it",
                "apps": [ctx.get("app")] if ctx.get("app") else []}
               if ctx.get("app") else
               {"action": "none", "label": "",
                "detail": "find what holds the port (netstat -ano | findstr :PORT on "
                          "Windows) and end it, or change the port the app uses"})
        return (why, fix)

    if sig["id"] == "out_of_ram":
        holding = ", ".join(others) if others else "nothing of Athena's is running"
        return ("the machine ran out of RAM, not the card — Windows killed it, or Python "
                "raised MemoryError while building something in system memory. This is a "
                "different ceiling from VRAM.",
                {"action": "stop_app", "label": "Free memory",
                 "detail": f"stop what is using it ({holding}) and try again with a "
                           "smaller request",
                 "apps": others})

    if sig["id"] == "out_of_disk":
        return ("the disk it was writing to is full — Windows reports WinError 112, "
                "Linux reports ENOSPC.",
                {"action": "shelf", "label": "Show what is filling the disk",
                 "detail": "the weights shelf weighs every library and names the ones "
                           "nothing reads; that is almost always where the space went"})

    if sig["id"] == "missing_package":
        mod = m.groupdict().get("mod") or m.groupdict().get("mod2") or "a package"
        why = (f"the app's environment has no '{mod}'. Guessing at the package name is "
               "how an environment gets quietly broken: the app's own installer knows "
               "its list.")
        return (why, {"action": "fix_app", "label": "Repair this app's environment",
                      "detail": f"re-run the app's own install script, which knows it "
                                f"needs '{mod}' and which version",
                      "module": mod})

    if sig["id"] == "gated_weights":
        repo = ""
        rm = re.search(r"Access to model ([\w.\-/]+) is restricted", text) or \
            re.search(r"repository\s+([\w.\-/]+)", text)
        if rm:
            repo = rm.group(1)
        why = (f"{repo + ' is ' if repo else 'that repository is '}gated: your "
               "HuggingFace account has to have accepted its licence, and the token "
               "has to carry that access. Athena shares Hermes' own token with an app, "
               "so one accepted licence fixes it everywhere.")
        return (why, {"action": "credentials", "label": "Check the shared credentials",
                      "detail": "accept the model's licence on HuggingFace with the "
                                "account the token belongs to, then pull it here",
                      "repo": repo})

    if sig["id"] == "cuda_build_mismatch":
        return ("the PyTorch build inside this app does not match this card or driver. "
                "The card is fine and the code is fine; the two were never built for "
                "each other.",
                {"action": "fix_app", "label": "Repair this app's environment",
                 "detail": "a matching build has to be installed into the app's own "
                           "environment — the app's installer picks the right wheel for "
                           "its Python"})

    return ("it could not reach the network (or the far end dropped it). Nothing is "
            "wrong with the app or the machine.",
            {"action": "none", "label": "", "detail": "try again; if it repeats, check "
                                                      "the connection and whether the "
                                                      "host is up"})


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def triage(text: str, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Name what went wrong in a log, and the action that answers it.

    `ctx` carries machine facts the text cannot know — `app` (which app this log
    belongs to) and `running` (what else is up right now). They change the answer:
    an out-of-memory warning with a listed neighbour is a card being held, and the
    same warning alone is a request that was too big.
    """
    ctx = dict(ctx or {})
    text = text or ""
    if len(text) > SCAN_BYTES:
        text = text[-SCAN_BYTES:]

    hits: List[Tuple[int, int, re.Match]] = []
    for order, sig in enumerate(_SIGS):
        for m in sig["find"].finditer(text):
            hits.append((m.start(), order, m))

    # The most *specific* reading wins, not simply the last line: one CUDA OOM
    # nests a traceback inside a traceback, and several signatures match the same
    # sentence. `_SIGS` is written most-specific-first, so its order is the rank;
    # among matches of the same signature the later one wins, because a failure
    # that repeated is the one that ended the run.
    hits.sort(key=lambda h: (h[1], -h[0]))

    lines = [l for l in text.splitlines() if l.strip()]
    scanned = {"lines": len(lines), "bytes": len(text.encode("utf-8", "replace"))}

    if not hits:
        return {"found": False, "cause": None, "title": None,
                "why": "Nothing in this log matches a failure Athena knows how to "
                       "name, so there is no cause here to give you — the tail is the "
                       "answer.",
                "fix": None, "evidence": None, "also": [],
                "scanned": scanned, "tail": lines[-TAIL_LINES:]}

    best_pos, best_order, best_m = hits[0]
    sig = _SIGS[best_order]
    why, fix = _say_why(sig, best_m, text, ctx)

    # Everything else that also matched, named but not acted on: a log that carries
    # a CUDA OOM *and* a missing import is usually one run of two problems, and
    # hiding the second one is how a fix stops working the next time.
    also: List[Dict[str, Any]] = []
    seen = {sig["id"]}
    # Listed latest-first: these are other readings, and the most recent one is the
    # one that came after the chosen cause, so it reads in the order it happened.
    for pos, order, m in sorted(hits[1:], key=lambda h: -h[0]):
        s = _SIGS[order]
        if s["id"] in seen or _ALIASES.get(s["id"], s["id"]) == sig["id"]:
            continue
        seen.add(s["id"])
        also.append({"cause": s["id"], "title": s["title"],
                     "line": _line_of(text, pos),
                     "text": m.group(0).strip()[:300]})

    return {
        "found": True,
        "cause": sig["id"],
        "title": sig["title"],
        "why": why,
        "fix": fix,
        "evidence": {"line": _line_of(text, best_pos),
                     "text": best_m.group(0).strip()[:400]},
        "also": also[:4],
        "scanned": scanned,
        "tail": lines[-TAIL_LINES:],
    }
