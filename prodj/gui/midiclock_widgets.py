import logging
import sys
import time
from typing import Optional, List, Tuple, Dict
from qtpy.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QGridLayout, QFrame, QSizePolicy, QDialog,
                             QGroupBox, QRadioButton, QDialogButtonBox, QSlider,
                             QMessageBox, QDoubleSpinBox, QScrollArea)
from qtpy.QtCore import Qt, Signal, QTimer, QEvent, QRect, QPoint
from qtpy.QtGui import QColor, QPainter, QPixmap, QPen, QBrush, QFont, QFontMetrics

# MIDI Clock imports
from prodj.midi.midiclock_rtmidi import MidiClock as RtMidiClock, list_ports as rtmidi_list_ports
AlsaMidiClock = None
if sys.platform.startswith('linux'):
    try:
        from prodj.midi.midiclock_alsaseq import MidiClock as AlsaMidiClock
    except ImportError:
        logging.warning(
            "AlsaMidiClock not available (alsaseq missing). Falling back to rtmidi."
        )
        AlsaMidiClock = None

# ── Touch BPM slider ─────────────────────────────────────────────────────────

class _TouchBpmSlider(QWidget):
    """Custom horizontal BPM slider designed for finger use on small touchscreens.

    - No tiny handle to grab: drag anywhere on the widget to scrub
    - Emits valueChanged(int) on every move and sliderReleased() on lift
    - Range: 300..2000  (= 30.0..200.0 BPM ×10)
    - Large hit area, amber fill, value displayed inside the groove
    """
    valueChanged  = Signal(int)
    sliderReleased = Signal()

    _BPM_MIN = 300
    _BPM_MAX = 2000
    _DRAG_THRESHOLD = 4   # px before motion is treated as a drag

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 1200          # default 120.0 BPM
        self._dragging = False
        self._drag_start_x = 0
        self._drag_start_val = 0
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self._enabled = True

    # ── public API (matches QSlider interface used by existing code) ─────

    def value(self) -> int:
        return self._value

    def setValue(self, v: int):
        v = max(self._BPM_MIN, min(self._BPM_MAX, int(v)))
        if v != self._value:
            self._value = v
            self.valueChanged.emit(v)
            self.update()

    def setRange(self, lo: int, hi: int):
        self._BPM_MIN = lo
        self._BPM_MAX = hi

    def setEnabled(self, enabled: bool):
        self._enabled = enabled
        self.update()

    def isEnabled(self) -> bool:
        return self._enabled

    # ── painting ─────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        groove_h = 14
        gy = (H - groove_h) // 2
        r = groove_h // 2

        fill_color  = QColor("#f59e0b") if self._enabled else QColor("#374151")
        track_color = QColor("#374151")
        text_color  = QColor("#1a1a1a") if self._enabled else QColor("#6b7280")

        span = self._BPM_MAX - self._BPM_MIN
        frac = (self._value - self._BPM_MIN) / span if span else 0.0
        fill_w = max(r * 2, int(frac * W))

        # Track background
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(track_color))
        p.drawRoundedRect(0, gy, W, groove_h, r, r)

        # Fill
        p.setBrush(QBrush(fill_color))
        p.drawRoundedRect(0, gy, fill_w, groove_h, r, r)

        # Value label inside groove
        bpm_str = f"{self._value / 10.0:.1f}"
        f = QFont()
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        p.setPen(text_color)
        p.drawText(QRect(0, gy, W, groove_h), Qt.AlignCenter, f"{bpm_str} BPM")

        # Handle indicator — vertical bar at fill end
        hx = fill_w - 2
        handle_h = H - 4
        hy = (H - handle_h) // 2
        p.setPen(QPen(QColor("#d97706") if self._enabled else QColor("#4b5563"), 3))
        p.drawLine(hx, hy, hx, hy + handle_h)

        p.end()

    # ── input ─────────────────────────────────────────────────────────────

    def _x_to_value(self, x: int) -> int:
        span = self._BPM_MAX - self._BPM_MIN
        frac = max(0.0, min(1.0, x / max(1, self.width())))
        return self._BPM_MIN + int(frac * span)

    def mousePressEvent(self, event):
        if not self._enabled:
            return
        self._drag_start_x   = event.pos().x()
        self._drag_start_val = self._value
        self._dragging = True
        # Jump to touched position immediately
        new_val = self._x_to_value(event.pos().x())
        self.setValue(new_val)

    def mouseMoveEvent(self, event):
        if not self._enabled or not self._dragging:
            return
        new_val = self._x_to_value(event.pos().x())
        self.setValue(new_val)

    def mouseReleaseEvent(self, event):
        if not self._enabled:
            return
        self._dragging = False
        new_val = self._x_to_value(event.pos().x())
        self.setValue(new_val)
        self.sliderReleased.emit()


MAX_TAPS_FOR_AVG = 4
TAP_TIMEOUT_SECONDS = 2.0

# Maximum number of phase-error history entries shown in the metrics sparkline
_SPARKLINE_LEN = 4

# ── Shared LED / indicator colour tokens ──────────────────────────────────────
# Used by: phase lock LED, track state LED, radio button ::indicator QSS,
#          phase error label, status bar dot.
_LED_GREEN        = "#10b981"   # locked / playing
_LED_GREEN_BORDER = "#059669"
_LED_AMBER        = "#f59e0b"   # drift / paused
_LED_AMBER_BORDER = "#d97706"
_LED_RED          = "#ef4444"   # large error
_LED_RED_BORDER   = "#dc2626"
_LED_OFF          = "#374151"   # inactive
_LED_OFF_BORDER   = "#4b5563"


class _WaveformZoomOverlay(QWidget):
    """Fullwidth overlay drawn directly on the top-level window.
    Tap anywhere to dismiss — no modal, no event blocking.
    """
    def __init__(self, data, beatgrid, position, top_window):
        super().__init__(top_window)          # child of main window
        self._data = data
        self._beatgrid = beatgrid
        self._position = position
        H = 280
        gp = top_window.rect()
        self.setGeometry(0, (gp.height() - H) // 2, gp.width(), H)
        self.setStyleSheet("background:#0d1117;")
        self.setCursor(Qt.PointingHandCursor)

        # Close button top-right
        close_btn = QPushButton("✕", self)
        close_btn.setFixedSize(44, 44)
        close_btn.move(gp.width() - 50, 4)
        close_btn.setStyleSheet(
            "QPushButton{background:#1f2937;border:1px solid #374151;"
            "border-radius:6px;color:#9ca3af;font-size:14pt;font-weight:bold;}"
            "QPushButton:pressed{background:#374151;}"
        )
        close_btn.clicked.connect(self._close)

        self.raise_()
        self.show()

    def _close(self):
        self.hide()
        self.deleteLater()

    def mousePressEvent(self, _e):
        self._close()

    def _get_beats(self):
        if not self._beatgrid:
            return []
        try:
            if hasattr(self._beatgrid, 'get'):
                return self._beatgrid.get("beats", []) or []
            return list(self._beatgrid)
        except Exception:
            return []

    def _parse_beats(self):
        if not self._beatgrid:
            return []
        try:
            if hasattr(self._beatgrid, 'get'):
                return self._beatgrid.get("beats", []) or []
            return list(self._beatgrid)
        except Exception:
            return []

    def paintEvent(self, _e):
        """Full waveform view, wandering orange needle."""
        p = QPainter(self)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, QColor("#0d1117"))

        if not self._data:
            p.setPen(QColor("#374151"))
            p.drawText(0, 0, W, H, Qt.AlignCenter, "No waveform data")
            p.end()
            return

        # Full track view
        data = self._data
        total_cols = len(data) // 2
        mid = H // 2
        for sx in range(W):
            ci = sx * total_cols // W
            if ci >= total_cols:
                break
            raw_h = data[2 * ci] & 0x1f
            white = data[2 * ci + 1] & 0x07
            r = g = 36 * white
            b = 140 + 16 * white
            bar_h = max(1, raw_h * (mid - 4) // 31)
            p.fillRect(sx, mid - bar_h, 1, bar_h * 2, QColor(r, g, b))

        p.fillRect(0, mid, W, 1, QColor("#1f2937"))

        # all beatgrid markers
        beats = self._parse_beats()
        if beats:
            total_ms = beats[-1]["time"]
            if total_ms > 0:
                for beat in beats:
                    bx = int(beat["time"] / total_ms * W)
                    is_one = beat.get("beat") == 1
                    col = QColor("#ef4444") if is_one else QColor("#374151")
                    th  = H if is_one else H // 3
                    bw  = 2 if is_one else 1
                    p.fillRect(bx, 0, bw, th, col)
                    p.fillRect(bx, H - th, bw, th, col)

        # wandering orange needle
        nx = int(self._position * W)
        p.fillRect(nx - 2, 0, 4, H, QColor("#f97316"))
        from qtpy.QtGui import QPolygon
        from qtpy.QtCore import QPoint
        p.setBrush(QColor("#f97316"))
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygon([QPoint(nx-6,0), QPoint(nx+6,0), QPoint(nx,10)]))
        p.drawPolygon(QPolygon([QPoint(nx-6,H), QPoint(nx+6,H), QPoint(nx,H-10)]))

        p.setPen(QColor("#4b5563"))
        p.drawText(W - 120, H - 18, 110, 16, Qt.AlignRight, "tap to close")
        p.end()


class _PreviewWaveformWidget(QWidget):
    """Dark-themed preview waveform + beatgrid for the Now Playing panel."""
    _redraw = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = None
        self._beatgrid = None
        self._position = 0.0
        self._duration = 0.0       # track duration in seconds
        self._bpm = 120.0          # current BPM for zoom calculation
        self._get_client_fn = None  # callable() -> client or None
        self._redraw.connect(self.update)
        self.setMinimumWidth(100)
        self.setCursor(Qt.PointingHandCursor)
        # 40ms timer (~25fps) for smooth position interpolation
        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(40)
        self._pos_timer.timeout.connect(self._interpolate_position)
        self._pos_timer.start()

    def set_client_fn(self, fn, duration: float, bpm: float = 120.0):
        """Register a callable that returns the active CDJ client object."""
        self._get_client_fn = fn
        self._duration = duration
        self._bpm = bpm

    def _interpolate_position(self):
        """Called every 40ms — interpolate client position and redraw."""
        if self._data is None or self._get_client_fn is None:
            return
        client = self._get_client_fn()
        if client is None:
            return
        dur = self._duration
        if dur and dur > 0:
            pos = getattr(client, 'position', None)
            if pos is not None:
                # linear interpolation since last position_timestamp
                import time as _time
                ts = getattr(client, 'position_timestamp', None)
                pitch = getattr(client, 'actual_pitch', 1.0) or 1.0
                play_state = getattr(client, 'play_state', '')
                if ts and play_state == 'playing':
                    pos = pos + pitch * (_time.time() - ts)
                rel = max(0.0, min(1.0, pos / dur))
                if abs(rel - self._position) > 0.0001:
                    self._position = rel
                    self.update()

    def setData(self, data, beatgrid=None):
        self._data = data
        self._beatgrid = beatgrid
        self._redraw.emit()

    def setPosition(self, pos: float):
        if abs(pos - self._position) > 0.001:
            self._position = max(0.0, min(1.0, pos))
            self._redraw.emit()

    def clear(self):
        self._data = None
        self._beatgrid = None
        self._position = 0.0
        self._redraw.emit()

    @staticmethod
    def _parse_beats(beatgrid):
        """Return list of beat dicts from ListContainer or dict."""
        if not beatgrid:
            return []
        try:
            if hasattr(beatgrid, 'get'):
                return beatgrid.get("beats", []) or []
            return list(beatgrid)
        except Exception:
            return []

    def _draw_waveform(self, p, data, beats, view_start, view_end, W, H):
        """Draw waveform bars + beatgrid into painter p for the given view window."""
        total_cols = len(data) // 2
        n_cols = max(1, int((view_end - view_start) * total_cols))
        col_start = int(view_start * total_cols)
        mid = H // 2
        for sx in range(W):
            ci = col_start + sx * n_cols // W
            if ci >= total_cols:
                break
            raw_h = data[2 * ci] & 0x1f
            white = data[2 * ci + 1] & 0x07
            r = g = 36 * white
            b = 140 + 16 * white
            bar_h = max(1, raw_h * (mid - 2) // 31)
            p.fillRect(sx, mid - bar_h, 1, bar_h * 2, QColor(r, g, b))
        # centre line
        p.fillRect(0, mid, W, 1, QColor("#1f2937"))
        # beatgrid markers
        span = view_end - view_start
        if beats:
            total_ms = beats[-1]["time"]
            if total_ms > 0:
                for beat in beats:
                    rel = beat["time"] / total_ms
                    if rel < view_start or rel > view_end:
                        continue
                    bx = int((rel - view_start) / span * W)
                    is_one = beat.get("beat") == 1
                    col = QColor("#ef4444") if is_one else QColor("#374151")
                    th = H if is_one else H // 3
                    bw = 2 if is_one else 1
                    p.fillRect(bx, 0, bw, th, col)
                    p.fillRect(bx, H - th, bw, th, col)

    def mousePressEvent(self, _e):
        if self._data is None:
            return
        top = self
        while top.parent():
            top = top.parent()
        _WaveformZoomOverlay(self._data, self._beatgrid, self._position, top)

    def paintEvent(self, _e):
        """CDJ-style: needle fixed in centre, waveform scrolls past it.
        Zoom = 5% of track visible — beatgrid ticks clearly readable.
        """
        p = QPainter(self)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, QColor("#0d1117"))

        if not self._data:
            p.setPen(QColor("#374151"))
            p.drawText(0, 0, W, H, Qt.AlignCenter, "Tap for waveform")
            p.end()
            return

        # 8 beats visible, BPM-adaptive zoom
        bpm = self._bpm if self._bpm > 0 else 120.0
        beat_dur_s = 60.0 / bpm
        dur = self._duration if self._duration > 0 else 300.0
        zoom = min(0.15, max(0.01, (8 * beat_dur_s) / dur))
        half = zoom / 2
        view_start = max(0.0, self._position - half)
        view_end   = view_start + zoom
        if view_end > 1.0:
            view_end   = 1.0
            view_start = max(0.0, 1.0 - zoom)

        beats = self._parse_beats(self._beatgrid)
        self._draw_waveform(p, self._data, beats, view_start, view_end, W, H)

        # Fixed centre needle — red, 3px + triangles top/bottom
        nx = W // 2
        p.fillRect(nx - 1, 0, 3, H, QColor("#ef4444"))
        # top triangle
        from qtpy.QtGui import QPolygon
        from qtpy.QtCore import QPoint
        p.setBrush(QColor("#ef4444"))
        p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygon([QPoint(nx-5,0), QPoint(nx+5,0), QPoint(nx,8)]))
        p.drawPolygon(QPolygon([QPoint(nx-5,H), QPoint(nx+5,H), QPoint(nx,H-8)]))

        # zoom hint
        p.setPen(QColor("#374151"))
        p.drawText(W - 58, H - 13, 52, 12, Qt.AlignRight, "+ zoom")
        p.end()


def _short_port_name(full_name: str) -> str:
    """Return a human-readable short name for a MIDI port string.

    On Windows, rtmidi returns names like 'CH345:CH345 MIDI 1 28:0'.
    We strip everything from the first ':' onward so the combo box shows
    only the device label (e.g. 'CH345').  On Linux/macOS the name is
    already short, so we return it unchanged.
    """
    if ':' in full_name:
        return full_name.split(':', 1)[0].strip()
    return full_name

class _CompactPlayerBadge(QFrame):
    """Compact ~42px player badge for the Now Playing panel."""
    selected_signal = Signal(int)

    def __init__(self, player_number, parent=None):
        super().__init__(parent)
        self.player_number = player_number
        self.is_dropped = False
        self.setFixedHeight(42)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setObjectName("PlayerFrame")
        self.setCursor(Qt.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(1)

        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self._num_label = QLabel(f"P{player_number}")
        self._num_label.setStyleSheet("color:#6b7280;font-size:9pt;font-weight:bold;")
        row1.addWidget(self._num_label)
        self._state_led = QFrame()
        self._state_led.setFixedSize(7, 7)
        self._state_led.setStyleSheet(f"background:{_LED_OFF};border-radius:3px;")
        row1.addWidget(self._state_led)
        row1.addStretch()
        self._tag_label = QLabel("")
        self._tag_label.setStyleSheet("color:#6b7280;font-size:7pt;")
        row1.addWidget(self._tag_label)
        lay.addLayout(row1)

        self._bpm_label = QLabel("--.--")
        self._bpm_label.setStyleSheet("color:#e5e7eb;font-size:11pt;font-weight:bold;")
        self._bpm_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._bpm_label)

        self.mousePressEvent = lambda _e: self.selected_signal.emit(self.player_number)

    def update_data(self, bpm, is_master, is_selected, play_state, is_dropped):
        self.is_dropped = is_dropped
        if is_dropped:
            self._bpm_label.setText("--")
            self._state_led.setStyleSheet(f"background:{_LED_RED};border-radius:3px;")
            self._tag_label.setText("off")
            self.setStyleSheet("QFrame#PlayerFrame{border:1px solid #ef4444;border-radius:6px;}")
            return
        self._bpm_label.setText(f"{bpm:.1f}" if isinstance(bpm, (float, int)) else "--")
        led = _LED_GREEN if play_state == "playing" else (_LED_AMBER if play_state in ("paused","cued") else _LED_OFF)
        self._state_led.setStyleSheet(f"background:{led};border-radius:3px;")
        tags = []
        if is_master:   tags.append("M")
        if is_selected: tags.append("SRC")
        self._tag_label.setText(" ".join(tags))
        self._tag_label.setStyleSheet(f"color:{'#10b981' if tags else '#6b7280'};font-size:7pt;font-weight:bold;")
        border = "#10b981" if is_selected else ("#0ea5e9" if is_master else "#374151")
        self.setStyleSheet(f"QFrame#PlayerFrame{{border:1px solid {border};border-radius:6px;}}")


class PlayerTileWidget(QFrame):
    """
    A widget to display information for a single player and allow selection.
    """
    selected_signal = Signal(int) # Emits player number when selected

    def __init__(self, player_number, parent=None):
        super().__init__(parent)
        self.player_number = player_number
        self.is_master = False
        self.is_selected_source = False
        self.is_dropped = False # New state

        self.setFrameStyle(QFrame.NoFrame)
        self.setObjectName("PlayerFrame")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(100)

        # Grid: col 0 expands, col 1 is fixed-width button column
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setSpacing(4)
        grid.setColumnStretch(0, 1)   # left content expands
        grid.setColumnStretch(1, 0)   # button column fixed

        # Row 0: player name (col 0) + status badge (col 0, right-aligned)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.player_label = QLabel(f"Player {self.player_number}")
        font = self.player_label.font()
        font.setBold(True)
        font.setPointSize(11)
        self.player_label.setFont(font)
        header.addWidget(self.player_label)
        header.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size:9pt; color:#6b7280;")
        header.addWidget(self.status_label)
        grid.addLayout(header, 0, 0)

        # Row 1: BPM — left column only, vertically centred
        self.bpm_label = QLabel("--.--")
        bpm_font = self.bpm_label.font()
        bpm_font.setPointSize(20)
        bpm_font.setBold(True)
        self.bpm_label.setFont(bpm_font)
        self.bpm_label.setAlignment(Qt.AlignCenter)
        grid.addWidget(self.bpm_label, 1, 0)

        # Row 2: delay label — left column only
        self.delay_label = QLabel("--.-- ms")
        self.delay_label.setStyleSheet("font-size:9pt; color:#9ca3af;")
        grid.addWidget(self.delay_label, 2, 0)

        # Button: right column, spans all 3 rows — always contained inside frame
        self.action_button = QPushButton("Select")
        self.action_button.setFixedWidth(72)
        self.action_button.setMinimumHeight(60)
        self.action_button.setCursor(Qt.PointingHandCursor)
        self.action_button.clicked.connect(self.handle_action_clicked)
        grid.addWidget(self.action_button, 0, 1, 3, 1)  # row 0, col 1, rowspan 3

        self.update_ui_elements()

    def handle_action_clicked(self):
        # If dropped, this button might mean "Try Reconnect" or "Clear Selection"
        # For now, it always emits selected_signal, and MainWindow decides.
        # If it's a "Use this source" button after reconnect, this signal is still fine.
        self.selected_signal.emit(self.player_number)

    def set_selected_source(self, is_selected):
        self.is_selected_source = is_selected
        self.update_ui_elements()

    def update_data(self, bpm, delay, is_master):
        self.bpm_label.setText(f"{bpm:.2f}" if isinstance(bpm, (float, int)) else "--.--")
        self.delay_label.setText(f"{delay * 1000:.2f} ms" if isinstance(delay, (float, int)) else "--.-- ms")
        self.is_master = is_master
        if self.is_dropped:
            self.set_dropped_status(False)  # use the guarded setter to avoid redundant repaints
        else:
            self.update_ui_elements()

    def set_dropped_status(self, is_dropped_now):
        if self.is_dropped != is_dropped_now:
            self.is_dropped = is_dropped_now
            self.update_ui_elements()

    def update_ui_elements(self):
        if self.is_dropped:
            self.bpm_label.setText("--.--")
            self.delay_label.setText("--.-- ms")
            self.status_label.setText("Dropped")
            self.status_label.setStyleSheet("font-size:8pt; color:#ef4444;")
            self.action_button.setText("Reconnect")
            self.setStyleSheet(
                "QFrame#PlayerFrame { border: 2px solid #ef4444; border-radius:10px; }")
            return

        # Build status text
        status_parts = []
        if self.is_master:
            status_parts.append("Master")
        if self.is_selected_source:
            status_parts.append("Source")
        self.status_label.setText("  ".join(status_parts) if status_parts else "")
        self.status_label.setStyleSheet("font-size:9pt; color:#10b981;"
                                        if status_parts else "font-size:9pt; color:#6b7280;")

        # Border colour: green=selected, blue=master, default
        if self.is_selected_source:
            border = "#10b981"
        elif self.is_master:
            border = "#0ea5e9"
        else:
            border = "#3b3b3b"

        self.setStyleSheet(
            f"QFrame#PlayerFrame {{ border: 2px solid {border}; border-radius:10px; }}")

        self.action_button.setText("Deselect" if self.is_selected_source else "Select")


class MidiClockMainWindow(QWidget):
    def __init__(self, prodj_instance, signal_bridge, parent=None):
        super().__init__(parent)
        self.prodj = prodj_instance
        self.signal_bridge = signal_bridge
        self.player_tiles = {} # player_number: PlayerTileWidget
        self._compact_badges = {} # player_number: _CompactPlayerBadge (Now Playing panel)
        self.selected_player_source = None # Player number of the selected source
        self.coasting_bpm = None # Stores the BPM value when coasting
        self.last_known_good_bpm = 120.0 # Default if no BPM ever received

        self.manual_bpm_mode_active = False
        self.manual_bpm_value = 120.0
        self.tap_timestamps = []
        self.last_prodj_beat_time = None

                # Auto phase correction state
        self.auto_phase_correction_enabled = True
        self.phase_error_ms = 0.0
        self.phase_error_history = []      # rolling history for smoothing
        self.PHASE_HISTORY_LEN = 4
        self._grid_offset_ms = 0.0         # persistent manual offset
        # Slip mode / pitch change detection
        self._slip_mode_active = False
        self._bpm_stable_count = 0         # beats with stable BPM after large change
        self._resync_pending = False        # Auto-Stop+Re-Sync nach grossem Tempowechsel

        self.midi_clock_instance = None  # AlsaMidiClock or RtMidiClock
        self.preferred_midi_backend = None  # "ALSA" or "rtmidi"
        self._last_applied_bpm = None  # guards against redundant setBpm calls
        self.MidiClockImpl = None
        self._beat_snap_pending = False  # one-shot grid snap on next beat packet
        self._waveform_loaded_key = None  # (pn, sl, tid) of last requested waveform

        # Sync Start count-in state
        self._sync_start_pending = False   # waiting for bar beat 1
        self._sync_start_countdown = 0     # CDJ beats remaining until we fire send_start()
        self._sync_beats_seen = 0          # CDJ beats received since arm (for absolute counting)
        # Clock output state: 'stopped' | 'waiting' | 'running'
        self._output_state = 'stopped'

        # Phase-error sparkline history (shown in metrics panel)
        self._sparkline: List[float] = []

        # Single persistent off-timer for the MIDI beat LED — created once,
        # restarted every beat.  Never recreated so there is no timer leak.
        self._led_off_timer = QTimer()
        self._led_off_timer.setSingleShot(True)
        self._led_off_timer.timeout.connect(self._led_turn_off)

        # Watchdog: checks every 2s whether the clock thread is still alive.
        self._watchdog_timer = QTimer()
        self._watchdog_timer.setInterval(2000)
        self._watchdog_timer.timeout.connect(self._check_clock_crashed)
        self._watchdog_timer.start()

        # Beat counter from CDJ (bar position)
        self._last_beat_number: int = 0
        self._last_beat_player: int = 0

        self.setWindowTitle("ProDJ Link MIDI Clock")
        self._init_ui()
        self._connect_signals()

        self.populate_midi_ports()
        self.update_player_display()
        self.update_global_status_label()
        self._refresh_track_info()

    def beat_received(self):
        """Called from the MIDI clock thread on every musical beat (24 ticks).
        Must be non-blocking — no I/O, no locks, no Qt calls other than emit().
        Qt queued connections deliver the signal safely to the GUI thread;
        if the GUI thread is busy the event is queued and processed in order,
        so we do not need a manual pending-flag guard here.
        """
        self.signal_bridge.beat_signal.emit()

    def _led_turn_off(self):
        """Slot connected to the persistent _led_off_timer — turns the beat LED off."""
        self.midi_led.setStyleSheet(
            "background:#2d2d2d;border:3px solid #4a4a4a;border-radius:20px;"
        )

    def _on_beat_signal(self):
        """Runs in the GUI thread — triggered by MIDI clock output beat.

        Flash the beat LED on for 60% of the beat period (min 80 ms, max 250 ms)
        so it is clearly visible at all BPMs.  Uses a single persistent QTimer
        that is simply restarted each beat — no timer objects are ever created
        or destroyed here.
        """
        # Derive on-time: 60% duty cycle, clamped so it's always visible
        on_ms = 80  # safe default
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            d = self.midi_clock_instance.delay
            if d > 0:
                beat_ms = d * 24.0 * 1000.0
                on_ms = int(max(80, min(250, beat_ms * 0.60)))

        self.midi_led.setStyleSheet(
            "background:qradialgradient(cx:0.5,cy:0.5,radius:0.5,"
            "fx:0.5,fy:0.5,stop:0 #10b981,stop:1 #059669);"
            "border:3px solid #10b981;border-radius:20px;"
        )
        # Restart the single persistent timer — cancels any previous countdown
        self._led_off_timer.start(on_ms)
        self._refresh_metrics()

    def _set_grid_step(self, val: int):
        """Select step size and update toggle buttons."""
        self._step_ms = val
        for v, btn in self._step_buttons.items():
            btn.setChecked(v == val)

    def adjust_grid_shift(self, direction):
        """Accumulate a persistent manual grid offset and apply it immediately.
        Works in both Auto-Sync ON and OFF modes — the offset is re-applied
        every beat by handle_prodj_beat_timing so Auto-Sync cannot wash it out.
        """
        shift_ms = self._step_ms * direction
        self._grid_offset_ms += shift_ms
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            self.midi_clock_instance.adjust_phase(shift_ms)
        # Brief flash showing the step that was just applied
        self.pitch_label.setText(f"{shift_ms:+d} ms")
        QTimer.singleShot(600, lambda: self.pitch_label.setText(""))
        self._update_offset_display()

    def reset_grid_shift(self):
        """Clear the persistent manual offset and re-snap the grid on next beat."""
        self._grid_offset_ms = 0.0
        self._beat_snap_pending = True   # hard snap on next CDJ beat resets any residual drift
        self.pitch_label.setText("")
        self._update_offset_display()

    def _update_offset_display(self):
        """Refresh the persistent offset display widget."""
        val = self._grid_offset_ms
        self._offset_display.setText(f"{val:+.0f} ms")
        if val == 0.0:
            color = "#10b981"  # green — no offset
        else:
            color = "#f59e0b"  # amber — offset active
        self._offset_display.setStyleSheet(
            f"color:{color};font-size:14pt;font-weight:bold;"
            "background:#1e1e1e;border:1px solid #2d2d2d;border-radius:5px;"
        )

    def _on_source_radio_changed(self):
        """Called when the Clock Source radio buttons change (checked side only)."""
        if self.source_master_radio.isChecked():
            self.source_player_combo.setEnabled(False)
            self.selected_player_source = None
            logging.info("Source: Follow Network Master")
        else:
            self.source_player_combo.setEnabled(True)
            player_num = int(self.source_player_combo.currentText())
            self.selected_player_source = player_num
            logging.info("Source: Locked to Player %d", player_num)
        self.phase_error_history.clear()
        self._sparkline.clear()
        self._grid_offset_ms = 0.0     # reset manual offset on source change
        self._beat_snap_pending = True  # re-snap grid to new source
        self.update_midi_clock_source_logic()
        self._update_active_source_label()

    def _on_source_player_combo_changed(self):
        """Called when the player number combo changes while player radio is active."""
        if self.source_player_radio.isChecked():
            player_num = int(self.source_player_combo.currentText())
            self.selected_player_source = player_num
            self.phase_error_history.clear()
            self.update_midi_clock_source_logic()
            self._update_active_source_label()

    def _update_active_source_label(self):
        """Refresh the 'Source: ...' readout in the Clock Source group."""
        if self.manual_bpm_mode_active:
            self.active_source_label.setText(f"Source: Manual {self.manual_bpm_value:.1f} BPM")
            self.active_source_label.setStyleSheet("color:#f59e0b; font-weight:bold;")
            return
        src = self._get_active_source_player_number()
        if src is not None:
            tile = self.player_tiles.get(src)
            is_master = tile and not tile.is_dropped and self.prodj.cl.getClient(src) and \
                        "master" in (self.prodj.cl.getClient(src).state or [])
            tag = " (Master)" if is_master else ""
            self.active_source_label.setText(f"Source: Player {src}{tag}")
            self.active_source_label.setStyleSheet("color:#10b981; font-weight:bold;")
        elif self.coasting_bpm is not None:
            self.active_source_label.setText(f"Source: Coasting {self.coasting_bpm:.1f} BPM")
            self.active_source_label.setStyleSheet("color:#f59e0b; font-weight:bold;")
        else:
            self.active_source_label.setText("Source: Waiting for CDJs...")
            self.active_source_label.setStyleSheet("color:#6b7280; font-weight:bold;")

    def toggle_auto_sync(self):
        self.auto_phase_correction_enabled = self.auto_sync_button.isChecked()
        if self.auto_phase_correction_enabled:
            self.auto_sync_button.setText("Auto Sync: ON")
            self.phase_error_history.clear()
            self._sparkline.clear()
            self._beat_snap_pending = True
            logging.info("Auto Sync ON (grid offset %.1f ms).", self._grid_offset_ms)
        else:
            self.auto_sync_button.setText("Auto Sync: OFF")
            self.phase_error_ms = 0.0
            self.phase_error_history.clear()
            self._sparkline.clear()
            self._update_phase_error_display()
            self._refresh_metrics()
            logging.info("Auto Sync OFF.")

    def _update_phase_error_display(self):
        """Update the phase error label and lock LED colour based on current phase_error_ms."""
        err = self.phase_error_ms
        self.phase_error_label.setText(f"{err:+.1f} ms")

        abs_err = abs(err)
        if abs_err < 1.0:
            color, border = _LED_GREEN, _LED_GREEN_BORDER
        elif abs_err < 5.0:
            color, border = _LED_AMBER, _LED_AMBER_BORDER
        else:
            color, border = _LED_RED, _LED_RED_BORDER

        self.phase_lock_led.setStyleSheet(
            f"background:{color};border:2px solid {border};border-radius:12px;"
        )
        self.phase_error_label.setStyleSheet(f"color:{color};")

    # ------------------------------------------------------------------
    # Track info bar
    # ------------------------------------------------------------------

    def _refresh_track_info(self) -> None:
        """Update the track info bar with data from the active source CDJ.
        Shows the CDJ that is currently the BPM source (master or locked player).
        Metadata is fetched asynchronously by the core layer; if not yet available
        we show what we know from the status packet (play_state, key).
        """
        src = self._get_active_source_player_number()
        if src is None:
            self._set_track_info_empty()
            return

        client = self.prodj.cl.getClient(src)
        if client is None:
            self._set_track_info_empty()
            return

        # Player badge
        is_master = "master" in (client.state or [])
        badge_color = "#0ea5e9" if is_master else "#6b7280"
        self._track_player_label.setText(f"P{src}")
        self._track_player_label.setStyleSheet(
            f"color:{badge_color};font-size:9pt;font-weight:bold;"
        )

        # Play-state LED
        play_state = getattr(client, "play_state", "no_track")
        if play_state == "playing":
            led_color = _LED_GREEN
        elif play_state in ("paused", "cued", "cueing"):
            led_color = _LED_AMBER
        else:
            led_color = _LED_OFF
        self._track_state_led.setStyleSheet(
            f"background:{led_color};border-radius:5px;"
        )

        # Metadata (may be None if not yet fetched or non-rekordbox track)
        meta = getattr(client, "metadata", None)
        if meta:
            title  = meta.get("title",  "") or ""
            artist = meta.get("artist", "") or ""
            key    = meta.get("key",    "") or ""
            dur    = meta.get("duration", None)
        else:
            # Fall back to status-packet fields only
            title  = ""
            artist = ""
            key    = str(getattr(client, "key", "") or "")
            dur    = None

        # Show track_number if title is empty (non-rekordbox USB/SD)
        if not title:
            track_no = getattr(client, "track_number", None)
            analyze  = getattr(client, "track_analyze_type", "")
            if track_no:
                title = f"Track {track_no}" if analyze != "rekordbox" else "Loading…"
            else:
                title = "No track" if play_state == "no_track" else "Unknown"

        self._track_title_label.setText(title)
        self._track_artist_label.setText(artist)

        # Key: prefer key_shift if set (indicates active Key Sync shift)
        key_shift = getattr(client, "key_shift", None)
        if key_shift and str(key_shift) not in ("", "0", "None"):
            self._track_key_label.setText(f"{key}→{key_shift}")
        elif key:
            self._track_key_label.setText(str(key))
        else:
            self._track_key_label.setText("")

        # Duration  "m:ss"
        if dur and isinstance(dur, (int, float)) and dur > 0:
            mins, secs = divmod(int(dur), 60)
            self._track_duration_label.setText(f"{mins}:{secs:02d}")
        else:
            self._track_duration_label.setText("")

        # Player badge font size fix for compact widget
        self._track_player_label.setStyleSheet(
            f"color:{badge_color};font-size:8pt;font-weight:bold;"
        )
        self._track_state_led.setStyleSheet(
            f"background:{led_color};border-radius:4px;"
        )

        # Request waveform + beatgrid async — callback updates _waveform_widget
        pn  = client.loaded_player_number
        sl  = client.loaded_slot
        tid = client.track_id
        if tid and tid != 0 and (pn, sl, tid) != self._waveform_loaded_key:
            self._waveform_loaded_key = (pn, sl, tid)
            self._waveform_widget.clear()

            def _get_beatgrid():
                try:
                    return self.prodj.data.beatgrid_store[(pn, sl, tid)]
                except KeyError:
                    return None

            def _wf_cb(request, _src_pn, _slot, _tid, data):
                """Called from DataProvider thread — emit signal to GUI thread."""
                if data is not None:
                    self.signal_bridge.waveform_ready_signal.emit(request, data)
            self.prodj.data.get_color_preview_waveform(pn, sl, tid, _wf_cb)
            self.prodj.data.get_beatgrid(pn, sl, tid, _wf_cb)

        # Register interpolation callback so waveform scrolls smoothly
        duration = getattr(client, "duration", None) or (dur if dur else None)
        if duration and duration > 0:
            client_bpm = self._bpm_from_client(client) or 120.0
            self._waveform_widget.set_client_fn(
                lambda s=src: self.prodj.cl.getClient(s),
                duration,
                bpm=client_bpm
            )

    def _set_track_info_empty(self) -> None:
        """Reset all track info bar widgets to their idle state."""
        self._track_player_label.setText("—")
        self._track_player_label.setStyleSheet("color:#6b7280;font-size:8pt;font-weight:bold;")
        self._track_state_led.setStyleSheet(f"background:{_LED_OFF};border-radius:4px;")
        self._track_title_label.setText("No track")
        self._track_artist_label.setText("")
        self._waveform_widget.clear()
        self._waveform_loaded_key = None
        self._track_key_label.setText("")
        self._track_duration_label.setText("")

    def _on_waveform_ready(self, request: str, data) -> None:
        """GUI-thread slot: called via waveform_ready_signal from DataProvider thread."""
        if request in ("color_preview_waveform", "preview_waveform"):
            # fetch beatgrid from store if already available
            key = self._waveform_loaded_key
            beatgrid = None
            if key:
                try:
                    beatgrid = self.prodj.data.beatgrid_store[key]
                except KeyError:
                    pass
            self._waveform_widget.setData(data, beatgrid=beatgrid)
        elif request == "beatgrid":
            if self._waveform_widget._data is not None:
                self._waveform_widget.setData(self._waveform_widget._data, beatgrid=data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase / timing helpers
    # ------------------------------------------------------------------

    def _next_beat_wall_time(self, clk, beat_period_ms: float) -> float:
        """Return wall-clock time of the next MIDI beat from the clock instance.

        Prefers next_beat_wall_time() (ALSA queue-anchored, precise).
        Falls back to last_beat_wall_time + beat_period (software estimate).
        """
        if hasattr(clk, 'next_beat_wall_time'):
            try:
                t = clk.next_beat_wall_time()
                if t > time.time() - beat_period_ms / 1000.0:
                    return t
            except Exception:
                pass
        # Fallback: last known beat + one period
        t_last = getattr(clk, 'last_beat_wall_time', None)
        if t_last is not None:
            return t_last + beat_period_ms / 1000.0
        return time.time() + beat_period_ms / 1000.0

    def _compute_snap_ms(self, clk, t_next_cdj: float = None,
                         beat_period_ms: float = None,
                         offset_ms: float = 0.0) -> Optional[float]:
        """Compute the phase nudge (ms) to align our next MIDI beat with t_next_cdj.

        For the send_start snap (t_next_cdj=None): snap so next beat = now.
        For the grid snap: snap so next beat = t_next_cdj + offset.
        Returns None if no timing reference is available.
        """
        if beat_period_ms is None:
            beat_period_ms = clk.delay * 24.0 * 1000.0
        if beat_period_ms <= 0:
            return None

        t_next_midi = self._next_beat_wall_time(clk, beat_period_ms)
        now = time.time()

        if t_next_cdj is None:
            # send_start mode: snap so next MIDI beat fires right NOW
            remaining_ms = (t_next_midi - now) * 1000.0
            # Wrap into (-beat_period/2, +beat_period/2]
            while remaining_ms > beat_period_ms / 2:
                remaining_ms -= beat_period_ms
            while remaining_ms < -beat_period_ms / 2:
                remaining_ms += beat_period_ms
            return -remaining_ms  # nudge earlier by this amount
        else:
            # grid snap: align to CDJ next beat
            snap_ms = (t_next_cdj - t_next_midi) * 1000.0 + offset_ms
            while snap_ms > beat_period_ms / 2:
                snap_ms -= beat_period_ms
            while snap_ms < -beat_period_ms / 2:
                snap_ms += beat_period_ms
            return snap_ms

    def nudge(self, ms: float) -> None:
        """Apply a one-shot phase nudge of *ms* milliseconds to the running clock.
        Positive = later, negative = earlier.  No-op when clock is stopped.
        """
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            if hasattr(self.midi_clock_instance, "adjust_phase"):
                self.midi_clock_instance.adjust_phase(ms)
            else:
                logging.warning("MIDI backend does not support phase adjustment.")

    def sync_to_grid(self) -> None:
        """Nudge the MIDI clock so its next beat boundary aligns with the last
        ProDJ beat timestamp.  Called internally; no UI button exposes this.
        """
        if self.last_prodj_beat_time is None:
            logging.warning("sync_to_grid: no ProDJ beat received yet.")
            return
        if not self.midi_clock_instance or not self.midi_clock_instance.is_alive():
            return

        elapsed_ms = (time.time() - self.last_prodj_beat_time) * 1000.0
        beat_period_ms = self.midi_clock_instance.delay * 24.0 * 1000.0
        if beat_period_ms <= 0:
            return

        misalignment = elapsed_ms % beat_period_ms
        if misalignment > beat_period_ms / 2:
            nudge_ms = -(beat_period_ms - misalignment)  # nudge earlier
        else:
            nudge_ms = -misalignment  # nudge later
        self.nudge(nudge_ms)
        logging.info("sync_to_grid: misalignment %.2f ms, nudge %.2f ms",
                     misalignment, nudge_ms)

    def _output_bpm(self) -> Optional[float]:
        """Return the BPM the clock is *actually* ticking at, derived from
        midi_clock_instance.delay, or None when the clock is stopped.
        """
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            d = self.midi_clock_instance.delay
            if d > 0:
                return 60.0 / (d * 24.0)
        return None

    # Hard BPM limits — below 20 is inaudible/useless, above 200 overflows
    # USB MIDI on Windows (WinMM saturates around 10ms/tick = 250 BPM;
    # we stay well clear at 200 BPM = 12.5ms/tick).
    BPM_MIN = 20.0
    BPM_MAX = 200.0
    # Minimum BPM change to actually push to the clock thread.
    # Prevents CDJ status packets (arriving ~8 Hz) from continuously
    # setting _delay_changed=True and resetting the tick deadline every
    # 6 ticks — which locked the output tempo to the CDJ packet rate.
    _BPM_CHANGE_THRESHOLD = 0.05  # BPM

    def _apply_bpm(self, bpm: float) -> None:
        """Single consolidated call-site for setBpm.  Always includes
        the current precision_pitch_offset.  Safe to call when clock is
        stopped (no-op).

        During count-in (_sync_start_pending) BPM updates are suppressed:
        the clock is already ticking at the seed BPM and we must not
        re-anchor its deadline accumulator while counting down beats.
        """
        alive = bool(self.midi_clock_instance and self.midi_clock_instance.is_alive())
        if not alive:
            self._last_applied_bpm = None  # reset so next start seeds correctly
            return
        if bpm <= 0:
            logging.error("_apply_bpm: invalid BPM %.2f — ignored.", bpm)
            return
        # Freeze BPM updates during count-in — changing the tick period now
        # would shift the deadline accumulator and make the snap land wrong.
        if self._sync_start_pending:
            logging.debug("_apply_bpm: suppressed during count-in (%.2f BPM)", bpm)
            return
        bpm = max(self.BPM_MIN, min(self.BPM_MAX, bpm))
        # Skip if BPM hasn't changed meaningfully — avoids resetting the
        # clock thread's deadline accumulator on every CDJ status packet.
        if (self._last_applied_bpm is not None
                and abs(bpm - self._last_applied_bpm) < self._BPM_CHANGE_THRESHOLD):
            return
                # Grosser Tempowechsel (>2 BPM) und Clock laeuft: Auto-Stop + Re-Sync
        if (self._last_applied_bpm is not None
                and abs(bpm - self._last_applied_bpm) > 2.0
                and self._output_state == 'running'
                and not self._slip_mode_active):
            logging.info("_apply_bpm: large tempo change %.2f->%.2f, auto-stop+resync",
                         self._last_applied_bpm, bpm)
            self._bpm_stable_count = 0
            self._resync_pending = True
            self._last_applied_bpm = bpm
            self.midi_clock_instance.setBpm(bpm)
            QTimer.singleShot(0, self._do_auto_resync)
            return
        self._last_applied_bpm = bpm
        logging.info("setBpm BPM=%.2f", bpm)
        self.midi_clock_instance.setBpm(bpm)

    def _refresh_metrics(self) -> None:
        """Update the live metrics panel widgets.  Called on every MIDI beat,
        on clock stop, on source change, and whenever manual mode is toggled.

        In manual BPM mode the CDJ-derived rows (BAR.BEAT, PHASE ERR, CDJ DELAY)
        are shown as — because they carry no meaning when the clock is free-running.
        """
        # ── Output BPM ────────────────────────────────────────────────────
        # Always derived from the actual clock tick rate, regardless of mode.
        bpm = self._output_bpm()
        if bpm is not None:
            self._metrics_bpm_label.setText(f"{bpm:.2f}")
            self._metrics_bpm_label.setStyleSheet(
                "color:#10b981;font-weight:bold;font-size:22pt;"
            )
        else:
            self._metrics_bpm_label.setText("---.--")
            self._metrics_bpm_label.setStyleSheet(
                "color:#4b5563;font-weight:bold;font-size:22pt;"
            )

        # Rows below are CDJ-derived — meaningless / stale in manual mode.
        if self.manual_bpm_mode_active:
            self._metrics_beat_label.setText("—")
            self._metrics_beat_label.setStyleSheet(
                "color:#4b5563;font-weight:bold;font-size:13pt;"
            )
            self._metrics_phase_hist_label.setText("—")
            self._metrics_phase_hist_label.setStyleSheet("color:#4b5563;font-size:9pt;")
            self._metrics_latency_label.setText("—")
            self._metrics_latency_label.setStyleSheet("color:#4b5563;font-size:11pt;")
            return

        # ── Beat / bar position ───────────────────────────────────────────
        if self._last_beat_number > 0:
            bar = ((self._last_beat_number - 1) // 4) + 1
            beat_in_bar = ((self._last_beat_number - 1) % 4) + 1
            self._metrics_beat_label.setText(f"{bar}.{beat_in_bar}")
            self._metrics_beat_label.setStyleSheet(
                "color:#9ca3af;font-weight:bold;font-size:13pt;"
            )
        else:
            self._metrics_beat_label.setText("-.–")
            self._metrics_beat_label.setStyleSheet(
                "color:#4b5563;font-weight:bold;font-size:13pt;"
            )

        # ── Phase error sparkline ─────────────────────────────────────────
        if self._sparkline:
            parts = [f"{e:+.1f}" for e in self._sparkline]
            last = abs(self._sparkline[-1])
            color = "#10b981" if last < 1.0 else ("#f59e0b" if last < 5.0 else "#ef4444")
            self._metrics_phase_hist_label.setText("  ".join(parts))
            self._metrics_phase_hist_label.setStyleSheet(f"color:{color};font-size:9pt;")
        else:
            self._metrics_phase_hist_label.setText("—")
            self._metrics_phase_hist_label.setStyleSheet("color:#4b5563;font-size:9pt;")

        # ── Network latency from active source tile ───────────────────────
        src = self._get_active_source_player_number()
        if src is not None:
            tile = self.player_tiles.get(src)
            if tile and not tile.is_dropped:
                self._metrics_latency_label.setText(tile.delay_label.text())
                self._metrics_latency_label.setStyleSheet("color:#9ca3af;font-size:11pt;")
                return
        self._metrics_latency_label.setText("—")
        self._metrics_latency_label.setStyleSheet("color:#4b5563;font-size:11pt;")

    def _init_ui(self) -> None:
        # Target: 1280×720 landscape, reTerminal 5" IPS touchscreen
        # Row heights: 50 toolbar + 110 players + 530 controls + 28 status = 718 (≈720)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ── Toolbar ~50px ─────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.midi_led = QFrame()
        self.midi_led.setFixedSize(34, 34)
        self.midi_led.setStyleSheet(
            "background:#2d2d2d;border:2px solid #4a4a4a;border-radius:17px;"
        )
        toolbar.addWidget(self.midi_led)

        self.midi_port_combo = QComboBox()   # hidden, keeps existing logic intact
        self.midi_port_combo.setVisible(False)
        self._midi_port_btn = QPushButton("Select MIDI Device")
        self._midi_port_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._midi_port_btn.setMinimumHeight(36)
        self._midi_port_btn.setStyleSheet(
            "QPushButton{background:#1e3a5f;border:1px solid #0ea5e9;"
            "border-radius:6px;color:#7dd3fc;font-weight:600;text-align:left;padding-left:10px;}"
            "QPushButton:hover{background:#1e4976;}"
            "QPushButton:disabled{background:#1f2937;color:#4b5563;border-color:#374151;}"
        )
        self._midi_port_btn.clicked.connect(self._open_midi_port_dialog)
        toolbar.addWidget(self._midi_port_btn, stretch=3)

        # Hidden legacy button — kept for test compatibility, not shown in UI
        self.start_stop_button = QPushButton("Start")
        self.start_stop_button.setCheckable(True)
        self.start_stop_button.setVisible(False)
        self.start_stop_button.clicked.connect(self.toggle_midi_clock_output)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setFixedWidth(110)
        self.settings_button.clicked.connect(self.open_settings_dialog)
        toolbar.addWidget(self.settings_button)

        self.exit_button = QPushButton("Exit")
        self.exit_button.setFixedWidth(80)
        self.exit_button.setStyleSheet(
            "background:#7f1d1d;border:1px solid #991b1b;color:white;"
        )
        self.exit_button.clicked.connect(self.close)
        toolbar.addWidget(self.exit_button)

        main_layout.addLayout(toolbar)



        # ── Controls area (fills remaining ~530px) ────────────────────────
        controls_frame = QFrame()
        controls_frame.setFrameStyle(QFrame.NoFrame)
        controls_layout = QVBoxLayout(controls_frame)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)

        # 3-column control grid
        ctrl_grid = QGridLayout()
        ctrl_grid.setSpacing(8)
        ctrl_grid.setColumnStretch(0, 1)
        ctrl_grid.setColumnStretch(1, 1)
        ctrl_grid.setColumnStretch(2, 1)

        # ── Col 0: Clock Source ───────────────────────────────────────────
        col0_layout = QVBoxLayout()
        col0_layout.setSpacing(4)

        source_group = QGroupBox("Clock Source")
        source_layout = QVBoxLayout()
        source_layout.setContentsMargins(8, 6, 8, 8)
        source_layout.setSpacing(6)
        self.source_master_radio = QRadioButton("Follow Network Master")
        self.source_master_radio.setChecked(True)
        self.source_master_radio.toggled.connect(
            lambda checked: checked and self._on_source_radio_changed()
        )
        source_layout.addWidget(self.source_master_radio)
        player_row = QHBoxLayout()
        self.source_player_radio = QRadioButton("Lock to Player:")
        self.source_player_radio.toggled.connect(
            lambda checked: checked and self._on_source_radio_changed()
        )
        player_row.addWidget(self.source_player_radio)
        self.source_player_combo = QComboBox()
        self.source_player_combo.addItems(["1", "2", "3", "4"])
        self.source_player_combo.setEnabled(False)
        self.source_player_combo.setFixedWidth(72)
        self.source_player_combo.currentIndexChanged.connect(
            self._on_source_player_combo_changed
        )
        player_row.addWidget(self.source_player_combo)
        player_row.addStretch()
        source_layout.addLayout(player_row)
        self.active_source_label = QLabel("Waiting for CDJs...")
        self.active_source_label.setStyleSheet(
            "color:#6b7280;font-weight:bold;padding:4px;"
            "border:1px solid #374151;border-radius:6px;"
        )
        self.active_source_label.setWordWrap(True)
        source_layout.addWidget(self.active_source_label)
        source_group.setLayout(source_layout)

        # Player tiles strip (the big 100px tiles with BPM)
        self.player_grid_layout = QGridLayout()
        self.player_grid_layout.setContentsMargins(0, 0, 0, 0)
        self.player_grid_layout.setSpacing(4)
        for _col in range(4):
            self.player_grid_layout.setColumnStretch(_col, 1)
        col0_layout.addLayout(self.player_grid_layout)

        # Now Playing: waveform + track info only
        nowplaying_group = QGroupBox("Now Playing")
        nowplaying_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        np_layout = QVBoxLayout()
        np_layout.setContentsMargins(8, 6, 8, 8)
        np_layout.setSpacing(4)
        self._waveform_widget = _PreviewWaveformWidget()
        self._waveform_widget.setFixedHeight(80)
        self._waveform_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        np_layout.addWidget(self._waveform_widget)
        track_bar = QHBoxLayout()
        track_bar.setContentsMargins(0, 2, 0, 0)
        track_bar.setSpacing(6)
        self._track_state_led = QFrame()
        self._track_state_led.setFixedSize(10, 10)
        self._track_state_led.setStyleSheet(f"background:{_LED_OFF};border-radius:5px;")
        track_bar.addWidget(self._track_state_led)
        self._track_player_label = QLabel("—")
        self._track_player_label.setStyleSheet("color:#6b7280;font-size:10pt;font-weight:bold;")
        self._track_player_label.setFixedWidth(28)
        track_bar.addWidget(self._track_player_label)
        self._track_title_label = QLabel("No track")
        self._track_title_label.setStyleSheet("color:#e5e7eb;font-size:11pt;font-weight:bold;")
        self._track_title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._track_title_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        track_bar.addWidget(self._track_title_label, stretch=3)
        self._track_artist_label = QLabel("")
        self._track_artist_label.setStyleSheet("color:#9ca3af;font-size:10pt;")
        self._track_artist_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._track_artist_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        track_bar.addWidget(self._track_artist_label, stretch=2)
        self._track_key_label = QLabel("")
        self._track_key_label.setStyleSheet("color:#a78bfa;font-size:10pt;font-weight:bold;")
        self._track_key_label.setFixedWidth(46)
        self._track_key_label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        track_bar.addWidget(self._track_key_label)
        self._track_duration_label = QLabel("")
        self._track_duration_label.setStyleSheet("color:#6b7280;font-size:10pt;")
        self._track_duration_label.setFixedWidth(34)
        self._track_duration_label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        track_bar.addWidget(self._track_duration_label)
        np_layout.addLayout(track_bar)
        nowplaying_group.setLayout(np_layout)

        col0_layout.addWidget(source_group)
        col0_layout.addWidget(nowplaying_group, stretch=1)
        ctrl_grid.addLayout(col0_layout, 0, 0)

        # ── Col 1: Grid Alignment + BPM Control (stacked) ────────────────
        mid_layout = QVBoxLayout()
        mid_layout.setSpacing(8)

        # ── Grid Alignment (Phase) ────────────────────────────────────────
        phase_group = QGroupBox("Grid Alignment (Phase)")
        phase_layout = QVBoxLayout()
        phase_layout.setContentsMargins(12, 8, 12, 10)
        phase_layout.setSpacing(8)

        # Live metrics panel
        metrics_frame = QFrame()
        metrics_frame.setObjectName("MetricsFrame")
        metrics_frame.setStyleSheet(
            "QFrame#MetricsFrame{"
            "background:#1e1e1e;border:1px solid #2d2d2d;border-radius:6px;"
            "}"
        )
        metrics_layout = QGridLayout(metrics_frame)
        metrics_layout.setContentsMargins(10, 6, 10, 6)
        metrics_layout.setSpacing(4)
        metrics_layout.setColumnStretch(1, 1)

        def _mlabel(text: str, style: str = "") -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(style or "color:#6b7280;font-size:9pt;")
            return lbl

        # Output BPM
        metrics_layout.addWidget(
            _mlabel("OUT BPM"), 0, 0, Qt.AlignLeft
        )
        self._metrics_bpm_label = QLabel("---.--")
        self._metrics_bpm_label.setStyleSheet(
            "color:#4b5563;font-weight:bold;font-size:22pt;"
        )
        self._metrics_bpm_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        metrics_layout.addWidget(self._metrics_bpm_label, 0, 1, Qt.AlignRight)

        # Bar.beat position
        metrics_layout.addWidget(_mlabel("BAR.BEAT"), 1, 0, Qt.AlignLeft)
        self._metrics_beat_label = QLabel("-.–")
        self._metrics_beat_label.setStyleSheet(
            "color:#9ca3af;font-weight:bold;font-size:13pt;"
        )
        self._metrics_beat_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        metrics_layout.addWidget(self._metrics_beat_label, 1, 1, Qt.AlignRight)

        # Phase error history (sparkline)
        metrics_layout.addWidget(_mlabel("PHASE ERR"), 2, 0, Qt.AlignLeft)
        self._metrics_phase_hist_label = QLabel("—")
        self._metrics_phase_hist_label.setStyleSheet("color:#4b5563;font-size:9pt;")
        self._metrics_phase_hist_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        metrics_layout.addWidget(self._metrics_phase_hist_label, 2, 1, Qt.AlignRight)

        # Network latency to active source CDJ
        metrics_layout.addWidget(_mlabel("CDJ DELAY"), 3, 0, Qt.AlignLeft)
        self._metrics_latency_label = QLabel("—")
        self._metrics_latency_label.setStyleSheet("color:#9ca3af;font-size:11pt;")
        self._metrics_latency_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        metrics_layout.addWidget(self._metrics_latency_label, 3, 1, Qt.AlignRight)

        phase_layout.addWidget(metrics_frame, stretch=1)
        phase_group.setLayout(phase_layout)
        mid_layout.addWidget(phase_group)

        # ── BPM Control ───────────────────────────────────────────────────
        manual_group = QGroupBox("BPM Control")
        manual_layout = QVBoxLayout()
        manual_layout.setContentsMargins(12, 8, 12, 10)
        manual_layout.setSpacing(10)

        manual_top = QHBoxLayout()
        manual_top.setSpacing(8)
        # Two clear states: unchecked = "Manual BPM", checked = "Auto BPM"
        self.manual_mode_button = QPushButton("Manual BPM")
        self.manual_mode_button.setCheckable(True)
        self.manual_mode_button.setMinimumHeight(44)
        self.manual_mode_button.clicked.connect(self.toggle_manual_bpm_mode)
        manual_top.addWidget(self.manual_mode_button)

        self.tap_tempo_button = QPushButton("Tap Tempo")
        self.tap_tempo_button.setMinimumHeight(44)
        self.tap_tempo_button.clicked.connect(self.handle_tap_tempo_clicked)
        self.tap_tempo_button.setEnabled(False)
        manual_top.addWidget(self.tap_tempo_button)
        manual_layout.addLayout(manual_top)

        # BPM label
        self.manual_bpm_label = QLabel("120.0 BPM")
        self.manual_bpm_label.setAlignment(Qt.AlignCenter)
        self.manual_bpm_label.setStyleSheet(
            "color:#f59e0b;font-size:22pt;font-weight:bold;"
        )
        self.manual_bpm_label.setEnabled(False)
        manual_layout.addWidget(self.manual_bpm_label)

        # Custom touch slider — full-width drag anywhere, no tiny handle
        self.manual_bpm_slider = _TouchBpmSlider()
        self.manual_bpm_slider.setRange(300, 2000)  # 30.0 – 200.0 BPM
        self.manual_bpm_slider.setValue(1200)
        self.manual_bpm_slider.valueChanged.connect(self._manual_bpm_label_update)
        self.manual_bpm_slider.sliderReleased.connect(self.manual_bpm_slider_changed)
        self.manual_bpm_slider.setEnabled(False)
        manual_layout.addWidget(self.manual_bpm_slider)

        manual_group.setLayout(manual_layout)
        mid_layout.addWidget(manual_group)

        ctrl_grid.addLayout(mid_layout, 0, 1)

        # ── Col 2: Sync Start/Stop + Grid Shift ──────────────────────────
        right_col = QVBoxLayout()
        right_col.setSpacing(4)
        right_col.setAlignment(Qt.AlignTop)

        # ── Device Sync ───────────────────────────────────────────────────
        sync_group = QGroupBox("Device Sync")
        sync_layout = QVBoxLayout()
        sync_layout.setContentsMargins(6, 4, 6, 6)
        sync_layout.setSpacing(4)

        # Countdown + status row
        countdown_row = QHBoxLayout()
        countdown_row.setSpacing(10)
        self._countdown_label = QLabel("—")
        self._countdown_label.setFixedSize(40, 40)
        self._countdown_label.setAlignment(Qt.AlignCenter)
        self._countdown_label.setStyleSheet(
            "color:#f59e0b;font-size:26pt;font-weight:bold;"
            "background:#1c1917;border:2px solid #44403c;border-radius:8px;"
        )
        countdown_row.addWidget(self._countdown_label)
        self._sync_status_label = QLabel("Press Start to sync\nto CDJ bar beat 1")
        self._sync_status_label.setWordWrap(True)
        self._sync_status_label.setStyleSheet("color:#6b7280;font-size:9pt;")
        countdown_row.addWidget(self._sync_status_label, stretch=1)
        sync_layout.addLayout(countdown_row)

        # Start button — full width, big
        self._sync_start_btn = QPushButton("▶   Start & Sync")
        self._sync_start_btn.setMinimumHeight(36)
        self._sync_start_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._sync_start_btn.setStyleSheet(
            "QPushButton{background:#065f46;border:2px solid #10b981;"
            "border-radius:8px;color:white;font-size:11pt;font-weight:bold;}"
            "QPushButton:pressed{background:#047857;}"
            "QPushButton:disabled{background:#1f2937;border-color:#374151;color:#4b5563;}"
        )
        self._sync_start_btn.clicked.connect(self._on_sync_start_clicked)
        sync_layout.addWidget(self._sync_start_btn)

        # Stop button — full width
        self._sync_stop_btn = QPushButton("■   Stop")
        self._sync_stop_btn.setMinimumHeight(32)
        self._sync_stop_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._sync_stop_btn.setStyleSheet(
            "QPushButton{background:#450a0a;border:2px solid #ef4444;"
            "border-radius:8px;color:white;font-size:11pt;font-weight:bold;}"
            "QPushButton:pressed{background:#dc2626;}"
            "QPushButton:disabled{background:#1f2937;border-color:#374151;color:#4b5563;}"
        )
        self._sync_stop_btn.setEnabled(False)
        self._sync_stop_btn.clicked.connect(self._on_sync_stop_clicked)
        sync_layout.addWidget(self._sync_stop_btn)

        sync_group.setLayout(sync_layout)
        right_col.addWidget(sync_group)

        # ── Auto Sync ─────────────────────────────────────────────────────
        autosync_group = QGroupBox("Auto Sync")
        autosync_layout = QVBoxLayout()
        autosync_layout.setContentsMargins(6, 4, 6, 6)
        autosync_layout.setSpacing(4)

        self.auto_sync_button = QPushButton("Auto Sync: ON")
        self.auto_sync_button.setCheckable(True)
        self.auto_sync_button.setChecked(True)
        self.auto_phase_correction_enabled = True
        self.auto_sync_button.setMinimumHeight(32)
        self.auto_sync_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.auto_sync_button.setStyleSheet(
            "QPushButton{background:#1f2937;border:2px solid #374151;"
            "border-radius:8px;color:#6b7280;font-size:10pt;font-weight:bold;}"
            "QPushButton:checked{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 #065f46,stop:1 #047857);"
            "border:2px solid #10b981;color:white;}"
        )
        self.auto_sync_button.clicked.connect(self.toggle_auto_sync)
        autosync_layout.addWidget(self.auto_sync_button)

        # Phase error display inside Auto Sync group
        phase_row = QHBoxLayout()
        self.phase_lock_led = QFrame()
        self.phase_lock_led.setFixedSize(20, 20)
        self.phase_lock_led.setStyleSheet(
            f"background:{_LED_OFF};border:2px solid {_LED_OFF_BORDER};border-radius:10px;"
        )
        phase_row.addWidget(self.phase_lock_led)
        self.phase_error_label = QLabel("±0.0 ms")
        self.phase_error_label.setStyleSheet(
            "color:#6b7280;font-weight:bold;font-size:12pt;"
        )
        phase_row.addWidget(self.phase_error_label)
        phase_row.addStretch()
        autosync_layout.addLayout(phase_row)

        autosync_group.setLayout(autosync_layout)
        right_col.addWidget(autosync_group)

        # ── Grid Shift (pure phase nudge, no BPM change) ──────────────────
        shift_group = QGroupBox("Grid Shift (Phase Only)")
        shift_layout = QVBoxLayout()
        shift_layout.setContentsMargins(6, 4, 6, 6)
        shift_layout.setSpacing(4)

        # Row 1: full-width Earlier and Later buttons — no spinbox in between
        shift_btn_row = QHBoxLayout()
        shift_btn_row.setSpacing(8)
        self.pitch_down_button = QPushButton("◀  Earlier")
        self.pitch_down_button.setMinimumHeight(32)
        self.pitch_down_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.pitch_down_button.setStyleSheet("font-size:10pt;font-weight:bold;")
        self.pitch_down_button.clicked.connect(lambda: self.adjust_grid_shift(-1))
        shift_btn_row.addWidget(self.pitch_down_button)
        self.pitch_up_button = QPushButton("Later  ▶")
        self.pitch_up_button.setMinimumHeight(32)
        self.pitch_up_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.pitch_up_button.setStyleSheet("font-size:10pt;font-weight:bold;")
        self.pitch_up_button.clicked.connect(lambda: self.adjust_grid_shift(1))
        shift_btn_row.addWidget(self.pitch_up_button)
        shift_layout.addLayout(shift_btn_row)

        # Row 2: step size toggle buttons (no spinbox — eliminates touch overlap)
        self._step_ms = 5
        self._step_buttons = {}
        step_row = QHBoxLayout()
        step_row.setSpacing(6)
        step_lbl = QLabel("Step:")
        step_lbl.setStyleSheet("color:#9ca3af;font-size:9pt;")
        step_row.addWidget(step_lbl)
        for v in [1, 5, 10, 25]:
            btn = QPushButton(f"{v} ms")
            btn.setCheckable(True)
            btn.setChecked(v == self._step_ms)
            btn.setFixedHeight(26)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.clicked.connect(lambda checked, val=v: self._set_grid_step(val))
            step_row.addWidget(btn)
            self._step_buttons[v] = btn
        shift_layout.addLayout(step_row)

        # hidden spinbox so any existing code referencing it doesn't crash
        self.pitch_amount_spinbox = QDoubleSpinBox()
        self.pitch_amount_spinbox.setValue(5.0)
        self.pitch_amount_spinbox.setVisible(False)

        # Row 3: last-nudge flash label
        self.pitch_label = QLabel("")
        self.pitch_label.setAlignment(Qt.AlignCenter)
        self.pitch_label.setStyleSheet("color:#0ea5e9;font-size:10pt;")
        self.pitch_label.setFixedHeight(16)
        shift_layout.addWidget(self.pitch_label)

        # Row 4: persistent offset display + reset button
        offset_row = QHBoxLayout()
        offset_row.setSpacing(6)
        offset_lbl = QLabel("Offset:")
        offset_lbl.setStyleSheet("color:#9ca3af;font-size:9pt;")
        offset_lbl.setFixedWidth(46)
        offset_row.addWidget(offset_lbl)
        self._offset_display = QLabel("+0 ms")
        self._offset_display.setAlignment(Qt.AlignCenter)
        self._offset_display.setStyleSheet(
            "color:#10b981;font-size:14pt;font-weight:bold;"
            "background:#1e1e1e;border:1px solid #2d2d2d;border-radius:5px;"
        )
        self._offset_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._offset_display.setFixedHeight(28)
        offset_row.addWidget(self._offset_display)
        self._grid_reset_button = QPushButton("Reset")
        self._grid_reset_button.setFixedWidth(60)
        self._grid_reset_button.setFixedHeight(28)
        self._grid_reset_button.setStyleSheet(
            "QPushButton{font-size:9pt;background:#450a0a;border:1px solid #ef4444;}"
            "QPushButton:pressed{background:#dc2626;}"
        )
        self._grid_reset_button.clicked.connect(self.reset_grid_shift)
        offset_row.addWidget(self._grid_reset_button)
        shift_layout.addLayout(offset_row)

        shift_group.setLayout(shift_layout)
        right_col.addWidget(shift_group)

        ctrl_grid.addLayout(right_col, 0, 2)

        controls_layout.addLayout(ctrl_grid, stretch=1)

        # ── Status bar ~28px ──────────────────────────────────────────────
        self.global_status_label = QLabel("● Stopped")
        self.global_status_label.setWordWrap(False)
        self.global_status_label.setStyleSheet("color:#6b7280;padding:2px 4px;")
        controls_layout.addWidget(self.global_status_label)

        main_layout.addWidget(controls_frame, stretch=1)
        self.setFixedSize(1280, 720)


    def _connect_signals(self):
        self.signal_bridge.client_change_signal.connect(self.handle_client_or_master_change)
        self.signal_bridge.beat_signal.connect(self._on_beat_signal)
        self.signal_bridge.prodj_beat_signal.connect(self.handle_prodj_beat)
        self.signal_bridge.prodj_beat_timing_signal.connect(self.handle_prodj_beat_timing)
        self.signal_bridge.metadata_ready_signal.connect(self._refresh_track_info)
        self.signal_bridge.waveform_ready_signal.connect(self._on_waveform_ready)

    def handle_client_or_master_change(self, player_number_changed=None):
        self.update_player_display()
        if not self.manual_bpm_mode_active:
            self.update_midi_clock_source_logic()
        self._update_active_source_label()
        self._refresh_metrics()
        self._refresh_track_info()
        # Slip Mode vom aktiven CDJ lesen
        src = self._get_active_source_player_number()
        if src is not None:
            client = self.prodj.cl.getClient(src)
            if client is not None:
                slip = getattr(client, 'slip_mode', False)
                if slip != self._slip_mode_active:
                    self._slip_mode_active = slip
                    if slip:
                        logging.info("Slip Mode ON - BPM changes ignored")
                    else:
                        logging.info("Slip Mode OFF - resuming normal sync")

    def update_player_display(self) -> None:
        """Rebuild the player tile strip.  Tiles are created once per player
        and kept (showing 'Dropped') when a CDJ goes offline, so the layout
        only ever grows — never shrinks mid-session.  Positions are assigned
        by sorted player number so the order is always 1-2-3-4.
        """
        logging.debug("Updating player display")
        active_numbers = {
            c.player_number for c in self.prodj.cl.clients if c.type == "cdj"
        }

        # Mark disappeared players as dropped
        for player_num, tile in self.player_tiles.items():
            if player_num not in active_numbers and not tile.is_dropped:
                tile.set_dropped_status(True)
                logging.info("Player %d marked as dropped.", player_num)
            elif player_num in active_numbers and tile.is_dropped:
                tile.set_dropped_status(False)
                logging.info("Player %d reconnected.", player_num)

        # Create tiles for newly seen players and place them in sorted order
        sorted_clients = sorted(
            (c for c in self.prodj.cl.clients if c.type == "cdj"),
            key=lambda c: c.player_number,
        )

        for grid_col, client in enumerate(sorted_clients):
            pn = client.player_number

            # Create tile if new
            if pn not in self.player_tiles:
                tile = PlayerTileWidget(pn)
                tile.selected_signal.connect(self.handle_player_tile_selected)
                self.player_tiles[pn] = tile
            else:
                tile = self.player_tiles[pn]

            # Ensure tile is in the correct grid cell
            idx = self.player_grid_layout.indexOf(tile)
            if idx == -1:
                self.player_grid_layout.addWidget(tile, 0, grid_col)
            else:
                r, c, *_ = self.player_grid_layout.getItemPosition(idx)
                if r != 0 or c != grid_col:
                    self.player_grid_layout.removeWidget(tile)
                    self.player_grid_layout.addWidget(tile, 0, grid_col)

            # Compute effective BPM and tick delay for this client
            effective_bpm = self._bpm_from_client(client)
            delay_value = (
                60.0 / effective_bpm / 24.0 if effective_bpm and effective_bpm > 0 else 0.0
            )

            tile.update_data(
                bpm=effective_bpm,
                delay=delay_value,
                is_master="master" in client.state,
            )
            tile.set_selected_source(self.selected_player_source == pn)

        self.update_global_status_label()

    def handle_player_tile_selected(self, player_number):
        """Tile click now syncs the radio buttons to match the selection."""
        logging.info("Player tile %d clicked.", player_number)
        # If already selected, go back to master-follow
        if self.selected_player_source == player_number:
            self.source_master_radio.setChecked(True)  # triggers _on_source_radio_changed
        else:
            # Switch combo to this player and activate the player radio
            idx = self.source_player_combo.findText(str(player_number))
            if idx >= 0:
                self.source_player_combo.blockSignals(True)
                self.source_player_combo.setCurrentIndex(idx)
                self.source_player_combo.blockSignals(False)
            self.source_player_radio.setChecked(True)  # triggers _on_source_radio_changed

        for num, tile_widget in self.player_tiles.items():
            tile_widget.set_selected_source(num == self.selected_player_source)

    def _determine_midi_backend(self):
        # Default to rtmidi if ALSA is not explicitly preferred or not available
        if sys.platform.startswith('linux') and AlsaMidiClock is not None and \
           (self.preferred_midi_backend == "ALSA" or self.preferred_midi_backend is None): # Prefer ALSA on Linux by default
            self.MidiClockImpl = AlsaMidiClock
            logging.info("Selected ALSA MIDI backend.")
        elif RtMidiClock is not None:
            self.MidiClockImpl = RtMidiClock
            logging.info("Selected rtmidi MIDI backend.")
        else:
            logging.error("No suitable MIDI implementation found!")
            self.MidiClockImpl = None

    def populate_midi_ports(self) -> None:
        """Enumerate available MIDI ports into the hidden combo and update the port button."""
        self.midi_port_combo.clear()
        self._determine_midi_backend()

        if self.MidiClockImpl is None:
            self.midi_port_combo.addItem("No MIDI Backend!")
            self._midi_port_btn.setText("No MIDI Backend")
            self._midi_port_btn.setEnabled(False)
            self.start_stop_button.setEnabled(False)
            return

        try:
            ports: List[Tuple[str, Dict]] = []  # (display_name, open_kwargs)
            if self.MidiClockImpl == AlsaMidiClock:
                tmp = AlsaMidiClock.__new__(AlsaMidiClock)
                tmp.__init__()
                for client_id, name, port_ids in tmp.iter_alsa_seq_clients():
                    for p_id in port_ids:
                        label = f"{name} ({client_id}:{p_id})"
                        ports.append((label, {'preferred_name': name, 'preferred_port': p_id}))
                del tmp
            elif self.MidiClockImpl == RtMidiClock:
                for idx, name in enumerate(rtmidi_list_ports()):
                    ports.append((name, {'preferred_port': idx}))

            if ports:
                for full_label, kwargs in ports:
                    short = _short_port_name(full_label)
                    self.midi_port_combo.addItem(short, userData=kwargs)
                self.midi_port_combo.setEnabled(True)
                self._midi_port_btn.setEnabled(True)
                self.start_stop_button.setEnabled(True)
                # Update button label to show current selection
                self._refresh_port_button_label()
            else:
                self.midi_port_combo.addItem("No MIDI Ports Found")
                self._midi_port_btn.setText("No MIDI Ports Found")
                self._midi_port_btn.setEnabled(False)
                self.start_stop_button.setEnabled(False)
        except Exception as exc:
            logging.error("Error listing MIDI ports: %s", exc, exc_info=True)
            self.midi_port_combo.addItem("Error listing ports")
            self._midi_port_btn.setText("Error listing ports")
            self._midi_port_btn.setEnabled(False)
            self.start_stop_button.setEnabled(False)

    def _refresh_port_button_label(self):
        """Sync the port button label to the current combo selection."""
        name = self.midi_port_combo.currentText()
        self._midi_port_btn.setText(f"MIDI:  {name}" if name else "Select MIDI Device")

    def _open_midi_port_dialog(self):
        """Show a fullscreen-friendly touch dialog to pick the MIDI output port."""
        count = self.midi_port_combo.count()
        if count == 0:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Select MIDI Output Device")
        dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dlg.setFixedSize(800, 500)
        dlg.setStyleSheet(
            "QDialog{background:#111827;border:2px solid #0ea5e9;border-radius:14px;}"
        )

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)

        title = QLabel("Select MIDI Output Device")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color:#7dd3fc;font-size:18pt;font-weight:bold;")
        layout.addWidget(title)

        # One big button per port
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        btn_layout = QVBoxLayout(inner)
        btn_layout.setSpacing(10)
        btn_layout.setContentsMargins(0, 0, 0, 0)

        current_idx = self.midi_port_combo.currentIndex()
        for i in range(count):
            name = self.midi_port_combo.itemText(i)
            btn = QPushButton(name)
            btn.setMinimumHeight(72)
            btn.setCheckable(True)
            btn.setChecked(i == current_idx)
            btn.setStyleSheet(
                "QPushButton{"
                "background:#1f2937;border:2px solid #374151;"
                "border-radius:10px;color:#e5e7eb;"
                "font-size:14pt;font-weight:600;text-align:left;padding-left:20px;}"
                "QPushButton:hover{border:2px solid #0ea5e9;}"
                "QPushButton:checked{"
                "background:#0c4a6e;border:2px solid #0ea5e9;color:#7dd3fc;}"
            )
            btn.clicked.connect(lambda _, idx=i, d=dlg: self._select_midi_port(idx, d))
            btn_layout.addWidget(btn)

        btn_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll, stretch=1)

        # Cancel button
        cancel = QPushButton("Cancel")
        cancel.setMinimumHeight(60)
        cancel.setStyleSheet(
            "QPushButton{background:#374151;border:2px solid #4b5563;"
            "border-radius:10px;color:#e5e7eb;font-size:13pt;font-weight:700;}"
            "QPushButton:pressed{background:#4b5563;}"
        )
        cancel.clicked.connect(dlg.reject)
        layout.addWidget(cancel)

        dlg.exec_()

    def _select_midi_port(self, index: int, dlg: 'QDialog'):
        """Select port by index, update combo + button label, close dialog."""
        self.midi_port_combo.setCurrentIndex(index)
        self._refresh_port_button_label()
        dlg.accept()


    def _check_clock_crashed(self) -> bool:
        """Return True if the clock instance exists but its thread has died.
        Cleans up state so a fresh start is possible."""
        if (self.midi_clock_instance is not None
                and not self.midi_clock_instance.is_alive()):
            logging.warning("MIDI clock thread died unexpectedly — cleaning up.")
            self.midi_clock_instance = None
            self._last_applied_bpm = None
            self.start_stop_button.setChecked(False)
            self.start_stop_button.setText("Start")
            self.midi_port_combo.setEnabled(True)
            self.update_global_status_label()
            return True
        return False

    def toggle_midi_clock_output(self) -> None:
        # Always check for a silently-crashed thread first.
        self._check_clock_crashed()

        if self.start_stop_button.isChecked():  # user wants to start
            # Guard: if the clock is already alive, do NOT stop/restart it.
            if self.midi_clock_instance is not None and self.midi_clock_instance.is_alive():
                logging.warning(
                    "toggle_midi_clock_output called while clock already running — "
                    "ignoring restart request; use setBpm() to change tempo."
                )
                self.start_stop_button.setText("Stop")
                self.update_global_status_label()
                return

            port_display = self.midi_port_combo.currentText()
            if not port_display or any(
                kw in port_display for kw in ("No MIDI", "Error listing")
            ):
                logging.warning("No valid MIDI output port selected.")
                self.start_stop_button.setChecked(False)
                return

            if self.MidiClockImpl is None:
                logging.error("No MIDI implementation available.")
                self.start_stop_button.setChecked(False)
                return

            self.midi_clock_instance = self.MidiClockImpl()
            open_kwargs = self.midi_port_combo.currentData() or {}
            logging.debug("Opening MIDI port '%s' kwargs=%s", port_display, open_kwargs)

            try:
                self.midi_clock_instance.open(**open_kwargs)
                self.midi_clock_instance.set_beat_callback(self.beat_received)
                # Seed BPM directly before the thread starts — _apply_bpm is a
                # no-op until is_alive() is True, so we call setBpm explicitly here.
                seed_bpm = self._resolve_bpm_for_seed()
                self.midi_clock_instance.setBpm(seed_bpm)
                self.midi_clock_instance.start()
                self._beat_snap_pending = True  # snap grid on first CDJ beat
                logging.info("MIDI clock started on '%s' at %.2f BPM",
                             port_display, seed_bpm)
                self.start_stop_button.setText("Stop")
                self.midi_port_combo.setEnabled(False)
                self.update_global_status_label()
            except Exception as exc:
                logging.error(
                    "Failed to start MIDI clock on '%s': %s", port_display, exc,
                    exc_info=True
                )
                self.midi_clock_instance = None
                self.start_stop_button.setChecked(False)
        else:  # user wants to stop
            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.stop()
                logging.info("MIDI clock stopped.")
            self.midi_clock_instance = None
            self._last_applied_bpm = None  # force fresh setBpm on next start
            self.start_stop_button.setText("Start")
            self.midi_port_combo.setEnabled(True)
            # Reset phase display
            self.phase_error_ms = 0.0
            self.phase_error_history.clear()
            self._sparkline.clear()
            self.phase_lock_led.setStyleSheet(
                f"background:{_LED_OFF};border:2px solid {_LED_OFF_BORDER};border-radius:12px;"
            )
            self.phase_error_label.setText("\u00b10.0 ms")
            self.phase_error_label.setStyleSheet(f"color:{_LED_OFF_BORDER};")
            self._refresh_metrics()
        self.update_global_status_label()

    def _on_sync_start_clicked(self):
        """Start & Sync: starts clock internally, waits for CDJ bar beat 1,
        then fires MIDI output + 0xFA exactly on beat 1."""

        if self._output_state == 'waiting':
            # Cancel pending count-in
            self._output_state = 'stopped'
            self._sync_start_pending = False
            self._sync_start_countdown = 0
            self._countdown_label.setText("—")
            self._sync_status_label.setText("Cancelled.")
            self._sync_status_label.setStyleSheet("color:#6b7280;font-size:9pt;")
            self._sync_start_btn.setText("▶   Start & Sync")
            self._sync_stop_btn.setEnabled(False)
            # Stop internal clock
            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.stop()
                self.midi_clock_instance = None
                self._last_applied_bpm = None
            self._midi_port_btn.setEnabled(True)
            self.update_global_status_label()
            return

        if self._output_state == 'running':
            # Already running — ignore, use Stop button
            return

        # ── STOPPED → WAITING ────────────────────────────────────────
        port_display = self.midi_port_combo.currentText()
        if not port_display or any(kw in port_display
                                   for kw in ("No MIDI", "Error listing", "No MIDI Ports")):
            self._sync_status_label.setText("Select a MIDI device first.")
            self._sync_status_label.setStyleSheet("color:#ef4444;font-size:9pt;")
            return

        # Start internal clock (silent — no MIDI output until beat 1)
        try:
            self.midi_clock_instance = self.MidiClockImpl()
            open_kwargs = self.midi_port_combo.currentData() or {}
            self.midi_clock_instance.open(**open_kwargs)
            self.midi_clock_instance.set_beat_callback(self.beat_received)
            seed_bpm = self._resolve_bpm_for_seed()
            self.midi_clock_instance.setBpm(seed_bpm)
            self.midi_clock_instance.start()
            self._beat_snap_pending = True
            self._last_applied_bpm = seed_bpm
            logging.info("Clock started internally at %.2f BPM, waiting for bar beat 1", seed_bpm)
        except Exception as exc:
            logging.error("Failed to start MIDI clock: %s", exc, exc_info=True)
            self.midi_clock_instance = None
            self._sync_status_label.setText("Failed to open MIDI port.")
            self._sync_status_label.setStyleSheet("color:#ef4444;font-size:9pt;")
            return

        self._output_state = 'waiting'
        self._sync_start_pending = True
        self._sync_beats_seen = 0
        self._midi_port_btn.setEnabled(False)
        self._sync_stop_btn.setEnabled(True)
        self._sync_start_btn.setText("✕   Cancel")
        self._sync_start_btn.setStyleSheet(
            "QPushButton{background:#78350f;border:2px solid #f59e0b;"
            "border-radius:8px;color:white;font-size:11pt;font-weight:bold;}"
            "QPushButton:pressed{background:#92400e;}"
        )
        beats_left = self._beats_until_bar_one()
        self._sync_start_countdown = beats_left
        self._countdown_label.setText(str(beats_left) if beats_left > 0 else "►")
        self._sync_status_label.setText("Waiting for\nbar beat 1…")
        self._sync_status_label.setStyleSheet("color:#f59e0b;font-size:9pt;font-weight:bold;")
        self.update_global_status_label()
        logging.info("Sync Start armed — %d beats until bar beat 1.", beats_left)

    def _on_sync_stop_clicked(self):
        """Stop MIDI output immediately — send 0xFC, stop clock."""
        self._sync_start_pending = False
        self._sync_start_countdown = 0
        self._output_state = 'stopped'
        self._countdown_label.setText("—")
        self._sync_start_btn.setText("▶   Start & Sync")
        self._sync_start_btn.setStyleSheet(
            "QPushButton{background:#065f46;border:2px solid #10b981;"
            "border-radius:8px;color:white;font-size:14pt;font-weight:bold;}"
            "QPushButton:pressed{background:#047857;}"
            "QPushButton:disabled{background:#1f2937;border-color:#374151;color:#4b5563;}"
        )
        self._sync_stop_btn.setEnabled(False)
        self._sync_start_btn.setEnabled(True)
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            if hasattr(self.midi_clock_instance, 'send_stop'):
                self.midi_clock_instance.send_stop()
            self.midi_clock_instance.stop()
            self.midi_clock_instance = None
            self._last_applied_bpm = None
        self._midi_port_btn.setEnabled(True)
        self._sync_status_label.setText("Stopped.")
        self._sync_status_label.setStyleSheet("color:#ef4444;font-size:9pt;")
        self.phase_error_ms = 0.0
        self.phase_error_history.clear()
        self._sparkline.clear()
        self._refresh_metrics()
        self.update_global_status_label()
        logging.info("Sync Stop sent, clock stopped.")

    def _beats_until_bar_one(self) -> int:
        """Return how many CDJ beats to wait before firing send_start().

        The CDJ sends beat 1..4 (bar position).  We want to fire exactly on
        the *next* beat-1 so the external device starts aligned to the bar.

        We NEVER fire immediately (min wait = 1 beat) so the user always sees
        at least '1' in the countdown — avoids a zero-latency race where the
        button-click and the incoming beat-1 packet collide.
        """
        bn = self._last_beat_number
        if bn <= 0:
            return 4  # no beat received yet — wait a full bar
        beat_in_bar = ((bn - 1) % 4) + 1  # normalise to 1..4
        # beats remaining to finish the current bar (min 1 so we never fire
        # on the same beat that was just received)
        # Always wait until the next bar beat 1 — minimum 1 beat, maximum 4
        remaining = (4 - beat_in_bar) + 1   # beats until next beat 1
        if remaining > 4:
            remaining = 4
        return remaining

    def handle_prodj_beat(self, player_number: int, beat_number: int) -> None:
        """Track the beat timestamp and bar position for the active source."""
        active_source = self._get_active_source_player_number()
        if player_number == active_source:
            self.last_prodj_beat_time = time.time()
            self._last_beat_number = beat_number
            self._last_beat_player = player_number

            # ── Sync Start count-in ──────────────────────────────────────
            # We count *received* CDJ beats since the button was pressed.
            # This is purely absolute — independent of bar position — so
            # there is no race between the button-click and an incoming
            # beat-1 packet arriving at the same moment.
            if self._sync_start_pending:
                self._sync_beats_seen += 1
                self._sync_start_countdown -= 1
                remaining = self._sync_start_countdown
                logging.debug("Count-in: beat seen #%d, %d remaining.",
                              self._sync_beats_seen, remaining)

                if remaining <= 0:
                    # Countdown reached zero — align grid then fire MIDI Start.
                    self._sync_start_pending = False
                    self._sync_start_countdown = 0
                    self._output_state = 'running'

                    # Hard-snap the ALSA queue to this beat boundary.
                    # The CDJ beat packet just arrived — wall-clock time is
                    # now ≈ beat boundary.  We compute how far our internal
                    # clock is ahead/behind and correct it so send_start()
                    # is scheduled exactly on our next beat tick.
                    clk = self.midi_clock_instance
                    if clk and clk.is_alive():
                        # Use next_beat_wall_time() if available (ALSA queue-anchored),
                        # otherwise fall back to last_beat_wall_time + period estimate.
                        snap_ms = self._compute_snap_ms(clk)
                        if snap_ms is not None:
                            clk.adjust_phase(snap_ms)
                            logging.info("Sync snap: %.1f ms before send_start", snap_ms)

                    self._countdown_label.setText("►")
                    self._sync_status_label.setText("Running — in sync!")
                    self._sync_status_label.setStyleSheet(
                        "color:#10b981;font-size:9pt;font-weight:bold;"
                    )
                    self._sync_start_btn.setText("▶   Start & Sync")
                    self._sync_start_btn.setEnabled(False)
                    if hasattr(self.midi_clock_instance, 'send_start'):
                        self.midi_clock_instance.send_start()
                    self.update_global_status_label()
                    logging.info("Sync Start fired after %d beats — output running.",
                                 self._sync_beats_seen)
                    QTimer.singleShot(2000, lambda: self._countdown_label.setText("—"))
                else:
                    self._countdown_label.setText(str(remaining))

    def _do_auto_resync(self):
        """Auto-Stop + Re-Sync nach grossem Tempowechsel laut Design-Doc.
        Stoppt MIDI Output, wartet auf stabiles BPM, startet neu auf Bar Beat 1.
        """
        if self._output_state != 'running':
            return
        logging.info("Auto-Resync: stopping output, waiting for bar beat 1")
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            if hasattr(self.midi_clock_instance, 'send_stop'):
                self.midi_clock_instance.send_stop()
        self._output_state = 'waiting'
        self._sync_start_pending = True
        self._sync_beats_seen = 0
        beats_left = self._beats_until_bar_one()
        self._sync_start_countdown = beats_left
        self._countdown_label.setText(str(beats_left))
        self._sync_status_label.setText("Tempo change!\nRe-syncing to bar beat 1...")
        self._sync_status_label.setStyleSheet("color:#f59e0b;font-size:9pt;font-weight:bold;")
        self.phase_error_history.clear()
        self._sparkline.clear()
        self._beat_snap_pending = True
        self._resync_pending = False
        self.update_global_status_label()

    def handle_prodj_beat_timing(self, player_number, beat_number, next_beat_ms):
        """Laut Design-Doc:
        - Slip Mode: BPM-Aenderungen ignorieren, Clock laeuft weiter
        - Einmaliger Snap beim Start via SETPOS_TIME
        - Danach: sanfte SKEW-Korrekturen max +-2ms pro Beat
        - CDJ Beat-Paket Ankunftszeit = echter Beat-Anker
        """
        if self.manual_bpm_mode_active:
            return
        if not self.midi_clock_instance or not self.midi_clock_instance.is_alive():
            return
        active_source = self._get_active_source_player_number()
        if player_number != active_source:
            return

        beat_period_ms = self.midi_clock_instance.delay * 24.0 * 1000.0
        if beat_period_ms <= 0:
            return

        # Einmaliger Grid-Snap nach Start oder Tempo-Wechsel
        if self._beat_snap_pending:
            self._beat_snap_pending = False
            t_cdj_beat = time.time()
            t_last_midi = getattr(self.midi_clock_instance, 'last_beat_wall_time', None)
            if t_last_midi is not None:
                error_ms = (t_cdj_beat - t_last_midi) * 1000.0 % beat_period_ms
                if error_ms > beat_period_ms / 2:
                    error_ms -= beat_period_ms
                snap_ms = -error_ms + self._grid_offset_ms
                self.midi_clock_instance.adjust_phase(snap_ms)
                logging.info("Beat grid snap: %.1f ms", snap_ms)
            return

        if not self.auto_phase_correction_enabled:
            return

        # Slip Mode: keine Korrektur
        if self._slip_mode_active:
            return

        # Phasenfehler messen (CDJ Beat-Ankunftszeit = echter Beat)
        t_cdj_beat = time.time()
        t_last_midi = getattr(self.midi_clock_instance, 'last_beat_wall_time', None)
        if t_last_midi is None:
            return

        error_ms = (t_cdj_beat - t_last_midi) * 1000.0 % beat_period_ms
        if error_ms > beat_period_ms / 2:
            error_ms -= beat_period_ms
        self.phase_error_ms = error_ms - self._grid_offset_ms

        # Sanfte SKEW-Korrektur: max +-2ms pro Beat
        # Kein SETPOS_TIME hier -- nur gradueller Nudge
        MAX_SKEW_MS = 2.0
        correction_ms = max(-MAX_SKEW_MS, min(MAX_SKEW_MS, -self.phase_error_ms))
        if abs(self.phase_error_ms) > 0.5:  # Toleranzband 0.5ms
            if hasattr(self.midi_clock_instance, 'adjust_phase'):
                # Nur SKEW verwenden (adjust_phase <= 20ms nutzt SKEW)
                self.midi_clock_instance.adjust_phase(correction_ms)

        self._sparkline.append(round(self.phase_error_ms, 1))
        if len(self._sparkline) > _SPARKLINE_LEN:
            self._sparkline.pop(0)

        logging.debug("Phase: err=%.1f ms corr=%.1f ms", self.phase_error_ms, correction_ms)
        self._update_phase_error_display()
    def _get_active_source_player_number(self):
        """Returns the player number of the current BPM/phase source, or None."""
        if self.selected_player_source is not None:
            tile = self.player_tiles.get(self.selected_player_source)
            if tile and not tile.is_dropped:
                return self.selected_player_source

        # Prefer network master
        active_cdjs = [
            c for c in self.prodj.cl.clients
            if c.type == "cdj" and not (self.player_tiles.get(c.player_number) and
                                        self.player_tiles[c.player_number].is_dropped)
        ]
        for client in active_cdjs:
            if "master" in client.state:
                return client.player_number

        # Only one CDJ connected — use it regardless of master flag
        if len(active_cdjs) == 1:
            return active_cdjs[0].player_number

        return None

    def update_midi_clock_source_logic(self) -> None:
        """Resolve the active BPM source and push it to the clock engine.
        Consolidated single call-site for _apply_bpm.
        """
        if self.manual_bpm_mode_active:
            self._apply_bpm(self.manual_bpm_value)
            self.update_global_status_label()
            return

        final_bpm: Optional[float] = None

        # 1. Try explicitly selected player
        if self.selected_player_source is not None:
            source_player = self.prodj.cl.getClient(self.selected_player_source)
            tile = self.player_tiles.get(self.selected_player_source)
            if source_player is not None and (tile is None or not tile.is_dropped):
                final_bpm = self._bpm_from_client(source_player)
                if final_bpm is not None:
                    # Only store as last-known-good when the player is actively playing
                    client_state = source_player.play_state if hasattr(source_player, 'play_state') else 'playing'
                    if client_state == 'playing':
                        self.last_known_good_bpm = final_bpm
                    self.coasting_bpm = None
                else:
                    logging.warning(
                        "Selected Player %d has no valid BPM.",
                        self.selected_player_source
                    )
            else:
                logging.warning(
                    "Selected Player %d unavailable or dropped.",
                    self.selected_player_source
                )

        # 2. Fall back to network master
        if final_bpm is None:
            for client in self.prodj.cl.clients:
                if client.type != "cdj" or "master" not in client.state:
                    continue
                tile = self.player_tiles.get(client.player_number)
                if tile and tile.is_dropped:
                    continue
                final_bpm = self._bpm_from_client(client)
                if final_bpm is not None:
                    # Only store as last-known-good when the player is actively playing
                    client_state = getattr(client, 'play_state', 'playing')
                    if client_state == 'playing':
                        self.last_known_good_bpm = final_bpm
                    self.coasting_bpm = None
                else:
                    logging.warning(
                        "Network Master Player %d has no valid BPM.",
                        client.player_number
                    )
                break
            else:
                logging.debug("No active network master found.")

        # 3. Coast on last known good BPM
        if final_bpm is None:
            fallback = self.last_known_good_bpm if self.last_known_good_bpm else 120.0
            self.coasting_bpm = fallback
            final_bpm = fallback
            if self.last_known_good_bpm:
                logging.info("No BPM source — coasting at %.2f BPM.", fallback)
            else:
                logging.warning("No BPM source and no history — defaulting to 120 BPM.")

        self._apply_bpm(final_bpm)
        self.update_global_status_label()

    def _resolve_bpm_for_seed(self) -> float:
        """Resolve the BPM to use when first starting the clock thread.
        Mirrors update_midi_clock_source_logic but returns the value instead
        of pushing it (because the thread is not alive yet).
        """
        if self.manual_bpm_mode_active:
            return self.manual_bpm_value
        if self.selected_player_source is not None:
            client = self.prodj.cl.getClient(self.selected_player_source)
            tile = self.player_tiles.get(self.selected_player_source)
            if client and (tile is None or not tile.is_dropped):
                bpm = self._bpm_from_client(client)
                if bpm:
                    return bpm
        for client in self.prodj.cl.clients:
            if client.type == "cdj" and "master" in client.state:
                bpm = self._bpm_from_client(client)
                if bpm:
                    return bpm
        return self.last_known_good_bpm if self.last_known_good_bpm else 120.0

    @staticmethod
    def _bpm_from_client(client) -> Optional[float]:
        """Extract effective BPM (bpm x actual_pitch) from a client object.
        Returns None if data is missing or invalid.
        actual_pitch of 0.0 means 'not yet received' on Pioneer gear — treat as 1.0.
        """
        try:
            bpm = float(client.bpm)
            pitch = float(client.actual_pitch)
            # Pioneer CDJs send actual_pitch=0.0 before the first status packet
            # that carries pitch info.  Treat 0.0 as 1.0 (no pitch offset).
            if pitch == 0.0:
                pitch = 1.0
            if bpm > 0:
                return bpm * pitch
        except (TypeError, ValueError):
            pass
        return None


    def toggle_manual_bpm_mode(self) -> None:
        """Toggle between Manual BPM and Auto (CDJ-follow) mode."""
        self.manual_bpm_mode_active = self.manual_mode_button.isChecked()
        self.manual_bpm_slider.setEnabled(self.manual_bpm_mode_active)
        self.manual_bpm_label.setEnabled(self.manual_bpm_mode_active)
        self.tap_tempo_button.setEnabled(self.manual_bpm_mode_active)

        if self.manual_bpm_mode_active:
            # Consistent two-state label: checked = "Auto BPM" (click to go back to auto)
            self.manual_mode_button.setText("Auto BPM")
            # Prefer the actual running clock BPM — most accurate.
            # Fall back to last known good (from a *playing* CDJ), then 120.
            running_bpm = self._output_bpm()
            seed_bpm = (
                running_bpm
                if running_bpm is not None
                else (self.last_known_good_bpm or 120.0)
            )
            self.manual_bpm_value = seed_bpm
            self.manual_bpm_slider.setValue(int(seed_bpm * 10))
            self.manual_bpm_label.setText(f"{seed_bpm:.1f} BPM")
            self.tap_timestamps = []
            # Clear stale CDJ-derived display state so the metrics panel
            # immediately shows — rather than the last CDJ values.
            self._last_beat_number = 0
            self._sparkline.clear()
            self.phase_error_ms = 0.0
            self.phase_error_history.clear()
            self._apply_bpm(self.manual_bpm_value)
            logging.info("Manual BPM mode enabled at %.1f BPM.", self.manual_bpm_value)
        else:
            self.manual_mode_button.setText("Manual BPM")
            self.tap_timestamps = []
            # Force _apply_bpm to push the real CDJ BPM even if numerically
            # close to the last manual value — avoids the clock staying locked
            # to the manual BPM after switching back to auto.
            self._last_applied_bpm = None
            self.phase_error_history.clear()
            self._sparkline.clear()
            logging.info("Manual BPM mode disabled — reverting to auto source.")
            self.update_midi_clock_source_logic()
        self.update_global_status_label()
        self._refresh_metrics()

    def _manual_bpm_label_update(self, value):
        """Called on every valueChanged - only updates the label display only."""
        self.manual_bpm_label.setText(f"{value / 10.0:.1f} BPM")

    def manual_bpm_slider_changed(self) -> None:
        """Called on sliderReleased — applies new BPM to the running clock."""
        new_bpm = self.manual_bpm_slider.value() / 10.0
        self.tap_timestamps = []
        # Auto-activate manual mode on first slider touch.  Block slider
        # signals while calling toggle so it cannot overwrite the position.
        if not self.manual_bpm_mode_active:
            self.manual_bpm_slider.blockSignals(True)
            self.manual_mode_button.setChecked(True)
            self.toggle_manual_bpm_mode()
            self.manual_bpm_slider.blockSignals(False)
        self.manual_bpm_value = new_bpm
        self.manual_bpm_label.setText(f"{new_bpm:.1f} BPM")
        self._apply_bpm(self.manual_bpm_value)
        self.update_global_status_label()

    def handle_tap_tempo_clicked(self) -> None:
        # Fix 5d: properly activate manual mode (enables slider + tap button)
        if not self.manual_bpm_mode_active:
            self.manual_mode_button.setChecked(True)
            self.toggle_manual_bpm_mode()

        current_time = time.time()

        if self.tap_timestamps and (current_time - self.tap_timestamps[-1] > TAP_TIMEOUT_SECONDS):
            self.tap_timestamps = []
            logging.debug("Tap timeout, resetting tap history.")

        self.tap_timestamps.append(current_time)

        if len(self.tap_timestamps) > MAX_TAPS_FOR_AVG:
            self.tap_timestamps = self.tap_timestamps[-MAX_TAPS_FOR_AVG:]

        if len(self.tap_timestamps) < 2:
            logging.debug("Not enough taps yet to calculate BPM.")
            return

        intervals = [self.tap_timestamps[i] - self.tap_timestamps[i-1] for i in range(1, len(self.tap_timestamps))]
        if not intervals: return

        avg_interval = sum(intervals) / len(intervals)

        if avg_interval > 0:
            tapped_bpm = 60.0 / avg_interval
            tapped_bpm = max(self.BPM_MIN, min(self.BPM_MAX, tapped_bpm))

            self.manual_bpm_value = tapped_bpm
            self.manual_bpm_slider.setValue(int(self.manual_bpm_value * 10))
            self.manual_bpm_label.setText(f"{self.manual_bpm_value:.1f} BPM")

            self._apply_bpm(self.manual_bpm_value)
            logging.info(
                "Tapped BPM: %.2f (avg over %d intervals)",
                self.manual_bpm_value, len(intervals)
            )
            self.update_global_status_label()
        else:
            logging.debug("Average interval is zero, cannot calculate BPM.")

    def update_global_status_label(self) -> None:
        """Build a concise one-line status bar summary and refresh the
        active-source label.  Derives all values from already-computed
        state — no duplicate client-list scanning.
        """
        self._update_active_source_label()

        running = bool(
            self.midi_clock_instance and self.midi_clock_instance.is_alive()
        )

        if not running:
            self.coasting_bpm = None
            self.global_status_label.setText("● Stopped")
            self.global_status_label.setStyleSheet("color:#6b7280;padding:2px 4px;")
            return

        # Port short name
        port = self.midi_port_combo.currentText()

        # Source description — reuse _update_active_source_label's logic
        # but return a compact string instead of updating a label
        if self.manual_bpm_mode_active:
            src_text = f"Manual {self.manual_bpm_value:.1f} BPM"
        elif self.coasting_bpm is not None:
            src_text = f"Coasting {self.coasting_bpm:.1f} BPM"
        else:
            src_num = self._get_active_source_player_number()
            if src_num is not None:
                client = self.prodj.cl.getClient(src_num)
                is_master = client and "master" in (client.state or [])
                tag = " Master" if is_master else ""
                bpm = self._bpm_from_client(client) if client else None
                bpm_str = f" · {bpm:.1f} BPM" if bpm else ""
                src_text = f"P{src_num}{tag}{bpm_str}"
            else:
                src_text = "No source"

        # Phase error
        err = self.phase_error_ms
        phase_str = f" · {err:+.1f} ms" if running and self.auto_phase_correction_enabled else ""

        text = f"● Running  {port}  ·  {src_text}{phase_str}"
        self.global_status_label.setText(text)
        self.global_status_label.setStyleSheet("color:#10b981;padding:2px 4px;")

    def closeEvent(self, event):
        # Ensure MIDI clock is stopped if running
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            self.midi_clock_instance.stop()
            logging.info("MIDI clock stopped (app close).")
        super().closeEvent(event)

    def open_settings_dialog(self) -> None:
        dialog = MidiClockSettingsDialog(self)
        if not dialog.has_configurable_settings():
            QMessageBox.information(
                self, "Settings",
                "No configurable settings for this platform."
            )
            return

        if dialog.exec_():
            new_backend = dialog.get_selected_backend()
            if self.preferred_midi_backend != new_backend:
                self.preferred_midi_backend = new_backend
                logging.info("Settings: MIDI backend → %s", new_backend)

                if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                    logging.info("Stopping clock for backend change.")
                    self.midi_clock_instance.stop()
                    self.midi_clock_instance = None
                    self.start_stop_button.setChecked(False)
                    self.start_stop_button.setText("Start")
                    self.midi_port_combo.setEnabled(True)

                self.populate_midi_ports()
                self.update_global_status_label()


class MidiClockSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.setWindowTitle("MIDI Clock Settings")
        self.setMinimumWidth(350)
        self.configurable_settings_present = False

        layout = QVBoxLayout(self)

        # --- Platform info label ---
        platform_names = {'linux': 'Linux', 'darwin': 'macOS', 'win32': 'Windows'}
        platform_str = platform_names.get(sys.platform, sys.platform)
        info_label = QLabel(f"Platform: {platform_str} | Backend: rtmidi"
                            + (" / ALSA" if AlsaMidiClock is not None else ""))
        info_label.setStyleSheet("color:#6b7280; font-size:10pt;")
        layout.addWidget(info_label)

        # --- Backend selection (Linux only, ALSA available) ---
        self.alsa_radio = None
        self.rtmidi_radio = None

        if sys.platform.startswith('linux') and AlsaMidiClock is not None:
            self.configurable_settings_present = True
            backend_group = QGroupBox("MIDI Backend Preference")
            backend_layout = QVBoxLayout()

            self.alsa_radio = QRadioButton("Prefer ALSA  (recommended — kernel-level timing)")
            self.rtmidi_radio = QRadioButton("Prefer rtmidi  (fallback, software timing)")

            current_preference = "ALSA"
            if self.parent_window and getattr(self.parent_window, 'preferred_midi_backend', None):
                 current_preference = self.parent_window.preferred_midi_backend

            if current_preference == "ALSA":
                self.alsa_radio.setChecked(True)
            else:
                self.rtmidi_radio.setChecked(True)

            backend_layout.addWidget(self.alsa_radio)
            backend_layout.addWidget(self.rtmidi_radio)
            backend_group.setLayout(backend_layout)
            layout.addWidget(backend_group)

        elif sys.platform == 'darwin':
            # macOS: rtmidi via CoreMIDI, no choice needed but show a note
            self.configurable_settings_present = False
            mac_label = QLabel(
                "macOS: using rtmidi with CoreMIDI backend.\n"
                "Timing is handled via mach_absolute_time — no further\n"
                "configuration required."
            )
            mac_label.setStyleSheet("color:#9ca3af; font-size:10pt;")
            mac_label.setWordWrap(True)
            layout.addWidget(mac_label)

        elif sys.platform == 'win32':
            self.configurable_settings_present = False
            win_label = QLabel(
                "Windows: using rtmidi with WinMM backend.\n"
                "High-resolution timer (timeBeginPeriod) is activated\n"
                "automatically while the MIDI clock is running."
            )
            win_label.setStyleSheet("color:#9ca3af; font-size:10pt;")
            win_label.setWordWrap(True)
            layout.addWidget(win_label)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def has_configurable_settings(self):
        return self.configurable_settings_present

    def get_selected_backend(self):
        if self.alsa_radio and self.alsa_radio.isChecked():
            return "ALSA"
        if self.rtmidi_radio and self.rtmidi_radio.isChecked():
            return "rtmidi"
        # macOS/Windows: always rtmidi
        if AlsaMidiClock is not None and sys.platform.startswith('linux'):
            return "ALSA"
        return "rtmidi"


if __name__ == '__main__':
    # Stand-alone smoke-test: python -m prodj.gui.midiclock_widgets
    from qtpy.QtWidgets import QApplication
    from qtpy.QtCore import QObject
    from unittest.mock import Mock

    logging.basicConfig(level=logging.DEBUG, format='%(levelname)-7s %(module)s: %(message)s')

    class _MockClient:
        def __init__(self, num, master=False, bpm=120.0, pitch=1.0):
            self.player_number = num
            self.model = "CDJ-MOCK"
            self.type = "cdj"
            self.bpm = bpm
            self.actual_pitch = pitch
            self.state = ["master"] if master else []
            self.fw = "1.00"

    class _MockProDj:
        def __init__(self):
            self.cl = Mock()
            self.cl.clients = [
                _MockClient(1, master=True, bpm=125.0),
                _MockClient(2, bpm=130.0),
            ]
            self.cl.getClient = lambda pn: next(
                (c for c in self.cl.clients if c.player_number == pn), None
            )
        def set_client_change_callback(self, cb): pass
        def start(self): pass
        def vcdj_set_player_number(self, n): pass
        def vcdj_enable(self): pass
        def stop(self): pass

    class _MockSignalBridge(QObject):
        client_change_signal     = Signal(int)
        master_change_signal     = Signal(int)
        beat_signal              = Signal()
        prodj_beat_signal        = Signal(int, int)
        prodj_beat_timing_signal = Signal(int, int, object)
        metadata_ready_signal    = Signal()

    _app = QApplication(sys.argv)
    _window = MidiClockMainWindow(_MockProDj(), _MockSignalBridge())
    _window.show()
    sys.exit(_app.exec_())