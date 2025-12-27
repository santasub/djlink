#!/usr/bin/env python3

import sys
import logging
import argparse

from qtpy.QtWidgets import QApplication
from qtpy.QtCore import Signal, QObject

from prodj.core.prodj import ProDj
from prodj.gui.midiclock_widgets import MidiClockMainWindow
# Placeholder for MidiClock class, will decide on exact import later
# from prodj.midi.midiclock_rtmidi import MidiClock as RtMidiClock
# try:
#     from prodj.midi.midiclock_alsaseq import MidiClock as AlsaMidiClock
# except ImportError:
#     AlsaMidiClock = None

DEFAULT_LOG_LEVEL = logging.INFO

class SignalBridge(QObject):
    """
    A QObject bridge to safely emit signals from non-Qt threads (like ProDj callbacks)
    to the Qt main thread.
    """
    client_change_signal = Signal(int)
    master_change_signal = Signal(int) # Player number of the new master, or 0 if no master
    # Add more signals as needed

class MidiClockApp:
    def __init__(self, args):
        self.args = args

        numeric_level = getattr(logging, args.loglevel.upper(), None)
        if not isinstance(numeric_level, int):
            # Should not happen if choices are enforced by argparse
            logging.error(f"Invalid log level: {args.loglevel}. Defaulting to INFO.")
            numeric_level = logging.INFO
        logging.basicConfig(level=numeric_level, format='%(levelname)-7s %(module)s: %(message)s')

        self.app = QApplication(sys.argv)
        self.app.setStyleSheet("""
            QWidget {
                background-color: #2e2e2e;
                color: #d0d0d0;
                font-family: "Arial", sans-serif; /* Adjust font as needed */
                font-size: 10pt; /* Default font size */
            }
            QPushButton {
                background-color: #4a4a4a;
                border: 1px solid #5a5a5a;
                padding: 6px;
                min-height: 20px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #5a5a5a;
            }
            QPushButton:pressed, QPushButton:checked {
                background-color: #0078d7;
                color: white;
                border: 1px solid #005394;
            }
            QPushButton:disabled {
                background-color: #383838;
                color: #707070;
            }
            QLabel {
                background-color: transparent;
                padding: 2px; /* Add some padding to labels */
            }
            QComboBox {
                background-color: #3c3c3c;
                border: 1px solid #5a5a5a;
                padding: 4px;
                min-height: 22px;
                border-radius: 3px;
            }
            QComboBox QAbstractItemView {
                background-color: #3c3c3c;
                border: 1px solid #5a5a5a;
                selection-background-color: #0078d7;
                color: #d0d0d0;
            }
            QSlider::groove:horizontal {
                border: 1px solid #5a5a5a;
                height: 8px;
                background: #3c3c3c;
                margin: 2px 0;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #0078d7;
                border: 1px solid #005394;
                width: 16px;
                margin: -4px 0;
                border-radius: 8px;
            }
            QFrame#PlayerFrame { /* Example for PlayerTileWidget if it uses this ID */
                border: 1px solid #505050;
                border-radius: 4px;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #444444;
                margin-top: 10px; /* Space for title */
                padding-top: 10px; /* Space inside for content */
                border-radius: 4px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px 0 5px;
                left: 10px;
                color: #b0b0b0; /* Lighter title for groupbox */
            }
            QRadioButton {
                spacing: 5px;
                padding: 2px;
            }
            QMenu {
                background-color: #3c3c3c;
                border: 1px solid #5a5a5a;
                color: #d0d0d0;
            }
            QMenu::item:selected {
                background-color: #0078d7;
                color: white;
            }
        """)
        self.prodj = ProDj(iface=self.args.iface)
        self.signal_bridge = SignalBridge()

        # Connect ProDj callbacks to signal bridge slots
        self.prodj.set_client_change_callback(self.handle_client_change)
        # We might need a more specific callback for master changes, or derive it in client_change.
        # For now, client_change can trigger UI updates which can check master status.

        # Moved import to top of file
        self.main_window = MidiClockMainWindow(self.prodj, self.signal_bridge)
        self.main_window.show()

        # Connect signals from bridge to main window slots
        # Note: MidiClockMainWindow._connect_signals already connects to signal_bridge.client_change_signal
        # If master_change_signal is used, connect it here or in the window.
        # self.signal_bridge.master_change_signal.connect(self.main_window.update_master_indicator)


    def handle_client_change(self, player_number):
        # This is called from ProDj's thread. Emit a signal to update UI in Qt thread.
        # The client_change_signal will trigger updates in MidiClockMainWindow,
        # which can then determine master status and other details.
        self.signal_bridge.client_change_signal.emit(player_number)


    def run(self):
        logging.info("Starting ProDJ Link listener for MidiClock UI...")
        self.prodj.start()
        # It's important that vCDJ is enabled if we want this app to have a presence
        # on the network, which might be needed for some interactions or if it's
        # supposed to act like a virtual device. For just listening and sending MIDI clock,
        # it might not be strictly necessary to have its own vCDJ player number if it's only observing.
        # However, midiclock.py does enable it.
        self.prodj.vcdj_set_player_number(6) # Use a different player number than default monitor
        self.prodj.vcdj_enable()

        exit_code = self.app.exec()

        logging.info("Shutting down MidiClock UI and ProDJ Link listener...")
        self.prodj.stop()
        sys.exit(exit_code)

def main():
    parser = argparse.ArgumentParser(description='ProDJ Link MIDI Clock Utility with Qt UI')

    loglevels = ['debug', 'info', 'warning', 'error', 'critical']
    parser.add_argument('--loglevel', choices=loglevels, default='info',
                        help="Set the logging level (default: info).")
    parser.add_argument('--iface', type=str,
                        help="Name of the interface to use (e.g. eth0).")
    # Add other arguments if needed, e.g., for forcing MIDI backend eventually

    args = parser.parse_args()

    # Convert loglevel string to logging module constant
    # Note: MidiClockApp __init__ currently takes the args.loglevel as is from old setup.
    # We need to pass the numeric level or have MidiClockApp handle the conversion.
    # For consistency, let's do conversion here and MidiClockApp can expect numeric_level.
    # However, __init__ uses args.loglevel directly for basicConfig. So, let basicConfig handle it.
    # The change will be in how basicConfig is called in __init__.

    app_instance = MidiClockApp(args)
    app_instance.run()

if __name__ == '__main__':
    main()
