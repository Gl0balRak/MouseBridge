#!/usr/bin/env python3
"""
MouseBridge — простой шаринг мыши/клавиатуры между двумя компьютерами.

Зависимости (pip):
    pynput
    zeroconf
    PyQt5
    pyobjc-framework-Quartz   (только Mac)

Запуск:
    python3 mousebridge.py            # авто-discovery peer'а через mDNS
    python3 mousebridge.py <peer_ip>  # ручной peer (порт 9999)
    python3 mousebridge.py --no-gui   # без графического окна (CLI)
"""

import json
import platform
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

from pynput import mouse, keyboard
from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser, ServiceListener
from PyQt5 import QtWidgets, QtCore, QtGui

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"

PORT = 9999
SERVICE_TYPE = "_mousebridge._udp.local."

# ============================================================
# Платформенные хелперы — управление курсором
# ============================================================

if IS_MAC:
    import Quartz

    def screen_size():
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return int(b.size.width), int(b.size.height)

    def cursor_pos():
        e = Quartz.CGEventCreate(None)
        loc = Quartz.CGEventGetLocation(e)
        return float(loc.x), float(loc.y)

    def warp_cursor(x, y):
        Quartz.CGWarpMouseCursorPosition((x, y))

    def hide_cursor():
        # Только визуально прячем — coupling не трогаем, чтобы pynput
        # видел реальные координаты и считал deltas корректно.
        Quartz.CGDisplayHideCursor(Quartz.CGMainDisplayID())

    def show_cursor():
        Quartz.CGDisplayShowCursor(Quartz.CGMainDisplayID())

elif IS_WIN:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def screen_size():
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))

    def cursor_pos():
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return float(pt.x), float(pt.y)

    def warp_cursor(x, y):
        user32.SetCursorPos(int(x), int(y))

    # Windows: ShowCursor(False) only affects the calling thread's cursor
    # counter and only takes effect when that thread owns the foreground
    # window. For *system-wide* cursor hiding we replace every system cursor
    # (arrow, ibeam, hand, etc.) with a 32x32 fully-transparent cursor via
    # SetSystemCursor. SystemParametersInfo(SPI_SETCURSORS) restores defaults.

    _OCR_IDS = (
        32512, 32513, 32514, 32515, 32516, 32631, 32640, 32641, 32642,
        32643, 32644, 32645, 32646, 32648, 32649, 32650, 32651,
    )
    _SPI_SETCURSORS = 0x0057
    _cursor_hidden_state = {"hidden": False, "blank": None}

    def _make_blank_cursor():
        # 32x32 monochrome cursor: AND mask = all 1s (transparent),
        # XOR mask = all 0s (no draw). 32*32 bits = 128 bytes per mask.
        and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
        xor_mask = (ctypes.c_ubyte * 128)(*([0x00] * 128))
        return user32.CreateCursor(None, 0, 0, 32, 32,
                                   ctypes.byref(and_mask),
                                   ctypes.byref(xor_mask))

    def hide_cursor():
        if _cursor_hidden_state["hidden"]:
            return
        if _cursor_hidden_state["blank"] is None:
            _cursor_hidden_state["blank"] = _make_blank_cursor()
        blank = _cursor_hidden_state["blank"]
        for cid in _OCR_IDS:
            # SetSystemCursor takes ownership of the handle, so copy each time.
            copied = user32.CopyIcon(blank)
            if copied:
                user32.SetSystemCursor(copied, cid)
        _cursor_hidden_state["hidden"] = True

    def show_cursor():
        if not _cursor_hidden_state["hidden"]:
            return
        # Restore default system cursors from registry.
        user32.SystemParametersInfoW(_SPI_SETCURSORS, 0, None, 0)
        _cursor_hidden_state["hidden"] = False

else:
    raise RuntimeError("Поддерживаются только macOS и Windows")


# ============================================================
# Конфиг
# ============================================================

def config_path():
    return Path(__file__).parent / "mousebridge.json"

def load_or_create_config():
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    cfg = {"uuid": str(uuid.uuid4()), "hostname": platform.node()}
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


# ============================================================
# mDNS-listener
# ============================================================

class MdnsListener(ServiceListener):
    def __init__(self, bridge):
        self.bridge = bridge

    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name)
        if not info:
            return
        peer_uuid = None
        if info.properties:
            v = info.properties.get(b"uuid")
            if v:
                peer_uuid = v.decode("utf-8", errors="ignore")
        if peer_uuid == self.bridge.cfg["uuid"]:
            return
        all_addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        v4 = [a for a in all_addrs if ":" not in a]
        if not v4:
            return
        port = info.port or PORT
        peer_addrs = [(ip, port) for ip in v4]
        print(f"[mdns] resolved {name} → {peer_addrs}")
        self.bridge.on_peer_found(peer_addrs, peer_uuid)

    def remove_service(self, zc, type_, name):
        pass

    def update_service(self, zc, type_, name):
        pass


# ============================================================
# Мост
# ============================================================

class Bridge:
    def __init__(self):
        self.cfg = load_or_create_config()
        self.sw, self.sh = screen_size()
        self.cx, self.cy = self.sw // 2, self.sh // 2

        print(f"[mousebridge] hostname={self.cfg['hostname']} "
              f"uuid={self.cfg['uuid'][:8]} screen={self.sw}x{self.sh}")

        self.local = True
        self.peer = None              # primary (first in peer_addrs)
        self.peer_addrs = []          # all candidate (ip, port) from mDNS
        self.peer_confirmed = None    # source addr of last actually-received packet
        self.peer_uuid = None
        self.expect_recenter = False
        # Secondary node = the one with the LEXICOGRAPHICALLY LARGER UUID.
        # On secondary the cursor is hidden so we don't have two visible
        # cursors fighting for attention. Input is still captured and sent.
        self.secondary = False
        self._cursor_hidden = False
        self.last_x, self.last_y = cursor_pos()

        self.last_esc_ms = 0
        self.panic_until = 0

        self.sent_count = 0
        self.recv_count = 0

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", PORT))

        self.zc = Zeroconf()
        self.register_mdns()
        ServiceBrowser(self.zc, SERVICE_TYPE, MdnsListener(self))

        threading.Thread(target=self.rx_loop, daemon=True).start()

        self.mouse_ctrl = mouse.Controller()
        self.kb_ctrl = keyboard.Controller()

        self.mouse_listener = mouse.Listener(
            on_move=self.on_mouse_move,
            on_click=self.on_mouse_click,
            on_scroll=self.on_mouse_scroll,
        )
        self.kb_listener = keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release,
        )
        self.mouse_listener.start()
        self.kb_listener.start()

    # -------- mDNS --------

    def register_mdns(self):
        local_ips = self.detect_local_ips()
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.cfg['uuid']}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip) for ip in local_ips],
            port=PORT,
            properties={"uuid": self.cfg["uuid"], "hostname": self.cfg["hostname"]},
            server=f"{self.cfg['hostname']}.local.",
        )
        self.zc.register_service(info)
        print(f"[mdns] registered as {self.cfg['uuid'][:8]} on {local_ips}:{PORT}")

    def detect_local_ips(self):
        ips = set()
        # default-route IP (could be VPN tunnel; we still include it).
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        # All addresses associated with the hostname.
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except Exception:
            pass
        # Filter out loopback / link-local. Keep everything else; we'll send
        # to ALL of them on the receiver side and let the first one that
        # actually routes win.
        result = sorted(ip for ip in ips
                        if not ip.startswith("127.")
                        and not ip.startswith("169.254."))
        return result or ["127.0.0.1"]

    def on_peer_found(self, addrs, peer_uuid):
        if self.peer_addrs:
            return
        self.peer_addrs = list(addrs)
        self.peer = addrs[0]
        self.peer_uuid = peer_uuid
        print(f"[mdns] peer found: {addrs} uuid={peer_uuid[:8] if peer_uuid else '?'}")
        self.refresh_role()

    # -------- сеть --------

    def in_panic(self):
        return (time.time() * 1000) < self.panic_until

    def panic_seconds_left(self):
        return max(0, int((self.panic_until - time.time() * 1000) / 1000))

    def send(self, msg):
        if self.in_panic():
            return
        targets = []
        if self.peer_confirmed:
            targets = [self.peer_confirmed]
        else:
            targets = list(self.peer_addrs)
        if not targets:
            return
        data = json.dumps(msg).encode("utf-8")
        for t in targets:
            try:
                self.sock.sendto(data, t)
            except Exception as e:
                print(f"[send] {t}: {e}")
        self.sent_count += 1

    def rx_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except Exception:
                continue
            if self.in_panic():
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            # Pin the actually-working source address for future sends.
            if self.peer_confirmed != addr:
                print(f"[rx] peer confirmed at {addr}")
            self.peer_confirmed = addr
            self.recv_count += 1
            self.handle_msg(msg)

    def handle_msg(self, msg):
        # Step 2: ONLY mouse-move sync. Click/wheel/key are dropped on the
        # receiver side too (defence in depth — if some old peer still sends).
        t = msg.get("type")
        if t == "Move":
            cx, cy = cursor_pos()
            nx = max(0, min(self.sw - 1, cx + msg["dx"]))
            ny = max(0, min(self.sh - 1, cy + msg["dy"]))
            warp_cursor(nx, ny)
            # Echo-suppression: keep last_x/last_y in sync with the warp so
            # the on_move callback for this synthetic motion computes dx=0
            # and doesn't bounce back.
            self.last_x, self.last_y = nx, ny

    def inject_key(self, k, pressed):
        try:
            if k.startswith("Key."):
                key = getattr(keyboard.Key, k.split(".", 1)[1], None)
                if key is None:
                    return
            else:
                key = k
            if pressed:
                self.kb_ctrl.press(key)
            else:
                self.kb_ctrl.release(key)
        except Exception:
            pass

    # -------- переключение состояний --------

    def go_local(self, edge="left"):
        self.local = True
        show_cursor()
        if edge == "left":
            warp_cursor(5, self.sh // 2)
        elif edge == "right":
            warp_cursor(self.sw - 5, self.sh // 2)
        else:
            warp_cursor(self.cx, self.cy)
        self.last_x, self.last_y = cursor_pos()
        print(f"[state] LOCAL (cursor here, edge={edge})")

    def go_captured(self):
        self.local = False
        hide_cursor()
        self.expect_recenter = True
        warp_cursor(self.cx, self.cy)
        self.last_x, self.last_y = self.cx, self.cy
        print("[state] CAPTURED (cursor on peer)")

    # -------- локальные input handlers --------

    def on_mouse_move(self, x, y):
        if self.in_panic() or not self.peer_addrs:
            self.last_x, self.last_y = x, y
            return
        dx = x - self.last_x
        dy = y - self.last_y
        # Echo from our own warp_cursor (when peer Move arrives, we call warp,
        # which fires on_move with pt == new pos; we already updated last_x/y
        # in handle_msg to match, so dx=0 here and we send nothing).
        self.last_x, self.last_y = x, y
        if dx != 0 or dy != 0:
            self.send({"type": "Move", "dx": int(dx), "dy": int(dy)})

    def on_mouse_click(self, x, y, button, pressed):
        # Step 2: clicks are NOT transmitted — they cause echo loops where
        # Controller.press() generates a click that Listener captures and
        # sends back. Only Move events are forwarded for now.
        return

    def on_mouse_scroll(self, x, y, dx, dy):
        # Step 2: scroll not transmitted (same echo-loop risk).
        return

    def on_key_press(self, key):
        # Only PanicKey detection; key events are NOT forwarded.
        if key == keyboard.Key.esc:
            now = time.time() * 1000
            if now - self.last_esc_ms < 500:
                self.trigger_panic()
                self.last_esc_ms = 0
                return
            self.last_esc_ms = now

    def on_key_release(self, key):
        return

    def refresh_role(self):
        # Decide who's secondary based on UUIDs (deterministic).
        if self.peer_uuid:
            self.secondary = self.cfg["uuid"] > self.peer_uuid
        else:
            self.secondary = False
        # Cursor is hidden ONLY when:
        #   - we have a peer, and
        #   - we are the secondary node, and
        #   - we are not in panic (panic must give the user back their cursor).
        want_hidden = (
            self.secondary
            and bool(self.peer_addrs)
            and not self.in_panic()
        )
        if want_hidden and not self._cursor_hidden:
            hide_cursor()
            self._cursor_hidden = True
            print("[role] secondary → cursor hidden")
        elif not want_hidden and self._cursor_hidden:
            show_cursor()
            self._cursor_hidden = False
            print("[role] cursor shown")

    def trigger_panic(self):
        print("[PANIC] двойной Esc → 30s disconnect")
        try:
            self.send({"type": "Disconnect"})
        except Exception:
            pass
        self.panic_until = time.time() * 1000 + 30_000
        self.go_local(edge="center")
        # Make the cursor visible right away regardless of role.
        self.refresh_role()


# ============================================================
# GUI (PyQt5)
# ============================================================

class StatusWindow(QtWidgets.QWidget):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
        self.setWindowTitle("MouseBridge")
        self.resize(500, 320)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)

        self.status_lbl = QtWidgets.QLabel()
        font = QtGui.QFont()
        font.setPointSize(22)
        font.setBold(True)
        self.status_lbl.setFont(font)
        self.status_lbl.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.status_lbl)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addWidget(line)

        self.host_lbl = QtWidgets.QLabel()
        layout.addWidget(self.host_lbl)
        self.peer_lbl = QtWidgets.QLabel()
        layout.addWidget(self.peer_lbl)
        self.counters_lbl = QtWidgets.QLabel()
        layout.addWidget(self.counters_lbl)

        line2 = QtWidgets.QFrame()
        line2.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addWidget(line2)

        hint = QtWidgets.QLabel(
            "<b>Управление:</b><br>"
            "• Довести курсор до правого/левого края экрана → handover к peer'у.<br>"
            "• Двойной <b>Esc</b> в течение 500 мс → panic disconnect (30 сек).<br>"
            "• Закрыть это окно → выход."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        b = self.bridge
        # Re-evaluate role / cursor visibility periodically so that when
        # the panic timer expires we automatically re-hide if secondary.
        b.refresh_role()
        if b.in_panic():
            self.status_lbl.setText(f"PANIC ({b.panic_seconds_left()}s)")
            self.status_lbl.setStyleSheet("color: #d97706;")
        elif not b.peer_addrs:
            self.status_lbl.setText("LOCAL — peer не найден")
            self.status_lbl.setStyleSheet("color: #16a34a;")
        elif b.secondary:
            self.status_lbl.setText("SYNC — secondary (курсор скрыт)")
            self.status_lbl.setStyleSheet("color: #6b7280;")
        else:
            self.status_lbl.setText("SYNC — primary (курсор виден)")
            self.status_lbl.setStyleSheet("color: #2563eb;")

        self.host_lbl.setText(
            f"<b>Этот узел:</b> {b.cfg['hostname']}  "
            f"<code>uuid={b.cfg['uuid'][:8]}</code>  "
            f"screen={b.sw}×{b.sh}"
        )
        if b.peer_addrs:
            puuid = b.peer_uuid[:8] if b.peer_uuid else "?"
            confirmed = f"  ✓ {b.peer_confirmed[0]}" if b.peer_confirmed else "  (не подтверждён)"
            addrs_str = ", ".join(f"{a[0]}" for a in b.peer_addrs)
            self.peer_lbl.setText(f"<b>Peer</b> uuid={puuid}: {addrs_str}{confirmed}")
        else:
            self.peer_lbl.setText("<b>Peer:</b> идёт поиск через mDNS…")

        self.counters_lbl.setText(
            f"<b>Стат.:</b> отправлено={b.sent_count}  получено={b.recv_count}"
        )


# ============================================================
# main
# ============================================================

def main():
    headless = "--no-gui" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    bridge = Bridge()

    if args:
        ip = args[0]
        bridge.peer_addrs = [(ip, PORT)]
        bridge.peer = (ip, PORT)
        print(f"[manual] peer = {bridge.peer}")

    if headless:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nbye")
        return

    app = QtWidgets.QApplication(sys.argv)
    win = StatusWindow(bridge)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
