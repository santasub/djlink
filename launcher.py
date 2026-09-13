#!/usr/bin/env python3
"""
reTerminal Device Launcher
--------------------------
Touch-friendly fullscreen menu for the reTerminal 5" screen (1280×720).
Options: Launch App · Update App · Reboot · Shutdown
"""

import sys
import os
import subprocess
import logging

from qtpy.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QMessageBox
)
from qtpy.QtCore import Qt, QProcess, QTimer
from qtpy.QtGui import QFont


_REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _btn(text: str, color: str, border: str) -> QPushButton:
    b = QPushButton(text)
    b.setMinimumHeight(120)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(f"""
        QPushButton {{
            background: {color};
            border: 2px solid {border};
            border-radius: 12px;
            color: white;
            font-size: 22pt;
            font-weight: 700;
        }}
        QPushButton:pressed {{
            background: {border};
        }}
    """)
    return b


class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProDJ Link — Launcher")
        self.setFixedSize(1280, 720)
        self._process = None

        self.setStyleSheet("""
            QWidget {
                background: #111827;
                color: #e5e7eb;
                font-family: "Segoe UI", Arial, sans-serif;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 40, 60, 40)
        root.setSpacing(24)

        # ── Title ──────────────────────────────────────────────────────────
        title = QLabel("ProDJ Link MIDI Clock")
        title.setAlignment(Qt.AlignCenter)
        f = title.font()
        f.setPointSize(32)
        f.setBold(True)
        title.setFont(f)
        title.setStyleSheet("color:#7dd3fc;")
        root.addWidget(title)

        sub = QLabel("reTerminal Device Launcher")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("color:#6b7280; font-size:13pt;")
        root.addWidget(sub)

        root.addSpacing(16)

        # ── Main action buttons ────────────────────────────────────────────
        btn_launch = _btn("▶  Launch App", "#065f46", "#10b981")
        btn_launch.clicked.connect(self.launch_app)
        root.addWidget(btn_launch)

        btn_update = _btn("⟳  Update App", "#1e3a5f", "#0ea5e9")
        btn_update.clicked.connect(self.update_app)
        root.addWidget(btn_update)

        # ── System row ────────────────────────────────────────────────────
        sys_row = QHBoxLayout()
        sys_row.setSpacing(24)

        btn_reboot = _btn("↺  Reboot", "#451a03", "#f59e0b")
        btn_reboot.clicked.connect(self.reboot)
        sys_row.addWidget(btn_reboot)

        btn_shutdown = _btn("⏻  Shutdown", "#450a0a", "#ef4444")
        btn_shutdown.clicked.connect(self.shutdown)
        sys_row.addWidget(btn_shutdown)

        root.addLayout(sys_row)

        # ── Status bar ────────────────────────────────────────────────────
        self._status = QLabel("Ready.")
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("color:#6b7280; font-size:10pt;")
        root.addWidget(self._status)

    def _set_status(self, msg: str, color: str = "#6b7280"):
        self._status.setText(msg)
        self._status.setStyleSheet(f"color:{color}; font-size:10pt;")

    def launch_app(self):
        self._set_status("Launching ProDJ Link MIDI Clock…", "#10b981")
        python = os.path.join(_REPO_DIR, ".venv", "bin", "python3")
        if not os.path.exists(python):
            python = os.path.join(_REPO_DIR, ".venv", "Scripts", "python")
        script = os.path.join(_REPO_DIR, "midiclock-qt.py")

        self._process = QProcess(self)
        self._process.finished.connect(self._app_finished)
        self._process.start(python, [script, "--fullscreen"])
        self.hide()

    def _app_finished(self, exit_code, _status):
        self.show()
        self._set_status(
            f"App exited (code {exit_code}).",
            "#10b981" if exit_code == 0 else "#ef4444"
        )

    def update_app(self):
        self._set_status("Running update script…", "#0ea5e9")
        script = os.path.join(_REPO_DIR, "update.sh")
        if not os.path.exists(script):
            self._set_status("update.sh not found.", "#ef4444")
            return

        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.finished.connect(self._update_finished)
        self._process.start("bash", [script])

    def _update_finished(self, exit_code, _status):
        out = bytes(self._process.readAllStandardOutput()).decode(errors="replace")
        if exit_code == 0:
            self._set_status("Update complete. Restart the app to use the new version.", "#10b981")
        else:
            self._set_status(f"Update failed (code {exit_code}). Check terminal.", "#ef4444")
        logging.info("update.sh output:\n%s", out)

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
