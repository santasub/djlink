#!/usr/bin/env python3

import logging
import sys
from PyQt5.QtWidgets import QApplication, QWidget, QGridLayout, QPushButton, QLabel, QVBoxLayout, QFrame, QProgressBar
from PyQt5.QtCore import pyqtSignal, Qt, QObject
import signal
from prodj.core.prodj import ProDj
from prodj.data.dbclient import DBClient
from prodj.network.nfsclient import NfsClient

class DjThief(QWidget):
    client_change_signal = pyqtSignal(int)

    def __init__(self, prodj):
        super().__init__()
        self.prodj = prodj
        self.setWindowTitle('DJ Thief')
        self.layout = QGridLayout(self)
        self.media_sources = {}
        self.client_change_signal.connect(self.client_change_slot)
        self.init_ui()

    def init_ui(self):
        self.setMinimumSize(400, 200)
        self.prodj.set_client_change_callback(self.client_change_callback)
        self.show()

    def client_change_callback(self, player_number):
        self.client_change_signal.emit(player_number)

    def client_change_slot(self, player_number):
        c = self.prodj.cl.getClient(player_number)
        if c is None:
            # Player disconnected, remove corresponding media sources
            for key, widget in list(self.media_sources.items()):
                if widget.player_number == player_number:
                    widget.deleteLater()
                    del self.media_sources[key]
            return

        if c.loaded_slot in ["sd", "usb"]:
            key = f"{c.player_number}:{c.loaded_slot}"
            if key not in self.media_sources:
                self.media_sources[key] = MediaSourceWidget(self, c.ip_addr, c.loaded_slot, c.player_number)
                self.layout.addWidget(self.media_sources[key], (len(self.media_sources) -1) // 2, (len(self.media_sources) - 1) % 2)
        else:
            # Media removed, remove corresponding media source
            key = f"{c.player_number}:{c.previous_loaded_slot}"
            if key in self.media_sources:
                self.media_sources[key].deleteLater()
                del self.media_sources[key]

class DownloadManager(QObject):
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal()

    def __init__(self, parent, prodj, player_number, slot):
        super().__init__()
        self.parent = parent
        self.prodj = prodj
        self.player_number = player_number
        self.slot = slot
        self.tracks_to_download = 0
        self.tracks_downloaded = 0

    def download_all_songs(self):
        logging.info(f"Downloading all songs from player {self.player_number}:{self.slot}")
        tracks = self.prodj.data.dbclient.query_list(self.player_number, self.slot, "title", [0], "title_request")
        if tracks:
            self.tracks_to_download = len(tracks)
            self.tracks_downloaded = 0
            for track in tracks:
                future = self.prodj.data.get_mount_info(
                    self.player_number,
                    self.slot,
                    track['track_id'],
                    self.prodj.nfs.enqueue_download_from_mount_info
                )
                future.add_done_callback(self.download_done_callback)
        else:
            self.finished_signal.emit()

    def download_done_callback(self, future):
        if future.exception() is not None:
            logging.error("download failed (callback): %s", future.exception())
        else:
            logging.info("download finished: %s", future.result())

        self.tracks_downloaded += 1
        self.progress_signal.emit(self.tracks_downloaded)
        if self.tracks_downloaded == self.tracks_to_download:
            self.finished_signal.emit()

class MediaSourceWidget(QFrame):
    def __init__(self, parent, ip_addr, slot, player_number):
        super().__init__(parent)
        self.parent = parent
        self.ip_addr = ip_addr
        self.slot = slot
        self.player_number = player_number
        self.download_manager = DownloadManager(self, parent.prodj, player_number, slot)
        self.download_manager.progress_signal.connect(self.update_progress)
        self.download_manager.finished_signal.connect(self.download_finished)
        self.init_ui()

    def init_ui(self):
        self.setFrameStyle(QFrame.Box | QFrame.Plain)
        layout = QVBoxLayout(self)
        self.label = QLabel(f"Media Source: Player {self.player_number} - {self.slot.upper()}")
        self.download_button = QPushButton("Download All Songs")
        self.download_button.clicked.connect(self.start_download)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.label)
        layout.addWidget(self.download_button)
        layout.addWidget(self.progress_bar)

    def start_download(self):
        self.download_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        tracks = self.parent.prodj.data.dbclient.query_list(self.player_number, self.slot, "title", [0], "title_request")
        if tracks:
            self.progress_bar.setMaximum(len(tracks))
            self.download_manager.download_all_songs()
        else:
            self.download_finished()

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def download_finished(self):
        self.download_button.setEnabled(True)
        self.progress_bar.setVisible(False)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Python ProDJ Link Thief')
    loglevels = ['debug', 'info', 'warning', 'error', 'critical', 'dump_packets']
    parser.add_argument('--loglevel', choices=loglevels, default='info',
                        help=f"Set the logging level (default: info). 'dump_packets' enables packet content logging.")
    args = parser.parse_args()

    numeric_level = getattr(logging, args.loglevel.upper(), None)
    if args.loglevel == 'dump_packets':
        numeric_level = 0 # Special case for packet dumping
    elif not isinstance(numeric_level, int):
        raise ValueError(f'Invalid log level: {args.loglevel}')
    logging.basicConfig(level=numeric_level, format='%(levelname)-7s %(module)s: %(message)s')

    prodj = ProDj()
    prodj.start()

    app = QApplication(sys.argv)
    djthief = DjThief(prodj)

    signal.signal(signal.SIGINT, lambda s, f: app.quit())

    app.exec()
    logging.info("Shutting down...")
    prodj.stop()
