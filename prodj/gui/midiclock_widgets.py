import logging
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QGridLayout, QFrame, QSizePolicy)
from PyQt5.QtCore import Qt, pyqtSignal

# MIDI Clock imports
from prodj.midi.midiclock_rtmidi import MidiClock as RtMidiClock
AlsaMidiClock = None
if sys.platform.startswith('linux'):
    try:
        from prodj.midi.midiclock_alsaseq import MidiClock as AlsaMidiClock
    except ImportError:
        logging.warning("AlsaMidiClock not available on this Linux system (alsaseq library missing). Falling back to rtmidi.")
        AlsaMidiClock = None # Explicitly set to None if import fails

import sys # For sys.platform check

class PlayerTileWidget(QFrame):
    """
    A widget to display information for a single player and allow selection.
    """
    selected_signal = pyqtSignal(int) # Emits player number when selected

    def __init__(self, player_number, parent=None):
        super().__init__(parent)
        self.player_number = player_number
        self.is_master = False
        self.is_selected_source = False

        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)

        self.player_label = QLabel(f"Player {self.player_number}")
        self.player_label.setAlignment(Qt.AlignCenter)
        font = self.player_label.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 2)
        self.player_label.setFont(font)
        layout.addWidget(self.player_label)

        self.bpm_label = QLabel("BPM: --.--")
        layout.addWidget(self.bpm_label)
        self.delay_label = QLabel("Delay: --.-- ms")
        layout.addWidget(self.delay_label)
        self.status_label = QLabel("Status: Normal") # e.g., Master, Selected, Dropped
        layout.addWidget(self.status_label)

        self.select_button = QPushButton("Select as Source")
        self.select_button.setCheckable(True)
        self.select_button.clicked.connect(self.handle_select_clicked)
        layout.addWidget(self.select_button)

        self.update_style()

    def handle_select_clicked(self):
        self.selected_signal.emit(self.player_number)

    def set_selected_source(self, is_selected):
        self.is_selected_source = is_selected
        self.select_button.setChecked(is_selected)
        self.select_button.setEnabled(not is_selected) # Disable if already selected
        self.update_style()

    def update_data(self, bpm, delay, is_master):
        self.bpm_label.setText(f"BPM: {bpm:.2f}" if isinstance(bpm, (float, int)) else "BPM: --.--")
        self.delay_label.setText(f"Delay: {delay * 1000:.2f} ms" if isinstance(delay, (float, int)) else "Delay: --.-- ms")
        self.is_master = is_master
        self.update_style()

    def set_dropped(self, is_dropped):
        if is_dropped:
            self.status_label.setText("Status: Network Drop")
            self.setEnabled(False) # Disable tile if player dropped
        else:
            self.setEnabled(True)
            self.update_style() # Re-evaluates status based on master/selected

    def update_style(self):
        status_parts = []
        if self.is_master:
            status_parts.append("Master")
        if self.is_selected_source:
            status_parts.append("Selected Source")

        if not status_parts:
            self.status_label.setText("Status: Normal")
        else:
            self.status_label.setText(f"Status: {', '.join(status_parts)}")

        if self.is_selected_source:
            self.setStyleSheet("PlayerTileWidget { border: 2px solid green; }")
        elif self.is_master:
            self.setStyleSheet("PlayerTileWidget { border: 2px solid blue; }")
        else:
            self.setStyleSheet("PlayerTileWidget { border: 1px solid gray; }")


class MidiClockMainWindow(QWidget):
    def __init__(self, prodj_instance, signal_bridge, parent=None):
        super().__init__(parent)
        self.prodj = prodj_instance
        self.signal_bridge = signal_bridge
        self.player_tiles = {} # player_number: PlayerTileWidget
        self.selected_player_source = None # Player number of the selected source

        self.midi_clock_instance = None # Will hold AlsaMidiClock or RtMidiClock instance
        self.preferred_midi_backend = None # "ALSA" or "rtmidi"
        self.MidiClockImpl = None # Actual class to use

        self.setWindowTitle("ProDJ Link MIDI Clock")
        self._init_ui()
        self._connect_signals()

        self.populate_midi_ports() # Populate MIDI ports after UI is created
        self.update_player_display() # Initial population
        self.update_global_status_label() # Initial status

    def _init_ui(self):
        main_layout = QVBoxLayout(self)

        # --- Player Tiles Area ---
        self.player_grid_layout = QGridLayout()
        self.player_grid_layout.setAlignment(Qt.AlignTop)
        main_layout.addLayout(self.player_grid_layout)

        # --- Global Controls Area ---
        controls_frame = QFrame()
        controls_frame.setFrameStyle(QFrame.StyledPanel)
        controls_layout = QHBoxLayout(controls_frame)

        self.midi_port_combo = QComboBox()
        # self.populate_midi_ports() # To be implemented
        controls_layout.addWidget(QLabel("MIDI Output Port:"))
        controls_layout.addWidget(self.midi_port_combo)

        self.start_stop_button = QPushButton("Start MIDI Clock")
        self.start_stop_button.setCheckable(True)
        self.start_stop_button.clicked.connect(self.toggle_midi_clock_output)
        controls_layout.addWidget(self.start_stop_button)

        self.global_status_label = QLabel("MIDI Clock: Stopped | Source: None")
        controls_layout.addWidget(self.global_status_label)
        controls_layout.addStretch()

        main_layout.addWidget(controls_frame)
        self.setMinimumSize(600, 300)


    def _connect_signals(self):
        self.signal_bridge.client_change_signal.connect(self.handle_client_or_master_change)
        # self.signal_bridge.master_change_signal.connect(self.handle_client_or_master_change) # Can simplify if client_change covers master status

    def handle_client_or_master_change(self, player_number_changed=None):
        # This slot is called when any client changes or master status might have changed.
        # We need to refresh all player tiles and potentially the selected source.
        self.update_player_display()
        # self.update_midi_clock_source_logic() # To be implemented

    def update_player_display(self):
        logging.debug("Updating player display in MidiClockMainWindow")
        active_player_numbers = {client.player_number for client in self.prodj.cl.clients if client.type == "cdj"}

        # Remove tiles for players no longer active
        for player_num in list(self.player_tiles.keys()):
            if player_num not in active_player_numbers:
                tile = self.player_tiles.pop(player_num)
                self.player_grid_layout.removeWidget(tile)
                tile.deleteLater()
                if self.selected_player_source == player_num:
                    self.selected_player_source = None # Handle this change

        # Add/Update tiles for active players
        # Simple grid layout for now, max 2 per row
        row, col = 0, 0
        for client in sorted(self.prodj.cl.clients, key=lambda c: c.player_number):
            if client.type != "cdj": # Only show CDJs
                continue

            if client.player_number not in self.player_tiles:
                tile = PlayerTileWidget(client.player_number)
                tile.selected_signal.connect(self.handle_player_tile_selected)
                self.player_tiles[client.player_number] = tile
                self.player_grid_layout.addWidget(tile, row, col)
            else:
                tile = self.player_tiles[client.player_number]

            is_master = "master" in client.state
            is_selected = (self.selected_player_source == client.player_number)

            # Calculate delay (placeholder, actual calculation needed)
            # This assumes midiclock.py's logic for setBpm and delay calculation
            # will be available or replicated.
            delay_value = 0.0
            if client.bpm is not None and client.actual_pitch is not None and client.bpm > 0:
                 try:
                    effective_bpm = client.bpm * client.actual_pitch
                    if effective_bpm > 0:
                        delay_value = 60.0 / effective_bpm / 24.0
                 except TypeError: # If bpm or pitch is not a number
                    effective_bpm = None # Or some default
                    delay_value = 0.0


            tile.update_data(
                bpm=client.bpm * client.actual_pitch if client.bpm and client.actual_pitch else None,
                delay=delay_value, # Placeholder
                is_master=is_master
            )
            tile.set_selected_source(is_selected)
            tile.set_dropped(False) # Assume not dropped if we got an update for it

            col += 1
            if col >= 2: # Max 2 tiles per row
                col = 0
                row += 1

        self.update_global_status_label()

    def handle_player_tile_selected(self, player_number):
        logging.info(f"Player tile {player_number} selected by user.")
        if self.selected_player_source == player_number:
            # If clicking the already selected player, deselect it.
            # This means clock will revert to Master or stop if no master.
            self.selected_player_source = None
        else:
            self.selected_player_source = player_number

        # Update all tiles to reflect new selection
        for num, tile_widget in self.player_tiles.items():
            tile_widget.set_selected_source(num == self.selected_player_source)

        self.update_midi_clock_source_logic()
        self.update_global_status_label()

    def _determine_midi_backend(self):
        if AlsaMidiClock is not None and (self.preferred_midi_backend == "ALSA" or sys.platform.startswith('linux')):
            self.MidiClockImpl = AlsaMidiClock
            logging.info("Selected ALSA MIDI backend.")
        elif RtMidiClock is not None:
            self.MidiClockImpl = RtMidiClock
            logging.info("Selected rtmidi MIDI backend.")
        else:
            logging.error("No suitable MIDI implementation found!")
            self.MidiClockImpl = None # Should not happen if requirements are met

    def populate_midi_ports(self):
        self.midi_port_combo.clear()
        self._determine_midi_backend()

        if self.MidiClockImpl is None:
            self.midi_port_combo.addItem("No MIDI Backend!")
            self.midi_port_combo.setEnabled(False)
            self.start_stop_button.setEnabled(False)
            return

        temp_clock_instance = self.MidiClockImpl()
        ports = []
        if hasattr(temp_clock_instance, 'iter_alsa_seq_clients'): # ALSA
            try:
                for client_id, name, port_ids in temp_clock_instance.iter_alsa_seq_clients():
                    for p_id in port_ids:
                        ports.append(f"{name} ({client_id}:{p_id})")
            except Exception as e:
                logging.error(f"Error listing ALSA MIDI ports: {e}")
        elif hasattr(temp_clock_instance, 'midiout'): # rtmidi
            try:
                rtmidi_ports = temp_clock_instance.midiout.get_ports()
                if rtmidi_ports:
                    ports.extend(rtmidi_ports)
            except Exception as e:
                logging.error(f"Error listing rtmidi MIDI ports: {e}")

        if ports:
            self.midi_port_combo.addItems(ports)
            self.midi_port_combo.setEnabled(True)
            self.start_stop_button.setEnabled(True)
        else:
            self.midi_port_combo.addItem("No MIDI Ports Found")
            self.midi_port_combo.setEnabled(False)
            self.start_stop_button.setEnabled(False)
        # temp_clock_instance is not started, will be garbage collected.

    def toggle_midi_clock_output(self):
        if self.start_stop_button.isChecked(): # User wants to start
            if self.midi_clock_instance is not None and self.midi_clock_instance.is_alive():
                logging.warning("MIDI clock already running. Stopping first.")
                self.midi_clock_instance.stop()
                self.midi_clock_instance = None

            selected_port_full_name = self.midi_port_combo.currentText()
            if not selected_port_full_name or "No MIDI" in selected_port_full_name:
                logging.warning("No valid MIDI output port selected.")
                self.start_stop_button.setChecked(False) # Uncheck button
                return

            if self.MidiClockImpl is None:
                logging.error("No MIDI implementation available.")
                self.start_stop_button.setChecked(False)
                return

            self.midi_clock_instance = self.MidiClockImpl()

            # Parsing port name for ALSA/rtmidi (simplified)
            # ALSA might need client:port, rtmidi might need index or name part
            # For simplicity, midiclock_alsaseq.open takes (name, port_id)
            # midiclock_rtmidi.open takes (name, port_index)
            # The combobox has "Name (id:port)" for ALSA or just "Name:port_num" for rtmidi
            # This parsing needs to be robust or the open methods need to handle these strings.
            # For now, let's assume open methods can parse or we pass parts.
            # This is a complex part, using default port 0 for now if parsing fails.
            device_name_to_open = selected_port_full_name
            port_to_open = 0
            # TODO: Refine port parsing from combobox string for open() methods
            # Example for rtmidi, it might use index: port_to_open = self.midi_port_combo.currentIndex()

            try:
                self.midi_clock_instance.open(preferred_name=device_name_to_open, preferred_port=port_to_open) # Adjust params as needed
                self.update_midi_clock_source_logic() # Set initial BPM
                self.midi_clock_instance.start()
                logging.info(f"Starting MIDI clock on port {selected_port_full_name}")
                self.start_stop_button.setText("Stop MIDI Clock")
                self.midi_port_combo.setEnabled(False) # Disable port selection while running
            except Exception as e:
                logging.error(f"Failed to start MIDI clock on {selected_port_full_name}: {e}")
                self.midi_clock_instance = None
                self.start_stop_button.setChecked(False) # Uncheck button
        else: # User wants to stop
            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.stop()
                logging.info("Stopping MIDI clock")
            self.midi_clock_instance = None
            self.start_stop_button.setText("Start MIDI Clock")
            self.midi_port_combo.setEnabled(True) # Re-enable port selection
        self.update_global_status_label()

    def update_midi_clock_source_logic(self):
        source_player = None
        if self.selected_player_source is not None:
            source_player = self.prodj.cl.getClient(self.selected_player_source)
            if source_player is None: # Selected player disappeared
                logging.warning(f"Selected player {self.selected_player_source} disappeared. Reverting to master.")
                self.selected_player_source = None
                # Update the tile for the dropped player (visual feedback)
                if self.selected_player_source in self.player_tiles:
                     self.player_tiles[self.selected_player_source].set_dropped(True)


        if source_player is None: # No selection or selected player gone, try to find master
            for client in self.prodj.cl.clients:
                if client.type == "cdj" and "master" in client.state:
                    source_player = client
                    logging.info(f"No source selected or selected dropped. Using network master Player {source_player.player_number}.")
                    break

        effective_bpm = 0
        if source_player and source_player.bpm is not None and source_player.actual_pitch is not None:
            try:
                current_bpm = float(source_player.bpm) # Ensure it's a float
                current_pitch = float(source_player.actual_pitch)
                if current_bpm > 0:
                    effective_bpm = current_bpm * current_pitch
            except (TypeError, ValueError):
                logging.warning(f"Invalid BPM/pitch for player {source_player.player_number if source_player else 'N/A'}")
                effective_bpm = 0 # Or some default like 120

        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            if effective_bpm > 0:
                self.midi_clock_instance.setBpm(effective_bpm)
            else:
                # What to do if BPM is 0 or invalid? Stop clock or send default?
                # midiclock.py warns on 0 BPM. Let's assume setBpm handles it.
                self.midi_clock_instance.setBpm(120) # Default to 120 if no valid source
                logging.warning("No valid BPM source, setting MIDI clock to 120 BPM (default).")

        self.update_global_status_label(source_player, effective_bpm)


    def update_global_status_label(self, source_player=None, effective_bpm=None):
        status_text = "MIDI Clock: "
        if self.midi_clock_instance and self.midi_clock_instance.is_alive():
            status_text += f"Running on {self.midi_port_combo.currentText()}"
            if source_player:
                status_text += f" | Source: Player {source_player.player_number}"
                if effective_bpm and effective_bpm > 0:
                     status_text += f" @ {effective_bpm:.2f} BPM"
            else:
                status_text += " | Source: None (or Master auto)"
        else:
            status_text += "Stopped"
        self.global_status_label.setText(status_text)


    def closeEvent(self, event):
        # Ensure MIDI clock is stopped if running
        if self.midi_clock_instance and self.midi_clock_instance.is_alive(): # Assuming is_alive
           self.midi_clock_instance.stop()
        super().closeEvent(event)

if __name__ == '__main__':
    # This is just for testing the widget in isolation if needed
    from PyQt5.QtWidgets import QApplication
    import sys

    logging.basicConfig(level=logging.DEBUG, format='%(levelname)-7s %(module)s: %(message)s')

    # Mock ProDj and SignalBridge for standalone testing
    class MockProDj:
        class MockClient:
            def __init__(self, num, master=False):
                self.player_number = num
                self.model = "CDJ-2000NXS"
                self.type = "cdj"
                self.bpm = 120.00 + num
                self.actual_pitch = 1.0
                self.state = ["master"] if master else []
                self.fw = "1.23"

        def __init__(self):
            self.cl = Mock()
            self.cl.clients = [self.MockClient(1, master=True), self.MockClient(2)]

    class MockSignalBridge:
        client_change_signal = pyqtSignal(int)
        master_change_signal = pyqtSignal(int)

    app = QApplication(sys.argv)
    mock_prodj = MockProDj()
    mock_bridge = MockSignalBridge()

    window = MidiClockMainWindow(mock_prodj, mock_bridge)
    window.show()
    sys.exit(app.exec_())
