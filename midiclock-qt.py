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
    beat_signal = Signal()  # Signal for MIDI beat events
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
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1a1a1a, stop:1 #2a2a2a);
                color: #e5e7eb;
                font-family: "Segoe UI", "San Francisco", "Helvetica Neue", Arial, sans-serif;
                font-size: 13pt;
            }
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3b3b3b, stop:1 #2d2d2d);
                border: 1px solid #4a4a4a;
                padding: 10px;
                min-height: 50px;
                border-radius: 8px;
                color: #e5e7eb;
                font-weight: 600;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #4a4a4a, stop:1 #3a3a3a);
                border: 1px solid #0ea5e9;
            }
            QPushButton:pressed, QPushButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0ea5e9, stop:1 #0284c7);
                color: white;
                border: 1px solid #0284c7;
            }
            QPushButton:disabled {
                background: #2a2a2a;
                color: #6b7280;
                border: 1px solid #374151;
            }
            QLabel {
                background-color: transparent;
                padding: 4px;
                color: #e5e7eb;
            }
            QComboBox {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3b3b3b, stop:1 #2d2d2d);
                border: 1px solid #4a4a4a;
                padding: 8px;
                min-height: 50px;
                border-radius: 8px;
                color: #e5e7eb;
            }
            QComboBox:hover {
                border: 1px solid #0ea5e9;
            }
            QComboBox::drop-down {
                border: none;
                width: 45px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 8px solid transparent;
                border-right: 8px solid transparent;
                border-top: 8px solid #e5e7eb;
                margin-right: 15px;
            }
            QComboBox QAbstractItemView {
                background-color: #2d2d2d;
                border: 1px solid #0ea5e9;
                selection-background-color: #0ea5e9;
                color: #e5e7eb;
                outline: none;
            }
            QSlider::groove:horizontal {
                border: 1px solid #4a4a4a;
                height: 20px;
                background: #2d2d2d;
                margin: 4px 0;
                border-radius: 10px;
            }
            QSlider::handle:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0ea5e9, stop:1 #0284c7);
                border: 2px solid #0369a1;
                width: 40px;
                height: 40px;
                margin: -12px 0;
                border-radius: 20px;
            }
            QSlider::handle:horizontal:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #38bdf8, stop:1 #0ea5e9);
            }
            QFrame#PlayerFrame {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2d2d2d, stop:1 #252525);
                border: 2px solid #3b3b3b;
                border-radius: 16px;
                padding: 12px;
            }
            QGroupBox {
                font-weight: 600;
                border: 2px solid #3b3b3b;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2d2d2d, stop:1 #252525);
                margin-top: 15px;
                padding-top: 15px;
                border-radius: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 10px 0 10px;
                left: 15px;
                color: #0ea5e9;
                font-size: 11pt;
            }
            QRadioButton {
                spacing: 12px;
                padding: 8px;
                color: #e5e7eb;
            }
            QRadioButton::indicator {
                width: 30px;
                height: 30px;
            }
            QDoubleSpinBox {
                background: #2d2d2d;
                border: 1px solid #4a4a4a;
                border-radius: 8px;
                padding: 10px;
                color: #e5e7eb;
                min-height: 50px;
            }
            QDoubleSpinBox:hover {
                border: 1px solid #0ea5e9;
            }
            QMenu {
                background-color: #2d2d2d;
                border: 1px solid #0ea5e9;
                color: #e5e7eb;
            }
            QMenu::item {
                padding: 12px 40px 12px 20px;
            }
            QMenu::item:selected {
                background-color: #0ea5e9;
                color: white;
            }
        """)
        self.prodj = ProDj(iface=self.args.iface)
        self.signal_bridge = SignalBridge()

        # Connect ProDj callbacks to signal bridge slots
        self.prodj.set_client_change_callback(self.handle_client_change)
        # We might need a more specific callback for master changes, or derive it in client_change.
        # For now, client_change can trigger UI updates which can check master status.

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
