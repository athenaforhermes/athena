"""A guest's screen, read the one way this plugin can read it: over VNC, server-side.

The panel is a browser context and cannot open raw sockets, and this plugin's routes are
buffered HTTP with no websocket upgrade. So the plugin is the VNC client: it speaks RFB to
QEMU's loopback VNC server, decodes the framebuffer, and hands the panel an image. Input
travels the other way along the same protocol, which is what makes the panel a *display
you can use* rather than a picture of one.

Two shapes live here. The one-shot readers (`read_frame`, `frame_png`, `send_input`)
connect, ask, read and close, which is what a caller that wants one picture wants; they are
also the fallback and the thing the tests drive. `ScreenSession` is what the panel's routes
use: one connection held open for one machine, because a keystroke should not cost a whole
RFB handshake and a second of a person's life should not cost four megabytes of pixels that
did not change. The session is reaped when nobody has looked at it for a while, and it ends
itself rather than lie if the guest changes resolution underneath it. That last sentence used
to be a promise it could not keep: a session that opened while a guest was still at its
firmware's screen kept asking for updates inside those old bounds, and QEMU answers a clipped
region with clipped pixels, so the session never found out the guest had grown. Measured on
this host: a session opened at 640x480 was still 640x480 forty seconds later, while a fresh
read of the same machine said 1280x800. So a session now says it is willing to be resized
(DesktopSize), acts on being told, and asks over a region wider than its own picture so a
server that answers by clipping still cannot hide a bigger guest from it.
"""
from __future__ import annotations

import io
import socket
import struct
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# RFB: the client may pick its own pixel format, so this file chooses the one that needs
# no per-pixel arithmetic — 32 bits, 8 per channel, blue in the low byte (BGRX in memory).
# PIL decodes that byte order directly with "raw", "BGRX".
BPP = 32
DEPTH = 24
_RED_SHIFT, _GREEN_SHIFT, _BLUE_SHIFT = 16, 8, 0
_RAW = 0  # encoding number for a plain rectangle: what a screenshot-sized read wants
# -223 is the newer RFB's way of saying "the guest's screen is now THIS size": one rectangle
# whose width and height are the new framebuffer and which carries no pixels at all. A client
# that never lists it is never told, which is exactly how a held session went stale.
_DESKTOP_SIZE = -223
# How much of the guest a request asks over. Not the size of our own picture: a server is free
# to answer with anything inside the guest's real screen, and asking only inside yesterday's
# bounds is what let a grown guest stay hidden. Asking wide costs nothing — a server clips the
# region to its own framebuffer — and it turns "the guest is bigger than this" into a reply.
_ASK_WIDE = 65535
# 6 is the "balanced" default and it is the wrong default here: the PNG never leaves this
# computer, so the pixels are free and the compression pass is what a person feels.
_PNG_COMPRESS_LEVEL = 1

# What a *live* picture is sent as. Lossless costs what it costs: measured with Pillow on this
# machine, a 1920x1080 desktop of flat colour is 11ms and 61KB, and the same size showing anything
# photographic is 105ms and 6.4MB -- per frame, on the thread a person is waiting behind. That
# asymmetry is the reported lag: typing into a terminal was fine and typing into anything busy was
# not, because the echo waits for the picture and the picture waits for a lossless encoder that has
# 6MB to write. A remote console is a look at a live thing, not an archive copy of it, so the live
# picture is JPEG: same picture, a fraction of the bytes, a fraction of the time. `_png` stays for
# the callers that want a still they can keep.
_JPEG_QUALITY = 82
_IMAGE_MIME = "image/jpeg"

# RFB message type 6, `ClientCutText`: the client's clipboard, handed to the guest. It
# was declared here as unused and never sent, which is why pasting into a guest used to
# arrive as a burst of keystrokes or, for anything longer than a line, not at all.
_CLIENT_CUT_TEXT = 6

# RFB's clipboard is ISO 8859-1 and nothing else. The message carries no encoding field,
# so a UTF-8 byte string is a different string in a guest that follows the spec — QEMU's
# own server pastes `â€™` where the text said `'`. Characters with no Latin-1 form are
# therefore reported rather than sent, the same way a key with no keysym is.
_CLIPBOARD_ENCODING = "iso-8859-1"

# RFB keysyms for everything an installer's text fields and menus need. Printable ASCII is
# its own keysym, so the table only has to cover the keys that are not characters.
KEYSYMS = {
    "Backspace": 0xFF08, "Tab": 0xFF09, "Enter": 0xFF0D, "Escape": 0xFF1B,
    "Delete": 0xFFFF, "Home": 0xFF50, "End": 0xFF57, "PageUp": 0xFF55,
    "PageDown": 0xFF56, "ArrowLeft": 0xFF51, "ArrowUp": 0xFF52,
    "ArrowRight": 0xFF53, "ArrowDown": 0xFF54,
    "Shift": 0xFFE1, "Control": 0xFFE3, "Alt": 0xFFE9, "Meta": 0xFFE7, "CapsLock": 0xFFE5,
    # Super and Hyper are separate keysyms from Meta, and the distinction is not pedantry: a
    # desktop like Omarchy (GNOME) opens its overview on Super_L, and a client that sends
    # Meta_L instead has sent a key nothing is listening for.
    "Super": 0xFFEB, "Hyper": 0xFFEE,
    "Insert": 0xFF63, " ": 0x0020,
}
for _i in range(1, 13):
    KEYSYMS[f"F{_i}"] = 0xFFBE + _i - 1

SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
    '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}
_UNSHIFTED_KEYS = {"-", "=", "[", "]", "\\", ";", "'", ",", ".", "/", "`"}

# Every name a modifier arrives under, from every direction a caller can come from: the panel
# sends `Super` for the Windows/Command key, an agent tool or a person writing JSON may send
# `meta`, `cmd`, `win` or `mod4`, and a combination that loses its modifier is not a smaller
# keystroke -- it is a different one, acted on by the guest. So the names are mapped rather
# than filtered, and `modifier_keysym` returning 0 is reported instead of dropped.
MODIFIERS = {
    "ctrl": "Control", "control": "Control", "ctl": "Control",
    "alt": "Alt", "option": "Alt", "opt": "Alt", "mod1": "Alt", "meta": "Meta",
    "super": "Super", "win": "Super", "windows": "Super", "cmd": "Super",
    "command": "Super", "mod4": "Super", "hyper": "Hyper",
}


def modifier_keysym(name: str) -> int:
    """The keysym for a modifier, however it was spelled. 0 means it is not one."""
    text = str(name or "").strip()
    if text in KEYSYMS:                        # the panel's own spelling, as-is
        return KEYSYMS[text]
    return KEYSYMS.get(MODIFIERS.get(text.lower(), ""), 0)


class RfbError(RuntimeError):
    """Anything that stopped this from being a screen — said in words, not a code."""


# How long a read that is already inside a message will wait with nothing arriving at all.
# It is not a connection timeout: the connection's own timeout stays short, so a still screen
# is noticed quickly. This is the patience a guest gets in the middle of a large rectangle.
READ_PATIENCE_SECONDS = 20.0


def _recv_exact(sock: socket.socket, n: int, patient: bool = False) -> bytes:
    """Exactly `n` bytes, or an error that says how far it got.

    `patient` is for reads that are already part-way into a message, and it exists because
    giving up in the middle of one is not a slow picture — it is a frame abandoned half-read
    with the rest of it still on the wire, so the next read begins inside the old one and
    reads pixels as headers. That is what "the guest's screen stopped answering (23/76800
    bytes)" was: a booting guest pausing for longer than the socket timeout inside one
    rectangle, and a session that then threw away a perfectly good connection and paid for
    another handshake — the black flash between two pictures of the same screen.

    So inside a message this waits for progress instead of for the clock: any byte at all
    restarts the countdown, and only `READ_PATIENCE_SECONDS` with nothing arriving counts as
    gone.
    """
    buf = b""
    watchdog = time.monotonic() + READ_PATIENCE_SECONDS if patient else 0.0
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout as e:
            if not patient or time.monotonic() >= watchdog:
                raise RfbError(f"the guest's screen stopped answering ({len(buf)}/{n} bytes)") from e
            continue
        if not chunk:
            raise RfbError(f"the guest's screen closed the connection ({len(buf)}/{n} bytes)")
        buf += chunk
        if patient:
            watchdog = time.monotonic() + READ_PATIENCE_SECONDS
    return buf


def _handshake(sock: socket.socket) -> Tuple[int, int, str]:
    """Version, security, and the size of the screen the guest currently has."""
    server = _recv_exact(sock, 12)
    if not server.startswith(b"RFB "):
        raise RfbError(f"that port is not a VNC server: {server[:12]!r}")
    # 3.8 when the server offers it, else the oldest number, which every server accepts.
    version = b"RFB 003.008\n" if server >= b"RFB 003.007\n" else b"RFB 003.003\n"
    sock.sendall(version)

    if version == b"RFB 003.003\n":
        sec = struct.unpack(">I", _recv_exact(sock, 4))[0]
        if sec != 1:
            raise RfbError(f"the guest's screen asks for security type {sec}, not none")
    else:
        count = _recv_exact(sock, 1)[0]
        offered = list(_recv_exact(sock, count)) if count else []
        if count == 0:
            reason_len = struct.unpack(">I", _recv_exact(sock, 4))[0]
            raise RfbError("the guest's screen refused the connection: "
                           + _recv_exact(sock, reason_len).decode("utf-8", "replace"))
        if 1 not in offered:
            raise RfbError(f"this VNC server wants authentication ({offered}); "
                           "a machine Athena made has none")
        sock.sendall(bytes([1]))
        if struct.unpack(">I", _recv_exact(sock, 4))[0] != 0:
            raise RfbError("the guest's screen rejected an unauthenticated connection")

    sock.sendall(bytes([1]))  # shared: never evict whoever else is watching
    head = _recv_exact(sock, 24)
    width, height = struct.unpack(">HH", head[:4])
    name_len = struct.unpack(">I", head[20:24])[0]
    name = _recv_exact(sock, name_len).decode("utf-8", "replace") if name_len else ""
    if not width or not height:
        raise RfbError("the guest's screen reported an empty framebuffer")
    return width, height, name


def _set_pixel_format(sock: socket.socket) -> None:
    fmt = struct.pack(">BBBBHHHBBB3x", BPP, DEPTH, 0, 1, 255, 255, 255,
                      _RED_SHIFT, _GREEN_SHIFT, _BLUE_SHIFT)
    sock.sendall(struct.pack(">B", 0) + b"\x00\x00\x00" + fmt)
    sock.sendall(struct.pack(">BBH", 2, 0, 2)
                 + struct.pack(">i", _RAW) + struct.pack(">i", _DESKTOP_SIZE))


def _request_update(sock: socket.socket, incremental: bool) -> None:
    """Ask for a frame. `incremental` means: send me only the rectangles that changed.

    A full request is how a session starts and how it resynchronises; an incremental one is
    what keeps a still desktop free. This is the whole difference between a screen that
    follows a person and one that trails them.

    The region asked over is the guest's whole possible screen rather than our own picture: a
    server clips a region to its framebuffer, so asking wide is free, and it means a guest that
    outgrew this session shows up as an answer too wide for the canvas instead of as nothing
    at all — which is how a session opened during a boot stayed a picture of a boot.
    """
    sock.sendall(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0,
                             _ASK_WIDE, _ASK_WIDE))


def _read_update(sock: socket.socket, canvas: bytearray, width: int, height: int,
                 tolerate_idle: bool = False) -> Tuple[str, int, int]:
    """Read one FramebufferUpdate into `canvas`.

    Answers with what happened and the size it happened at:
      ("frame", w, h)  — pixels arrived, and they changed the canvas
      ("same", w, h)   — pixels arrived, and they were the pixels already on it
      ("idle", w, h)   — nothing changed, which is what a still desktop looks like
      ("resize", w, h) — the guest's screen is now this size, and the pixels that came with
                         it were thrown away, because a canvas of the old size cannot hold
                         them. The caller makes a new canvas and asks again.

    An incremental request is answered only if something changed, so a quiet screen means no
    message at all — the normal case, not an error. `tolerate_idle` is how the caller says
    which of the two it is waiting for. It is only ever consulted on the first byte of a
    message, where nothing has been read yet and stopping costs nothing. Every read after
    that one is `patient`: once a message has begun, the bytes the guest still owes have to be
    read before anything else on this connection can mean anything.
    """
    resized: Optional[Tuple[int, int]] = None
    drew = False
    moved = False
    while True:
        try:
            first = sock.recv(1)
        except socket.timeout:
            if tolerate_idle:
                return ("idle", width, height)
            raise RfbError("the guest's screen stopped answering")
        if not first:
            raise RfbError("the guest's screen closed the connection")
        kind = first[0]
        if kind == 0:  # FramebufferUpdate
            _recv_exact(sock, 1, patient=True)
            rects = struct.unpack(">H", _recv_exact(sock, 2, patient=True))[0]
            for _ in range(rects):
                x, y, w, h, enc = struct.unpack(">HHHHi", _recv_exact(sock, 12, patient=True))
                if enc == _DESKTOP_SIZE:
                    # The guest resized and this rectangle is the announcement: its width and
                    # height are the new screen, and there are no pixels behind it. Read the
                    # rest of the update rather than leaving it in the stream, but keep none
                    # of it: what it describes is a picture this canvas cannot hold.
                    resized = (w, h)
                    continue
                if enc != _RAW:
                    raise RfbError(f"the guest's screen sent encoding {enc}, not a plain rectangle")
                if x + w > width or y + h > height:
                    # A guest that grew, seen by a server that answered with the wider
                    # picture rather than clipping it. There is nothing to salvage at this
                    # size, so the whole update is read and dropped and the caller is told
                    # to start again at the size the guest is really at.
                    _recv_exact(sock, w * h * 4, patient=True)
                    resized = (x + w, y + h)
                    continue
                data = _recv_exact(sock, w * h * 4, patient=True)
                if resized:
                    continue          # discarded: the frame it belongs to is the wrong size
                for row in range(h):
                    start = ((y + row) * width + x) * 4
                    chunk = data[row * w * 4:(row + 1) * w * 4]
                    # A rectangle is not a change. A guest sends one whenever it has something
                    # to say -- a server may re-send the same pixels, a still desktop can repeat
                    # a cursor's rectangle, and a pointer that moves across something identical
                    # says nothing at all -- and treating "pixels arrived" as "the picture is
                    # new" is what threw away an encoded frame for a screen that had not moved.
                    # The comparison costs one slice per row and saves a 4 MB encode.
                    if chunk != canvas[start:start + w * 4]:
                        canvas[start:start + w * 4] = chunk
                        moved = True
                drew = True
            if resized:
                return ("resize", resized[0], resized[1])
            if not drew:
                return ("idle", width, height)
            # "same" is a real answer and not a quieter "idle": pixels arrived, and they were the
            # pixels already here. The caller has a picture; it just does not have a new one, and
            # the difference is the whole point — an encoded frame is kept rather than thrown away
            # and rebuilt for a screen that did not move.
            return ("frame" if moved else "same", width, height)
        if kind == 1:  # SetColourMapEntries, only ever sent for palette formats
            _recv_exact(sock, 5, patient=True)
            n = struct.unpack(">H", _recv_exact(sock, 2, patient=True))[0]
            _recv_exact(sock, n * 6, patient=True)
        elif kind == 2:  # Bell
            continue
        elif kind == 3:  # ServerCutText
            _recv_exact(sock, 3, patient=True)
            n = struct.unpack(">I", _recv_exact(sock, 4, patient=True))[0]
            _recv_exact(sock, n, patient=True)
        else:
            raise RfbError(f"the guest's screen sent message type {kind}, which is not one Athena reads")


def _ask_for_a_frame(sock: socket.socket, width: int, height: int) -> Tuple[bytes, int, int]:
    """One full-screen update, then hand back the raw pixels and the size they are.

    The size is part of the answer because a guest can grow between the handshake and the
    pixels — a loader handing over to a graphical session does it — and pixels from a guest
    that grew need a canvas of the new size, and a PNG made at the old one would be a lie.
    """
    for _ in range(8):
        _request_update(sock, False)
        canvas = bytearray(width * height * 4)
        kind, w, h = _read_update(sock, canvas, width, height)
        if kind in ("frame", "same"):
            # Both carry pixels; only one of them moved. The first frame of a session is asked
            # for on a fresh canvas, so it is a picture either way — and a guest whose whole
            # screen really is identical to a zeroed buffer is still a screen to hand over.
            return bytes(canvas), w, h
        width, height = w, h       # resized, or nothing to draw yet: ask again at the new size
    raise RfbError("the guest's screen kept changing size without settling")


# A VNC endpoint is named the way QEMU names it, because the same string is used in
# three places — the `-vnc` argument, the address this reader dials, and the sentence
# the panel shows — and three spellings of one endpoint is how a screen ends up being
# read from the wrong one. `127.0.0.1:5901` is a loopback port every local process can
# also reach; `unix:<path>` is a file, and a file can be given a mode.
UNIX_PREFIX = "unix:"


def _open(host: str, port: int, timeout: float = 8.0) -> socket.socket:
    """Connect to a VNC endpoint named `host:port` or `unix:<path>`.

    Both spellings, in one place, because the transport is a property of the address
    rather than a flag threaded through every caller — `read_frame`, `frame_png`,
    `session_for` and the panel's route all pass a `host` they do not interpret.

    The TCP form is kept, not deprecated: a machine created before the socket change
    stores a port and nothing else, and a screen reader that refused to look at it
    would have broken a machine it was supposed to be watching.
    """
    target = str(host or "")
    if target.startswith(UNIX_PREFIX):
        path = target[len(UNIX_PREFIX):]
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(path)
        except (OSError, AttributeError) as e:
            # `AttributeError` is the whole story on a Python without AF_UNIX, and a
            # reader that raised it raw would report a missing socket as a broken plugin.
            raise RfbError(f"nothing is listening on {path} — {e}") from e
        return sock
    try:
        sock = socket.create_connection((target, int(port)), timeout=timeout)
    except OSError as e:
        raise RfbError(f"nothing is listening on {target}:{int(port)} — {e}") from e
    sock.settimeout(timeout)
    return sock


def read_frame(host: str = "127.0.0.1", port: int = 5900, timeout: float = 8.0) -> Dict[str, Any]:
    """The guest's current screen as pixels, plus the size it currently is.

    Read fresh every time on purpose: a screen is a live thing, and the size changes as a
    guest switches from a text-mode loader to a graphical installer.
    """
    with _open(host, port, timeout) as sock:
        width, height, name = _handshake(sock)
        _set_pixel_format(sock)
        pixels, width, height = _ask_for_a_frame(sock, width, height)
    return {"ok": True, "width": width, "height": height, "name": name, "pixels": pixels,
            "format": "BGRX"}


def _png(pixels: bytes, width: int, height: int) -> bytes:
    """Pixels to PNG. Compressed lightly on purpose: this image only ever travels to a panel
    on this computer, where a byte costs nothing and a millisecond costs a frame."""
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover — Pillow is a declared dependency
        raise RfbError(f"reading a screen needs Pillow: {e}") from e
    img = Image.frombytes("RGB", (width, height), pixels, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=_PNG_COMPRESS_LEVEL)
    return buf.getvalue()


def _jpeg(pixels: bytes, width: int, height: int) -> bytes:
    """Pixels to JPEG, which is what a live picture travels as. See `_JPEG_QUALITY`.

    `subsampling=0` keeps colour at full resolution: this carries text, and half-resolution
    colour on small coloured text is the difference between readable and smeared.
    """
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover — Pillow is a declared dependency
        raise RfbError(f"reading a screen needs Pillow: {e}") from e
    img = Image.frombytes("RGB", (width, height), pixels, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=_JPEG_QUALITY, subsampling=0, optimize=False)
    return buf.getvalue()


def frame_png(host: str = "127.0.0.1", port: int = 5900, timeout: float = 8.0) -> Dict[str, Any]:
    """The guest's current screen as a PNG, ready for a panel or a person to look at."""
    frame = read_frame(host, port, timeout)
    return {"ok": True, "png": _png(frame["pixels"], frame["width"], frame["height"]),
            "width": frame["width"], "height": frame["height"], "name": frame["name"]}


def frame_image(host: str = "127.0.0.1", port: int = 5900, timeout: float = 8.0) -> Dict[str, Any]:
    """The guest's current screen as the picture the panel is shown, which is a JPEG."""
    frame = read_frame(host, port, timeout)
    return {"ok": True, "image": _jpeg(frame["pixels"], frame["width"], frame["height"]),
            "mime": _IMAGE_MIME, "width": frame["width"], "height": frame["height"],
            "name": frame["name"]}


def keysym_for(key: str) -> Tuple[int, bool]:
    """The keysym for a key name or a single character, and whether shift is needed.

    Shift is part of the answer because an installer text field needs an `@` typed as
    shift+2, not a keysym for `@` — RFB has no such keysym, and a guest that ignores the
    shift sees a `2` and a user who thinks their keyboard is broken.
    """
    key = str(key or "")
    if key in KEYSYMS:
        return KEYSYMS[key], False
    if len(key) == 1:
        if key in SHIFTED:
            return ord(SHIFTED[key]), True
        if key.isupper():
            return ord(key.lower()), True
        return ord(key), False
    if len(key) > 1 and key.startswith("Key") and len(key) == 4:
        return keysym_for(key[3:].lower())
    if key.startswith("Digit") and len(key) == 6:
        return ord(key[5]), False
    return 0, False


def clipboard_text(text: str) -> Tuple[bytes, int]:
    """Text as RFB clipboard bytes, and how many characters had no form in them.

    Per character rather than one `encode()` for the whole string, because the failure
    mode of the whole-string form is all-or-nothing: one emoji in a page of ASCII and a
    guest that could have received the ASCII receives nothing. The count is returned
    rather than raised so the caller can name what was left out, which is what the rest
    of this module does with anything it cannot send.
    """
    kept: List[bytes] = []
    lost = 0
    for ch in text or "":
        try:
            kept.append(ch.encode(_CLIPBOARD_ENCODING))
        except UnicodeEncodeError:
            lost += 1
    return b"".join(kept), lost


def _client_cut_text(body: bytes) -> bytes:
    """`ClientCutText` — the message that makes a paste arrive as a paste.

    Type, three padding bytes, a big-endian length, then the text. The padding is not
    optional and not spare: the format follows `SetEncodings`, whose first field is a
    byte and whose second is two, so everything after it is four-byte aligned.
    """
    return struct.pack(">B3xI", _CLIENT_CUT_TEXT, len(body)) + body


def _key_messages(keysym: int, shift: bool) -> List[bytes]:
    if not keysym:
        return []
    def down(code: int, pressed: bool) -> bytes:
        return struct.pack(">BBHI", 4, 1 if pressed else 0, 0, code)
    if shift:
        return [down(KEYSYMS["Shift"], True), down(keysym, True), down(keysym, False),
                down(KEYSYMS["Shift"], False)]
    return [down(keysym, True), down(keysym, False)]


def _pointer_message(mask: int, x: int, y: int) -> bytes:
    return struct.pack(">BBHH", 5, mask & 0xFF, max(0, min(32767, int(x))),
                       max(0, min(32767, int(y))))


def pointer_mask(buttons: Iterable[str] = (), wheel: str = "") -> int:
    """RFB's button mask: 1 left, 2 middle, 4 right, then the wheel as up/down/left/right."""
    mask = 0
    for name in buttons:
        mask |= {"left": 1, "middle": 2, "right": 4}.get(str(name).lower(), 0)
    mask |= {"up": 8, "down": 16, "left": 32, "right": 64}.get(str(wheel).lower(), 0)
    return mask


def messages_for(events: Iterable[Dict[str, Any]]) -> Tuple[List[bytes], List[str], List[Dict]]:
    """The RFB messages a list of pointer/key events turns into, the keys they typed, and what
    could not be sent at all.

    Split out from `send_input` because a held connection has to be able to build the same
    messages without a handshake in front of them: a keystroke costs a few bytes, not a
    session.

    The third value is the honest half. A key with no keysym and a combination whose modifier
    is not a modifier both used to disappear while the call answered `ok: true, sent: N` — the
    modifier was filtered with `if m` and the keystroke went to the guest without it, which
    arrives as a *different* keystroke. Anything left out is named here instead, and the
    callers pass it on as `dropped`.
    """
    messages: List[bytes] = []
    sent_keys: List[str] = []
    notes: List[Dict] = []
    for event in events or []:
        kind = str(event.get("kind") or "").lower()
        if kind == "pointer":
            messages.append(_pointer_message(pointer_mask(event.get("buttons") or (),
                                                          event.get("wheel") or ""),
                                             event.get("x") or 0, event.get("y") or 0))
        elif kind == "click":
            mask = pointer_mask(event.get("buttons") or ("left",))
            x, y = event.get("x") or 0, event.get("y") or 0
            messages.append(_pointer_message(mask, x, y))
            messages.append(_pointer_message(0, x, y))
        elif kind == "wheel":
            x, y = event.get("x") or 0, event.get("y") or 0
            mask = pointer_mask(wheel=event.get("wheel") or "down")
            messages.append(_pointer_message(mask, x, y))
            messages.append(_pointer_message(0, x, y))
        elif kind in ("key", "text"):
            for ch in (str(event.get("text") or event.get("key") or "") if kind == "text"
                       else [event.get("key")]):
                keysym, shift = keysym_for(ch)
                if not keysym:
                    notes.append({"event": event, "why": f"no keysym for the key {ch!r}"})
                    continue
                messages += _key_messages(keysym, shift)
                sent_keys.append(str(ch))
        elif kind == "clipboard":
            # A paste, sent as the guest's own clipboard rather than as keystrokes. Typing
            # a short string into a guest works; typing a URL, a key, or a block of config
            # one keysym at a time does not — it is slow, it races the guest's own
            # auto-repeat, and every character of it lands in whatever is listening, which
            # for a shell means a paste arrives as input nobody typed. This is the one RFB
            # message for it, and it is the one this plugin never sent.
            body, lost = clipboard_text(str(event.get("text") or ""))
            if body:
                messages.append(_client_cut_text(body))
                sent_keys.append(f"clipboard ({len(body)} bytes)")
            elif not lost:
                notes.append({"event": {"kind": "clipboard"},
                              "why": "there was nothing to paste"})
            if lost:
                # Named rather than silently dropped, like a key with no keysym: an agent
                # that pastes a config into a guest and is told "ok, sent: 1" would go on
                # to read a file that is missing characters it never learned about.
                notes.append({
                    "event": {"kind": "clipboard", "bytes": len(body)},
                    "why": (f"{lost} character(s) in the pasted text have no "
                            f"{_CLIPBOARD_ENCODING} form, which is the only encoding RFB's "
                            f"clipboard defines — they were left out rather than sent as "
                            f"mojibake")})
        elif kind == "combo":
            mods: List[int] = []
            unmapped: List[str] = []
            for name in (event.get("mods") or []):
                code = modifier_keysym(name)
                if code:
                    mods.append(code)
                else:
                    unmapped.append(str(name))
            keysym, shift = keysym_for(str(event.get("key") or ""))
            # A combination whose modifier cannot be mapped is not sent at all. Sending it
            # without the modifier would be a *different* keystroke, and the guest acts on what
            # it receives: `Super+Enter` arriving as a bare `Enter` is a line submitted.
            if unmapped or not keysym:
                notes.append({"event": event, "why": (
                    f"no keysym for the modifier {unmapped[0]!r}" if unmapped
                    else f"no keysym for the key {event.get('key')!r}")})
            else:
                messages += [struct.pack(">BBHI", 4, 1, 0, m) for m in mods]
                messages += _key_messages(keysym, shift)
                messages += [struct.pack(">BBHI", 4, 0, 0, m) for m in reversed(mods)]
                sent_keys.append(str(event.get("key")))
        else:
            notes.append({"event": event, "why": f"nothing to send for a {kind!r} event"})
    return messages, sent_keys, notes


def send_input(host: str, port: int, events: Iterable[Dict[str, Any]],
               timeout: float = 8.0) -> Dict[str, Any]:
    """Send pointer and key events to the guest, then let the connection go.

    For one-off callers. A person typing at a screen wants the held session above instead,
    or every letter pays for a handshake it does not need.
    """
    messages, sent_keys, dropped = messages_for(events)
    if not messages:
        return {"ok": True, "sent": 0, "keys": [], "dropped": dropped}
    with _open(host, port, timeout) as sock:
        _handshake(sock)
        _set_pixel_format(sock)
        for message in messages:
            sock.sendall(message)
    return {"ok": True, "sent": len(messages), "keys": sent_keys, "dropped": dropped}

# How long a session may sit unread before it gives its connection back. Long enough that a
# person reading a screen, thinking, and then typing does not pay for a reconnect; short
# enough that walking away does not leave a VNC client attached to a machine forever.
SESSION_IDLE_SECONDS = 60.0


class ScreenSession:
    """One machine's screen, on one connection that stays open.

    Both of the panel's routes used to open a connection, do their one thing and close it: a
    keystroke paid a whole RFB handshake, and every frame asked for the *whole* screen in
    plain rectangles, copied it row by row in Python and compressed it to PNG from scratch.
    On a 1280x800 guest that is about four megabytes and a compression pass per tick, almost
    all of it pixels that did not change — the difference between a screen that follows a
    person and one that trails them.

    This does the ordinary thing a VNC client does instead. The connection is held. Updates
    are requested *incrementally*, so the guest sends only the rectangles that changed. The
    pixels live in a buffer here, and the PNG is re-encoded only when pixels actually
    arrived, so an idle desktop costs a poll nothing. Input goes out on the same socket with
    no setup at all. A reader thread does the waiting, which is the part that matters most:
    a keystroke never queues behind a frame.

    It gives up honestly rather than serving a lie. If the guest changes resolution the
    buffer is the wrong size and cannot be resized behind the server's back, so the session
    ends and the next read starts again at the size the guest reports.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5900,
                 timeout: float = 8.0, idle_timeout: float = 2.0) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.idle_timeout = float(idle_timeout)
        self.width = 0
        self.height = 0
        self.name = ""
        self.died = ""
        self.touched = time.monotonic()
        # Counters a test can read: how many connections this cost, how many frames were
        # requested, and how many were actually encoded. The point of the class is that
        # frames and keystrokes stop adding to the first two.
        self.connections = 0
        self.requests = 0
        self.encoded = 0
        self.resizes = 0
        self._canvas = bytearray()
        self._image_bytes: Optional[bytes] = None
        self._dirty = True
        self._have_frame = False
        # Bumped every time pixels arrive. An encode happens outside the lock now, and this is
        # how a picture that was encoded from pixels that have since been replaced is told
        # apart from one that still describes the screen: the first is handed to the caller and
        # thrown away, the second is kept.
        self._generation = 0
        self._stop = False
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()   # the canvas, the cached PNG, the counters
        self._send = threading.Lock()   # writes, so a request and a keystroke never interleave

    # -- life and death ---------------------------------------------------------------
    @property
    def alive(self) -> bool:
        return bool(self._sock) and not self._stop and not self.died

    def start(self) -> "ScreenSession":
        """Connect and read one full frame before returning.

        A session with no picture in it is not yet a screen: the first caller gets pixels,
        not a promise of them.
        """
        sock = _open(self.host, self.port, self.timeout)
        try:
            width, height, name = _handshake(sock)
            _set_pixel_format(sock)
            pixels, width, height = _ask_for_a_frame(sock, width, height)
            canvas = bytearray(pixels)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self._sock = sock
        self.width, self.height, self.name = width, height, name
        self._canvas = canvas
        self._have_frame = True
        self.connections += 1
        self.requests += 1
        sock.settimeout(self.idle_timeout)
        self._reader = threading.Thread(target=self._pump, name=f"athena-screen-{self.port}",
                                       daemon=True)
        self._reader.start()
        return self

    def close(self) -> None:
        """Let the connection go. The reader is unblocked rather than waited on: shutting the
        socket down is what makes a read give up, and it comes back on its own."""
        self._stop = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _pump(self) -> None:
        """Wait on the guest's screen, forever, without holding anything a call needs.

        The request goes out and then this blocks. When the guest has something to send it
        arrives as soon as it exists, so the block costs nothing; when the guest is still,
        the read times out and the request is renewed. Either way a caller's `frame()` only
        ever reads what is already here.

        The connection's short timeout is for the first byte only — the wait for a guest that
        has nothing to say. Once a message has started, the reads behind it are patient: a
        guest mid-boot can pause for seconds inside one rectangle, and abandoning it there
        would leave the rest of that rectangle on the wire to be misread as the next message.
        """
        while not self._stop:
            sock = self._sock
            if sock is None:
                return
            try:
                with self._send:
                    _request_update(sock, True)
                    self.requests += 1
                kind, w, h = _read_update(sock, self._canvas, self.width, self.height,
                                          tolerate_idle=True)
            except (RfbError, OSError) as e:
                self.died = str(e)
                return
            if kind == "resize":
                # The guest is not the size this session was opened at any more. Make the
                # canvas match, then ask for a whole frame at the new size — one blank frame
                # on the way, instead of a picture of the smaller guest forever.
                self.resizes += 1
                with self._lock:
                    self.width, self.height = w, h
                    self._canvas = bytearray(self.width * self.height * 4)
                    self._have_frame = True
                    self._dirty = True
                    self._image_bytes = None
                    self._generation += 1
                try:
                    with self._send:
                        _request_update(sock, False)
                        self.requests += 1
                except OSError as e:
                    self.died = f"the guest's screen stopped taking requests ({e})"
                    return
                continue
            if kind == "same":
                # Pixels arrived and they were the pixels already here. Nothing to invalidate:
                # the encoded frame still describes this screen, and re-encoding four megabytes
                # to produce the same picture is what made a moving pointer cost a full frame.
                continue
            if kind == "frame":
                with self._lock:
                    self._dirty = True
                    self._image_bytes = None
                    self._generation += 1

    # -- what the routes call ---------------------------------------------------------
    def frame(self) -> Dict[str, Any]:
        """The screen as a PNG, re-encoded only when pixels actually arrived.

        The encode happens *outside* the lock. A 1280x800 screen is four megabytes of pixels and
        tens of milliseconds of compression, and the lock this used to be held under is the one
        the reader thread takes to apply arriving rectangles -- so encoding under it stopped the
        reader, and the frame a person was waiting for landed after the picture of the frame
        before it. What is taken under the lock is a snapshot: the pixels of this moment, copied
        once. The encode runs on the copy while the reader keeps working, and the result is only
        cached if the screen has not moved on in the meantime.
        """
        self.touched = time.monotonic()
        with self._lock:
            if self._image_bytes is None:
                if not self._have_frame:
                    raise RfbError(self.died or "the guest's screen has not sent a picture yet")
                pixels = bytes(self._canvas)
                width, height, generation = self.width, self.height, self._generation
                self._dirty = False
            else:
                return self._frame_result(self._image_bytes)
        encoded = _jpeg(pixels, width, height)
        with self._lock:
            self.encoded += 1
            if self._generation == generation:
                self._image_bytes = encoded      # still the screen as it is: worth keeping
                return self._frame_result(encoded)
            # Pixels arrived while this was being compressed, so this picture is already old.
            # It is still the truest thing we have, so it is what the caller gets -- once.
            return self._frame_result(encoded)

    def _frame_result(self, image: Optional[bytes]) -> Dict[str, Any]:
        """What every caller of `frame` is handed: the picture and how it was come by."""
        return {"ok": True, "image": image, "mime": _IMAGE_MIME, "width": self.width,
                "height": self.height, "name": self.name, "alive": self.alive,
                "encoded": self.encoded, "connections": self.connections,
                "resizes": self.resizes, "note": self.died}

    def input(self, events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Pointer and key events, down the connection that is already open."""
        self.touched = time.monotonic()
        messages, sent_keys, dropped = messages_for(events)
        if not messages:
            return {"ok": True, "sent": 0, "keys": [], "dropped": dropped,
                    "connections": self.connections}
        sock = self._sock
        if sock is None:
            raise RfbError(self.died or "the guest's screen connection is gone")
        try:
            with self._send:
                for message in messages:
                    sock.sendall(message)
        except OSError as e:
            self.died = f"the guest's screen stopped taking input ({e})"
            raise RfbError(self.died) from e
        return {"ok": True, "sent": len(messages), "keys": sent_keys, "dropped": dropped,
                "connections": self.connections}


# The held connections, one per machine. Guarded because the plugin host answers requests on
# more than one thread, and keyed by the name a person knows the machine by.
_SESSIONS: Dict[str, "ScreenSession"] = {}
_SESSIONS_LOCK = threading.Lock()


def session_for(name: str, host: str = "127.0.0.1", port: int = 5900,
                drop: bool = False) -> Optional["ScreenSession"]:
    """The held screen connection for a machine: made if there is none, reaped if idle.

    Reaping is not housekeeping, it is the point. A connection that is never let go is a VNC
    client attached to a machine forever, and a machine stopped, restarted or killed
    underneath one would leave the panel reading a socket that only looks alive. So idle ones
    are dropped here, dead ones are replaced rather than returned, and the connection that
    comes back is one a caller can use.
    """
    if drop:
        close_session(name)
        return None
    reap_sessions()
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(name)
        if session is not None and not session.alive:
            close_session(name)
            session = None
        if session is not None:
            return session

    # Nothing is held for this machine yet, so one has to be connected — outside the lock.
    # The handshake is a full RFB conversation with up to eight seconds of the guest's own
    # time in it (`_ask_for_a_frame` retries while a booting guest keeps changing size), and
    # a lock held across it is not a lock on this dictionary: it is a lock on every machine's
    # screen. Measured before this changed: a port that accepted and never spoke froze
    # `/screen` and `/screen/input` for *all* machines, which is one sick guest presenting as
    # a broken plugin.
    fresh = ScreenSession(host, port).start()
    with _SESSIONS_LOCK:
        held = _SESSIONS.get(name)
        if held is not None and held.alive:
            # Somebody else finished connecting for this machine while this one was on its
            # way. Theirs is the held connection; this one is a second VNC client the guest
            # does not need, so it goes rather than being left to age out.
            fresh.close()
            return held
        _SESSIONS[name] = fresh
        return fresh


def reap_sessions(now: Optional[float] = None) -> List[str]:
    """Let go of every held connection nobody has looked at for a while.

    Returns what it let go of, so a test can say which machine's screen was released rather
    than inferring it from a socket that went quiet.
    """
    moment = time.monotonic() if now is None else now
    with _SESSIONS_LOCK:
        idle = [k for k, ses in _SESSIONS.items()
                if ses.touched + SESSION_IDLE_SECONDS < moment]
    for key in idle:
        close_session(key)
    return idle


def close_session(name: str) -> None:
    """Forget a machine's held connection, whether or not it wants to go quietly."""
    session = _SESSIONS.pop(name, None)
    if session is not None:
        session.close()


def sessions_held() -> List[str]:
    """The machines whose screens are being held open right now."""
    with _SESSIONS_LOCK:
        return sorted(_SESSIONS)
