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

from qtpy.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QButtonGroup, QScrollArea,
    QFrame, QPlainTextEdit, QSizePolicy
)
from qtpy.QtCore import Qt, QProcess
from qtpy.QtGui import QFont

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
_PREFS_FILE = os.path.join(_REPO_DIR, ".launcher_prefs.json")


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
        self.setFixedSize(1280, 720)
        self._process = None
        self._prefs = _load_prefs()
        self._selected_iface = self._prefs.get("iface", "")
        self._iface_buttons = {}   # name -> QPushButton

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

        btn_update = _make_btn("⟳   Update App", "#1e3a5f", "#0ea5e9", 90)
        btn_update.clicked.connect(self.update_app)
        root.addWidget(btn_update)

        sys_row = QHBoxLayout()
        sys_row.setSpacing(16)
        btn_reboot = _make_btn("↺  Reboot", "#451a03", "#f59e0b", 80)
        btn_reboot.clicked.connect(self.reboot)
        sys_row.addWidget(btn_reboot)
        btn_shutdown = _make_btn("⏻  Shutdown", "#450a0a", "#ef4444", 80)
        btn_shutdown.clicked.connect(self.shutdown)
        sys_row.addWidget(btn_shutdown)
        root.addLayout(sys_row)

        # ── Status bar ────────────────────────────────────────────────────
        self._status = QLabel("")
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("color:#6b7280; font-size:10pt;")
        root.addWidget(self._status)

        # ── Update log panel (hidden until update runs) ──────────────────
        self._log_panel = QPlainTextEdit()
        self._log_panel.setReadOnly(True)
        self._log_panel.setFixedHeight(180)
        self._log_panel.setVisible(False)
        self._log_panel.setStyleSheet("""
            QPlainTextEdit {
                background: #0d1117;
                color: #9ca3af;
                border: 1px solid #374151;
                border-radius: 6px;
                font-family: "Consolas", "Courier New", monospace;
                font-size: 9pt;
                padding: 6px;
            }
        """)
        root.addWidget(self._log_panel)

        # Build interface buttons last — needs _btn_launch and _status to exist
        self._build_iface_buttons()

    # ── Interface buttons ─────────────────────────────────────────────────

    def _build_iface_buttons(self):
        """Populate the interface button row from live system interfaces."""
        # Clear existing
        while self._iface_row.count() > 1:  # keep trailing stretch
            item = self._iface_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._iface_buttons.clear()

        ifaces = _get_interfaces()
        if not ifaces:
            lbl = QLabel("No network interfaces found.")
            lbl.setStyleSheet("color:#ef4444; font-size:11pt;")
            self._iface_row.insertWidget(0, lbl)
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
        self._process.finished.connect(self._app_finished)
        self._process.start(python, [script, "--fullscreen", "--iface", self._selected_iface])
        self.hide()

    def _app_finished(self, exit_code, _status):
        self.show()
        self._build_iface_buttons()  # refresh — interface may have changed
        self._set_status(
            f"App exited (code {exit_code}).",
            "#10b981" if exit_code == 0 else "#ef4444"
        )

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
        self._start_update_process("bash", [script])

    def _update_windows(self):
        """On Windows: git pull + pip install via Python — no bash needed."""
        python = os.path.join(_REPO_DIR, ".venv", "Scripts", "python.exe")
        if not os.path.exists(python):
            python = sys.executable
        req = os.path.join(_REPO_DIR, "requirements.txt")
        # Run as a small inline script so we get live output
        cmd = (
            f"import subprocess, sys; "
            f"subprocess.run(['git', 'pull', 'origin', 'main'], check=False); "
            f"subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', r'{req}', '-q'], check=False)"
        )
        self._start_update_process(python, ["-c", cmd])

    def _start_update_process(self, program: str, args: list):
        self._log_panel.clear()
        self._log_panel.setVisible(True)
        self._set_status("Updating…", "#0ea5e9")
        self._process = QProcess(self)
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
            self._set_status("Update complete — relaunch the app to use the new version.", "#10b981")
            self._log_panel.appendPlainText("\n✔ Done.")
        else:
            self._set_status(f"Update failed (exit code {exit_code}).", "#ef4444")
            self._log_panel.appendPlainText(f"\n✘ Failed (code {exit_code}).")
        logging.info("Update finished (code %d)", exit_code)

    def _confirm(self, title: str, msg: str) -> bool:
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(msg)
        dlg.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        dlg.setDefaultButton(QMessageBox.Cancel)
        dlg.setStyleSheet("""
            QMessageBox { background:#1f2937; color:#e5e7eb; }
            QPushButton { min-width:120px; min-height:50px; font-size:14pt; }
        """)
        return dlg.exec_() == QMessageBox.Yes

    def reboot(self):
        if self._confirm("Reboot", "Reboot the device now?"):
            subprocess.run(["sudo", "reboot"])

    def shutdown(self):
        if self._confirm("Shutdown", "Shut down the device now?"):
            subprocess.run(["sudo", "shutdown", "-h", "now"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    app = QApplication(sys.argv)
    win = LauncherWindow()
    win.showFullScreen()
    sys.exit(app.exec_())
