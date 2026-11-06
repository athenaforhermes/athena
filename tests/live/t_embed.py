"""An app that refuses to be framed, and the reason Athena can now give for it.

Framing is a rule about the page that owns the <iframe>, not about the app that
sits inside it, and the packaged desktop owns its frame from a `file://` document
— an opaque origin. An app whose policy allows only this machine's loopback
addresses and the launcher that started it therefore paints a white rectangle
while running perfectly, and nothing in the panel can discover why: the app is
cross-origin, so its own headers are unreadable there. The question is asked in
the backend instead, and this is what the answer has to get right:

  * the policy is read, not guessed — every `frame-ancestors` source across every
    policy header, since a second policy intersects the first rather than
    replacing it;
  * only a specific allow-list refuses the frame: no header at all, `*` and a
    `file:` scheme-source all leave it legal, and so does an address that answers
    nothing, where the frame's own error is the honest one;
  * HEAD carries the headers, and a server that refuses HEAD is asked once more
    with a GET, because refusing HEAD is not refusing the frame;
  * the answer is remembered, and expires: a poll every 2.5s that asked a
    streaming app each time would queue behind the app's own work.

Run with `env -u PYTHONPATH -u ATHENA_HOME` from this directory.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from email.message import Message
from pathlib import Path

# The Hermes data directory this harness runs against. Derived, with an
# override, rather than written down as one machine's home directory: the
# suite ships in the repo and has to run somewhere other than where it was born.
HERMES_ROOT = Path(os.environ.get("ATHENA_HERMES_ROOT")
                   or Path.home() / "AppData" / "Local" / "hermes")

PLUGIN = Path(str(HERMES_ROOT) + r"/plugins/athena")
DASH = PLUGIN / "dashboard"

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


# `runtime` imports its siblings by bare name (`naming`), because in production it
# is loaded by path out of a directory that is not a package.
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))
runtime = load("athena_runtime_under_test", DASH / "runtime.py")

# Maestro's own policy, as served by the app that produced the white frame.
MAESTRO = ("default-src 'self'; frame-ancestors 'self' http://localhost:* "
           "https://localhost:* http://127.0.0.1:* https://127.0.0.1:* "
           "http://[::1]:* https://[::1]:* http://192.168.1.99:42001")
APP_URL = "http://127.0.0.1:42003/"


class _Resp:
    """The part of an HTTP response this code touches."""

    def __init__(self, policies):
        headers = Message()
        for value in policies:
            headers["Content-Security-Policy"] = value
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub(policies=(), refuse_head=False, calls=None):
    """A stand-in for urlopen that answers with these policy headers."""
    def urlopen(req, timeout=None):
        if calls is not None:
            calls.append(req.get_method())
        if refuse_head and req.get_method() == "HEAD":
            raise runtime.urllib.error.HTTPError(req.full_url, 405, "no HEAD", None, None)
        return _Resp(policies)
    return urlopen


def verdict(policies, refuse_head=False):
    """Ask for a verdict with the network replaced, and report what was asked."""
    runtime._FRAME_POLICY.clear()
    calls: list[str] = []
    real = runtime.urllib.request.urlopen
    runtime.urllib.request.urlopen = stub(policies, refuse_head, calls)
    try:
        return runtime.frame_policy(APP_URL), calls
    finally:
        runtime.urllib.request.urlopen = real
        runtime._FRAME_POLICY.clear()


print("what the policy says")
_one = runtime.frame_ancestors(["frame-ancestors 'self' http://127.0.0.1:*"])
check("its sources are read, in order", _one == ("'self'", "http://127.0.0.1:*"),
      f"got {_one}")
_among = runtime.frame_ancestors(["default-src 'none'; script-src 'self'; "
                                  "frame-ancestors 'self' http://127.0.0.1:*"])
check("and found among the directives it sits beside",
      _among == ("'self'", "http://127.0.0.1:*"),
      f"a directive after another must not be swallowed; got {_among}")
check("a second policy header adds to the first, because they intersect",
      runtime.frame_ancestors(["frame-ancestors 'self'", "frame-ancestors http://localhost:*"])
      == ("'self'", "http://localhost:*"),
      "a second policy must not be dropped or overwrite the first")
check("a policy that names no ancestors is no sources",
      runtime.frame_ancestors(["default-src 'self'"]) == (),
      "a directive that is absent is not a directive that allows nothing")
check("nothing to read is no sources",
      runtime.frame_ancestors([]) == () and runtime.frame_ancestors(None) == (),
      "a missing header list must not raise")

print("who may frame the app")
got, calls = verdict([MAESTRO])
check("an app that allows loopback and its launcher alone refuses this window",
      got.get("frameable") is False, f"got {got!r}")
check("and the reason names the address, the launcher and the way out",
      "loopback" in got.get("note", "") and "browser" in got.get("note", ""),
      f"the note must explain itself: {got.get('note')!r}")
check("the policy is quoted back, not paraphrased",
      got.get("policy", "").startswith("frame-ancestors 'self' http://localhost:*")
      and "192.168.1.99:42001" in got.get("policy", ""),
      f"got {got.get('policy')!r}")
check("a policy that allows anyone frames",
      verdict(["frame-ancestors *"])[0].get("frameable") is True,
      "`*` is every ancestor, including an opaque one")
check("a policy that allows file: frames",
      verdict(["frame-ancestors file:"])[0].get("frameable") is True,
      "a file: scheme-source is the one origin the desktop's own document has")
check("a policy that allows nobody still says so, rather than silently framing",
      verdict(["frame-ancestors 'none'"])[0].get("frameable") is False,
      "'none' must not be read as no policy")
check("no header at all frames", verdict([])[0].get("frameable") is True
      and verdict([])[0].get("policy") is None,
      "an app that says nothing is not refusing")

def unreachable(exc: Exception) -> dict:
    """The verdict for an address that refuses to answer at all."""
    runtime._FRAME_POLICY.clear()
    real = runtime.urllib.request.urlopen

    def urlopen(req, timeout=None):
        raise exc

    runtime.urllib.request.urlopen = urlopen
    try:
        return runtime.frame_policy(APP_URL)
    finally:
        runtime.urllib.request.urlopen = real
        runtime._FRAME_POLICY.clear()


check("an address that answers nothing is not a refusal",
      unreachable(runtime.urllib.error.URLError("connection refused")).get("frameable") is True
      and unreachable(TimeoutError("no answer")).get("frameable") is True,
      "the frame's own error is the honest one there, and a reason invented "
      "here would replace it")

got, calls = verdict([MAESTRO], refuse_head=True)
check("a server that refuses HEAD is asked once more with a GET",
      calls == ["HEAD", "GET"], f"got {calls}")
check("and its policy is still read", got.get("frameable") is False,
      "refusing HEAD is not refusing the frame")

runtime._FRAME_POLICY.clear()
real = runtime.urllib.request.urlopen
calls: list[str] = []
runtime.urllib.request.urlopen = stub([MAESTRO], calls=calls)
try:
    runtime.frame_policy(APP_URL)
    runtime.frame_policy(APP_URL)
finally:
    runtime.urllib.request.urlopen = real
    runtime._FRAME_POLICY.clear()
check("the answer is remembered, so a poll does not ask the app again",
      calls == ["HEAD"], f"two asks over one address fetched {calls}")

runtime._FRAME_POLICY.clear()
real, ttl = runtime.urllib.request.urlopen, runtime._FRAME_POLICY_TTL
calls: list[str] = []
runtime.urllib.request.urlopen = stub([MAESTRO], calls=calls)
runtime._FRAME_POLICY_TTL = 0.0
try:
    runtime.frame_policy(APP_URL)
    runtime.frame_policy(APP_URL)
finally:
    runtime.urllib.request.urlopen = real
    runtime._FRAME_POLICY_TTL = ttl
    runtime._FRAME_POLICY.clear()
check("and it expires, so a different app on that address is asked again",
      calls == ["HEAD", "HEAD"], f"with a spent TTL the answer must be re-read, got {calls}")

print("wired, end to end")
api = (PLUGIN / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
check("the route the panel already polls carries the verdict",
      '"embed": athena_runtime.frame_policy(url) if url else None,' in api,
      "the panel learns this from /apps/{id}/status, not from a second round trip")
js = (PLUGIN / "desktop" / "plugin.js").read_text(encoding="utf-8")
check("the panel reads it from that answer", "setEmbed(s.embed || null)" in js,
      "the poll's result must reach the viewer")
check("and frames only what will be framed",
      "sub === 'app' && url && !refused" in js,
      "the iframe must be withheld when the app refuses it")
check("a refused app gets a reason and a way out, not an empty rectangle",
      "will not display here." in js and "Open in browser" in js,
      "the purpose of the whole check is that this is never silent")
check("and the way out uses the SDK's own door",
      "const os = ctx && ctx.os" in js and "os.openExternal(url)" in js
      and "typeof host.openExternal" not in js and "if (host.openExternal)" not in js,
      "the call must be ctx.os.openExternal — a call shaped `host.openExternal` "
      "silently becomes window.open, which the desktop denies; prose naming the "
      "right door is not a call through it")

print()
print(f"t_embed: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("failed: " + "; ".join(FAILURES))
sys.exit(1 if FAIL else 0)
