#!/usr/bin/env python3

import logging
import sys
import os
import asyncio
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
        # Run the async download logic in the event loop of the nfs client
        asyncio.run_coroutine_threadsafe(self.async_download_all_songs(), self.prodj.nfs.loop)

    async def async_download_all_songs(self):
        try:
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

            all_tracks = []
            chunk_size = 100
            for i in range(0, track_count, chunk_size):
                tracks = self.prodj.data.dbc.query_list(self.player_number, self.slot, "title", [i, chunk_size], "title_request")
                if tracks:
                    all_tracks.extend(tracks)
                else:
                    break

            semaphore = asyncio.Semaphore(4)
            tasks = [self.download_track(semaphore, track) for track in all_tracks]
            await asyncio.gather(*tasks)

        except Exception as e:
            logging.error(f"Failed to get track list: {e}")
        finally:
            self.finished_signal.emit()

    async def download_track(self, semaphore, track):
        async with semaphore:
            try:
                future = self.prodj.data.get_mount_info(
                    self.player_number,
                    self.slot,
                    track['track_id'],
                    self.prodj.nfs.enqueue_download_from_mount_info
                )
                # get_mount_info is not async, but it returns a future. We need to await it.
                # However, since it's being run in a different thread context, we can't directly await it.
                # The callback system is the way to go. Let's adapt.
                # The future from get_mount_info resolves when the download is enqueued, not finished.
                # The future inside *that* future is the one we need.

                # Let's create an awaitable future
                loop = asyncio.get_running_loop()
                aio_future = loop.create_future()

                def download_done_callback(f):
                    if f.exception() is not None:
                        logging.error("download failed (callback): %s", f.exception())
                        loop.call_soon_threadsafe(aio_future.set_exception, f.exception())
                    else:
                        logging.info("download finished: %s", f.result())
                        loop.call_soon_threadsafe(aio_future.set_result, f.result())

                    self.tracks_downloaded += 1
                    self.progress_signal.emit(self.tracks_downloaded)

                # The future returned by enqueue_download_from_mount_info is what we need to add the callback to.
                # get_mount_info itself returns a future that resolves to the result of the enqueue function.
                enqueue_future_future = self.prodj.data.get_mount_info(
                    self.player_number,
                    self.slot,
                    track['track_id'],
                    self.prodj.nfs.enqueue_download_from_mount_info
                )

                def on_enqueue_done(f):
                    try:
                        # The result of this future is the actual download future
                        download_future = f.result()
                        if download_future:
                            download_future.add_done_callback(download_done_callback)
                        else:
                            # Handle case where enqueueing failed
                            exc = RuntimeError("Failed to enqueue download.")
                            loop.call_soon_threadsafe(aio_future.set_exception, exc)
                            self.tracks_downloaded += 1
                            self.progress_signal.emit(self.tracks_downloaded)

                    except Exception as e:
                        logging.error(f"Error during enqueue process: {e}")
                        loop.call_soon_threadsafe(aio_future.set_exception, e)
                        self.tracks_downloaded += 1
                        self.progress_signal.emit(self.tracks_downloaded)

                enqueue_future_future.add_done_callback(on_enqueue_done)

                await aio_future

            except Exception as e:
                logging.error(f"Failed to download track {track.get('track_id')}: {e}")

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
        self.download_button = QPushButton("Download All Songs")
        self.download_button.clicked.connect(self.start_download)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.label)
        layout.addWidget(self.download_button)
        layout.addWidget(self.progress_bar)

    def check_db_status(self):
        # Disable download button until PDB is loaded
        self.download_button.setEnabled(False)
        # The get_db function in PDBProvider is synchronous, so we can just call it and check the result
        if not os.path.exists(f"databases/player-{self.player_number}-{self.slot}.pdb"):
            try:
                db = self.parent.prodj.data.pdb.get_db(self.player_number, self.slot)
                if db:
                    self.download_button.setEnabled(True)
            except Exception as e:
                logging.error(f"Failed to get PDB database: {e}")
        else:
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
    # Unmount all NFS mounts before stopping prodj
    unmount_future = asyncio.run_coroutine_threadsafe(prodj.nfs.unmount_all(), prodj.nfs.loop)
    unmount_future.result(timeout=10) # Wait for unmount to complete
    prodj.stop()
    cleanup_databases()
