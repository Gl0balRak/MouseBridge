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
        Quartz.CGAssociateMouseAndMouseCursorPosition(False)
        Quartz.CGDisplayHideCursor(Quartz.CGMainDisplayID())

    def show_cursor():
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
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

    def hide_cursor():
        for _ in range(64):
            if user32.ShowCursor(False) < 0:
                break

    def show_cursor():
        for _ in range(64):
            if user32.ShowCursor(True) >= 0:
                break

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
        addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        if not addrs:
            return
        peer_addr = (addrs[0], info.port or PORT)
        self.bridge.on_peer_found(peer_addr, peer_uuid)

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
        self.peer = None
        self.peer_uuid = None
        self.expect_recenter = False
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
        local_ip = self.detect_local_ip()
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.cfg['uuid']}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=PORT,
            properties={"uuid": self.cfg["uuid"], "hostname": self.cfg["hostname"]},
            server=f"{self.cfg['hostname']}.local.",
        )
        self.zc.register_service(info)
        print(f"[mdns] registered as {self.cfg['uuid'][:8]} on {local_ip}:{PORT}")

    def detect_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def on_peer_found(self, addr, peer_uuid):
        if self.peer is not None:
            return
        self.peer = addr
        self.peer_uuid = peer_uuid
        print(f"[mdns] peer found: {addr} uuid={peer_uuid[:8] if peer_uuid else '?'}")
        if peer_uuid and self.cfg["uuid"] > peer_uuid:
            print("[bootstrap] my uuid > peer uuid → yielding cursor")
            self.go_captured()

    # -------- сеть --------

    def in_panic(self):
        return (time.time() * 1000) < self.panic_until

    def panic_seconds_left(self):
        return max(0, int((self.panic_until - time.time() * 1000) / 1000))

    def send(self, msg):
        if not self.peer or self.in_panic():
            return
        try:
            self.sock.sendto(json.dumps(msg).encode("utf-8"), self.peer)
            self.sent_count += 1
        except Exception as e:
            print(f"[send] error: {e}")

    def rx_loop(self):
        while True:
            try:
                data, _addr = self.sock.recvfrom(4096)
            except Exception:
                continue
            if self.in_panic():
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            self.recv_count += 1
            self.handle_msg(msg)

    def handle_msg(self, msg):
        t = msg.get("type")
        if t == "Move":
            if self.local:
                cx, cy = cursor_pos()
                nx = max(0, min(self.sw - 1, cx + msg["dx"]))
                ny = max(0, min(self.sh - 1, cy + msg["dy"]))
                warp_cursor(nx, ny)
        elif t == "Button":
            if self.local:
                btn_map = {1: mouse.Button.left, 2: mouse.Button.right, 3: mouse.Button.middle}
                btn = btn_map.get(msg["button"])
                if btn:
                    if msg["pressed"]:
                        self.mouse_ctrl.press(btn)
                    else:
                        self.mouse_ctrl.release(btn)
        elif t == "Wheel":
            if self.local:
                self.mouse_ctrl.scroll(msg.get("dx", 0), msg.get("dy", 0))
        elif t == "Key":
            if self.local:
                self.inject_key(msg["key"], msg["pressed"])
        elif t == "TakeOver":
            self.go_local(edge=msg.get("edge", "left"))
        elif t == "Disconnect":
            self.go_local(edge="center")

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
        if self.in_panic() or not self.peer:
            return
        if self.expect_recenter and abs(x - self.cx) < 2 and abs(y - self.cy) < 2:
            self.expect_recenter = False
            self.last_x, self.last_y = self.cx, self.cy
            return

        if self.local:
            if x >= self.sw - 1:
                print(f"[edge] right at ({x},{y}) → TakeOver(left)")
                self.send({"type": "TakeOver", "edge": "left"})
                self.go_captured()
                return
            if x <= 0:
                print(f"[edge] left at ({x},{y}) → TakeOver(right)")
                self.send({"type": "TakeOver", "edge": "right"})
                self.go_captured()
                return
            self.last_x, self.last_y = x, y
        else:
            dx = x - self.last_x
            dy = y - self.last_y
            if dx != 0 or dy != 0:
                self.send({"type": "Move", "dx": int(dx), "dy": int(dy)})
            self.expect_recenter = True
            warp_cursor(self.cx, self.cy)
            self.last_x, self.last_y = self.cx, self.cy

    def on_mouse_click(self, x, y, button, pressed):
        if self.in_panic() or self.local or not self.peer:
            return
        btn_map = {mouse.Button.left: 1, mouse.Button.right: 2, mouse.Button.middle: 3}
        b = btn_map.get(button)
        if b:
            self.send({"type": "Button", "button": b, "pressed": pressed})

    def on_mouse_scroll(self, x, y, dx, dy):
        if self.in_panic() or self.local or not self.peer:
            return
        self.send({"type": "Wheel", "dx": int(dx), "dy": int(dy)})

    def on_key_press(self, key):
        if key == keyboard.Key.esc:
            now = time.time() * 1000
            if now - self.last_esc_ms < 500:
                self.trigger_panic()
                self.last_esc_ms = 0
                return
            self.last_esc_ms = now

        if self.in_panic() or self.local or not self.peer:
            return
        try:
            k = key.char if hasattr(key, "char") and key.char else str(key)
            self.send({"type": "Key", "key": k, "pressed": True})
        except Exception:
            pass

    def on_key_release(self, key):
        if self.in_panic() or self.local or not self.peer:
            return
        try:
            k = key.char if hasattr(key, "char") and key.char else str(key)
            self.send({"type": "Key", "key": k, "pressed": False})
        except Exception:
            pass

    def trigger_panic(self):
        print("[PANIC] двойной Esc → 30s disconnect")
        try:
            self.send({"type": "Disconnect"})
        except Exception:
            pass
        self.panic_until = time.time() * 1000 + 30_000
        self.go_local(edge="center")


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
        if b.in_panic():
            self.status_lbl.setText(f"PANIC ({b.panic_seconds_left()}s)")
            self.status_lbl.setStyleSheet("color: #d97706;")
        elif b.local:
            self.status_lbl.setText("LOCAL — курсор здесь")
            self.status_lbl.setStyleSheet("color: #16a34a;")
        else:
            self.status_lbl.setText("CAPTURED — курсор у peer'а")
            self.status_lbl.setStyleSheet("color: #2563eb;")

        self.host_lbl.setText(
            f"<b>Этот узел:</b> {b.cfg['hostname']}  "
            f"<code>uuid={b.cfg['uuid'][:8]}</code>  "
            f"screen={b.sw}×{b.sh}"
        )
        if b.peer:
            puuid = b.peer_uuid[:8] if b.peer_uuid else "?"
            self.peer_lbl.setText(f"<b>Peer:</b> {b.peer[0]}:{b.peer[1]} <code>uuid={puuid}</code>")
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
