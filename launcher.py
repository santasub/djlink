#!/usr/bin/env python3
"""
reTerminal Device Launcher
--------------------------
Touch-friendly fullscreen menu for the reTerminal 5" screen (1280x720).
Options: Launch App (with network interface selector) · Update · Reboot · Shutdown
"""

import sys
import os
import socket
import struct
import subprocess
import logging
import json
import urllib.request
import urllib.error

from qtpy.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QButtonGroup, QScrollArea,
    QFrame, QPlainTextEdit, QSizePolicy, QDialog
)
from qtpy.QtCore import Qt, QProcess, QTimer, QThread, Signal, QPoint, QEvent
from qtpy.QtGui import QFont

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_PREFS_FILE = os.path.join(_REPO_DIR, ".launcher_prefs.json")
_GITHUB_REPO = "santasub/djlink"             # GitHub repo für Branch-Liste
_DEFAULT_BRANCH = "main"


# ── Helpers ───────────────────────────────────────────────────────────────────

# Names/prefixes to always skip
_SKIP_PREFIXES = ("lo", "loopback", "docker", "br-", "veth", "virbr",
                  "vmnet", "vbox", "ppp", "tun", "tap")
_SKIP_CONTAINS = ("pseudo", "loopback", "isatap", "teredo", "vmware",
                  "vethernet", "wsl", "hyper-v", "bluetooth", "virtual")


def _is_useful(name: str, ip: str) -> bool:
    nl = name.lower()
    if any(nl.startswith(p) for p in _SKIP_PREFIXES):
        return False
    if any(s in nl for s in _SKIP_CONTAINS):
        return False
    if not ip or ip.startswith("127.") or ip.startswith("169.254."):
        return False
    return True


def _get_interfaces_windows():
    """Use `netsh interface ip show addresses` to get friendly name + IP on Windows."""
    ifaces = []
    try:
        out = subprocess.check_output(
            ["netsh", "interface", "ip", "show", "addresses"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode(errors="replace")
        current_name = None
        for line in out.splitlines():
            # Line like: Configuration for interface "Wi-Fi"  (any locale)
            if '"' in line and ("interface" in line.lower() or "schnittstelle" in line.lower()
                                or "konfiguration" in line.lower()):
                parts = line.split('"')
                if len(parts) >= 2:
                    current_name = parts[1].strip()
            # Line like:     IP Address:  192.168.1.175  (any locale)
            elif current_name and ("ip" in line.lower() or "ip-adresse" in line.lower()
                                   or "adresse" in line.lower()):
                # grab the last token that looks like an IPv4 address
                for token in reversed(line.split()):
                    parts = token.split(".")
                    if len(parts) == 4 and all(p.isdigit() for p in parts):
                        ip = token
                        if _is_useful(current_name, ip):
                            ifaces.append((current_name, ip))
                        current_name = None
                        break
    except Exception:
        pass
    return ifaces


def _get_interfaces():
    """Return list of (friendly_name, ip) for real interfaces with a routable IPv4."""
    ifaces = []

    # Linux / Pi: `ip` command gives clean output
    if sys.platform != "win32":
        try:
            out = subprocess.check_output(
                ["ip", "-o", "-4", "addr", "show", "up"],
                stderr=subprocess.DEVNULL, timeout=3
            ).decode()
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                name = parts[1]
                ip = parts[3].split("/")[0]
                if _is_useful(name, ip):
                    ifaces.append((name, ip))
            if ifaces:
                return ifaces
        except Exception:
            pass

    # Windows: netsh gives friendly names
    if sys.platform == "win32":
        ifaces = _get_interfaces_windows()
        if ifaces:
            return ifaces

    # macOS / fallback: netifaces
    try:
        import netifaces
        for name in netifaces.interfaces():
            addrs = netifaces.ifaddresses(name).get(netifaces.AF_INET, [])
            for addr in addrs:
                ip = addr.get("addr", "")
                if _is_useful(name, ip):
                    ifaces.append((name, ip))
                    break
        if ifaces:
            return ifaces
    except Exception:
        pass

    return ifaces


def _load_prefs() -> dict:
    try:
        with open(_PREFS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_prefs(prefs: dict):
    try:
        with open(_PREFS_FILE, "w") as f:
            json.dump(prefs, f)
    except Exception:
        pass


# ── Branch fetch (background thread) ────────────────────────────────────────

class _BranchFetcher(QThread):
    """Fetches branch list from GitHub API in a background thread."""
    finished = Signal(list)   # emits list[str] of branch names
    error    = Signal(str)    # emits error message

    def run(self):
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/branches?per_page=100"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": "prodj-launcher/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            branches = [b["name"] for b in data]
            self.finished.emit(branches)
        except Exception as exc:
            self.error.emit(str(exc))


# ── Kinetic-scroll helper ───────────────────────────────────────────────────

class _SwipeScrollArea(QScrollArea):
    """
    QScrollArea with finger-swipe / kinetic scrolling.
    No scrollbar visible — the user drags the content directly.
    A small momentum effect is applied after release.
    """
    # Pixels of movement before we treat it as a scroll (not a tap)
    _DRAG_THRESHOLD = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._drag_active  = False
        self._drag_start_y = 0          # finger start position (viewport coords)
        self._scroll_start = 0          # scrollbar value at drag start
        self._last_y       = 0
        self._velocity     = 0.0        # px/tick for momentum
        self._is_scrolling = False      # True once threshold is crossed

        # Kinetic momentum timer (fires every 16 ms ≈ 60 fps)
        self._momentum_timer = QTimer(self)
        self._momentum_timer.setInterval(16)
        self._momentum_timer.timeout.connect(self._momentum_tick)

        # Allow the viewport to receive mouse / touch events
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.viewport():
            t = event.type()
            if t == QEvent.MouseButtonPress:
                self._on_press(event)
            elif t == QEvent.MouseMove:
                self._on_move(event)
            elif t == QEvent.MouseButtonRelease:
                self._on_release(event)
        return super().eventFilter(obj, event)

    # ——— touch / mouse handlers ————————————————————————————————————

    def _on_press(self, event):
        self._momentum_timer.stop()
        self._drag_active  = True
        self._is_scrolling = False
        self._drag_start_y = event.pos().y()
        self._last_y       = self._drag_start_y
        self._scroll_start = self.verticalScrollBar().value()
        self._velocity     = 0.0

    def _on_move(self, event):
        if not self._drag_active:
            return
        dy = event.pos().y() - self._drag_start_y
        if not self._is_scrolling and abs(dy) < self._DRAG_THRESHOLD:
            return                          # still within tap tolerance
        self._is_scrolling = True
        # velocity: difference from last position (for momentum)
        self._velocity = event.pos().y() - self._last_y
        self._last_y   = event.pos().y()
        self.verticalScrollBar().setValue(self._scroll_start - dy)

    def _on_release(self, event):
        if not self._drag_active:
            return
        self._drag_active = False
        if self._is_scrolling and abs(self._velocity) > 2:
            self._momentum_timer.start()

    def _momentum_tick(self):
        """Gradually decelerate after finger lift."""
        self._velocity *= 0.82            # friction factor
        if abs(self._velocity) < 0.5:
            self._momentum_timer.stop()
            return
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() - int(self._velocity))

    def is_scrolling(self) -> bool:
        """True while a swipe is in progress — lets buttons suppress their click."""
        return self._is_scrolling


# ── Branch selector dialog ────────────────────────────────────────────────────

class BranchSelectDialog(QDialog):
    """Touch-friendly modal that lets the user pick a git branch."""

    def __init__(self, current_branch: str, parent=None):
        super().__init__(parent)
        self._selected = current_branch
        self._scroll   = None           # set after build — used by branch buttons
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setFixedSize(700, 460)
        self.setStyleSheet("""
            QDialog {
                background: #1f2937;
                border: 2px solid #374151;
                border-radius: 14px;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 28)
        root.setSpacing(16)

        # Title
        lbl_title = QLabel("Select Update Branch")
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet("color:#7dd3fc;font-size:18pt;font-weight:bold;")
        root.addWidget(lbl_title)

        lbl_sub = QLabel("Swipe to scroll  •  Changes take effect on next  ⟳ Update")
        lbl_sub.setAlignment(Qt.AlignCenter)
        lbl_sub.setStyleSheet("color:#6b7280;font-size:10pt;")
        root.addWidget(lbl_sub)

        # Swipeable branch list
        self._scroll = _SwipeScrollArea()
        self._scroll.setWidgetResizable(True)
        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setSpacing(8)
        self._list_layout.setContentsMargins(4, 4, 4, 4)
        self._scroll.setWidget(self._list_widget)
        root.addWidget(self._scroll, 1)

        # Loading indicator
        self._lbl_loading = QLabel("Fetching branches from GitHub…")
        self._lbl_loading.setAlignment(Qt.AlignCenter)
        self._lbl_loading.setStyleSheet("color:#9ca3af;font-size:12pt;")
        self._list_layout.addWidget(self._lbl_loading)
        self._list_layout.addStretch()

        # Bottom row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(16)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setMinimumHeight(72)
        btn_cancel.setStyleSheet(
            "QPushButton{background:#374151;border:2px solid #4b5563;"
            "border-radius:10px;color:#e5e7eb;font-size:15pt;font-weight:700;}"
            "QPushButton:pressed{background:#4b5563;}"
        )
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        self._btn_ok = QPushButton("Use this branch")
        self._btn_ok.setMinimumHeight(72)
        self._btn_ok.setStyleSheet(
            "QPushButton{background:#1e3a5f;border:2px solid #0ea5e9;"
            "border-radius:10px;color:white;font-size:15pt;font-weight:700;}"
            "QPushButton:pressed{background:#0ea5e9;}"
        )
        self._btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(self._btn_ok)

        root.addLayout(btn_row)

        self._branch_group = QButtonGroup(self)
        self._branch_group.setExclusive(True)

        # Start background fetch
        self._fetcher = _BranchFetcher(self)
        self._fetcher.finished.connect(self._on_branches)
        self._fetcher.error.connect(self._on_error)
        self._fetcher.start()

    def selected_branch(self) -> str:
        return self._selected

    def _on_branches(self, branches: list):
        # Remove loading label + trailing stretch
        self._lbl_loading.deleteLater()
        item = self._list_layout.takeAt(self._list_layout.count() - 1)
        if item:
            del item

        for name in branches:
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setMinimumHeight(62)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton {
                    background: #111827;
                    border: 2px solid #374151;
                    border-radius: 8px;
                    color: #e5e7eb;
                    font-size: 13pt;
                    font-weight: 600;
                    text-align: left;
                    padding: 0 16px;
                }
                QPushButton:checked {
                    background: #0c4a6e;
                    border: 2px solid #0ea5e9;
                    color: #7dd3fc;
                    font-weight: 700;
                }
            """)
            # Only register click when the finger didn't scroll
            def _on_click(checked, n=name, b=btn):
                if self._scroll and self._scroll.is_scrolling():
                    b.setChecked(n == self._selected)   # revert visual state
                    return
                self._pick(n)
            btn.clicked.connect(_on_click)
            self._branch_group.addButton(btn)
            self._list_layout.addWidget(btn)
            if name == self._selected:
                btn.setChecked(True)

        self._list_layout.addStretch()

    def _on_error(self, msg: str):
        self._lbl_loading.setText(f"Could not fetch branches:\n{msg}")
        self._lbl_loading.setStyleSheet("color:#ef4444;font-size:11pt;")

    def _pick(self, name: str):
        self._selected = name


# ── Button factory ────────────────────────────────────────────────────────────

def _make_btn(text: str, color: str, border: str, height: int = 100) -> QPushButton:
    b = QPushButton(text)
    b.setMinimumHeight(height)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(f"""
        QPushButton {{
            background: {color};
            border: 2px solid {border};
            border-radius: 10px;
            color: white;
            font-size: 20pt;
            font-weight: 700;
        }}
        QPushButton:pressed {{ background: {border}; }}
        QPushButton:checked {{
            background: {border};
            border: 3px solid white;
        }}
    """)
    return b


# ── Main window ───────────────────────────────────────────────────────────────

class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProDJ Link — Launcher")
        self.setWindowFlags(Qt.FramelessWindowHint)
        self._process = None
        self._prefs = _load_prefs()
        self._selected_iface = self._prefs.get("iface", "")
        self._selected_branch = self._prefs.get("branch", _DEFAULT_BRANCH)
        self._iface_buttons = {}   # name -> QPushButton
        self._known_ifaces = []    # last seen (name, ip) list — used to detect changes

        self.setStyleSheet("""
            QWidget {
                background: #111827;
                color: #e5e7eb;
                font-family: "Segoe UI", Arial, sans-serif;
            }
            QScrollArea { border: none; background: transparent; }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 24, 40, 16)
        root.setSpacing(16)

        # ── Title ─────────────────────────────────────────────────────────
        title = QLabel("ProDJ Link MIDI Clock")
        title.setAlignment(Qt.AlignCenter)
        f = title.font()
        f.setPointSize(30)
        f.setBold(True)
        title.setFont(f)
        title.setStyleSheet("color:#7dd3fc;")
        root.addWidget(title)

        # ── Network interface selector ─────────────────────────────────────
        iface_header = QHBoxLayout()
        lbl = QLabel("Network interface:")
        lbl.setStyleSheet("color:#9ca3af; font-size:11pt;")
        iface_header.addWidget(lbl)
        iface_header.addStretch()
        self._iface_status = QLabel("")
        self._iface_status.setStyleSheet("color:#6b7280; font-size:10pt;")
        iface_header.addWidget(self._iface_status)
        root.addLayout(iface_header)

        # Scrollable row of interface buttons
        scroll = QScrollArea()
        scroll.setFixedHeight(90)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)

        iface_container = QWidget()
        iface_container.setStyleSheet("background: transparent;")
        self._iface_row = QHBoxLayout(iface_container)
        self._iface_row.setSpacing(10)
        self._iface_row.setContentsMargins(0, 0, 0, 0)
        self._iface_row.addStretch()

        scroll.setWidget(iface_container)
        root.addWidget(scroll)

        # ── Action buttons ────────────────────────────────────────────────
        self._btn_launch = _make_btn("▶   Launch App", "#065f46", "#10b981", 110)
        self._btn_launch.clicked.connect(self.launch_app)
        self._btn_launch.setEnabled(False)
        root.addWidget(self._btn_launch)

        # Update row: update button + branch selector
        update_row = QHBoxLayout()
        update_row.setSpacing(10)
        self._btn_update = _make_btn("⟳   Update App", "#1e3a5f", "#0ea5e9", 90)
        self._btn_update.clicked.connect(self.update_app)
        update_row.addWidget(self._btn_update, 1)

        self._btn_branch = QPushButton(self._selected_branch)
        self._btn_branch.setMinimumHeight(90)
        self._btn_branch.setMinimumWidth(160)
        self._btn_branch.setCursor(Qt.PointingHandCursor)
        self._btn_branch.setToolTip("Select branch to update from")
        self._btn_branch.clicked.connect(self._select_branch)
        self._update_branch_btn_style()
        update_row.addWidget(self._btn_branch)
        root.addLayout(update_row)

        sys_row = QHBoxLayout()
        sys_row.setSpacing(16)
        self._btn_reboot = _make_btn("↺  Reboot", "#451a03", "#f59e0b", 80)
        self._btn_reboot.clicked.connect(self.reboot)
        sys_row.addWidget(self._btn_reboot)
        self._btn_shutdown = _make_btn("⏻  Shutdown", "#450a0a", "#ef4444", 80)
        self._btn_shutdown.clicked.connect(self.shutdown)
        sys_row.addWidget(self._btn_shutdown)
        root.addLayout(sys_row)

        self._btn_close_log = _make_btn("✕   Close Log", "#1f2937", "#374151", 60)
        self._btn_close_log.clicked.connect(self._close_log)
        self._btn_close_log.setVisible(False)
        root.addWidget(self._btn_close_log)

        # ── Status bar ────────────────────────────────────────────────────
        self._status = QLabel("")
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("color:#6b7280; font-size:10pt;")
        root.addWidget(self._status)

        # ── Update log panel (hidden until update runs) ──────────────────
        self._log_panel = QPlainTextEdit()
        self._log_panel.setReadOnly(True)
        self._log_panel.setMinimumHeight(200)
        self._log_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._log_panel.setVisible(False)
        self._log_panel.setStyleSheet("""
            QPlainTextEdit {
                background: #0d1117;
                color: #9ca3af;
                border: 2px solid #0ea5e9;
                border-radius: 6px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 9pt;
                padding: 6px;
            }
            QScrollBar:vertical {
                background: #1f2937;
                width: 12px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #374151;
                border-rigkadius: 6px;
                min-height: 20px;
            }
        """)
        root.addWidget(self._log_panel)

                # Build interface buttons last — needs _btn_launch and _status to exist
        self._build_iface_buttons()

        # Poll for new/removed interfaces every 5 seconds
        self._iface_poll_timer = QTimer(self)
        self._iface_poll_timer.setInterval(5000)
        self._iface_poll_timer.timeout.connect(self._poll_interfaces)
        self._iface_poll_timer.start()

    # ── Interface buttons ─────────────────────────────────────────────────

    def _poll_interfaces(self):
        """Called every 5 s — rebuild buttons only when the interface list changed."""
        # Don't disturb the UI while an update or app process is running
        if self._process is not None and self._process.state() != QProcess.NotRunning:
            return
        current = _get_interfaces()
        if current != self._known_ifaces:
            logging.info("Interface list changed: %s", current)
            self._build_iface_buttons()

    def _build_iface_buttons(self):
        """Populate the interface button row from live system interfaces."""
        # Clear existing
        while self._iface_row.count() > 1:  # keep trailing stretch
            item = self._iface_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._iface_buttons.clear()

        ifaces = _get_interfaces()
        self._known_ifaces = ifaces   # remember for change-detection

        if not ifaces:
            lbl = QLabel("No network interfaces found — retrying…")
            lbl.setStyleSheet("color:#ef4444; font-size:11pt;")
            self._iface_row.insertWidget(0, lbl)
            self._btn_launch.setEnabled(False)
            self._set_status("Waiting for network interface…", "#f59e0b")
            return

        group = QButtonGroup(self)
        group.setExclusive(True)

        for i, (name, ip) in enumerate(ifaces):
            label = f"{name}\n{ip}" if ip else name
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setMinimumWidth(160)
            btn.setMinimumHeight(70)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton {
                    background: #1f2937;
                    border: 2px solid #374151;
                    border-radius: 8px;
                    color: #e5e7eb;
                    font-size: 12pt;
                    font-weight: 600;
                    padding: 6px 12px;
                }
                QPushButton:hover  { border: 2px solid #0ea5e9; }
                QPushButton:checked {
                    background: #0c4a6e;
                    border: 2px solid #0ea5e9;
                    color: #7dd3fc;
                }
            """)
            btn.clicked.connect(lambda checked, n=name, ip=ip: self._select_iface(n, ip))
            group.addButton(btn)
            self._iface_row.insertWidget(i, btn)
            self._iface_buttons[name] = btn

            # Restore saved selection
            if name == self._selected_iface:
                btn.setChecked(True)

        # Always ensure something is selected:
        # use saved pref if still available, otherwise fall back to first.
        if self._selected_iface not in self._iface_buttons:
            self._selected_iface = ifaces[0][0]

        selected_ip = next(ip for n, ip in ifaces if n == self._selected_iface)
        self._iface_buttons[self._selected_iface].setChecked(True)
        self._iface_status.setText(f"{self._selected_iface}  {selected_ip}")
        self._btn_launch.setEnabled(True)
        self._set_status(f"Ready — launching on {self._selected_iface}.", "#10b981")

    def _select_iface(self, name: str, ip: str):
        self._selected_iface = name
        self._iface_status.setText(f"{name}  {ip}" if ip else name)
        self._prefs["iface"] = name
        _save_prefs(self._prefs)
        self._btn_launch.setEnabled(True)
        self._set_status(f"Ready — launching on {name}.", "#10b981")

    # ── Actions ───────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = "#6b7280"):
        self._status.setText(msg)
        self._status.setStyleSheet(f"color:{color}; font-size:10pt;")

    def launch_app(self):
        if not self._selected_iface:
            self._set_status("Please select a network interface first.", "#f59e0b")
            return

        python = os.path.join(_REPO_DIR, ".venv", "bin", "python3")
        if not os.path.exists(python):
            python = os.path.join(_REPO_DIR, ".venv", "Scripts", "python")
        script = os.path.join(_REPO_DIR, "midiclock-qt.py")

        self._set_status(f"Launching on {self._selected_iface}…", "#10b981")
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.finished.connect(self._app_finished)
        # Redirect stdout+stderr to log file for crash diagnostics
        log_path = os.path.join(_REPO_DIR, "/tmp/midiclock.log")
        self._process.setStandardOutputFile(log_path)
        self._process.start(python, [
            script, "--fullscreen", "--iface", self._selected_iface,
            "--loglevel", "debug"
        ])
        self.hide()

    def _app_finished(self, exit_code, _status):
        self.showFullScreen()
        self._build_iface_buttons()  # refresh — interface may have changed
        self._set_status(
            f"App exited (code {exit_code}).",
            "#10b981" if exit_code == 0 else "#ef4444"
        )

    # ── Branch selection ───────────────────────────────────────────────────

    def _update_branch_btn_style(self):
        is_main = self._selected_branch == _DEFAULT_BRANCH
        bg     = "#1a2e1a" if is_main else "#2d1a00"
        border = "#10b981" if is_main else "#f59e0b"
        color  = "#10b981" if is_main else "#f59e0b"
        self._btn_branch.setText(self._selected_branch)
        self._btn_branch.setStyleSheet(f"""
            QPushButton {{
                background: {bg};
                border: 2px solid {border};
                border-radius: 10px;
                color: {color};
                font-size: 13pt;
                font-weight: 700;
            }}
            QPushButton:pressed {{ background: {border}; color: white; }}
        """)

    def _select_branch(self):
        dlg = BranchSelectDialog(self._selected_branch, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            chosen = dlg.selected_branch()
            if chosen != self._selected_branch:
                self._selected_branch = chosen
                self._prefs["branch"] = chosen
                _save_prefs(self._prefs)
                self._update_branch_btn_style()
                self._set_status(
                    f"Branch set to '{chosen}' — press ⟳ Update to apply.",
                    "#f59e0b" if chosen != _DEFAULT_BRANCH else "#10b981"
                )

    # ── Update ────────────────────────────────────────────────────────────────

    def update_app(self):
        # On Windows run update.sh via Git Bash or WSL if available,
        # otherwise fall back to a simple git pull + pip via Python directly.
        if sys.platform == "win32":
            self._update_windows()
        else:
            self._update_unix()

    def _update_unix(self):
        script = os.path.join(_REPO_DIR, "update.sh")
        if not os.path.exists(script):
            self._set_status("update.sh not found.", "#ef4444")
            return
        # Pass the branch via environment so update.sh picks it up
        env = os.environ.copy()
        env["GIT_BRANCH"] = self._selected_branch
        self._start_update_process("bash", [script], env=env)

    def _update_windows(self):
        """On Windows: git pull + pip install via Python — no bash needed."""
        python = os.path.join(_REPO_DIR, ".venv", "Scripts", "python.exe")
        if not os.path.exists(python):
            python = sys.executable
        req = os.path.join(_REPO_DIR, "requirements.txt")
        branch = self._selected_branch
        cmd = (
            f"import subprocess, sys; "
            f"subprocess.run(['git', 'fetch', 'origin'], check=False); "
            f"subprocess.run(['git', 'reset', '--hard', 'origin/{branch}'], check=False); "
            f"subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', r'{req}', '-q'], check=False)"
        )
        self._start_update_process(python, ["-c", cmd])

    def _start_update_process(self, program: str, args: list, env: dict = None):
        self._log_panel.clear()
        self._log_panel.setVisible(True)
        # Hide main buttons while update runs so the log has full space
        self._btn_launch.setVisible(False)
        self._btn_update.setVisible(False)
        self._btn_branch.setVisible(False)
        self._btn_reboot.setVisible(False)
        self._btn_shutdown.setVisible(False)
        self._set_status(f"Updating from branch '{self._selected_branch}'…", "#0ea5e9")
        self._process = QProcess(self)
        if env is not None:
            from qtpy.QtCore import QProcessEnvironment
            qenv = QProcessEnvironment()
            for k, v in env.items():
                qenv.insert(k, v)
            self._process.setProcessEnvironment(qenv)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.setWorkingDirectory(_REPO_DIR)
        self._process.readyRead.connect(self._update_log_ready)
        self._process.finished.connect(self._update_finished)
        self._process.start(program, args)

    def _update_log_ready(self):
        """Stream output lines into the log panel as they arrive."""
        from qtpy.QtGui import QTextCursor
        raw = bytes(self._process.readAllStandardOutput()).decode(errors="replace")
        self._log_panel.moveCursor(QTextCursor.End)
        self._log_panel.insertPlainText(raw)
        self._log_panel.moveCursor(QTextCursor.End)

    def _update_finished(self, exit_code, _status):
        if exit_code == 0:
            self._set_status("Update complete — restart launcher to apply changes.", "#10b981")
            self._log_panel.appendPlainText("\n✔ Update complete.")
            self._btn_close_log.setText("↺  Restart Launcher")
            self._btn_close_log.setStyleSheet(
                "QPushButton{background:#065f46;border:2px solid #10b981;"
                "border-radius:10px;color:white;font-size:18pt;font-weight:700;}"
                "QPushButton:pressed{background:#047857;}"
            )
            self._btn_close_log.clicked.disconnect()
            self._btn_close_log.clicked.connect(self._restart_launcher)
        else:
            self._set_status(f"Update failed (exit code {exit_code}).", "#ef4444")
            self._log_panel.appendPlainText(f"\n✘ Failed (code {exit_code}).")
        logging.info("Update finished (code %d)", exit_code)
        self._btn_close_log.setVisible(True)

    def _restart_launcher(self):
        """Relaunch this script detached, then exit the current process."""
        python = os.path.join(_REPO_DIR, ".venv", "bin", "python3")
        if not os.path.exists(python):
            python = os.path.join(_REPO_DIR, ".venv", "Scripts", "python.exe")
        if not os.path.exists(python):
            python = sys.executable
        script = os.path.join(_REPO_DIR, "launcher.py")
        QProcess.startDetached(python, [script, "--fullscreen"])
        QApplication.quit()

    def _close_log(self):
        """Dismiss the update log and restore the main buttons."""
        self._log_panel.setVisible(False)
        # Reset close button to default state for next update run
        self._btn_close_log.setText("✕   Close Log")
        self._btn_close_log.setStyleSheet("")
        try:
            self._btn_close_log.clicked.disconnect()
        except Exception:
            pass
        self._btn_close_log.clicked.connect(self._close_log)
        self._btn_close_log.setVisible(False)
        self._btn_launch.setVisible(True)
        self._btn_update.setVisible(True)
        self._btn_branch.setVisible(True)
        self._btn_reboot.setVisible(True)
        self._btn_shutdown.setVisible(True)

    def _confirm(self, title: str, msg: str, confirm_text: str = "Yes",
                 confirm_color: str = "#065f46", confirm_border: str = "#10b981") -> bool:
        """Full-size touch-friendly confirmation dialog — no system QMessageBox."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dlg.setFixedSize(640, 300)
        dlg.setStyleSheet("""
            QDialog {
                background: #1f2937;
                border: 2px solid #374151;
                border-radius: 14px;
            }
        """)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(40, 32, 40, 32)
        layout.setSpacing(24)

        lbl_title = QLabel(title)
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setStyleSheet("color:#f9fafb;font-size:20pt;font-weight:bold;")
        layout.addWidget(lbl_title)

        lbl_msg = QLabel(msg)
        lbl_msg.setAlignment(Qt.AlignCenter)
        lbl_msg.setStyleSheet("color:#9ca3af;font-size:14pt;")
        lbl_msg.setWordWrap(True)
        layout.addWidget(lbl_msg)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(20)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setMinimumHeight(80)
        btn_cancel.setStyleSheet(
            "QPushButton{background:#374151;border:2px solid #4b5563;"
            "border-radius:10px;color:#e5e7eb;font-size:16pt;font-weight:700;}"
            "QPushButton:pressed{background:#4b5563;}"
        )
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_cancel)

        btn_ok = QPushButton(confirm_text)
        btn_ok.setMinimumHeight(80)
        btn_ok.setStyleSheet(
            f"QPushButton{{background:{confirm_color};border:2px solid {confirm_border};"
            "border-radius:10px;color:white;font-size:16pt;font-weight:700;}"
            f"QPushButton:pressed{{background:{confirm_border};}}"
        )
        btn_ok.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_ok)

        layout.addLayout(btn_row)
        return dlg.exec_() == QDialog.Accepted

    def reboot(self):
        if self._confirm("Reboot", "Reboot the device now?",
                         confirm_text="Reboot", confirm_color="#78350f",
                         confirm_border="#f59e0b"):
            subprocess.Popen(["sudo", "reboot"])

    def shutdown(self):
        if self._confirm("Shutdown", "Shut down the device now?",
                         confirm_text="Shutdown", confirm_color="#450a0a",
                         confirm_border="#ef4444"):
            subprocess.Popen(["sudo", "shutdown", "-h", "now"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    app = QApplication(sys.argv)
    win = LauncherWindow()
    win.showFullScreen()
    sys.exit(app.exec_())
