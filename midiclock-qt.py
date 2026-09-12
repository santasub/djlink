#!/usr/bin/env python3

import sys
import logging
import argparse

from qtpy.QtWidgets import QApplication
from qtpy.QtCore import Signal, QObject

from prodj.core.prodj import ProDj
from prodj.gui.midiclock_widgets import MidiClockMainWindow

class SignalBridge(QObject):
    """
    A QObject bridge to safely emit signals from non-Qt threads (like ProDj callbacks)
    to the Qt main thread.
    """
    client_change_signal     = Signal(int)
    master_change_signal     = Signal(int)
    beat_signal              = Signal()
    prodj_beat_signal        = Signal(int, int)
    prodj_beat_timing_signal = Signal(int, int, object)
    # Emitted when metadata for any player arrives (triggers track info bar refresh)
    metadata_ready_signal    = Signal()

class MidiClockApp:
    def __init__(self, args):
        self.args = args

        numeric_level = getattr(logging, args.loglevel.upper(), None)
        if not isinstance(numeric_level, int):
            # Should not happen if choices are enforced by argparse
            logging.error("Invalid log level: %s. Defaulting to INFO.", args.loglevel)
            numeric_level = logging.INFO
        logging.basicConfig(level=numeric_level, format='%(levelname)-7s %(module)s: %(message)s')

        self.app = QApplication(sys.argv)
        # Target: 1280x720 landscape (reTerminal 5" IPS), finger-friendly (>=44px targets)
        self.app.setStyleSheet("""
            QWidget {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1a1a1a, stop:1 #242424);
                color: #e5e7eb;
                font-family: "Segoe UI", "San Francisco", Arial, sans-serif;
                font-size: 11pt;
            }
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3b3b3b, stop:1 #2d2d2d);
                border: 1px solid #4a4a4a;
                padding: 6px 10px;
                min-height: 36px;
                border-radius: 6px;
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
                background: #242424;
                color: #4b5563;
                border: 1px solid #374151;
            }
            QLabel {
                background-color: transparent;
                padding: 1px;
                color: #e5e7eb;
            }
            QComboBox {
                background: #2d2d2d;
                border: 1px solid #4a4a4a;
                padding: 4px 8px;
                min-height: 36px;
                border-radius: 6px;
                color: #e5e7eb;
            }
            QComboBox:hover { border: 1px solid #0ea5e9; }
            QComboBox::drop-down { border: none; width: 28px; }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 6px solid #e5e7eb;
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                background-color: #2d2d2d;
                border: 1px solid #0ea5e9;
                selection-background-color: #0ea5e9;
                color: #e5e7eb;
            }
            QSlider::groove:horizontal {
                border: 1px solid #4a4a4a;
                height: 14px;
                background: #2d2d2d;
                margin: 2px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0ea5e9, stop:1 #0284c7);
                border: 2px solid #0369a1;
                width: 32px;
                height: 32px;
                margin: -10px 0;
                border-radius: 16px;
            }
            QFrame#PlayerFrame {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #2d2d2d, stop:1 #252525);
                border: 2px solid #3b3b3b;
                border-radius: 10px;
                padding: 6px;
            }
            QGroupBox {
                font-weight: 600;
                border: 1px solid #2a2a2a;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #232323, stop:1 #1e1e1e);
                margin-top: 22px;
                padding-top: 8px;
                border-radius: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                top: 0px;
                padding: 2px 8px;
                background: #0c4a6e;
                color: #7dd3fc;
                font-size: 8pt;
                font-weight: 700;
                letter-spacing: 0.06em;
                border-radius: 3px;
            }
            QRadioButton {
                spacing: 10px;
                padding: 4px;
                color: #e5e7eb;
            }
            QRadioButton::indicator {
                width: 18px;
                height: 18px;
                border-radius: 9px;
                border: 2px solid #4b5563;
                background: #1e1e1e;
            }
            QRadioButton::indicator:hover {
                border: 2px solid #7dd3fc;
                background: #1e2a35;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #059669;
                background: qradialgradient(
                    cx:0.5, cy:0.5, radius:0.5,
                    fx:0.5, fy:0.5,
                    stop:0 #10b981,
                    stop:0.55 #10b981,
                    stop:0.56 #065f46,
                    stop:1 #065f46
                );
            }
            QRadioButton::indicator:checked:hover {
                border: 2px solid #10b981;
            }
            QDoubleSpinBox {
                background: #2d2d2d;
                border: 1px solid #4a4a4a;
                border-radius: 6px;
                padding: 4px 6px;
                min-height: 36px;
                color: #e5e7eb;
            }
            QDoubleSpinBox:hover { border: 1px solid #0ea5e9; }
            QMenu {
                background-color: #2d2d2d;
                border: 1px solid #0ea5e9;
                color: #e5e7eb;
            }
            QMenu::item { padding: 8px 32px 8px 16px; }
            QMenu::item:selected { background-color: #0ea5e9; color: white; }
        """)
        self.prodj = ProDj(iface=self.args.iface)
        self.signal_bridge = SignalBridge()

        # Fix 2a: set_client_keepalive_callback fires on every keepalive packet
        # (~1 Hz per CDJ) while set_client_change_callback fires only when
        # player state actually changes.  We only need the change callback for
        # the MIDI-clock UI; keepalive is used solely for timeout / presence
        # detection in the core layer and does not need a UI signal.
        self.prodj.set_client_change_callback(
            lambda pn: self.signal_bridge.client_change_signal.emit(pn)
        )
        # Wire a metadata callback so the track info bar updates as soon as
        # title/artist data arrives from the CDJ database (asynchronous).
        # We replace the clientlist's logPlayedTrackCallback with one that
        # also emits metadata_ready_signal into the Qt thread.
        _orig_log = self.prodj.cl.logPlayedTrackCallback
        _bridge = self.signal_bridge
        def _meta_callback(request, source_player_number, slot, item_id, reply):
            _orig_log(request, source_player_number, slot, item_id, reply)
            _bridge.metadata_ready_signal.emit()
        self.prodj.cl.logPlayedTrackCallback = _meta_callback
        self.prodj.cl.beat_callback = (
            lambda pn, bn: self.signal_bridge.prodj_beat_signal.emit(pn, bn)
        )
        self.prodj.cl.beat_with_timing_callback = (
            lambda pn, bn, nb_ms: self.signal_bridge.prodj_beat_timing_signal.emit(pn, bn, nb_ms)
        )

        self.main_window = MidiClockMainWindow(self.prodj, self.signal_bridge)
        if self.args.fullscreen:
            self.main_window.showFullScreen()
        else:
            self.main_window.show()

    def run(self):
        logging.info("Starting ProDJ Link MIDI Clock...")
        self.prodj.start()
        # Player 6 keeps us off the standard 1-4 CDJ slots
        self.prodj.vcdj_set_player_number(5)  # must be 1-4 for DB queries; 5 is safe observer slot
        self.prodj.vcdj_enable()

        exit_code = self.app.exec()

        logging.info("Shutting down.")
        self.prodj.stop()
        sys.exit(exit_code)

def main():
    parser = argparse.ArgumentParser(description='ProDJ Link MIDI Clock Utility with Qt UI')

    loglevels = ['debug', 'info', 'warning', 'error', 'critical']
    parser.add_argument('--loglevel', choices=loglevels, default='info',
                        help="Set the logging level (default: info).")
    parser.add_argument('--iface', type=str,
                        help="Name of the interface to use (e.g. eth0).")
    parser.add_argument('--fullscreen', action='store_true',
                        help="Launch in fullscreen mode (no window borders).")
    # Add other arguments if needed, e.g., for forcing MIDI backend eventually

    args = parser.parse_args()
    app_instance = MidiClockApp(args)
    app_instance.run()

if __name__ == '__main__':
    main()
