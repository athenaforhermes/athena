"""A VNC server small enough to keep in a test file's shadow, and honest enough to trust.

Lifted out of `t_screen.py` so two tests can share one fake rather than growing a second one
that is wrong in its own way. It offers what QEMU offers, answers the handshake, and then reads
whatever the client sends so the test can inspect it. `t_screen.py` uses it to check the client;
`t_perf.py` uses it to measure what a still screen and a re-sent rectangle cost.

Nothing here talks to a real guest, and nothing here is a mock of Athena's own code: it is a
server, speaking RFB, on a loopback port.

Usage: `from vncfake import FakeVnc, close, recvn, PFMT` (after putting this directory on the path).
"""
import io
import socket
import struct
import threading
import time


def recvn(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


PFMT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


class FakeVnc(threading.Thread):
    """Enough of a VNC server to answer this client, and to remember what it was told."""

    def __init__(self, width=4, height=2, rects=None, security=(1,), refuse="",
                 name="fake guest", version=b"RFB 003.008\n", quiet_after=None,
                 then_rects=None, then_shift=0, then_desktop=None, stall=None,
                 arm=None):
        super().__init__(daemon=True)
        self.width, self.height = width, height
        self.rects = rects if rects is not None else [(0, 0, width, height)]
        # `quiet_after` is a guest that has nothing more to say: an incremental request goes
        # unanswered, which is what a desktop sitting still looks like from this side.
        # `then_rects`/`then_shift` are a guest that changes after the first frame.
        self.quiet_after = quiet_after
        self.then_rects = then_rects
        self.then_shift = then_shift
        # `arm` is a `threading.Event` that holds the change back until the test has looked at
        # the picture as it was. Without it the change can arrive before the first `frame()` the
        # test takes, and then the reference picture already *is* the changed one: every poll
        # afterwards re-encodes the same pixels, the two are equal for ever, and a check that
        # reads "the picture changed" fails on a guest that did change. (Seen once under load.)
        self.arm = arm
        # `then_desktop` is a guest that announces its new size the way RFB 3.8 does: one
        # rectangle whose width and height are the new screen, encoding -223, no pixels at all.
        self.then_desktop = then_desktop
        # `stall` is a guest that goes quiet in the middle of a rectangle it is already
        # sending: (seconds, bytes_to_send_first). A machine booting does exactly this, and
        # the read that gives up there throws away a connection that was working.
        self.stall = stall
        self.updates_sent = 0
        self.security, self.refuse, self.name = list(security), refuse, name
        self.version_offered = version
        self.messages = []       # (type, payload bytes) in the order the client sent them
        self.client_version = b""
        self.pixel_format = None
        self.encodings = None
        self.updates = []
        self.shared = None
        self.error = ""
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def run(self):
        try:
            self._serve()
        except Exception as e:  # noqa: BLE001 — a broken fake is a failed check, not a crash
            self.error = f"{type(e).__name__}: {e}"

    def _serve(self):
        conn, _ = self.sock.accept()
        conn.settimeout(10)
        with conn:
            conn.sendall(self.version_offered)
            self.client_version = recvn(conn, 12)
            if self.refuse:
                conn.sendall(b"\x00" + struct.pack(">I", len(self.refuse))
                             + self.refuse.encode())
                return
            conn.sendall(bytes([len(self.security)]) + bytes(self.security))
            if self.security == [1]:
                choice = recvn(conn, 1)
                self.messages.append(("security-choice", choice))
                conn.sendall(struct.pack(">I", 0))
            self.shared = recvn(conn, 1)
            conn.sendall(struct.pack(">HH", self.width, self.height) + PFMT
                         + struct.pack(">I", len(self.name)) + self.name.encode())
            while True:
                head = conn.recv(1)
                if not head:
                    return
                kind = head[0]
                if kind == 0:
                    body = recvn(conn, 19)
                    self.pixel_format = body[3:]
                    self.messages.append(("set-pixel-format", head + body))
                elif kind == 2:
                    head2 = recvn(conn, 3)
                    count = struct.unpack(">H", head2[1:3])[0]
                    body = recvn(conn, count * 4)
                    self.encodings = [struct.unpack(">i", body[i * 4:(i + 1) * 4])[0]
                                      for i in range(count)]
                    self.messages.append(("set-encodings", head + head2 + body))
                elif kind == 3:
                    body = recvn(conn, 9)
                    self.updates.append(struct.unpack(">BBHHHH", head + body))
                    self.messages.append(("update-request", head + body))
                    seen = len(self.updates)
                    if self.quiet_after is not None and seen > self.quiet_after:
                        continue  # nothing changed, so nothing is sent
                    if seen > 1 and self.then_desktop:
                        nw, nh = self.then_desktop
                        self.then_desktop = None
                        self.width, self.height, self.rects = nw, nh, [(0, 0, nw, nh)]
                        conn.sendall(struct.pack(">BBH", 0, 0, 1)
                                     + struct.pack(">HHHHi", 0, 0, nw, nh, -223))
                        continue
                    held_back = self.arm is not None and not self.arm.is_set()
                    if seen > 1 and self.then_rects and not held_back:
                        self._send_update(conn, self.then_rects, self.then_shift)
                    else:
                        self._send_update(conn)
                elif kind == 4:
                    body = recvn(conn, 7)
                    self.messages.append(("key", head + body))
                elif kind == 5:
                    body = recvn(conn, 5)
                    self.messages.append(("pointer", head + body))
                elif kind == 6:
                    body = recvn(conn, 7)
                    n = struct.unpack(">I", body[3:7])[0]
                    self.messages.append(("cut-text", head + body + recvn(conn, n)))
                else:
                    self.messages.append((f"unknown-{kind}", head))
                    return

    def _send_update(self, conn, rects=None, shift=0):
        rects = self.rects if rects is None else rects
        out = struct.pack(">BBH", 0, 0, len(rects))
        for (x, y, w, h) in rects:
            out += struct.pack(">HHHHi", x, y, w, h, 0)
            out += self.pixels_for(x, y, w, h, shift)
        self.updates_sent += 1
        if self.stall and self.updates_sent == 2:
            seconds, first = self.stall
            conn.sendall(out[:first])
            time.sleep(seconds)
            conn.sendall(out[first:])
            return
        conn.sendall(out)

    def pixels_for(self, x, y, w, h, shift=0):
        """BGRX bytes, in the order they go on the wire: blue, green, red, unused.

        Blue is first because it is the low byte of the pixel value, and red sits at shift
        16. This is the format Athena asks for, and a real QEMU frame decoded in this same
        order came back pixel-for-pixel identical to QEMU's own screendump.
        """
        out = b""
        for row in range(h):
            for col in range(w):
                out += bytes([((x + col) * 10 + shift) % 256, (y + row) * 10 % 256, 200, 0])
        return out


def _image_size(data):
    """The pixel size of a picture, whichever format the live one travels as."""
    from PIL import Image
    return Image.open(io.BytesIO(data)).size


def _png_size(png):
    """The width and height of a PNG, read from its header — no decoder needed."""
    if not png or png[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", png[16:24])


def close(fake):
    fake.join(timeout=5)
    try:
        fake.sock.close()
    except OSError:
        pass
