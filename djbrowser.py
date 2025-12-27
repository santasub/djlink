#!/usr/bin/env python3

import logging
import sys
import signal
from PyQt5.QtWidgets import QApplication, QWidget, QGridLayout, QLabel, QVBoxLayout, QListWidget, QListWidgetItem, QSplitter
from PyQt5.QtCore import pyqtSignal, Qt
from prodj.core.prodj import ProDj
from prodj.gui.gui_browser import Browser

class MediaListWidget(QListWidget):
    mediaSelected = pyqtSignal(int, str) # player_number, slot

    def __init__(self, parent=None):
        super().__init__(parent)
        self.itemClicked.connect(self.on_item_clicked)

    def add_media(self, player_number, slot, ip_addr):
        item_text = f"Player {player_number}: {slot.upper()} ({ip_addr})"
        item = QListWidgetItem(item_text)
        item.setData(Qt.UserRole, (player_number, slot))
        self.addItem(item)

    def remove_media(self, player_number, slot):
        for i in range(self.count()):
            item = self.item(i)
            p, s = item.data(Qt.UserRole)
            if p == player_number and s == slot:
                self.takeItem(i)
                break

    def on_item_clicked(self, item):
        player_number, slot = item.data(Qt.UserRole)
        self.mediaSelected.emit(player_number, slot)

import collections
import uuid
from dataclasses import dataclass, field
from PyQt5.QtWidgets import (QApplication, QWidget, QGridLayout, QLabel, QVBoxLayout, 
                             QListWidget, QListWidgetItem, QSplitter, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QProgressBar, QPushButton, 
                             QHBoxLayout, QFrame)
from PyQt5.QtCore import pyqtSignal, Qt, QObject, QTimer

# ... (Previous imports remain, ensure they are present or re-imported if needed contextually)

@dataclass
class DownloadItem:
    uuid: str
    player_number: int
    slot: str
    track_id: int
    title: str
    status: str = "Queued" # Queued, Downloading, Finished, Failed, Cancelled
    progress: int = 0
    speed: str = ""
    future: object = None
    mount_path: str = None # Used to match progress callbacks

class DownloadManager(QObject):
    itemAdded = pyqtSignal(object) # DownloadItem
    itemUpdated = pyqtSignal(str) # uuid
    queueStatusChanged = pyqtSignal(bool) # is_paused

    def __init__(self, prodj):
        super().__init__()
        self.prodj = prodj
        self.queue = collections.deque()
        self.active_item = None
        self.items_by_uuid = {}
        self.is_paused = False
        
        # Connect to low-level progress callback
        self.prodj.nfs.set_progress_callback(self.handle_progress)

    def enqueue(self, player_number, slot, track_id, title="Unknown Track"):
        item = DownloadItem(
            uuid=str(uuid.uuid4()),
            player_number=player_number,
            slot=slot,
            track_id=track_id,
            title=title
        )
        self.queue.append(item)
        self.items_by_uuid[item.uuid] = item
        self.itemAdded.emit(item)
        self.process_queue()

    def process_queue(self):
        if self.is_paused or self.active_item:
            return

        if not self.queue:
            return

        # Get next item
        self.active_item = self.queue.popleft()
        self.active_item.status = "Starting..."
        self.itemUpdated.emit(self.active_item.uuid)

        # Start download process
        self.start_download(self.active_item)

    def start_download(self, item):
        # 1. Get Mount Info
        self.prodj.data.get_mount_info(
            item.player_number,
            item.slot,
            item.track_id,
            lambda r, p, s, tid, mi: self.handle_mount_info(item, mi)
        )

    def handle_mount_info(self, item, mount_info):
        if mount_info is None:
            self.fail_item(item, "Mount info missing")
            return
        
        item.mount_path = mount_info.get("mount_path")
        
        # 2. Enqueue NFS Download
        future = self.prodj.nfs.enqueue_download_from_mount_info(
            "mount_info", # request type, assumed
            item.player_number,
            item.slot,
            item.track_id,
            mount_info
        )

        if future:
            item.future = future
            item.status = "Downloading"
            # We access the internal NfsDownload object to get progress if possible, 
            # but usually we only have the future. 
            # nfsclient.enqueue_download returns a future.
            # We might need to look at how to get the NfsDownload object or rely on the future result.
            # Actually, `enqueue_download` returns a Task/Future.
            # We will use the add_done_callback to handle completion.
            future.add_done_callback(lambda f: self.on_download_done(f, item))
            self.itemUpdated.emit(item.uuid)
        else:
            self.fail_item(item, "Failed to start NFS download")

    def handle_progress(self, src_path, progress):
        # Update progress for active item if paths match
        if self.active_item and self.active_item.mount_path == src_path:
            self.active_item.progress = progress
            self.itemUpdated.emit(self.active_item.uuid)

    def on_download_done(self, future, item):
        try:
            result = future.result()
            item.status = "Finished"
            item.progress = 100
        except Exception as e:
            item.status = f"Failed: {str(e)}"
        
        self.itemUpdated.emit(item.uuid)
        self.active_item = None
        self.process_queue()

    def fail_item(self, item, reason):
        item.status = f"Failed: {reason}"
        self.itemUpdated.emit(item.uuid)
        self.active_item = None
        self.process_queue()

    def cancel_item(self, uuid):
        if uuid in self.items_by_uuid:
            item = self.items_by_uuid[uuid]
            if item == self.active_item:
                if item.future:
                    item.future.cancel()
                item.status = "Cancelled"
                self.itemUpdated.emit(uuid)
                self.active_item = None
                self.process_queue()
            else:
                if item in self.queue:
                    self.queue.remove(item)
                    item.status = "Cancelled"
                    self.itemUpdated.emit(uuid)
                elif item.status in ["Failed", "Cancelled"]:
                    # Already cancelled/failed, do nothing or allow remove
                    pass

    def retry_item(self, uuid):
        if uuid in self.items_by_uuid:
            item = self.items_by_uuid[uuid]
            item.status = "Queued"
            item.progress = 0
            self.queue.append(item)
            self.itemUpdated.emit(uuid)
            self.process_queue()

    def remove_item(self, uuid):
        if uuid in self.items_by_uuid:
            del self.items_by_uuid[uuid]
            # Emit signal to remove row from UI
            # For simplicity, we might just need a new signal or handle it in UI
            self.itemUpdated.emit(uuid) # Trigger update, UI checks existence

    def set_paused(self, paused):
        self.is_paused = paused
        self.queueStatusChanged.emit(paused)
        if not paused:
            self.process_queue()


class DownloadQueueWidget(QWidget):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

        # Header with controls
        header_layout = QHBoxLayout()
        self.title_label = QLabel("Download Queue")
        self.title_label.setStyleSheet("font_weight: bold;")
        header_layout.addWidget(self.title_label)
        
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.clicked.connect(self.toggle_pause)
        header_layout.addWidget(self.pause_btn)
        
        header_layout.addStretch()
        self.layout.addLayout(header_layout)

        # Queue Table
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["TRACK TITLE", "STATUS", "PROGRESS", "ACTION"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 100) # Status
        self.table.setColumnWidth(2, 200) # Progress
        self.table.setColumnWidth(3, 150)  # Action - Increased size further to 150
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QTableWidget.NoSelection) # Disable row selection
        self.table.setFocusPolicy(Qt.NoFocus) # Remove focus outline
        self.layout.addWidget(self.table)

        # Connect signals
        self.manager.itemAdded.connect(self.add_row)
        self.manager.itemUpdated.connect(self.update_row)
        self.manager.queueStatusChanged.connect(self.update_pause_btn)

        self.row_map = {} # uuid -> row_index

    def add_row(self, item):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.row_map[item.uuid] = row

        title_item = QTableWidgetItem(item.title)
        title_item.setFlags(Qt.ItemIsEnabled) # Make read-only
        self.table.setItem(row, 0, title_item)
        
        status_item = QTableWidgetItem(item.status)
        status_item.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(row, 1, status_item)
        
        pbar = QProgressBar()
        pbar.setRange(0, 100)
        pbar.setValue(item.progress)
        # Clean progress bar look
        pbar.setTextVisible(False)
        pbar.setFixedHeight(6) 
        
        # Center the pbar vertically in the cell
        pbar_container = QWidget()
        pbar_layout = QVBoxLayout(pbar_container)
        pbar_layout.setContentsMargins(10, 0, 10, 0)
        pbar_layout.setAlignment(Qt.AlignCenter)
        pbar_layout.addWidget(pbar)
        self.table.setCellWidget(row, 2, pbar_container)

        self._create_action_btn(row, item)

    def _create_action_btn(self, row, item):
        btn_text = "CANCEL"
        btn_color = "#ff6b6b" # Red
        callback = lambda: self.manager.cancel_item(item.uuid)

        if item.status in ["Finished"]:
            btn_text = "CLEAR"
            btn_color = "#6b6bff" # Blue
            callback = lambda: self.remove_row(item.uuid)
        elif item.status in ["Cancelled", "Failed"]:
            btn_text = "RETRY"
            btn_color = "#6bff6b" # Green
            callback = lambda: self.manager.retry_item(item.uuid)

        action_btn = QPushButton(btn_text)
        action_btn.setFixedSize(60, 24)
        action_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #3a3a3a;
                color: {btn_color};
                border: 1px solid #555;
                font-size: 10px;
                border-radius: 3px;
            }}
            QPushButton:hover {{
                background-color: #4a4a4a;
                border: 1px solid {btn_color};
            }}
        """)
        action_btn.setCursor(Qt.PointingHandCursor)
        # Disconnect old signals? No, we recreate the widget.
        action_btn.clicked.connect(callback)
        
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(0,0,0,0)
        btn_layout.setAlignment(Qt.AlignCenter)
        btn_layout.addWidget(action_btn)
        self.table.setCellWidget(row, 3, btn_container)

    def remove_row(self, uuid):
        if uuid in self.row_map:
            row = self.row_map[uuid]
            self.table.removeRow(row)
            del self.row_map[uuid]
            self.manager.remove_item(uuid)
            # Rebuild row map because indices shift
            self.row_map = {}
            for r in range(self.table.rowCount()):
                 # This is tricky because we don't store UUID in the table item directly
                 # But we can assume list integrity. 
                 # Better: store uuid in item data.
                 # For now, let's just clear map and re-add? No.
                 # Let's iterate.
                 pass
            # Quick fix: Re-scan table to rebuild map is safest
            # But we didn't store UUID in items. MISTAKE.
            # Let's store UUID in title item data.
            pass
        
        # ACTUALLY, rebuilding the map properly:
        self.rebuild_row_map()

    def rebuild_row_map(self):
        self.row_map = {}
        for r in range(self.table.rowCount()):
            # We need to retrieve the UUID. 
            u_item = self.table.item(r, 0)
            if u_item:
                u = u_item.data(Qt.UserRole)
                if u:
                    self.row_map[u] = r

    def add_row(self, item):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.row_map[item.uuid] = row

        title_item = QTableWidgetItem(item.title)
        title_item.setFlags(Qt.ItemIsEnabled) 
        title_item.setData(Qt.UserRole, item.uuid) # Store UUID here
        self.table.setItem(row, 0, title_item)
        
        status_item = QTableWidgetItem(item.status)
        status_item.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(row, 1, status_item)
        
        pbar = QProgressBar()
        pbar.setRange(0, 100)
        pbar.setValue(item.progress)
        pbar.setTextVisible(False)
        pbar.setFixedHeight(6) 
        
        pbar_container = QWidget()
        pbar_layout = QVBoxLayout(pbar_container)
        pbar_layout.setContentsMargins(10, 0, 10, 0)
        pbar_layout.setAlignment(Qt.AlignCenter)
        pbar_layout.addWidget(pbar)
        self.table.setCellWidget(row, 2, pbar_container)

        self._create_action_btn(row, item)

    def update_row(self, uuid):
        if uuid in self.row_map:
            row = self.row_map[uuid]
            item = self.manager.items_by_uuid[uuid]
            
            self.table.item(row, 1).setText(item.status)
            
            container = self.table.cellWidget(row, 2)
            if container:
                pbar = container.findChild(QProgressBar)
                if pbar:
                    pbar.setValue(item.progress)
            
            # Refresh action button state (e.g. Cancel -> Retry)
            self._create_action_btn(row, item)

    def toggle_pause(self, checked):
        self.manager.set_paused(checked)

    def update_pause_btn(self, paused):
        self.pause_btn.setText("Resume" if paused else "Pause")
        self.pause_btn.setChecked(paused)


class DownloadableBrowser(Browser):
    downloadRequested = pyqtSignal(int, str) # track_id, title

    def downloadTrack(self):
        if self.track_id:
            # Extract metadata from current selection
            idx = self.view.currentIndex()
            if not idx.isValid():
                return
                
            track_id = self.track_id
            if not track_id:
                return

            # Attempt to get Artist - Title from model
            # Columns vary but usually Title is first, Artist second etc.
            # We'll just grab everything we can find.
            row = idx.row()
            model = self.view.model() # Might be proxy
            
            # Helper to get text from column name
            def get_col_text(header_name):
                for c in range(model.columnCount()):
                    if model.headerData(c, Qt.Horizontal) == header_name:
                        return model.data(model.index(row, c))
                return None

            title = get_col_text("Title") or get_col_text("Track") or str(track_id)
            artist = get_col_text("Artist")
            
            if artist:
                full_title = f"{artist} - {title}"
            else:
                full_title = title

            self.downloadRequested.emit(track_id, full_title)

class DjBrowser(QWidget):
    client_change_signal = pyqtSignal(int)

    def __init__(self, prodj):
        super().__init__()
        self.prodj = prodj
        self.download_manager = DownloadManager(prodj)

        self.setWindowTitle('DJ Browser')
        self.resize(1000, 700)
        
        self.layout = QVBoxLayout(self)
        
        # Upper Splitter: Media List | Browser
        self.upper_splitter = QSplitter(Qt.Horizontal)
        
        self.media_list = MediaListWidget()
        self.media_list.mediaSelected.connect(self.open_browser)
        self.upper_splitter.addWidget(self.media_list)
        
        self.browser_container = QWidget()
        self.browser_layout = QVBoxLayout(self.browser_container)
        self.browser_layout.setContentsMargins(0, 0, 0, 0)
        self.current_browser = None
        self.upper_splitter.addWidget(self.browser_container)
        
        self.upper_splitter.setStretchFactor(1, 1)

        # Main Vertical Splitter: Upper Content | Download Queue
        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.addWidget(self.upper_splitter)
        
        self.queue_widget = DownloadQueueWidget(self.download_manager)
        self.main_splitter.addWidget(self.queue_widget)

        self.layout.addWidget(self.main_splitter)

        self.client_change_signal.connect(self.client_change_slot)
        self.prodj.set_client_change_callback(self.client_change_callback)
        self.active_media = set()

    def client_change_callback(self, player_number):
        self.client_change_signal.emit(player_number)

    # ... (client_change_slot and other existing methods remain the same) ...
    def client_change_slot(self, player_number):
        c = self.prodj.cl.getClient(player_number)
        if c is None:
            return
        # USB
        usb_key = (player_number, "usb")
        if c.loaded_slot == "usb":
            if usb_key not in self.active_media:
                logging.info(f"Media found: Player {player_number} USB")
                self.media_list.add_media(player_number, "usb", c.ip_addr)
                self.active_media.add(usb_key)
        else:
            if usb_key in self.active_media:
                 self.media_list.remove_media(player_number, "usb")
                 self.active_media.remove(usb_key)
        # SD
        sd_key = (player_number, "sd")
        if c.loaded_slot == "sd":
            if sd_key not in self.active_media:
                logging.info(f"Media found: Player {player_number} SD")
                self.media_list.add_media(player_number, "sd", c.ip_addr)
                self.active_media.add(sd_key)
        else:
            if sd_key in self.active_media:
                 self.media_list.remove_media(player_number, "sd")
                 self.active_media.remove(sd_key)

    def open_browser(self, player_number, slot):
        if self.current_browser:
            self.browser_layout.removeWidget(self.current_browser)
            self.current_browser.deleteLater()
            self.current_browser = None

        logging.info(f"Opening browser for Player {player_number} {slot}")
        self.current_browser = DownloadableBrowser(self.prodj, player_number)
        
        # Connect the download request to the manager
        self.current_browser.downloadRequested.connect(
            lambda tid, name: self.download_manager.enqueue(player_number, slot, tid, name)
        )
        
        self.browser_layout.addWidget(self.current_browser)
        self.current_browser.slot = slot
        self.current_browser.rootMenu(slot)

def cleanup():
     pass

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='DJ Browser')
    parser.add_argument('--loglevel', default='info')
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.loglevel.upper()), format='%(levelname)-7s %(module)s: %(message)s')

    prodj = ProDj()
    prodj.start()
    prodj.vcdj_set_player_number(5)
    prodj.vcdj_enable()

    # Modern Dark Theme Stylesheet
    # Inspired by professional DJ software aesthetics
    MODERN_STYLESHEET = """
    QWidget {
        background-color: #1e1e1e;
        color: #e0e0e0;
        font-family: 'Segoe UI', '.AppleSystemUIFont', 'Roboto', sans-serif;
        font-size: 13px;
    }

    /* Splitters */
    QSplitter::handle {
        background-color: #333333;
        width: 1px;
    }

    /* Lists and Tables */
    QListWidget, QTableWidget {
        background-color: #252525;
        border: 1px solid #333333;
        border-radius: 4px;
        gridline-color: #333333;
        selection-background-color: #007bff;
        selection-color: white;
    }
    QListWidget::item, QTableWidget::item {
        padding: 8px;
    }
    QListWidget::item:hover, QTableWidget::item:hover {
        background-color: #333333;
    }
    QHeaderView::section {
        background-color: #2d2d2d;
        color: #aaaaaa;
        padding: 6px;
        border: none;
        border-bottom: 1px solid #333333;
        font-weight: bold;
        text-transform: uppercase;
        font-size: 11px;
    }

    /* Buttons */
    QPushButton {
        background-color: #3a3a3a;
        border: none;
        border-radius: 4px;
        padding: 6px 12px;
        color: white;
        font-weight: bold;
    }
    QPushButton:hover {
        background-color: #4a4a4a;
    }
    QPushButton:pressed {
        background-color: #2a2a2a;
    }
    QPushButton:checked {
        background-color: #007bff;
    }

    /* Progress Bar */
    QProgressBar {
        border: none;
        background-color: #333333;
        border-radius: 4px;
        text-align: center;
        color: white;
        font-weight: bold;
    }
    QProgressBar::chunk {
        background-color: #007bff;
        border-radius: 4px;
    }

    /* Labels */
    QLabel {
        color: #e0e0e0;
    }
    """

    app = QApplication(sys.argv)
    app.setStyleSheet(MODERN_STYLESHEET)
    
    window = DjBrowser(prodj)
    window.show()

    signal.signal(signal.SIGINT, lambda s, f: app.quit())

    app.exec()
    prodj.stop()
