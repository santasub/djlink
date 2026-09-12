import logging
import sys # Moved to be among the first imports
from qtpy.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QGridLayout, QFrame, QSizePolicy, QDialog,
                             QGroupBox, QRadioButton, QDialogButtonBox, QSlider,
                             QMessageBox, QDoubleSpinBox) # Added QDoubleSpinBox and QMessageBox
from qtpy.QtCore import Qt, Signal, QTimer

# MIDI Clock imports
from prodj.midi.midiclock_rtmidi import MidiClock as RtMidiClock, list_ports as rtmidi_list_ports
AlsaMidiClock = None
if sys.platform.startswith('linux'): # Now sys is defined
    try:
        from prodj.midi.midiclock_alsaseq import MidiClock as AlsaMidiClock
    except ImportError:
        logging.warning("AlsaMidiClock not available on this Linux system (alsaseq library missing). Falling back to rtmidi.")
        AlsaMidiClock = None # Explicitly set to None if import fails

import time # For Tap Tempo (sys import was here, now removed as it's at top)

MAX_TAPS_FOR_AVG = 4
TAP_TIMEOUT_SECONDS = 2.0

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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        # Player number + status badge
        top_row = QHBoxLayout()
        self.player_label = QLabel(f"Player {self.player_number}")
        font = self.player_label.font()
        font.setBold(True)
        font.setPointSize(11)
        self.player_label.setFont(font)
        top_row.addWidget(self.player_label)
        top_row.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("font-size:8pt; color:#6b7280;")
        top_row.addWidget(self.status_label)
        layout.addLayout(top_row)

        # Big BPM
        self.bpm_label = QLabel("--.--")
        bpm_font = self.bpm_label.font()
        bpm_font.setPointSize(22)
        bpm_font.setBold(True)
        self.bpm_label.setFont(bpm_font)
        self.bpm_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.bpm_label)

        # Delay + select button
        bot_row = QHBoxLayout()
        self.delay_label = QLabel("--.-- ms")
        self.delay_label.setStyleSheet("font-size:8pt; color:#9ca3af;")
        bot_row.addWidget(self.delay_label)
        bot_row.addStretch()
        self.action_button = QPushButton("Select")
        self.action_button.setFixedSize(80, 32)
        self.action_button.clicked.connect(self.handle_action_clicked)
        bot_row.addWidget(self.action_button)
        layout.addLayout(bot_row)

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
            self.is_dropped = False
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
        self.status_label.setStyleSheet("font-size:8pt; color:#10b981;"
                                        if status_parts else "font-size:8pt; color:#6b7280;")

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
        self.selected_player_source = None # Player number of the selected source
        self.coasting_bpm = None # Stores the BPM value when coasting
        self.last_known_good_bpm = 120.0 # Default if no BPM ever received

        self.manual_bpm_mode_active = False
        self.manual_bpm_value = 120.0
        self.tap_timestamps = []
        self.precision_pitch_offset = 0.0
        self.last_prodj_beat_time = None

        # Auto phase correction state
        self.auto_phase_correction_enabled = True
        self.phase_error_ms = 0.0          # last measured phase error in ms
        self.phase_correction_strength = 0.4  # 0.0-1.0, how aggressively we correct
        self.phase_error_history = []      # rolling history for smoothing
        self.PHASE_HISTORY_LEN = 4

        self.midi_clock_instance = None # Will hold AlsaMidiClock or RtMidiClock instance
        self.preferred_midi_backend = None # "ALSA" or "rtmidi"
        self.MidiClockImpl = None # Actual class to use

        self.setWindowTitle("ProDJ Link MIDI Clock")
        self._init_ui()
        self._connect_signals()

        self.populate_midi_ports() # Populate MIDI ports after UI is created
        self.update_player_display() # Initial population
        self.update_global_status_label() # Initial status

    def beat_received(self):
        # This is called from MIDI clock thread, so we need to use a signal
        # to communicate with the GUI thread
        self.signal_bridge.beat_signal.emit()

    def _on_beat_signal(self):
        # This runs in the GUI thread - triggered by MIDI clock output tick
            self.midi_led.setStyleSheet("""
            background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                fx:0.5, fy:0.5, stop:0 #10b981, stop:1 #059669);
            border: 3px solid #10b981;
            border-radius: 20px;
        """)
            QTimer.singleShot(100, lambda: self.midi_led.setStyleSheet("""
            background: #2d2d2d;
            border: 3px solid #4a4a4a;
            border-radius: 20px;
        """))

    def adjust_precision_pitch(self, direction):
        amount = self.pitch_amount_spinbox.value()
        self.precision_pitch_offset += amount * direction
        self.pitch_label.setText(f"Pitch: {self.precision_pitch_offset:+.1f} ms")
        self.update_midi_clock_source_logic()
    
    def reset_precision_pitch(self):
        self.precision_pitch_offset = 0.0
        self.pitch_label.setText("Pitch: 0.0 ms")
        self.update_midi_clock_source_logic()

    def _on_source_radio_changed(self):
        """Called when the Clock Source radio buttons change."""
        if self.source_master_radio.isChecked():
            self.source_player_combo.setEnabled(False)
            self.selected_player_source = None
            logging.info("Source: Follow Network Master")
        else:
            self.source_player_combo.setEnabled(True)
            player_num = int(self.source_player_combo.currentText())
            self.selected_player_source = player_num
            logging.info(f"Source: Locked to Player {player_num}")
        self.phase_error_history.clear()
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
            logging.info("Auto phase correction enabled.")
        else:
            self.auto_sync_button.setText("Auto Sync: OFF")
            self.phase_error_ms = 0.0
            self._update_phase_error_display()
            logging.info("Auto phase correction disabled.")

    def _update_phase_error_display(self):
        """Update the phase error label and lock LED colour based on current phase_error_ms."""
        err = self.phase_error_ms
        self.phase_error_label.setText(f"{err:+.1f} ms")

        abs_err = abs(err)
        if abs_err < 1.0:       # tight lock — green
            color = "#10b981"
            border = "#059669"
            text_color = "#10b981"
        elif abs_err < 5.0:     # slight drift — yellow
            color = "#f59e0b"
            border = "#d97706"
            text_color = "#f59e0b"
        else:                   # large error — red
            color = "#ef4444"
            border = "#dc2626"
            text_color = "#ef4444"

        self.phase_lock_led.setStyleSheet(
            f"background:{color};border:2px solid {border};border-radius:12px;"
        )
        self.phase_error_label.setStyleSheet(f"color:{text_color};")

    def nudge(self, ms):
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            if hasattr(self.midi_clock_instance, "adjust_phase"):
                self.midi_clock_instance.adjust_phase(ms)
            else:
                logging.warning("MIDI backend does not support phase adjustment (nudge).")

    def sync_to_grid(self):
        # We want to align the NEXT MIDI tick to a ProDJ beat boundary.
        # This is a bit complex in a distributed system, but a manual "adjust_phase"
        # of the current offset between MIDI beat and ProDJ beat is a good start.
        # For now, let's keep it simple: just nudge to align with the *last* known ProDJ beat.
        if self.last_prodj_beat_time is None:
            QMessageBox.warning(self, "Sync Error", "No ProDJ Link beat received yet. Play a track first.")
            return
        
        # Calculate time since last beat
        elapsed_since_beat = time.time() - self.last_prodj_beat_time
        # We want the next MIDI 'i % 24' to happen exactly at beat transitions.
        # This implementation will be refined, but let's start with a basic phase shift.
        # For a manual sync button, we'll just send a nudge of the current misalignment.
        # Actually, let's just use a large nudge or a special 'reset grid' command if available.
        # For now, we'll just allow the user to manual nudge.
        pass

    def _init_ui(self):
        # Target: 1280x720 landscape, reTerminal 5" IPS touchscreen
        # Row heights: 50 toolbar + 110 players + 530 controls + 30 status = 720
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ── Toolbar ~50px ──────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.midi_led = QFrame()
        self.midi_led.setFixedSize(34, 34)
        self.midi_led.setStyleSheet(
            "background:#2d2d2d;border:2px solid #4a4a4a;border-radius:17px;")
        toolbar.addWidget(self.midi_led)

        lbl_port = QLabel("MIDI Port:")
        lbl_port.setStyleSheet("color:#9ca3af;")
        toolbar.addWidget(lbl_port)
        self.midi_port_combo = QComboBox()
        self.midi_port_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        toolbar.addWidget(self.midi_port_combo, stretch=3)

        self.start_stop_button = QPushButton("Start")
        self.start_stop_button.setCheckable(True)
        self.start_stop_button.setFixedWidth(110)
        self.start_stop_button.clicked.connect(self.toggle_midi_clock_output)
        toolbar.addWidget(self.start_stop_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setFixedWidth(110)
        self.settings_button.clicked.connect(self.open_settings_dialog)
        toolbar.addWidget(self.settings_button)

        self.exit_button = QPushButton("Exit")
        self.exit_button.setFixedWidth(80)
        self.exit_button.setStyleSheet(
            "background:#7f1d1d;border:1px solid #991b1b;color:white;")
        self.exit_button.clicked.connect(self.close)
        toolbar.addWidget(self.exit_button)

        main_layout.addLayout(toolbar)

        # ── Player strip ~110px (4 tiles side by side) ──────────────────────
        self.player_grid_layout = QGridLayout()
        self.player_grid_layout.setSpacing(6)
        self.player_grid_layout.setAlignment(Qt.AlignTop)
        # 4 equal columns for players
        for col in range(4):
            self.player_grid_layout.setColumnStretch(col, 1)
        main_layout.addLayout(self.player_grid_layout)

        # ── Controls area (stretches to fill remaining space) ────────────────
        controls_frame = QFrame()
        controls_frame.setFrameStyle(QFrame.NoFrame)
        controls_layout = QVBoxLayout(controls_frame)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)

        # ── 3-column control grid (fills remaining ~530px height) ─────────────
        ctrl_grid = QGridLayout()
        ctrl_grid.setSpacing(8)
        ctrl_grid.setColumnStretch(0, 1)
        ctrl_grid.setColumnStretch(1, 1)
        ctrl_grid.setColumnStretch(2, 1)

        # ── Col 0: Clock Source ────────────────────────────────────────────
        source_group = QGroupBox("Clock Source")
        source_layout = QVBoxLayout()
        source_layout.setContentsMargins(12, 8, 12, 10)
        source_layout.setSpacing(10)

        self.source_master_radio = QRadioButton("Follow Network Master")
        self.source_master_radio.setChecked(True)
        self.source_master_radio.toggled.connect(self._on_source_radio_changed)
        source_layout.addWidget(self.source_master_radio)

        player_row = QHBoxLayout()
        self.source_player_radio = QRadioButton("Lock to Player:")
        self.source_player_radio.toggled.connect(self._on_source_radio_changed)
        player_row.addWidget(self.source_player_radio)
        self.source_player_combo = QComboBox()
        self.source_player_combo.addItems(["1", "2", "3", "4"])
        self.source_player_combo.setEnabled(False)
        self.source_player_combo.setFixedWidth(80)
        self.source_player_combo.currentIndexChanged.connect(self._on_source_player_combo_changed)
        player_row.addWidget(self.source_player_combo)
        player_row.addStretch()
        source_layout.addLayout(player_row)

        source_layout.addStretch()
        self.active_source_label = QLabel("Waiting for CDJs...")
        self.active_source_label.setStyleSheet(
            "color:#6b7280; font-weight:bold; padding:6px;"
            "border:1px solid #374151; border-radius:6px;")
        self.active_source_label.setWordWrap(True)
        source_layout.addWidget(self.active_source_label)
        source_group.setLayout(source_layout)
        ctrl_grid.addWidget(source_group, 0, 0)

        # ── Col 1: Grid Alignment + BPM Control (stacked vertically) ─────────
        mid_layout = QVBoxLayout()
        mid_layout.setSpacing(8)

        phase_group = QGroupBox("Grid Alignment (Phase)")
        phase_layout = QVBoxLayout()
        phase_layout.setContentsMargins(12, 8, 12, 10)
        phase_layout.setSpacing(10)

        auto_row = QHBoxLayout()
        self.auto_sync_button = QPushButton("Auto Sync: ON")
        self.auto_sync_button.setCheckable(True)
        self.auto_sync_button.setChecked(True)
        self.auto_sync_button.setStyleSheet(
            "QPushButton:checked { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "stop:0 #065f46,stop:1 #047857); border: 2px solid #10b981; }"
        )
        self.auto_sync_button.clicked.connect(self.toggle_auto_sync)
        auto_row.addWidget(self.auto_sync_button)
        self.phase_lock_led = QFrame()
        self.phase_lock_led.setFixedSize(26, 26)
        self.phase_lock_led.setStyleSheet(
            "background:#374151;border:2px solid #4b5563;border-radius:13px;")
        auto_row.addWidget(self.phase_lock_led)
        self.phase_error_label = QLabel("±0.0 ms")
        self.phase_error_label.setStyleSheet("color:#6b7280; font-weight:bold; font-size:12pt;")
        auto_row.addWidget(self.phase_error_label)
        auto_row.addStretch()
        phase_layout.addLayout(auto_row)

        nudge_row = QHBoxLayout()
        nudge_row.setSpacing(6)
        self.nudge_minus_button = QPushButton("<< 5ms")
        self.nudge_minus_button.clicked.connect(lambda: self.nudge(-5.0))
        nudge_row.addWidget(self.nudge_minus_button)
        self.nudge_plus_button = QPushButton("5ms >>")
        self.nudge_plus_button.clicked.connect(lambda: self.nudge(5.0))
        nudge_row.addWidget(self.nudge_plus_button)
        self.sync_button = QPushButton("Force Sync")
        self.sync_button.setStyleSheet("border: 1px solid #3b82f6;")
        self.sync_button.clicked.connect(self.sync_to_grid)
        nudge_row.addWidget(self.sync_button)
        phase_layout.addLayout(nudge_row)
        phase_group.setLayout(phase_layout)
        mid_layout.addWidget(phase_group)

        manual_group = QGroupBox("BPM Control")
        manual_layout = QVBoxLayout()
        manual_layout.setContentsMargins(12, 8, 12, 10)
        manual_layout.setSpacing(10)

        manual_top = QHBoxLayout()
        manual_top.setSpacing(8)
        self.manual_mode_button = QPushButton("Manual BPM")
        self.manual_mode_button.setCheckable(True)
        self.manual_mode_button.clicked.connect(self.toggle_manual_bpm_mode)
        manual_top.addWidget(self.manual_mode_button)
        self.tap_tempo_button = QPushButton("Tap Tempo")
        self.tap_tempo_button.clicked.connect(self.handle_tap_tempo_clicked)
        self.tap_tempo_button.setEnabled(False)
        manual_top.addWidget(self.tap_tempo_button)
        manual_layout.addLayout(manual_top)

        manual_bottom = QHBoxLayout()
        self.manual_bpm_slider = QSlider(Qt.Horizontal)
        self.manual_bpm_slider.setRange(300, 3000)
        self.manual_bpm_slider.setValue(1200)
        self.manual_bpm_slider.valueChanged.connect(self.manual_bpm_slider_changed)
        self.manual_bpm_slider.setEnabled(False)
        manual_bottom.addWidget(self.manual_bpm_slider)
        self.manual_bpm_label = QLabel("120.0")
        self.manual_bpm_label.setFixedWidth(65)
        self.manual_bpm_label.setAlignment(Qt.AlignCenter)
        self.manual_bpm_label.setEnabled(False)
        manual_bottom.addWidget(self.manual_bpm_label)
        manual_layout.addLayout(manual_bottom)
        manual_group.setLayout(manual_layout)
        mid_layout.addWidget(manual_group)

        ctrl_grid.addLayout(mid_layout, 0, 1)

        # ── Col 2: Precision Pitch ────────────────────────────────────────
        pitch_group = QGroupBox("Precision Pitch (Speed)")
        pitch_layout = QVBoxLayout()
        pitch_layout.setContentsMargins(12, 8, 12, 10)
        pitch_layout.setSpacing(12)

        self.pitch_label = QLabel("+0.0 ms")
        pitch_label_font = self.pitch_label.font()
        pitch_label_font.setBold(True)
        pitch_label_font.setPointSize(28)
        self.pitch_label.setFont(pitch_label_font)
        self.pitch_label.setStyleSheet("color:#0ea5e9;")
        self.pitch_label.setAlignment(Qt.AlignCenter)
        pitch_layout.addWidget(self.pitch_label)

        pitch_btn_row = QHBoxLayout()
        pitch_btn_row.setSpacing(8)
        self.pitch_down_button = QPushButton("-")
        self.pitch_down_button.setMinimumHeight(60)
        self.pitch_down_button.clicked.connect(lambda: self.adjust_precision_pitch(-1))
        pitch_btn_row.addWidget(self.pitch_down_button)
        self.pitch_up_button = QPushButton("+")
        self.pitch_up_button.setMinimumHeight(60)
        self.pitch_up_button.clicked.connect(lambda: self.adjust_precision_pitch(1))
        pitch_btn_row.addWidget(self.pitch_up_button)
        pitch_layout.addLayout(pitch_btn_row)

        reset_row = QHBoxLayout()
        reset_button = QPushButton("Reset")
        reset_button.clicked.connect(self.reset_precision_pitch)
        reset_row.addWidget(reset_button)
        lbl_step = QLabel("Step:")
        lbl_step.setStyleSheet("color:#9ca3af;")
        reset_row.addWidget(lbl_step)
        self.pitch_amount_spinbox = QDoubleSpinBox()
        self.pitch_amount_spinbox.setRange(0.1, 10.0)
        self.pitch_amount_spinbox.setSingleStep(0.1)
        self.pitch_amount_spinbox.setSuffix(" ms")
        self.pitch_amount_spinbox.setValue(1.0)
        reset_row.addWidget(self.pitch_amount_spinbox)
        pitch_layout.addLayout(reset_row)

        pitch_layout.addStretch()
        pitch_group.setLayout(pitch_layout)
        ctrl_grid.addWidget(pitch_group, 0, 2)

        controls_layout.addLayout(ctrl_grid, stretch=1)

        # ── Status bar ~28px ────────────────────────────────────────────
        self.global_status_label = QLabel("MIDI Clock: Stopped")
        self.global_status_label.setWordWrap(False)
        self.global_status_label.setStyleSheet("color:#6b7280; padding:2px 4px;")
        controls_layout.addWidget(self.global_status_label)

        main_layout.addWidget(controls_frame, stretch=1)
        self.setFixedSize(1280, 720)


    def _connect_signals(self):
        self.signal_bridge.client_change_signal.connect(self.handle_client_or_master_change)
        self.signal_bridge.beat_signal.connect(self._on_beat_signal)
        self.signal_bridge.prodj_beat_signal.connect(self.handle_prodj_beat)
        self.signal_bridge.prodj_beat_timing_signal.connect(self.handle_prodj_beat_timing)

    def handle_client_or_master_change(self, player_number_changed=None):
        self.update_player_display()
        self.update_midi_clock_source_logic()
        self._update_active_source_label()

    def update_player_display(self):
        logging.debug("Updating player display in MidiClockMainWindow")
        active_player_numbers = {client.player_number for client in self.prodj.cl.clients if client.type == "cdj"}

        # Update existing tiles and mark dropped ones
        for player_num, tile in list(self.player_tiles.items()): # Iterate over a copy for safe removal/modification
            if player_num not in active_player_numbers:
                if not tile.is_dropped: # Mark as dropped if not already
                    tile.set_dropped_status(True)
                    logging.info(f"Player {player_num} marked as dropped.")
                # Don't remove the tile immediately, keep it to show "Network Drop"
            else: # Player is active
                if tile.is_dropped: # Was dropped, now it's active again
                    tile.set_dropped_status(False)
                    logging.info(f"Player {player_num} reconnected.")
                    # User needs to click to re-select if it was the source

        # Add new tiles for newly discovered players and update layout
        row, col = 0, 0
        # Sort by player number for consistent layout
        sorted_clients = sorted([c for c in self.prodj.cl.clients if c.type == "cdj"], key=lambda c: c.player_number)

        for client in sorted_clients:
            if client.player_number not in self.player_tiles:
                tile = PlayerTileWidget(client.player_number)
                tile.selected_signal.connect(self.handle_player_tile_selected)
                self.player_tiles[client.player_number] = tile
                # Add to layout, ensuring it's not added multiple times if update_player_display is rapid
                current_item = self.player_grid_layout.itemAtPosition(row, col)
                if current_item is None or current_item.widget() != tile :
                    if current_item is not None : # if something else is there, remove it
                        old_widget = current_item.widget()
                        self.player_grid_layout.removeWidget(old_widget)
                        old_widget.deleteLater()
                    self.player_grid_layout.addWidget(tile, row, col)
            else:
                tile = self.player_tiles[client.player_number]
                # Ensure it's in the correct grid position if layout changes or widgets are reordered
                # This is a bit complex; simpler to rebuild if order changes drastically.
                # For now, assume if it exists, it's in a reasonable place or will be repositioned by this loop.
                # If tile is not parented to this grid layout, or at wrong pos, re-add
                if tile.parentWidget() != self or self.player_grid_layout.indexOf(tile) == -1:
                     self.player_grid_layout.addWidget(tile, row, col)
                elif self.player_grid_layout.getItemPosition(self.player_grid_layout.indexOf(tile)) != (row,col) :
                     # It is in the layout but wrong place, remove and re-add
                     self.player_grid_layout.removeWidget(tile)
                     self.player_grid_layout.addWidget(tile, row, col)


            is_master = "master" in client.state
            is_selected = (self.selected_player_source == client.player_number)

            delay_value = 0.0
            effective_bpm_val = None
            if client.bpm is not None and client.actual_pitch is not None:
                 try:
                    # Ensure bpm is treated as float, especially if it could be string like "--.--"
                    bpm_float = float(client.bpm)
                    pitch_float = float(client.actual_pitch)
                    if bpm_float > 0:
                        effective_bpm_val = bpm_float * pitch_float
                        if effective_bpm_val > 0:
                            delay_value = 60.0 / effective_bpm_val / 24.0
                 except (TypeError, ValueError):
                    effective_bpm_val = None
                    delay_value = 0.0

            tile.update_data(
                bpm=effective_bpm_val,
                delay=delay_value,
                is_master=is_master
            )
            tile.set_selected_source(is_selected)
            if client.player_number in active_player_numbers and tile.is_dropped: # Ensure it's marked not dropped if active
                tile.set_dropped_status(False)


            col += 1
            if col >= 2: # Max 2 tiles per row
                col = 0
                row += 1

        # Clean up any tiles in player_grid_layout that are no longer in self.player_tiles
        # This can happen if a player is removed entirely.
        # Not strictly necessary if set_dropped_status handles visual cue for long-gone players.
        # For a cleaner grid, one might remove widgets not in self.player_tiles.keys()

        self.update_global_status_label()

    def handle_player_tile_selected(self, player_number):
        """Tile click now syncs the radio buttons to match the selection."""
        logging.info(f"Player tile {player_number} clicked.")
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

    def populate_midi_ports(self):
        self.midi_port_combo.clear()
        self._determine_midi_backend()

        if self.MidiClockImpl is None:
            self.midi_port_combo.addItem("No MIDI Backend!")
            self.midi_port_combo.setEnabled(False)
            self.start_stop_button.setEnabled(False)
            return

        try:
            ports = []  # list of (display_name, open_kwargs)
            if self.MidiClockImpl == AlsaMidiClock:
                # ALSA: use a temp instance only to read /proc - no WinMM handles
                tmp = AlsaMidiClock.__new__(AlsaMidiClock)
                tmp.__init__()  # safe: only opens alsaseq client
                for client_id, name, port_ids in tmp.iter_alsa_seq_clients():
                    for p_id in port_ids:
                        label = f"{name} ({client_id}:{p_id})"
                        ports.append((label, {'preferred_name': name, 'preferred_port': p_id}))
                del tmp
            elif self.MidiClockImpl == RtMidiClock:
                # Use the static helper - no MidiOut handle kept open
                for idx, name in enumerate(rtmidi_list_ports()):
                    ports.append((name, {'preferred_port': idx}))

            if ports:
                for label, kwargs in ports:
                    # store open-kwargs as UserRole data so toggle_midi_clock_output
                    # never has to parse the display string
                    self.midi_port_combo.addItem(label, userData=kwargs)
                self.midi_port_combo.setEnabled(True)
                self.start_stop_button.setEnabled(True)
            else:
                self.midi_port_combo.addItem("No MIDI Ports Found")
                self.midi_port_combo.setEnabled(False)
                self.start_stop_button.setEnabled(False)
        except Exception as e:
            logging.error(f"Error listing MIDI ports: {e}", exc_info=True)
            self.midi_port_combo.addItem("Error listing ports")
            self.midi_port_combo.setEnabled(False)
            self.start_stop_button.setEnabled(False)


    def toggle_midi_clock_output(self):
        if self.start_stop_button.isChecked(): # User wants to start
            if self.midi_clock_instance is not None and self.midi_clock_instance.is_alive():
                logging.warning("MIDI clock already running. Stopping first.")
                self.midi_clock_instance.stop()
                self.midi_clock_instance = None

            selected_port_full_name = self.midi_port_combo.currentText()
            if not selected_port_full_name or "No MIDI" in selected_port_full_name or "Error listing" in selected_port_full_name:
                logging.warning("No valid MIDI output port selected.")
                self.start_stop_button.setChecked(False) # Uncheck button
                return

            if self.MidiClockImpl is None:
                logging.error("No MIDI implementation available to start clock.")
                self.start_stop_button.setChecked(False)
                return

            self.midi_clock_instance = self.MidiClockImpl()

            # Retrieve the open-kwargs stored by populate_midi_ports
            open_kwargs = self.midi_port_combo.currentData() or {}
            logging.debug(f"Opening MIDI port '{selected_port_full_name}' with kwargs {open_kwargs}")

            try:
                self.midi_clock_instance.open(**open_kwargs)
                self.midi_clock_instance.set_beat_callback(self.beat_received)
                self.update_midi_clock_source_logic() # Set initial BPM
                if not self.midi_clock_instance.is_alive(): # Check if thread started (it should by .start())
                    self.midi_clock_instance.start()

                logging.info(f"Starting MIDI clock on port {selected_port_full_name}")
                self.start_stop_button.setText("Stop")
                self.midi_port_combo.setEnabled(False)
            except Exception as e:
                logging.error(f"Failed to start MIDI clock on {selected_port_full_name}: {e}", exc_info=True)
                self.midi_clock_instance = None
                self.start_stop_button.setChecked(False)
        else: # User wants to stop
            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.stop()
                logging.info("Stopping MIDI clock")
            self.midi_clock_instance = None
            self.start_stop_button.setText("Start")
            self.midi_port_combo.setEnabled(True)
            # reset phase display when clock stops
            self.phase_error_ms = 0.0
            self.phase_error_history.clear()
            self.phase_lock_led.setStyleSheet("background:#374151;border:2px solid #4b5563;border-radius:12px;")
            self.phase_error_label.setText("\u00b10.0 ms")
            self.phase_error_label.setStyleSheet("color:#6b7280;")
        self.update_global_status_label()

    def handle_prodj_beat(self, player_number, beat_number):
        # legacy: just track beat time, used by manual sync_to_grid
        active_source = self._get_active_source_player_number()
        if player_number == active_source:
            self.last_prodj_beat_time = time.time()

    def handle_prodj_beat_timing(self, player_number, beat_number, next_beat_ms):
        """Called on every beat packet from the CDJ with exact next_beat distance in ms.
        This is the heart of automatic phase correction."""
        if self.manual_bpm_mode_active:
            return
        if not self.auto_phase_correction_enabled:
            return
        if next_beat_ms is None:
            return  # status-packet beat, no distance info
        if not self.midi_clock_instance or not self.midi_clock_instance.is_alive():
            return

        active_source = self._get_active_source_player_number()
        if player_number != active_source:
            return

        # next_beat_ms is the time (in ms) until the CDJ's next beat.
        # Our MIDI clock delay per tick is self.midi_clock_instance.delay (in seconds).
        # One MIDI beat = 24 ticks.
        beat_period_ms = self.midi_clock_instance.delay * 24.0 * 1000.0
        if beat_period_ms <= 0:
            return

        # The CDJ just fired beat N.  next_beat_ms tells us how long until beat N+1.
        # We want our MIDI clock beat boundary to land at the same time.
        # Error: how far ahead/behind is next_beat_ms from one full beat period?
        # If next_beat_ms == beat_period_ms  → perfect alignment
        # If next_beat_ms <  beat_period_ms  → we're running SLOW  (MIDI next beat is too late)
        # If next_beat_ms >  beat_period_ms  → we're running FAST  (MIDI next beat is too early)
        raw_error_ms = next_beat_ms - beat_period_ms

        # Wrap to ±half a beat period so we always take the shortest path
        while raw_error_ms > beat_period_ms / 2:
            raw_error_ms -= beat_period_ms
        while raw_error_ms < -beat_period_ms / 2:
            raw_error_ms += beat_period_ms

        # Smooth over last N beats
        self.phase_error_history.append(raw_error_ms)
        if len(self.phase_error_history) > self.PHASE_HISTORY_LEN:
            self.phase_error_history.pop(0)
        smoothed_error_ms = sum(self.phase_error_history) / len(self.phase_error_history)

        # Apply a fraction of the error as correction
        correction_ms = smoothed_error_ms * self.phase_correction_strength

        self.phase_error_ms = smoothed_error_ms
        self.midi_clock_instance.adjust_phase(correction_ms)

        logging.debug(
            f"Phase correction: next_beat={next_beat_ms:.1f}ms, "
            f"beat_period={beat_period_ms:.1f}ms, error={smoothed_error_ms:.2f}ms, "
            f"correction={correction_ms:.2f}ms"
        )

        # Update phase error display in UI
        self._update_phase_error_display()

    def _get_active_source_player_number(self):
        """Returns the player number of the current BPM/phase source, or None."""
        if self.selected_player_source is not None:
            tile = self.player_tiles.get(self.selected_player_source)
            if tile and not tile.is_dropped:
                return self.selected_player_source
        # Fall back to network master
        for client in self.prodj.cl.clients:
            if client.type == "cdj" and "master" in client.state:
                tile = self.player_tiles.get(client.player_number)
                if tile is None or not tile.is_dropped:
                    return client.player_number
        return None

    def sync_to_grid(self):
        if self.last_prodj_beat_time is None:
            QMessageBox.warning(self, "Sync Error", "No ProDJ Link beat received yet. Play a track first.")
            return

        if not self.midi_clock_instance or not self.midi_clock_instance.is_alive():
            return

        # Simple approach: How long ago was the last beat?
        # We want to shift the MIDI phase so that Tick 0 aligns with that beat.
        elapsed_since_beat = (time.time() - self.last_prodj_beat_time) * 1000.0 # ms
        
        # We need the current BPM to know the beat period
        # This is stored in self.midi_clock_instance.delay (but converted to ms)
        delay_ms = self.midi_clock_instance.delay * 1000.0 # Time per MIDI tick
        beat_period_ms = delay_ms * 24.0
        
        # Misalignment is elapsed_since_beat modulo beat_period
        misalignment = elapsed_since_beat % beat_period_ms
        
        # We want to nudge the MIDI clock SOONER by 'misalignment' ms 
        # or LATER by 'beat_period - misalignment' ms.
        # Let's nudge by the smaller amount for faster sync.
        if misalignment > beat_period_ms / 2:
            nudge_amount = beat_period_ms - misalignment # Nudge forward (negative phase shift)
            self.nudge(-nudge_amount)
        else:
            nudge_amount = -misalignment # Nudge backward (positive phase shift)
            self.nudge(nudge_amount)

        logging.info(f"Sync Grid: Misalignment was {misalignment:.2f}ms. Nudging by {nudge_amount:.2f}ms")

    def update_midi_clock_source_logic(self):
        if self.manual_bpm_mode_active:
            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.setBpm(self.manual_bpm_value, self.precision_pitch_offset)
            self.update_global_status_label()
            return

        source_player = None
        source_player_description = "None"
        final_bpm_to_set = None
        is_coasting = False

        if self.selected_player_source is not None:
            source_player = self.prodj.cl.getClient(self.selected_player_source)
            if source_player is not None and not self.player_tiles[source_player.player_number].is_dropped: # Check if not dropped
                if source_player.bpm is not None and source_player.actual_pitch is not None:
                    try:
                        current_bpm = float(source_player.bpm)
                        current_pitch = float(source_player.actual_pitch)
                        if current_bpm > 0:
                            final_bpm_to_set = current_bpm * current_pitch
                            self.last_known_good_bpm = final_bpm_to_set
                            self.coasting_bpm = None
                            source_player_description = f"Player {source_player.player_number} (Selected)"
                    except (TypeError, ValueError):
                        logging.warning(f"Invalid BPM/pitch for selected player {source_player.player_number}")
                if final_bpm_to_set is None:
                    logging.warning(f"Selected Player {source_player.player_number} has no valid BPM currently.")
            else: # Selected player has disappeared or is marked dropped
                if source_player is None: # Truly gone from client list
                    logging.warning(f"Previously selected player {self.selected_player_source} no longer exists.")
                # If tile is marked dropped, source_player might still be the client object but tile.is_dropped is true
                # We fall through to master/coasting.
                # The selected_player_source attribute remains, allowing "reconnect" by user re-selecting tile.
                pass

        if final_bpm_to_set is None:
            network_master_player = None
            for client in self.prodj.cl.clients:
                if client.type == "cdj" and "master" in client.state and \
                   (client.player_number not in self.player_tiles or not self.player_tiles[client.player_number].is_dropped) : # Ensure master is not dropped
                    network_master_player = client
                    break

            if network_master_player:
                if network_master_player.bpm is not None and network_master_player.actual_pitch is not None:
                    try:
                        current_bpm = float(network_master_player.bpm)
                        current_pitch = float(network_master_player.actual_pitch)
                        if current_bpm > 0:
                            final_bpm_to_set = current_bpm * current_pitch
                            self.last_known_good_bpm = final_bpm_to_set
                            self.coasting_bpm = None
                            source_player_description = f"Player {network_master_player.player_number} (Network Master)"
                    except (TypeError, ValueError):
                        logging.warning(f"Invalid BPM/pitch for network master {network_master_player.player_number}")
                if final_bpm_to_set is None:
                     logging.warning(f"Network Master Player {network_master_player.player_number} has no valid BPM currently.")
            else:
                logging.info("No specific source and no (active) network master found.")

        if final_bpm_to_set is None:
            if self.last_known_good_bpm is not None:
                final_bpm_to_set = self.last_known_good_bpm
                self.coasting_bpm = final_bpm_to_set
                source_player_description = f"Coasting @ {final_bpm_to_set:.2f} BPM (Last Known)"
                is_coasting = True
                logging.info(f"No active BPM source. Coasting at {final_bpm_to_set:.2f} BPM.")
            else:
                final_bpm_to_set = 120.0
                self.coasting_bpm = final_bpm_to_set
                source_player_description = f"Coasting @ {final_bpm_to_set:.2f} BPM (Default)"
                is_coasting = True
                logging.warning("No BPM source and no last known good BPM. Defaulting to 120 BPM for coasting.")

        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            if final_bpm_to_set is not None and final_bpm_to_set > 0:
                self.midi_clock_instance.setBpm(final_bpm_to_set, self.precision_pitch_offset)
            else:
                logging.error("Attempting to set invalid BPM (None or <=0). Defaulting to 120.")
                self.midi_clock_instance.setBpm(120, self.precision_pitch_offset)

        self.update_global_status_label()


    def toggle_manual_bpm_mode(self):
        self.manual_bpm_mode_active = self.manual_mode_button.isChecked()
        self.manual_bpm_slider.setEnabled(self.manual_bpm_mode_active)
        self.manual_bpm_label.setEnabled(self.manual_bpm_mode_active)
        self.tap_tempo_button.setEnabled(self.manual_bpm_mode_active)

        if self.manual_bpm_mode_active:
            self.manual_mode_button.setText("Switch to Auto BPM")
            current_effective_bpm = self.coasting_bpm if self.coasting_bpm is not None else self.last_known_good_bpm
            if current_effective_bpm is None: current_effective_bpm = 120.0

            self.manual_bpm_value = current_effective_bpm
            self.manual_bpm_slider.setValue(int(self.manual_bpm_value * 10))
            self.manual_bpm_label.setText(f"{self.manual_bpm_value:.1f} BPM")
            self.tap_timestamps = []

            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.setBpm(self.manual_bpm_value, self.precision_pitch_offset)
            logging.info(f"Manual BPM mode enabled. Set to {self.manual_bpm_value:.1f} BPM.")
        else:
            self.manual_mode_button.setText("Enable Manual BPM")
            self.tap_timestamps = []
            logging.info("Manual BPM mode disabled. Reverting to automatic source.")
            self.update_midi_clock_source_logic()
        self.update_global_status_label()

    def manual_bpm_slider_changed(self, value):
        new_bpm = value / 10.0
        self.manual_bpm_label.setText(f"{new_bpm:.1f} BPM")
        self.tap_timestamps = []
        # Auto-activate manual mode when user moves the slider.
        # Block signals on the slider while activating so toggle_manual_bpm_mode
        # cannot reset the slider value and overwrite what the user just set.
        if not self.manual_bpm_mode_active:
            self.manual_bpm_slider.blockSignals(True)
            self.manual_mode_button.setChecked(True)
            self.toggle_manual_bpm_mode()   # may call slider.setValue internally
            self.manual_bpm_slider.blockSignals(False)
        # Now apply the user's intended value
        self.manual_bpm_value = new_bpm
        self.manual_bpm_label.setText(f"{new_bpm:.1f} BPM")
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            self.midi_clock_instance.setBpm(self.manual_bpm_value, self.precision_pitch_offset)
            self.update_global_status_label()

    def handle_tap_tempo_clicked(self):
        if not self.manual_bpm_mode_active:
            self.manual_mode_button.setChecked(True)

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
            tapped_bpm = max(30.0, min(300.0, tapped_bpm))

            self.manual_bpm_value = tapped_bpm
            self.manual_bpm_slider.setValue(int(self.manual_bpm_value * 10))
            self.manual_bpm_label.setText(f"{self.manual_bpm_value:.1f} BPM")

            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.setBpm(self.manual_bpm_value, self.precision_pitch_offset)
            logging.info(f"Tapped BPM: {self.manual_bpm_value:.2f} (avg over {len(intervals)} intervals)")
            self.update_global_status_label()
        else:
            logging.debug("Average interval is zero, cannot calculate BPM.")

    def update_global_status_label(self):
        self._update_active_source_label()
        source_desc = "None"
        current_bpm_val = None
        is_coasting_val = self.coasting_bpm is not None and not self.manual_bpm_mode_active

        if self.manual_bpm_mode_active:
            source_desc = f"Manual @ {self.manual_bpm_value:.1f} BPM"
            current_bpm_val = self.manual_bpm_value
        elif self.selected_player_source is not None:
            client = self.prodj.cl.getClient(self.selected_player_source)
            if client and (client.player_number not in self.player_tiles or not self.player_tiles[client.player_number].is_dropped) : # Check if not dropped
                source_desc = f"Player {client.player_number} (Selected)"
                if client.bpm and client.actual_pitch:
                    try:
                        current_bpm_val = float(client.bpm) * float(client.actual_pitch)
                    except (TypeError, ValueError):
                        current_bpm_val = None
        elif not is_coasting_val:
            for client in self.prodj.cl.clients:
                if client.type == "cdj" and "master" in client.state and \
                   (client.player_number not in self.player_tiles or not self.player_tiles[client.player_number].is_dropped):
                    source_desc = f"Player {client.player_number} (Network Master)"
                    if client.bpm and client.actual_pitch:
                        try:
                            current_bpm_val = float(client.bpm) * float(client.actual_pitch)
                        except (TypeError, ValueError):
                            current_bpm_val = None
                    break

        if is_coasting_val:
            source_desc = f"Coasting @ {self.coasting_bpm:.1f} BPM (Last Known)"
            current_bpm_val = self.coasting_bpm

        if current_bpm_val is None and not self.manual_bpm_mode_active:
             current_bpm_val = self.last_known_good_bpm if self.last_known_good_bpm else 120.0
             if not is_coasting_val and source_desc == "None":
                 source_desc = f"Default @ {current_bpm_val:.1f} BPM"

        status_text = "MIDI Clock: "
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            status_text += f"Running on {self.midi_port_combo.currentText()}"
            status_text += f" | Source: {source_desc}"
            if not self.manual_bpm_mode_active and not is_coasting_val and \
               current_bpm_val and isinstance(current_bpm_val, (int, float)) and \
               source_desc.startswith("Player"):
                 status_text += f" @ {current_bpm_val:.2f} BPM"
        else:
            status_text += "Stopped"
            self.coasting_bpm = None # Clear coasting BPM when clock is stopped

        self.global_status_label.setText(status_text)

    def closeEvent(self, event):
        # Ensure MIDI clock is stopped if running
        if self.midi_clock_instance and self.midi_clock_instance.is_alive(): # Assuming is_alive
           self.midi_clock_instance.stop()
        super().closeEvent(event)

    def open_settings_dialog(self):
        dialog = MidiClockSettingsDialog(self)
        if not dialog.has_configurable_settings():
            QMessageBox.information(self, "Settings", "No specific settings currently available for your platform.")
            return

        if dialog.exec_(): # Modal execution
            new_preferred_backend = dialog.get_selected_backend()
            if self.preferred_midi_backend != new_preferred_backend:
                self.preferred_midi_backend = new_preferred_backend
                logging.info(f"Settings updated. Preferred MIDI backend: {self.preferred_midi_backend}")

                if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                    logging.info("Stopping MIDI clock due to backend change.")
                    self.midi_clock_instance.stop()
                    self.midi_clock_instance = None
                    self.start_stop_button.setChecked(False) # Ensure button state is reset
                    self.start_stop_button.setText("Start MIDI Clock")
                    self.midi_port_combo.setEnabled(True) # Re-enable port selection

                self.populate_midi_ports() # This will use the new preference
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
    from PyQt5.QtWidgets import QApplication
    from unittest.mock import Mock # For MockProDj

    logging.basicConfig(level=logging.DEBUG, format='%(levelname)-7s %(module)s: %(message)s')

    class MockProDj:
        class MockClient:
            def __init__(self, num, master=False, bpm=120.0, pitch=1.0):
                self.player_number = num
                self.model = "CDJ-MOCK"
                self.type = "cdj"
                self.bpm = bpm
                self.actual_pitch = pitch
                self.state = ["master"] if master else []
                self.fw = "1.00"

        def __init__(self):
            self.cl = Mock()
            self.cl.clients = [self.MockClient(1, master=True, bpm=125.0), self.MockClient(2, bpm=130.0)]
            self.cl.getClient = self._get_client # Assign method directly

        def _get_client(self, player_number):
            for client_obj in self.cl.clients:
                if client_obj.player_number == player_number:
                    return client_obj
            return None

        def set_client_change_callback(self, cb): pass # Mock
        def start(self): pass # Mock
        def vcdj_set_player_number(self, num): pass # Mock
        def vcdj_enable(self): pass # Mock
        def stop(self): pass # Mock


    class MockSignalBridge(QObject):
        client_change_signal = pyqtSignal(int)
        master_change_signal = pyqtSignal(int)

    app = QApplication(sys.argv)
    mock_prodj_instance = MockProDj()
    mock_bridge_instance = MockSignalBridge()

    window = MidiClockMainWindow(mock_prodj_instance, mock_bridge_instance)
    window.show()
    sys.exit(app.exec_())
