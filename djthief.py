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
        # This function is now much simpler. It just needs to trigger the core
        # download function and wire up the progress reporting.

        def progress_callback(downloaded, total):
            # This callback will be called from the asyncio thread, so we need
            # to be careful with GUI updates. We can emit a signal.
            # Here we just update the progress bar's max value and current value.
            if self.parent.progress_bar.maximum() != total:
                self.parent.progress_bar.setMaximum(total)
            self.progress_signal.emit(downloaded)

        # Run the async download logic in the event loop of the nfs client
        future = asyncio.run_coroutine_threadsafe(
            run_download_session(self.prodj, self.player_number, self.slot, progress_callback),
            self.prodj.nfs.loop
        )
        # Add a callback to the future to re-enable the button when done
        future.add_done_callback(lambda f: self.finished_signal.emit())

async def run_download_session(prodj, player_number, slot, progress_callback=None, dry_run=False):
    """
    Core logic for downloading all tracks from a given player and slot.
    Can be used by both CLI and GUI.
    """
    if dry_run:
        logging.info(f"Starting DRY RUN session for player {player_number}:{slot}")
    else:
        logging.info(f"Starting download session for player {player_number}:{slot}")

    # Helper to emit progress if a callback is provided
    def report_progress(downloaded, total):
        if progress_callback:
            progress_callback(downloaded, total)

    try:
        client = prodj.cl.getClient(player_number)
        if not client:
            logging.error(f"Player {player_number} not found.")
            return

        if slot == "usb":
            track_count = client.usb_info.get("track_count", 0)
        elif slot == "sd":
            track_count = client.sd_info.get("track_count", 0)
        else:
            track_count = 0

        if track_count == 0:
            logging.info("No tracks to download.")
            report_progress(0, 0)
            return

        logging.info(f"Found {track_count} tracks to download.")
        report_progress(0, track_count)

        all_tracks = []
        chunk_size = 100
        for i in range(0, track_count, chunk_size):
            tracks = prodj.data.dbc.query_list(player_number, slot, "title", [i, chunk_size], "title_request")
            if tracks:
                all_tracks.extend(tracks)
            else:
                break

        tracks_downloaded = 0

        async def download_track(semaphore, track):
            nonlocal tracks_downloaded
            async with semaphore:
                try:
                    if dry_run:
                        logging.info(f"[DRY RUN] Would download: {track.get('mount_path')}")
                        await asyncio.sleep(0.01) # Simulate some work
                        tracks_downloaded += 1
                        report_progress(tracks_downloaded, track_count)
                        return

                    # This logic remains complex due to the underlying library's structure
                    loop = asyncio.get_running_loop()
                    aio_future = loop.create_future()

                    def download_done_callback(f):
                        nonlocal tracks_downloaded
                        if f.exception() is not None:
                            logging.error(f"Download failed for track {track.get('track_id')}: {f.exception()}")
                            loop.call_soon_threadsafe(aio_future.set_exception, f.exception())
                        else:
                            logging.info(f"Download finished: {f.result()}")
                            loop.call_soon_threadsafe(aio_future.set_result, f.result())

                        tracks_downloaded += 1
                        report_progress(tracks_downloaded, track_count)

                    enqueue_future_future = prodj.data.get_mount_info(
                        player_number, slot, track['track_id'], prodj.nfs.enqueue_download_from_mount_info
                    )

                    def on_enqueue_done(f):
                        nonlocal tracks_downloaded
                        try:
                            download_future = f.result()
                            if download_future:
                                download_future.add_done_callback(download_done_callback)
                            else:
                                exc = RuntimeError(f"Failed to enqueue download for track {track.get('track_id')}.")
                                loop.call_soon_threadsafe(aio_future.set_exception, exc)
                                tracks_downloaded += 1
                                report_progress(tracks_downloaded, track_count)
                        except Exception as e:
                            logging.error(f"Error during enqueue for track {track.get('track_id')}: {e}")
                            loop.call_soon_threadsafe(aio_future.set_exception, e)
                            tracks_downloaded += 1
                            report_progress(tracks_downloaded, track_count)

                    enqueue_future_future.add_done_callback(on_enqueue_done)
                    await aio_future
                except Exception as e:
                    logging.error(f"Failed to process download for track {track.get('track_id')}: {e}")

        semaphore = asyncio.Semaphore(4)
        tasks = [download_track(semaphore, track) for track in all_tracks]
        await asyncio.gather(*tasks)

    except Exception as e:
        logging.error(f"An error occurred during the download session: {e}")
    finally:
        logging.info("Download session finished.")


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
    parser.add_argument('--no-gui', action='store_true', help="Run in command-line mode without a GUI.")
    parser.add_argument('--player', type=int, help="Player number to target in CLI mode.")
    parser.add_argument('--slot', choices=['usb', 'sd'], help="Media slot to target in CLI mode.")
    parser.add_argument('--mock-player', action='store_true', help="Use a mock player for testing.")
    parser.add_argument('--list', action='store_true', help="List available players and exit.")
    parser.add_argument('--dry-run', action='store_true', help="List tracks that would be downloaded, but don't download them.")
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

    if args.mock_player:
        # Create a mock client and add it to the client list
        from unittest.mock import Mock, patch
        mock_client = Mock()
        mock_client.player_number = args.player or 2
        mock_client.ip_addr = "192.168.1.99"
        mock_client.loaded_slot = args.slot or "usb"
        mock_client.usb_info = {"track_count": 2}
        mock_client.sd_info = {"track_count": 0}

        # Directly mock the getClient method
        prodj.cl.getClient = Mock(return_value=mock_client)

        # Mock the db query to return some dummy tracks
        def mock_query_list(player, slot, query_type, params, request_id):
            return [
                {'track_id': 1, 'mount_path': '/test/track1.wav'},
                {'track_id': 2, 'mount_path': '/test/track2.wav'}
            ]
        prodj.data.dbc.query_list = mock_query_list

        # Mock the NFS download to avoid real network calls
        async def mock_handle_download(ip, slot, src_path, dst_path):
            logging.info(f"[MOCK] 'Downloading' {src_path} to {dst_path}")
            # Create a dummy file
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            with open(dst_path, 'w') as f:
                f.write(f"mock content for {src_path}")
            return dst_path
        prodj.nfs.handle_download = mock_handle_download


    if args.no_gui:
        if not args.player or not args.slot:
            parser.error("--player and --slot are required when using --no-gui")

        logging.info(f"Running in CLI mode for player {args.player} and slot {args.slot}")

        # This is a bit tricky since we need to run an async function from a sync context
        # and wait for it. We can create a temporary asyncio loop for this.
        async def cli_main():
            if args.list:
                logging.info("Listing available players...")
                # Wait a few seconds for players to announce themselves
                await asyncio.sleep(5)
                players = prodj.cl.clients
                if not players:
                    print("No players found on the network.")
                    return

                print(f"{'Player #':<10} {'Model':<15} {'IP Address':<18} {'Media'}")
                print(f"{'-'*8:<10} {'-'*13:<15} {'-'*16:<18} {'-'*5}")
                for p in sorted(players, key=lambda x: x.player_number):
                    media_info = []
                    if p.usb_state == 'loaded':
                        media_info.append(f"USB ({p.usb_info.get('track_count', 'N/A')} tracks)")
                    if p.sd_state == 'loaded':
                        media_info.append(f"SD ({p.sd_info.get('track_count', 'N/A')} tracks)")

                    print(f"{p.player_number:<10} {p.model:<15} {p.ip_addr:<18} {', '.join(media_info) or 'None'}")
                return

            # Wait for the player to appear
            if not args.player or not args.slot:
                parser.error("--player and --slot are required for downloading.")

            player = None
            logging.info("Waiting for player to appear...")
            while player is None:
                player = prodj.cl.getClient(args.player)
                if player and player.loaded_slot == args.slot:
                    logging.info(f"Player {args.player} with slot {args.slot} found.")
                    break
                await asyncio.sleep(1)

            # Define a simple progress bar for the console
            def console_progress(downloaded, total):
                if total > 0:
                    percent = int(downloaded / total * 100)
                    bar = '#' * (percent // 2) + ' ' * (50 - (percent // 2))
                    sys.stdout.write(f"\r[{bar}] {percent}% ({downloaded}/{total})")
                    sys.stdout.flush()

            # Run the download session
            await run_download_session(prodj, args.player, args.slot, console_progress, args.dry_run)
            if not args.dry_run:
                print("\nDownload complete.")

        try:
            # We need to run our async main in the prodj nfs loop
            cli_future = asyncio.run_coroutine_threadsafe(cli_main(), prodj.nfs.loop)
            cli_future.result() # Wait for the CLI main task to complete
        except KeyboardInterrupt:
            logging.info("CLI mode interrupted by user.")
        except Exception as e:
            logging.error(f"An error occurred in CLI mode: {e}")
        finally:
            logging.info("CLI mode finished. Shutting down...")
            unmount_future = asyncio.run_coroutine_threadsafe(prodj.nfs.unmount_all(), prodj.nfs.loop)
            unmount_future.result(timeout=10)
            prodj.stop()
            cleanup_databases()

    else:
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
