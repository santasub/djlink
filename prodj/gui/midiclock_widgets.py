import logging
import sys # Moved to be among the first imports
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QComboBox, QGridLayout, QFrame, QSizePolicy, QDialog,
                             QGroupBox, QRadioButton, QDialogButtonBox, QSlider) # Added QSlider
from PyQt5.QtCore import Qt, pyqtSignal

# MIDI Clock imports
from prodj.midi.midiclock_rtmidi import MidiClock as RtMidiClock
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
    selected_signal = pyqtSignal(int) # Emits player number when selected

    def __init__(self, player_number, parent=None):
        super().__init__(parent)
        self.player_number = player_number
        self.is_master = False
        self.is_selected_source = False
        self.is_dropped = False # New state

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
        self.status_label = QLabel("Status: Normal")
        layout.addWidget(self.status_label)

        self.action_button = QPushButton("Select as Source")
        # self.action_button.setCheckable(True) # Not a checkable button anymore, direct action
        self.action_button.clicked.connect(self.handle_action_clicked)
        layout.addWidget(self.action_button)

        self.update_ui_elements() # Changed from update_style to a more comprehensive update

    def handle_action_clicked(self):
        # If dropped, this button might mean "Try Reconnect" or "Clear Selection"
        # For now, it always emits selected_signal, and MainWindow decides.
        # If it's a "Use this source" button after reconnect, this signal is still fine.
        self.selected_signal.emit(self.player_number)

    def set_selected_source(self, is_selected):
        self.is_selected_source = is_selected
        self.update_ui_elements()

    def update_data(self, bpm, delay, is_master):
        self.bpm_label.setText(f"BPM: {bpm:.2f}" if isinstance(bpm, (float, int)) else "BPM: --.--")
        self.delay_label.setText(f"Delay: {delay * 1000:.2f} ms" if isinstance(delay, (float, int)) else "Delay: --.-- ms")
        self.is_master = is_master
        # If data is updated, it means it's not dropped (or just reconnected)
        if self.is_dropped: # Was dropped, now getting data
             self.is_dropped = False
             # MainWindow will decide if it was the selected_player_source and needs special handling
        self.update_ui_elements()

    def set_dropped_status(self, is_dropped_now):
        if self.is_dropped != is_dropped_now:
            self.is_dropped = is_dropped_now
            self.update_ui_elements()

    def update_ui_elements(self):
        status_parts = []
        current_style = "PlayerTileWidget { border: 1px solid gray; }" # Default
        button_text = "Select as Source"
        button_enabled = True

        if self.is_dropped:
            status_parts.append("Network Drop")
            button_text = "Use as Source (Reconnect)" # Or just "Select", re-selection implies reconnect
            current_style = "PlayerTileWidget { border: 1px solid red; background-color: #400000; }"
            # Keep button enabled to allow re-selection if it comes back.
            # Or disable it: button_enabled = False
            self.bpm_label.setText("BPM: --.--") # Clear stale data
            self.delay_label.setText("Delay: --.-- ms")

        else: # Not dropped
            if self.is_master:
                status_parts.append("Master")
                current_style = "PlayerTileWidget { border: 2px solid blue; }"

            if self.is_selected_source:
                status_parts.append("Selected Source")
                current_style = "PlayerTileWidget { border: 2px solid green; }"
                button_text = "Deselect Source" # Change button text when selected
                # self.action_button.setChecked(True) # If it were checkable
            # else:
                # self.action_button.setChecked(False) # If it were checkable

        if not status_parts and not self.is_dropped:
            self.status_label.setText("Status: Normal")
        else:
            self.status_label.setText(f"Status: {', '.join(status_parts)}")

        self.setStyleSheet(current_style)
        self.action_button.setText(button_text)
        self.action_button.setEnabled(button_enabled)
        self.player_label.setEnabled(button_enabled) # Also enable/disable labels with button
        self.bpm_label.setEnabled(button_enabled)
        self.delay_label.setEnabled(button_enabled)
        self.status_label.setEnabled(button_enabled)


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
        controls_layout.addSpacing(20)

        # --- Manual BPM Controls ---
        self.manual_mode_button = QPushButton("Enable Manual BPM")
        self.manual_mode_button.setCheckable(True)
        self.manual_mode_button.clicked.connect(self.toggle_manual_bpm_mode)
        controls_layout.addWidget(self.manual_mode_button)

        self.manual_bpm_slider = QSlider(Qt.Horizontal)
        self.manual_bpm_slider.setRange(300, 3000) # e.g., 30.0 BPM to 300.0 BPM, scaled by 10
        self.manual_bpm_slider.setValue(1200) # Default 120.0 BPM
        self.manual_bpm_slider.setFixedWidth(150)
        self.manual_bpm_slider.valueChanged.connect(self.manual_bpm_slider_changed)
        self.manual_bpm_slider.setEnabled(False) # Disabled initially
        controls_layout.addWidget(self.manual_bpm_slider)

        self.manual_bpm_label = QLabel("120.0 BPM")
        self.manual_bpm_label.setFixedWidth(70)
        self.manual_bpm_label.setEnabled(False) # Disabled initially
        controls_layout.addWidget(self.manual_bpm_label)
        controls_layout.addSpacing(10)

        self.tap_tempo_button = QPushButton("Tap Tempo")
        self.tap_tempo_button.clicked.connect(self.handle_tap_tempo_clicked)
        self.tap_tempo_button.setEnabled(False) # Enable when manual mode is active
        controls_layout.addWidget(self.tap_tempo_button)
        controls_layout.addSpacing(20)

        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.open_settings_dialog)
        controls_layout.addWidget(self.settings_button)
        # --- End Manual BPM Controls ---

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
        self.update_midi_clock_source_logic() # Ensure clock source logic is re-evaluated on any change

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
        logging.info(f"Player tile {player_number} selected by user.")
        tile = self.player_tiles.get(player_number)
        if tile and tile.is_dropped: # If a dropped tile is clicked
            # Treat as attempt to use this source again. If it's still not on network,
            # update_midi_clock_source_logic will fail to get client and revert.
            # If it is back, it will become the source.
            logging.info(f"Attempting to re-select dropped player {player_number} as source.")
            # tile.set_dropped_status(False) # Assume it's back if user clicks, let logic confirm

        if self.selected_player_source == player_number:
            self.selected_player_source = None # Deselect
        else:
            self.selected_player_source = player_number

        for num, tile_widget in self.player_tiles.items():
            tile_widget.set_selected_source(num == self.selected_player_source)

        self.update_midi_clock_source_logic()
        self.update_global_status_label()

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
        self._determine_midi_backend() # Ensure self.MidiClockImpl is set

        if self.MidiClockImpl is None:
            self.midi_port_combo.addItem("No MIDI Backend!")
            self.midi_port_combo.setEnabled(False)
            self.start_stop_button.setEnabled(False)
            return

        # Create a temporary instance to list ports
        # This instance should not start any threads or acquire system resources beyond port listing.
        temp_clock_instance = None
        try:
            temp_clock_instance = self.MidiClockImpl()
            ports = []
            if self.MidiClockImpl == AlsaMidiClock:
                if hasattr(temp_clock_instance, 'iter_alsa_seq_clients'):
                    for client_id, name, port_ids in temp_clock_instance.iter_alsa_seq_clients():
                        for p_id in port_ids:
                            ports.append(f"{name} ({client_id}:{p_id})")
            elif self.MidiClockImpl == RtMidiClock:
                if hasattr(temp_clock_instance, 'midiout'):
                    rtmidi_ports = temp_clock_instance.midiout.get_ports()
                    if rtmidi_ports:
                        ports.extend(rtmidi_ports)

            if ports:
                self.midi_port_combo.addItems(ports)
                self.midi_port_combo.setEnabled(True)
                self.start_stop_button.setEnabled(True)
            else:
                self.midi_port_combo.addItem("No MIDI Ports Found")
                self.midi_port_combo.setEnabled(False)
                self.start_stop_button.setEnabled(False)
        except Exception as e:
            logging.error(f"Error listing MIDI ports: {e}")
            self.midi_port_combo.addItem("Error listing ports")
            self.midi_port_combo.setEnabled(False)
            self.start_stop_button.setEnabled(False)
        finally:
            # Ensure any resources from temp_clock_instance are released if necessary
            # For MidiClock, __del__ might handle it, or if it has an explicit close/del.
            # Since it's not started, it should be minimal.
            del temp_clock_instance


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

            device_name_to_open = None
            port_to_open = 0 # Default or index

            if self.MidiClockImpl == RtMidiClock:
                # rtmidi typically uses port index or full name.
                # If names are unique, full name is fine. Otherwise, index.
                # For simplicity, let's try to use the name directly if possible,
                # or fall back to index if names are not unique or parsing is hard.
                # The current rtmidi open() takes preferred_name and preferred_port (index).
                # We'll pass the full name as preferred_name and let open() try to find it or use index 0.
                # A better way would be to store (name, index) tuples in combobox user data.
                port_index = self.midi_port_combo.currentIndex()
                device_name_to_open = selected_port_full_name # rtmidi can often open by name
                port_to_open = port_index # Pass index as preferred_port

            elif self.MidiClockImpl == AlsaMidiClock:
                # ALSA needs "client_name_or_id:port_id" or separate name and port_id
                # Example: "Virtual Raw MIDI (20:0)" -> name="Virtual Raw MIDI", port_id=0, client_id=20
                # The current midiclock_alsaseq.open() takes (preferred_name, preferred_port)
                # Let's try to parse it.
                import re
                match = re.match(r"^(.*) \((\d+):(\d+)\)$", selected_port_full_name)
                if match:
                    device_name_to_open = match.group(1).strip()
                    # client_id_to_open = int(match.group(2)) # Not directly used by open()
                    port_to_open = int(match.group(3))
                else: # Fallback if parsing fails, pass full name
                    device_name_to_open = selected_port_full_name
                    port_to_open = 0
                    logging.warning(f"Could not parse ALSA port string '{selected_port_full_name}', using raw name and port 0.")

            try:
                logging.debug(f"Attempting to open MIDI port: Name='{device_name_to_open}', PortNum/ID='{port_to_open}' using {self.MidiClockImpl.__name__}")
                self.midi_clock_instance.open(preferred_name=device_name_to_open, preferred_port=port_to_open)
                self.update_midi_clock_source_logic() # Set initial BPM
                if not self.midi_clock_instance.is_alive(): # Check if thread started (it should by .start())
                    self.midi_clock_instance.start()

                logging.info(f"Starting MIDI clock on port {selected_port_full_name}")
                self.start_stop_button.setText("Stop MIDI Clock")
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
            self.start_stop_button.setText("Start MIDI Clock")
            self.midi_port_combo.setEnabled(True)
        self.update_global_status_label()

    def update_midi_clock_source_logic(self):
        if self.manual_bpm_mode_active:
            if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                self.midi_clock_instance.setBpm(self.manual_bpm_value)
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
                self.midi_clock_instance.setBpm(final_bpm_to_set)
            else:
                logging.error("Attempting to set invalid BPM (None or <=0). Defaulting to 120.")
                self.midi_clock_instance.setBpm(120)

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
                self.midi_clock_instance.setBpm(self.manual_bpm_value)
            logging.info(f"Manual BPM mode enabled. Set to {self.manual_bpm_value:.1f} BPM.")
        else:
            self.manual_mode_button.setText("Enable Manual BPM")
            self.tap_timestamps = []
            logging.info("Manual BPM mode disabled. Reverting to automatic source.")
            self.update_midi_clock_source_logic()
        self.update_global_status_label()

    def manual_bpm_slider_changed(self, value):
        self.manual_bpm_value = value / 10.0
        self.manual_bpm_label.setText(f"{self.manual_bpm_value:.1f} BPM")
        self.tap_timestamps = []
        if self.manual_bpm_mode_active and self.midi_clock_instance and self.midi_clock_instance.is_alive():
            self.midi_clock_instance.setBpm(self.manual_bpm_value)
        if self.manual_bpm_mode_active:
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
                self.midi_clock_instance.setBpm(self.manual_bpm_value)
            logging.info(f"Tapped BPM: {self.manual_bpm_value:.2f} (avg over {len(intervals)} intervals)")
            self.update_global_status_label()
        else:
            logging.debug("Average interval is zero, cannot calculate BPM.")

    def update_global_status_label(self):
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
        if dialog.exec_(): # Modal execution
            new_preferred_backend = dialog.get_selected_backend()
            if self.preferred_midi_backend != new_preferred_backend:
                self.preferred_midi_backend = new_preferred_backend
                logging.info(f"Settings updated. Preferred MIDI backend: {self.preferred_midi_backend}")

                # Stop clock if running, as backend change requires re-initialization
                if self.midi_clock_instance and self.midi_clock_instance.is_alive():
                    logging.info("Stopping MIDI clock due to backend change.")
                    self.midi_clock_instance.stop()
                    self.midi_clock_instance = None
                    self.start_stop_button.setChecked(False)
                    self.start_stop_button.setText("Start MIDI Clock")
                    self.midi_port_combo.setEnabled(True)

                self.populate_midi_ports()
                self.update_global_status_label()


class MidiClockSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.setWindowTitle("MIDI Clock Settings")
        self.setMinimumWidth(300)

        layout = QVBoxLayout(self)

        if sys.platform.startswith('linux') and AlsaMidiClock is not None:
            backend_group = QGroupBox("MIDI Backend Preference (Linux)")
            backend_layout = QVBoxLayout()

            self.alsa_radio = QRadioButton("Prefer ALSA")
            self.rtmidi_radio = QRadioButton("Prefer rtmidi")

            # Use a local variable for current_preference to avoid issues if parent_window attribute is temporarily None
            current_preference = None
            if self.parent_window:
                 current_preference = getattr(self.parent_window, 'preferred_midi_backend', "ALSA") # Default to ALSA on Linux

            if current_preference == "ALSA":
                self.alsa_radio.setChecked(True)
            elif current_preference == "rtmidi": # rtmidi or None (if parent_window was None initially)
                self.rtmidi_radio.setChecked(True)
            else: # Default if somehow still None or unexpected value
                 self.alsa_radio.setChecked(True)


            backend_layout.addWidget(self.alsa_radio)
            backend_layout.addWidget(self.rtmidi_radio)
            backend_group.setLayout(backend_layout)
            layout.addWidget(backend_group)
        else:
            self.alsa_radio = None
            self.rtmidi_radio = None


        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def get_selected_backend(self):
        if self.alsa_radio and self.alsa_radio.isChecked():
            return "ALSA"
        if self.rtmidi_radio and self.rtmidi_radio.isChecked():
            return "rtmidi"

        # Fallback default if UI elements aren't available (e.g. non-Linux)
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
