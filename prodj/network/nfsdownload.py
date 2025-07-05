import asyncio
import functools
import logging
import os
import time
from concurrent.futures import Future
from enum import Enum

class NfsDownloadType(Enum):
  buffer = 1,
  file = 2,
  failed = 3

class NfsDownload:
  def __init__(self, nfsclient, host, mount_handle, src_path):
    self.nfsclient = nfsclient
    self.host = host # tuple of (ip, port)
    self.mount_handle = mount_handle
    self.src_path = src_path
    self.dst_path = None
    self.fhandle = None # set by lookupCallback
    self.progress = -3
    self.started_at = 0 # set by start
    self.last_write_at = None # Timestamp of the last successful block write
    self.speed = 0
    self.future = Future()

    self.max_in_flight = 4
    self.in_flight = 0
    # Application-level retry for when write_offset seems stuck
    self.single_request_timeout = 5 # Increased timeout for app-level retry decision
    self.max_stuck_retries = 3 # Max times we declare "stuck" and attempt to kickstart
    self.stuck_retry_count = 0

    self.size = 0
    self.read_offset = 0 # How much data we've *requested*
    self.write_offset = 0 # How much data we've *written* (contiguous)
    self.type = NfsDownloadType.buffer
    self.download_buffer = b""
    self.download_file_handle = None

    self.blocks = dict() # Stores received blocks: offset -> data

  async def start(self):
    try:
        lookup_result = await self.nfsclient.NfsLookupPath(self.host, self.mount_handle, self.src_path)
        self.size = lookup_result.attrs.size
        self.fhandle = lookup_result.fhandle
        self.started_at = time.time()
        self.last_write_at = time.time() # Initialize to prevent immediate timeout checks
        self.sendReadRequests()
        return await asyncio.wrap_future(self.future)
    except Exception as e:
        if not self.future.done():
            self.fail_download(f"NfsDownload.start() failed: {str(e)}")
        raise

  def setFilename(self, dst_path=""):
    self.dst_path = dst_path
    if os.path.exists(self.dst_path):
      # Allow overwriting for simplicity in this context, or add specific error.
      # For now, let's assume overwrite is okay or path is unique.
      logging.warning(f"File {self.dst_path} already exists, will be overwritten.")
    dirname = os.path.dirname(self.dst_path)
    if dirname:
      os.makedirs(dirname, exist_ok=True)
    self.download_file_handle = open(self.dst_path, "wb")
    self.type = NfsDownloadType.file

  def sendReadRequest(self, offset):
    if self.future.done() or self.type == NfsDownloadType.failed:
        return 0 # Don't send if download is already finalized

    remaining = self.size - offset
    if remaining <= 0:
        return 0

    chunk = min(self.nfsclient.download_chunk_size, remaining)
    self.in_flight += 1
    # logging.debug(f"Sending read request for offset {offset}, size {chunk}. In-flight: {self.in_flight}")
    task = asyncio.create_task(self.nfsclient.NfsReadData(self.host, self.fhandle, offset, chunk))
    # We pass 'task' itself to the callback to potentially remove it from a tracking set if needed later
    task.add_done_callback(functools.partial(self.readCallback, offset, task))
    return chunk

  def sendReadRequests(self):
    if self.future.done() or self.type == NfsDownloadType.failed or self.write_offset == self.size:
        return

    # Check if we appear stuck (write_offset not advancing)
    if self.write_offset < self.size and (self.write_offset not in self.blocks) and \
       self.last_write_at and (time.time() - self.last_write_at > self.single_request_timeout):
        if self.stuck_retry_count >= self.max_stuck_retries:
            self.fail_download(f"Download stuck at offset {self.write_offset} after {self.max_stuck_retries} checks. Last write at {self.last_write_at}. Timeouts likely occurred.")
            return
        else:
            logging.warning(f"Download may be stuck at offset {self.write_offset} (stuck check {self.stuck_retry_count + 1}/{self.max_stuck_retries}). Pausing sending new requests for this cycle.")
            self.stuck_retry_count += 1
            self.last_write_at = time.time() # Reset timeout for next check
            return # Pause sending new requests, let existing ones resolve or time out

    # Fill pipeline
    while self.in_flight < self.max_in_flight and self.read_offset < self.size:
        if self.future.done() or self.type == NfsDownloadType.failed:
            break
        sent_bytes = self.sendReadRequest(self.read_offset)
        if sent_bytes == 0 and self.read_offset < self.size : # Should not happen if remaining > 0
             logging.warning("sendReadRequest returned 0 bytes to send, but not at EOF.")
             break # Avoid potential infinite loop
        self.read_offset += sent_bytes
        if self.read_offset >= self.size: # All requests for data have been made
             logging.debug("All necessary read requests have been dispatched.")
             break


  def readCallback(self, offset, task_ref, future_obj_from_partial):
    self.in_flight = max(0, self.in_flight - 1)
    # logging.debug(f"readCallback for offset {offset}. In-flight: {self.in_flight}")

    if self.future.done():
        return

    try:
        reply = task_ref.result() # This re-raises exceptions from NfsReadData, like ReceiveTimeout
        # If this offset was the one we were "stuck" on, reset stuck counter
        if offset == self.write_offset:
            self.stuck_retry_count = 0
    except asyncio.CancelledError:
        logging.debug(f"Read task for offset {offset} was cancelled.")
        return
    except Exception as e:
        logging.warning(f"Read request for offset {offset} failed: {e}")
        self.fail_download(f"Failure on read at offset {offset}: {str(e)}")
        return

    if offset >= self.write_offset:
        if offset in self.blocks:
            logging.warning(f"Offset {offset} received (again?), but already in blocks. Ignoring new.")
        else:
            self.blocks[offset] = reply.data
    else: # offset < self.write_offset (already processed this data)
        logging.warning(f"Offset {offset} received but data already written past this point ({self.write_offset}). Ignoring.")

    self.writeBlocks()

    if self.future.done():
        return

    if self.write_offset == self.size:
        # All data has been written. Now we must wait for all *sent* requests to be acknowledged.
        if self.in_flight == 0:
            self.finish()
        else:
            logging.debug(f"All data written ({self.write_offset}/{self.size}), but {self.in_flight} requests still in flight. Waiting for them.")
    else:
      self.sendReadRequests() # Try to send more

  def updateProgress(self, current_written_offset): # Changed arg to be explicit
    # Only update progress based on contiguously written data
    if self.size == 0: return # Avoid division by zero if size isn't known yet
    new_progress = int(100 * current_written_offset / self.size)
    if new_progress > self.progress + 3 or new_progress == 100 or self.progress == -3 : # also update on first block
      self.progress = new_progress
      if time.time() > self.started_at: # Avoid division by zero if time hasn't passed
        self.speed = current_written_offset / (time.time() - self.started_at) / 1024 / 1024
      else:
        self.speed = 0
      logging.info("download progress %d%% (%d/%d Bytes, %.2f MiB/s)",
                   self.progress, current_written_offset, self.size, self.speed)

  def writeBlocks(self):
    while self.write_offset in self.blocks:
      data_block = self.blocks.pop(self.write_offset)
      expected_length = min(self.nfsclient.download_chunk_size, self.size - self.write_offset)
      if len(data_block) != expected_length:
        logging.warning("Received %d bytes for offset %d instead of %d. Clamping or erroring.",
                        len(data_block), self.write_offset, expected_length)
        # This could be a problem. For now, we'll write what we got.
        # data_block = data_block[:expected_length] # Option: truncate

      if self.type == NfsDownloadType.buffer:
        self.download_buffer += data_block
      elif self.type == NfsDownloadType.file:
        self.download_file_handle.write(data_block)

      self.write_offset += len(data_block)
      self.last_write_at = time.time() # Update timestamp of last successful write
      self.updateProgress(self.write_offset) # Update progress based on new write_offset

    # No debug log here about remaining blocks to reduce noise, covered by stuck check.

  def downloadToFileHandler(self, data): # Kept for compatibility if called directly, but writeBlocks is main
    if self.download_file_handle and not self.download_file_handle.closed:
        self.download_file_handle.write(data)

  def downloadToBufferHandler(self, data): # Kept for compatibility
    self.download_buffer += data

  def finish(self):
    if not self.future.done():
        # Ensure all data is accounted for before declaring success
        if self.write_offset != self.size:
            self.fail_download(f"Finish called but not all data written: {self.write_offset}/{self.size}")
            return

        logging.info("finished downloading %s to %s, %d bytes, %.2f MiB/s",
                     self.src_path, self.dst_path, self.write_offset, self.speed)
        if self.in_flight > 0:
             # This should ideally be 0 if finish() is called correctly after all callbacks resolve.
            logging.warning("BUG?: finishing download of %s with %d packets still nominally in flight.", self.src_path, self.in_flight)

        if self.type == NfsDownloadType.buffer:
            self.future.set_result(self.download_buffer)
        elif self.type == NfsDownloadType.file:
            if self.download_file_handle and not self.download_file_handle.closed:
                self.download_file_handle.close()
            self.future.set_result(self.dst_path)
    else:
        logging.warning("finish() called for %s but future was already done. State: %s", self.src_path, self.future)

  def fail_download(self, message="Unknown error"):
    if not self.future.done():
        logging.error("Download failed for %s: %s", self.src_path, message)
        self.type = NfsDownloadType.failed
        # No task cancellation here in this reverted version for now.
        self.future.set_exception(RuntimeError(message))

        if self.type == NfsDownloadType.file and self.download_file_handle and not self.download_file_handle.closed:
            try:
                self.download_file_handle.close()
            except Exception as e:
                logging.error(f"Error closing download file handle during fail_download: {e}")
    else:
        logging.warning("fail_download() called for '%s' (msg: %s) but future was already done. State: %s", self.src_path, message, self.future)

def generic_file_download_done_callback(future):
  if future.exception() is not None:
    logging.error("download failed (callback): %s", future.exception())
