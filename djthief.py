#!/usr/bin/env python3

import logging
import sys
import os
from PyQt5.QtWidgets import QApplication, QWidget, QGridLayout, QPushButton, QLabel, QVBoxLayout, QFrame, QProgressBar
from PyQt5.QtCore import pyqtSignal, Qt, QObject, QTimer
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
            logging.debug(f"Player {player_number} disconnected.")
            # Player disconnected, remove corresponding media sources
            for key, widget in list(self.media_sources.items()):
                if widget.player_number == player_number:
                    logging.debug(f"Removing widget for player {player_number}")
                    widget.deleteLater()
                    del self.media_sources[key]
            return

        logging.debug(f"Player {player_number} changed. Loaded slot: {c.loaded_slot}")
        if c.loaded_slot in ["sd", "usb"]:
            key = f"{c.player_number}:{c.loaded_slot}"
            if key not in self.media_sources:
                logging.debug(f"Adding widget for player {player_number} slot {c.loaded_slot}")
                self.media_sources[key] = MediaSourceWidget(self, c.ip_addr, c.loaded_slot, c.player_number)
                self.layout.addWidget(self.media_sources[key], (len(self.media_sources) -1) // 2, (len(self.media_sources) - 1) % 2)
        else:
            # Media removed, remove corresponding media source
            key = f"{c.player_number}:{c.previous_loaded_slot}"
            if key in self.media_sources:
                logging.debug(f"Removing widget for player {player_number} slot {c.previous_loaded_slot}")
                self.media_sources[key].deleteLater()
                del self.media_sources[key]

from collections import deque

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
        self.download_queue = deque()
        self.is_downloading = False

    def download_all_songs(self):
        logging.info(f"Downloading all songs from player {self.player_number}:{self.slot}")
        try:
            # First, get the total number of tracks
            client = self.prodj.cl.getClient(self.player_number)
            if self.slot == "usb":
                track_count = client.usb_info.get("track_count", 0)
            elif self.slot == "sd":
                track_count = client.sd_info.get("track_count", 0)
            else:
                track_count = 0

            if track_count == 0:
                self.finished_signal.emit()
                return

            self.tracks_to_download = track_count
            self.tracks_downloaded = 0
            self.parent.progress_bar.setMaximum(self.tracks_to_download)

            # Then, get the track list in chunks and fill the queue
            chunk_size = 100
            for i in range(0, track_count, chunk_size):
                tracks = self.prodj.data.dbc.query_list(self.player_number, self.slot, "title", [i, chunk_size], "title_request")
                if tracks:
                    for track in tracks:
                        self.download_queue.append(track['track_id'])
                else:
                    break
            
            logging.info(f"Queued {len(self.download_queue)} tracks for download")
            self.process_queue()
                
        except Exception as e:
            logging.error(f"Failed to get track list: {e}")
            self.finished_signal.emit()
            return

    def process_queue(self):
        if not self.download_queue:
            logging.info("Download queue empty, finished.")
            self.finished_signal.emit()
            return

        if self.is_downloading:
            return

        track_id = self.download_queue.popleft()
        self.is_downloading = True
        
        logging.debug(f"Processing track ID {track_id}, {len(self.download_queue)} remaining")

        try:
            # Pass our own callback to handle the mount info response
            # We don't use the future returned by get_mount_info because it resolves 
            # as soon as mount info is received, NOT when download is done.
            self.prodj.data.get_mount_info(
                self.player_number,
                self.slot,
                track_id,
                self.handle_mount_info_response
            )
        except Exception as e:
            logging.error(f"Failed to initiate download for track {track_id}: {e}")
            self.is_downloading = False
            self.process_queue() # Try next one

    def handle_mount_info_response(self, request, player_number, slot, track_id, mount_info):
        # This callback is invoked by DataProvider when mount info is available.
        # We manually trigger the NFS download here so we can capture the download future.
        try:
            if mount_info is None:
                logging.warning(f"Mount info request returned None for track {track_id}")
                self.is_downloading = False
                self.process_queue()
                return

            download_future = self.prodj.nfs.enqueue_download_from_mount_info(
                request, player_number, slot, track_id, mount_info
            )
            
            if download_future:
                download_future.add_done_callback(self.download_done_callback)
            else:
                logging.error("Failed to enqueue download (invalid mount info?)")
                self.is_downloading = False
                self.process_queue()
        except Exception as e:
            logging.error(f"Error handling mount info response: {e}")
            self.is_downloading = False
            self.process_queue()

    def download_done_callback(self, future):
        self.is_downloading = False
        try:
            if future.exception() is not None:
                logging.error("download failed (callback): %s", future.exception())
            else:
                logging.info("download finished: %s", future.result())
        except Exception as e:
             logging.error(f"Error in download callback: {e}")

        self.tracks_downloaded += 1
        self.progress_signal.emit(self.tracks_downloaded)
        
        # Process next item in queue
        self.process_queue()

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
        self.check_db_status()

    def init_ui(self):
        self.setFrameStyle(QFrame.Box | QFrame.Plain)
        layout = QVBoxLayout(self)
        self.label = QLabel(f"Media Source: Player {self.player_number} - {self.slot.upper()}")
        
        # Add refresh DB button for debugging
        self.refresh_db_button = QPushButton("Check Database Status")
        self.refresh_db_button.clicked.connect(self.check_db_status)
        
        self.download_button = QPushButton("Download All Songs")
        self.download_button.clicked.connect(self.start_download)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setVisible(False)
        
        layout.addWidget(self.label)
        layout.addWidget(self.refresh_db_button)
        layout.addWidget(self.download_button)
        layout.addWidget(self.progress_bar)
        
        # Check DB status once, but don't block
        self.check_db_status()

    def check_db_status(self):
        pdb_path = f"databases/player-{self.player_number}-{self.slot}.pdb"
        logging.info(f"Checking for PDB at {pdb_path}")
        
        if not os.path.exists(pdb_path):
            logging.warning(f"PDB file not found at {pdb_path}")
            # Try to force a download via get_db if possible
            try:
                # This triggers a download if missing
                db = self.parent.prodj.data.pdb.get_db(self.player_number, self.slot)
                if db:
                    logging.info("PDB loaded successfully via get_db")
                else:
                    logging.warning("get_db returned None")
            except Exception as e:
                logging.error(f"Failed to get PDB database: {e}")
        else:
            logging.info(f"PDB file exists at {pdb_path}")
        
        # Always enable download button, allow user to try
        self.download_button.setEnabled(True)

    def start_download(self):
        self.download_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.download_manager.download_all_songs()

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def download_finished(self):
        self.download_button.setEnabled(True)
        self.progress_bar.setVisible(False)


def cleanup_databases():
    import os
    import glob
    for f in glob.glob("databases/player-*-*.pdb"):
        os.remove(f)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Python ProDJ Link Thief')
    loglevels = ['debug', 'info', 'warning', 'error', 'critical', 'dump_packets']
    parser.add_argument('--loglevel', choices=loglevels, default='info',
                        help=f"Set the logging level (default: info). 'dump_packets' enables packet content logging.")
    parser.add_argument('--logfile', help="Log to file instead of stdout")
    args = parser.parse_args()

    numeric_level = getattr(logging, args.loglevel.upper(), None)
    if args.loglevel == 'dump_packets':
        numeric_level = 0 # Special case for packet dumping
    elif not isinstance(numeric_level, int):
        raise ValueError(f'Invalid log level: {args.loglevel}')

    log_format = '%(levelname)-7s %(module)s: %(message)s'
    logging.basicConfig(level=numeric_level, format=log_format)
    if args.logfile:
        file_handler = logging.FileHandler(args.logfile, mode='w')
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)

    prodj = ProDj()
    prodj.start()
    prodj.vcdj_set_player_number(5)
    prodj.vcdj_enable()

    app = QApplication(sys.argv)
    djthief = DjThief(prodj)

    signal.signal(signal.SIGINT, lambda s, f: app.quit())

    app.exec()
    logging.info("Shutting down...")
    prodj.stop()
    cleanup_databases()
