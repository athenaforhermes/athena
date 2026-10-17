"""
The panel's screen: RFB, checked against a server this file writes itself.

A screenshot cannot be tested by taking one, so this suite speaks the protocol back at the
client — a tiny VNC server in a thread that offers what QEMU offers, answers the handshake,
and then reads whatever the client sends and hands it back for inspection. That makes the
parts that would otherwise need a booted guest provable in milliseconds: the pixel format
Athena asks for, how a rectangle lands in the canvas, the byte order (proved here with red,
which is the one colour a channel swap would turn blue), what a shifted character turns
into, and what a click puts on the wire.

A real guest is still the only way to prove a real guest draws. These checks are about the
half that is Athena's.

Usage: env -u PYTHONPATH <venv python> t_screen.py
"""
import io
import socket
import struct
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUG = HERE.parent.parent
sys.path.insert(0, str(PLUG / "dashboard"))

import screen  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


sys.path.insert(0, str(HERE))
from vncfake import FakeVnc, close, recvn, PFMT, _image_size, _png_size  # noqa: E402




# ---- the handshake, and what Athena asks the guest for ------------------------------

fake = FakeVnc(width=4, height=2)
fake.start()
frame = screen.read_frame("127.0.0.1", fake.port)
close(fake)

check("a frame comes back with the size the server announced",
      frame["ok"] and frame["width"] == 4 and frame["height"] == 2, str(frame)[:120])
check("... and the guest's own name for its screen is carried through",
      frame["name"] == "fake guest", frame["name"])
check("... and the pixels are exactly the size of the framebuffer, four bytes each",
      len(frame["pixels"]) == 4 * 2 * 4, len(frame["pixels"]))
check("it answers with a version the server offered, and 3.8 when there is a choice",
      fake.client_version == b"RFB 003.008\n", fake.client_version)
check("it asks for one shared session, so a second viewer is never evicted",
      fake.shared == b"\x01", repr(fake.shared))
check("it picks its own pixel format: 32 bits, 24 deep, blue in the low byte",
      fake.pixel_format == PFMT, repr(fake.pixel_format))
check("... which is the format PIL decodes directly, so no pixel is ever hand-swapped",
      frame["format"] == "BGRX", frame["format"])
check("it asks for plain rectangles to decode, and to be told when the guest resizes",
      fake.encodings == [0, -223], repr(fake.encodings))
check("... asking over the guest's whole possible screen, not over our own picture of it",
      fake.updates and fake.updates[0][-2:] == (65535, 65535), repr(fake.updates))
check("... and for a whole frame rather than an incremental one, when a session starts",
      fake.updates and fake.updates[0][1] == 0, repr(fake.updates))

# ---- colour: the one mistake a monochrome screenshot cannot reveal -------------------

w = 3
fake = FakeVnc(width=w, height=1, rects=[(0, 0, w, 1)])
fake.start()
png = screen.frame_png("127.0.0.1", fake.port)
close(fake)

from PIL import Image  # noqa: E402

img = Image.open(io.BytesIO(png["png"])).convert("RGB")
check("a frame becomes a PNG of the guest's size, not the padding's",
      img.size == (w, 1), str(img.size))
check("a red pixel sent in the red channel comes back red, and its neighbours too",
      img.getpixel((1, 0)) == (200, 0, 10), str(img.getpixel((1, 0))))
check("... so a channel swap would be caught here rather than in a person's screenshot",
      img.getpixel((2, 0)) == (200, 0, 20), str(img.getpixel((2, 0))))

# ---- rectangles: a partial update has to land where the server put it ----------------

fake = FakeVnc(width=4, height=4, rects=[(0, 0, 4, 4), (1, 1, 2, 1)])
fake.start()
wide = screen.read_frame("127.0.0.1", fake.port)
close(fake)

pixel = lambda x, y: wide["pixels"][(y * 4 + x) * 4:(y * 4 + x) * 4 + 4]  # noqa: E731
check("the first rectangle fills the canvas", pixel(3, 3) == bytes([30, 30, 200, 0]),
      repr(pixel(3, 3)))  # blue 30, green 30, red 200
check("a later rectangle lands at its own coordinates, not at the top left",
      pixel(1, 1) == bytes([10, 10, 200, 0]) and pixel(2, 1) == bytes([20, 10, 200, 0]),
      f"{pixel(1, 1)!r} {pixel(2, 1)!r}")
check("... and it does not smear across the row it was drawn in",
      pixel(3, 1) == bytes([30, 10, 200, 0]), repr(pixel(3, 1)))

# ---- what a screen that cannot be read says out loud ---------------------------------

dead = socket.socket()
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()
try:
    screen.read_frame("127.0.0.1", dead_port, timeout=2)
    check("a port with nothing on it is refused in words", False, "no error raised")
except screen.RfbError as e:
    check("a port with nothing on it is refused in words, not with a stack trace",
          "nothing is listening" in str(e), str(e))

bystander = socket.socket()
bystander.bind(("127.0.0.1", 0))
bystander.listen(1)
bystander_port = bystander.getsockname()[1]
threading.Thread(target=lambda: (bystander.accept()[0].sendall(b"HTTP/1.1 400 Bad\r\n"),
                                 time.sleep(0.1)), daemon=True).start()
try:
    screen.read_frame("127.0.0.1", bystander_port, timeout=3)
    check("a port that is not a VNC server is called out", False, "no error raised")
except screen.RfbError as e:
    check("a port running something else is named as not a VNC server",
          "not a VNC server" in str(e), str(e))
bystander.close()

fake = FakeVnc(security=[2], refuse="")
fake.start()
try:
    screen.read_frame("127.0.0.1", fake.port, timeout=3)
    check("a server that wants a password is refused, not guessed at", False, "no error raised")
except screen.RfbError as e:
    check("a server that wants a password says so, and Athena does not invent one",
          "authentication" in str(e), str(e))
close(fake)

fake = FakeVnc(security=[], refuse="too many watchers")
fake.start()
try:
    screen.read_frame("127.0.0.1", fake.port, timeout=3)
    check("a refusal carries the server's own reason", False, "no error raised")
except screen.RfbError as e:
    check("a refusal carries the server's reason rather than a generic failure",
          "too many watchers" in str(e), str(e))
close(fake)

# ---- keys: what a character turns into ------------------------------------------------

check("a plain character is its own keysym",
      screen.keysym_for("a") == (97, False) and screen.keysym_for("7") == (55, False))
check("... an upper case letter is the lower one with shift, because RFB has no `A`",
      screen.keysym_for("A") == (97, True) and screen.keysym_for("Z") == (122, True))
check("... and a shifted symbol is sent as its key plus shift, so `@` is not a lost `2`",
      screen.keysym_for("@") == (50, True) and screen.keysym_for("!") == (49, True)
      and screen.keysym_for("_") == (45, True), str(screen.keysym_for("_")))
check("a named key takes its keysym from the table, and needs no shift",
      screen.keysym_for("Enter") == (0xFF0D, False)
      and screen.keysym_for("ArrowUp") == (0xFF52, False)
      and screen.keysym_for("F5") == (0xFFC2, False)
      and screen.keysym_for("Backspace") == (0xFF08, False), str(screen.keysym_for("F5")))
check("space is a character and not a named key that got lost",
      screen.keysym_for(" ") == (0x0020, False), str(screen.keysym_for(" ")))
check("something that is not a key yields nothing rather than a wrong key",
      screen.keysym_for("banana") == (0, False) and screen.keysym_for("") == (0, False)
      and screen.keysym_for(None) == (0, False))

msgs = screen._key_messages(97, True)
check("a shifted letter is pressed shift-down, letter-down, letter-up, shift-up",
      len(msgs) == 4 and struct.unpack(">I", msgs[0][4:8])[0] == 0xFFE1
      and struct.unpack(">I", msgs[1][4:8])[0] == 97
      and msgs[1][1] == 1 and msgs[2][1] == 0
      and struct.unpack(">I", msgs[3][4:8])[0] == 0xFFE1, str(msgs))
check("... and an unshifted key is just down and up, with no stray shift",
      len(screen._key_messages(97, False)) == 2)

# ---- pointer: the mask, and the bytes -------------------------------------------------

check("the button mask is the one RFB defines, left first",
      screen.pointer_mask(["left"]) == 1 and screen.pointer_mask(["middle"]) == 2
      and screen.pointer_mask(["right"]) == 4
      and screen.pointer_mask(["left", "right"]) == 5)
check("... and the wheel is a button too, so scrolling is not a special case",
      screen.pointer_mask(wheel="up") == 8 and screen.pointer_mask(wheel="down") == 16
      and screen.pointer_mask(wheel="left") == 32 and screen.pointer_mask(wheel="right") == 64)
check("a button name that is not a button is ignored rather than shifting the mask",
      screen.pointer_mask(["elbow", "left"]) == 1)
check("a pointer event is six bytes: type, mask, then the position",
      screen._pointer_message(1, 300, 200) == struct.pack(">BBHH", 5, 1, 300, 200),
      repr(screen._pointer_message(1, 300, 200)))

# ---- input, over a real socket, read back by the server ------------------------------

fake = FakeVnc(width=8, height=8)
fake.start()
res = screen.send_input("127.0.0.1", fake.port,
                        [{"kind": "pointer", "buttons": ["left"], "x": 3, "y": 4},
                         {"kind": "click", "x": 5, "y": 6},
                         {"kind": "wheel", "wheel": "down", "x": 5, "y": 6},
                         {"kind": "key", "key": "a"},
                         {"kind": "text", "text": "Hi"},
                         {"kind": "combo", "mods": ["Control"], "key": "c"}])
time.sleep(0.3)
close(fake)

kinds = [k for k, _ in fake.messages]
pointers = [m for k, m in fake.messages if k == "pointer"]
keys = [m for k, m in fake.messages if k == "key"]
check("input arrives as pointer and key messages, and nothing else on the connection",
      set(kinds) <= {"pointer", "key", "security-choice", "set-pixel-format", "set-encodings"},
      str(kinds))
check("a click is a press and a release in the same place, or the guest never sees it end",
      any(struct.unpack(">BBHH", p) == (5, 1, 5, 6) for p in pointers)
      and any(struct.unpack(">BBHH", p) == (5, 0, 5, 6) for p in pointers), str(pointers[:4]))
check("a wheel notch is one press and one release, with the wheel's own bit",
      any(struct.unpack(">BBHH", p)[1] == 16 for p in pointers), str(pointers))
check("the same buffer answers for what it did, so a panel can say it was sent",
      res["ok"] and res["sent"] >= 8 and res["keys"] == ["a", "H", "i", "c"], str(res))
shifted = any(struct.unpack(">I", k[4:8])[0] == 0xFFE1 for k in keys)
check("typing `Hi` sends shift-strokes, so the guest gets capitals rather than lower case",
      shifted and sum(1 for k in keys if struct.unpack(">I", k[4:8])[0] == ord("h")) == 2,
      str(keys))
check("... and a combo holds the modifier down across the key, then lets it go",
      [struct.unpack(">I", k[4:8])[0] for k in keys][-4:] == [0xFFE3, 99, 99, 0xFFE3],
      str(keys[-4:]))

fake = FakeVnc()
fake.start()
nothing = screen.send_input("127.0.0.1", fake.port, [])
close(fake)
check("input with nothing in it sends nothing rather than opening a pointless connection",
      nothing == {"ok": True, "sent": 0, "keys": [], "dropped": []}, str(nothing))

fake = FakeVnc()
fake.start()
junk = screen.send_input("127.0.0.1", fake.port,
                         [{"kind": "elbow"}, {"kind": "key", "key": "banana"}, {}])
close(fake)
check("input Athena does not understand is dropped instead of being typed at the guest",
      junk["sent"] == 0 and junk["keys"] == [], str(junk))
# ...and it says which of them it dropped, instead of answering `ok: true, sent: 0` over them.
check("... and what was left out is named rather than hidden behind ok:true",
      [d.get("why") for d in junk["dropped"]] == ["nothing to send for a 'elbow' event",
                                                 "no keysym for the key 'banana'",
                                                 "nothing to send for a '' event"],
      str(junk["dropped"]))

# ---- Super, and every other name a modifier arrives under -------------------------------
# Reported: "Omarchy keyboard shortcuts act on Hermes instead of the guest." Super is the key
# that desktop's own shortcuts live on, and the guest keysym for it is Super (0xFFEB) -- not
# Meta (0xFFE7). A client that sends Meta_L has sent a key nothing there is listening for.
check("Super has its own keysym, under every name it is spelled with",
      [screen.modifier_keysym(n) for n in ("Super", "super", "win", "cmd", "mod4", "Meta")]
      == [0xFFEB, 0xFFEB, 0xFFEB, 0xFFEB, 0xFFEB, 0xFFE7],
      str([screen.modifier_keysym(n) for n in ("Super", "super", "win", "cmd", "mod4", "Meta")]))
check("... and the rest of the modifiers answer to theirs",
      [screen.modifier_keysym(n) for n in ("Control", "ctrl", "Ctrl", "Alt", "option", "Shift")]
      == [0xFFE3, 0xFFE3, 0xFFE3, 0xFFE9, 0xFFE9, 0xFFE1])
check("... and a word that is not a modifier is not treated as one",
      screen.modifier_keysym("banana") == 0 and screen.modifier_keysym("") == 0)

_msgs, _keys, _dropped = screen.messages_for([{"kind": "combo", "mods": ["Super"], "key": "Enter"}])
check("a Super+Enter combination puts Super down, presses the key, and lets Super go",
      [struct.unpack(">I", m[4:8])[0] for m in _msgs] == [0xFFEB, 0xFF0D, 0xFF0D, 0xFFEB]
      and _dropped == [] and _keys == ["Enter"],
      str([struct.unpack(">I", m[4:8])[0] for m in _msgs]))
_msgs, _keys, _dropped = screen.messages_for([{"kind": "combo", "mods": ["Super", "Shift"],
                                               "key": "e"}])
check("... and two modifiers hold both down, the way round they were given",
      [struct.unpack(">I", m[4:8])[0] for m in _msgs] == [0xFFEB, 0xFFE1, 101, 101, 0xFFE1, 0xFFEB],
      str([struct.unpack(">I", m[4:8])[0] for m in _msgs]))
_msgs, _keys, _dropped = screen.messages_for([{"kind": "combo", "mods": ["Elbow"], "key": "Enter"}])
check("... and a combination naming a modifier RFB has no keysym for is not sent at all",
      _msgs == [] and "Elbow" in str(_dropped), f"{_msgs} {_dropped}")

# ---- the way this plugin is actually loaded ------------------------------------------
# `dashboard/` is not a package on sys.path — the plugin host loads the backend by path, so
# every sibling is imported by name. A `from . import x` therefore resolves at import time
# and fails at *call* time inside the host, which is exactly how the panel's screen path
# shipped broken once: the protocol was perfect and the route answered
# "attempted relative import with no known parent package".
import importlib.util  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

_dash = PLUG / "dashboard"
_handwritten = [f for f in sorted(_dash.glob("*.py")) if f.name != "screen.py"]
_relative = [f.name for f in _handwritten
             if re.search(r"^\s*from\s*\.\s*import", f.read_text(encoding="utf-8"), re.M)]
check("nothing in the dashboard imports its own siblings as a package",
      not _relative, str(_relative))

_saved_path = list(sys.path)
try:
    if str(_dash) not in sys.path:
        sys.path.insert(0, str(_dash))
    _spec = importlib.util.spec_from_file_location("athena_plugin_api_standalone",
                                                   str(_dash / "plugin_api.py"))
    _sa = importlib.util.module_from_spec(_spec)
    sys.modules["athena_plugin_api_standalone"] = _sa
    _spec.loader.exec_module(_sa)
    _loaded = True
    _why = ""
except Exception as e:  # noqa: BLE001
    _loaded, _why = False, f"{type(e).__name__}: {e}"
finally:
    sys.path[:] = _saved_path

check("the backend loads the way the plugin host loads it, with its siblings found by name",
      _loaded, _why)
if _loaded:
    check("... so the screen reader is there, rather than None until the first click",
          _sa.athena_screen is not None and hasattr(_sa.athena_screen, "frame_png"))
    _r = _sa.machines_host.screen("nothing-here")
    check("... and asking it for a screen is a sentence about the machine, not about an import",
          _r.get("ok") is False and "no machine called" in str(_r.get("error")),
          str(_r)[:200])

# ---- one connection, held: what the panel's routes use --------------------------------
#
# Reported from a real machine: "the input in the virtual machine was still a little laggy.
# & just laggy in general." Every frame and every keystroke used to open its own connection —
# a whole RFB handshake per letter, and a full-screen transfer plus an image re-encode per tick,
# almost all of it pixels that had not changed. A session holds the connection, asks for only
# what changed, and does not re-encode a picture that did not. All of that is checkable here,
# against the same fake server, in milliseconds.

fake = FakeVnc(width=8, height=4)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
first = ses.frame()
check("a session hands back a picture the moment it is made, rather than a promise of one",
      first["image"][:3] == b"\xff\xd8\xff" and first["mime"] == "image/jpeg"
      and (first["width"], first["height"]) == (8, 4),
      str(first)[:140])
check("... over exactly one connection", ses.connections == 1)
check("... asking first for the whole screen, because it has nothing to build on",
      bool(fake.updates) and fake.updates[0][1] == 0, repr(fake.updates[:2]))
for _ in range(60):  # the reader is its own thread: give it a moment to ask again
    if len(fake.updates) >= 2:
        break
    time.sleep(0.05)
check("... and then for only what changed, so a desktop sitting still sends nothing",
      len(fake.updates) >= 2 and fake.updates[1][1] == 1, repr(fake.updates[:3]))

ses.input([{"kind": "text", "text": "abc"}])
ses.input([{"kind": "click", "x": 2, "y": 1}])
for _ in range(40):  # the fake consumes what it is sent on its own thread
    _told = [m[0] for m in fake.messages]
    if _told.count("key") == 6 and _told.count("pointer") == 2:
        break
    time.sleep(0.05)
check("input goes down the connection that is already open, so a letter costs no handshake",
      ses.connections == 1 and _told.count("key") == 6 and _told.count("pointer") == 2,
      f"connections={ses.connections} keys={_told.count('key')}")
check("... and the handshake happened once, at the start, for all of it",
      _told.count("security-choice") == 1, repr(_told[:6]))
ses.close()
close(fake)

# A guest with nothing to report is asked again and answers nothing, which must cost nothing.
fake = FakeVnc(width=8, height=4, quiet_after=1)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
one, two, three = ses.frame(), ses.frame(), ses.frame()
check("a screen nobody is changing is not re-compressed: three polls, one encode",
      ses.encoded == 1 and one["image"] == two["image"] == three["image"],
      f"encoded={ses.encoded}")
check("... and its bytes are the same each time, which is how the panel knows to slow down",
      len(one["image"]) == len(three["image"]))
ses.close()
close(fake)

# A quiet desktop is not the only screen that should cost nothing. A guest that *re-sends the same
# pixels* — a server repeating a cursor's rectangle, a pointer moving across something identical —
# used to invalidate the encoded frame and pay a full re-encode for a picture that had not moved:
# on a 1280x800 guest that is four megabytes and tens of milliseconds per arriving rectangle, which
# is the parked mouse-lag item. The fake re-sends byte-identical rectangles here, so the assertion
# is about the pixels that arrived, not about a screen that said nothing.
fake = FakeVnc(width=8, height=4, then_rects=[(0, 0, 8, 4)], then_shift=0)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
one = ses.frame()
time.sleep(0.6)                       # several further updates arrive, all identical
two = ses.frame()
check("a guest that re-sends the same pixels costs no encode",
      ses.encoded == 1 and one["image"] == two["image"],
      f"encoded={ses.encoded} updates_sent={fake.updates_sent}")
check("... and those updates really did arrive, so this is not just a quiet desktop",
      fake.updates_sent >= 2, f"updates_sent={fake.updates_sent}")
ses.close()
close(fake)

# The encode used to happen inside the lock the reader thread takes to apply arriving rectangles,
# so on a large screen the reader was stopped for the length of a compression run and the frame a
# person was waiting for landed after the picture of the frame before it. The lock is the reader's,
# so the test asks for it from inside the encode: if it can still be taken, the encode is not
# holding it. (Against the old code this check hangs for its whole timeout and then fails.)
fake = FakeVnc(width=8, height=4)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
_real_encode = screen._jpeg
_seen = {}


def _nosy_encode(pixels, width, height):
    got = ses._lock.acquire(timeout=1.0)
    _seen["free"] = got
    if got:
        ses._lock.release()
    return _real_encode(pixels, width, height)


screen._jpeg = _nosy_encode
try:
    shot = ses.frame()
finally:
    screen._jpeg = _real_encode
check("compressing a picture does not stop the reader: the lock is free while it runs",
      _seen.get("free") is True, f"the lock was held across the encode: {_seen}")
check("... and the picture comes back whole", shot.get("ok")
      and shot.get("image", b"")[:3] == b"\xff\xd8\xff", str(shot.get("image"))[:40])
ses.close()
close(fake)

# A rectangle that does arrive has to change the picture, and be encoded because of it.
#
# The change is held back (`arm`) until the reference picture has been taken, because otherwise
# the reference can itself be a picture of the rectangle that "arrived later": this check failed
# exactly once, under a loaded machine, with `changed=False encoded=61` — the guest had changed
# before the baseline was read, every poll re-encoded the same pixels, and the two pictures were
# equal for ever. What is asserted either side of the arm is therefore stronger than the original
# check: nothing moves while the guest has not moved, and the picture does move once it has.
armed = threading.Event()
fake = FakeVnc(width=8, height=4, then_rects=[(0, 0, 8, 4)], then_shift=37, arm=armed)
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
before = ses.frame()["image"]
still = True
for _ in range(20):
    time.sleep(0.05)
    if ses.frame()["image"] != before:
        still = False
        break
check("a guest that has not changed is not drawn as a different picture",
      still, f"the picture moved with nothing sent: encoded={ses.encoded}")
encoded_before = ses.encoded
armed.set()
changed = False
for _ in range(60):
    time.sleep(0.05)
    if ses.frame()["image"] != before:
        changed = True
        break
check("a rectangle that does arrive changes the picture, and is encoded because of it",
      changed and ses.encoded > encoded_before,
      f"changed={changed} encoded={ses.encoded} (was {encoded_before})")
ses.close()
close(fake)

# A guest that grows, seen by a server that answers with the wider picture instead of clipping
# it to the region we asked over. The canvas is the wrong size for it, so the session takes the
# new size rather than a picture of the old one — which is the reported bug: a session opened
# while a guest was small stayed small, and the panel showed a squash of the first seconds of a
# boot for as long as the tab was open.
fake = FakeVnc(width=8, height=4, then_rects=[(0, 0, 8, 6)])
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
for _ in range(60):
    if ses.resizes:
        break
    time.sleep(0.05)
check("a guest that outgrows the session is followed, rather than written outside the canvas",
      ses.alive and ses.resizes == 1 and (ses.width, ses.height) == (8, 6),
      f"alive={ses.alive} resizes={ses.resizes} {ses.width}x{ses.height} {ses.died[:60]}")
image = None
for _ in range(40):
    time.sleep(0.05)
    try:
        image = ses.frame()["image"]
    except screen.RfbError as e:
        check("... and a picture is available at the new size", False, str(e))
        break
    if image:
        break
check("... and what a caller is handed next is a picture of the size the guest is now",
      bool(image) and _image_size(image) == (8, 6), str(_image_size(image) if image else None))
ses.close()
close(fake)

# A guest that only announces the change and repaints nothing: still followed, because the
# announcement is the whole message. A client that does not ask to be told never hears it, and
# this is the case that a clipping server hides completely.
fake = FakeVnc(width=8, height=4, then_desktop=(12, 7))
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
for _ in range(60):
    if ses.resizes:
        break
    time.sleep(0.05)
check("... and a guest that only says it is bigger is followed just the same",
      ses.alive and ses.resizes == 1 and (ses.width, ses.height) == (12, 7),
      f"alive={ses.alive} resizes={ses.resizes} {ses.width}x{ses.height}")
ses.close()
close(fake)

# ---- the registry: one per machine, and let go of --------------------------------------

fake = FakeVnc(width=4, height=2)
fake.start()
asked = screen.session_for("hold-me", "127.0.0.1", fake.port)
again = screen.session_for("hold-me", "127.0.0.1", fake.port)
check("asking twice for one machine's screen keeps one connection, not two",
      asked is again and asked.connections == 1)
check("... and the machine is listed as one whose screen is being held",
      "hold-me" in screen.sessions_held(), repr(screen.sessions_held()))
check("... and letting it go closes the connection and forgets the machine",
      screen.session_for("hold-me", "127.0.0.1", fake.port, drop=True) is None
      and "hold-me" not in screen.sessions_held() and not asked.alive)
close(fake)

fake = FakeVnc(width=4, height=2)
fake.start()
held = screen.session_for("idle-me", "127.0.0.1", fake.port)
_keep = screen.SESSION_IDLE_SECONDS
try:
    screen.SESSION_IDLE_SECONDS = 0.2
    time.sleep(0.35)
    let_go = screen.reap_sessions()
finally:
    screen.SESSION_IDLE_SECONDS = _keep
check("a connection nobody has looked at is let go, not held on to forever",
      "idle-me" in let_go and "idle-me" not in screen.sessions_held() and not held.alive,
      repr(let_go))
close(fake)

fake = FakeVnc(width=4, height=2)
fake.start()
watched = screen.session_for("watched", "127.0.0.1", fake.port)
watched.frame()
check("... and one that is being looked at is left alone",
      "watched" not in screen.reap_sessions() and "watched" in screen.sessions_held())
for _name in screen.sessions_held():
    screen.close_session(_name)
close(fake)

# ---- a guest that pauses, and a machine that never answers -----------------------------
#
# Two different faults, one symptom — "the screen is laggy" — and neither of them was about
# pixels moving slowly. The first was a session declared dead in the middle of a rectangle
# the guest was still sending: the connection's short timeout did double duty, so a booting
# guest pausing for a couple of seconds lost its connection and paid for a fresh handshake,
# which is the black flash between two pictures of the same screen. The second was one
# machine's handshake holding the lock that every other machine's screen route needs, so a
# guest that was up but not ready made every screen in the panel hang.

fake = FakeVnc(width=4, height=2, stall=(2.5, 20))
fake.start()
ses = screen.ScreenSession("127.0.0.1", fake.port, idle_timeout=0.3).start()
# The guest pauses for 2.5 s with the socket timeout at 0.3 s: mid-rectangle, and inside the
# one read that used to give up.
for _ in range(100):
    if ses.died or ses.requests > 2:
        break
    time.sleep(0.05)
check("a guest that pauses inside a rectangle is not declared dead",
      not ses.died and ses.alive, f"died={ses.died!r} alive={ses.alive}")
check("... and the frame it was in the middle of still arrives on that same connection",
      ses.requests >= 3 and not ses.died,
      f"requests={ses.requests} died={ses.died!r}")
ses.close()
close(fake)

# A machine whose VNC port accepts the connection and then says nothing at all.
deaf = socket.socket()
deaf.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
deaf.bind(("127.0.0.1", 0))
deaf.listen(1)
deaf_port = deaf.getsockname()[1]


def _accept_and_say_nothing():
    conn, _ = deaf.accept()
    time.sleep(5.0)
    conn.close()


def _connect_to_the_silent_one():
    try:
        screen.session_for("deaf", "127.0.0.1", deaf_port)
    except screen.RfbError:
        pass                      # it never answers, so it never becomes a screen


threading.Thread(target=_accept_and_say_nothing, daemon=True).start()
threading.Thread(target=_connect_to_the_silent_one, daemon=True).start()
time.sleep(0.5)                   # let it get as far as waiting on the handshake

fake = FakeVnc(width=4, height=2)
fake.start()
started = time.time()
quick = screen.session_for("quick", "127.0.0.1", fake.port)
waited = time.time() - started
check("a machine that accepts and never answers does not hold up another machine's screen",
      waited < 1.5 and quick is not None and quick.alive, f"waited {waited:.2f}s")
screen.close_session("quick")
close(fake)
deaf.close()

_api_src = (PLUG / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
check("both of the panel's screen routes go through the held connection",
      _api_src.count("_held_session(") >= 4,
      f"mentions={_api_src.count('_held_session(')}")


print(f"\n{passed} ok, {failed} failed")
sys.exit(1 if failed else 0)
