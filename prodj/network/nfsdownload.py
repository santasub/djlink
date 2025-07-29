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
    self.single_request_timeout = 10
    self.max_stuck_retries = 3
    self.stuck_retry_count = 0

    self.size = 0
    self.read_offset = 0
    self.write_offset = 0
    self.type = NfsDownloadType.buffer
    self.download_buffer = b""
    self.download_file_handle = None

    self.blocks = dict()
    self.active_read_tasks = set() # Re-added for task cancellation

  async def start(self):
    try:
        lookup_result = await self.nfsclient.NfsLookupPath(self.host, self.mount_handle, self.src_path)
        self.size = lookup_result.attrs.size
        if self.size == 0: # Handle zero-byte files
            logging.info(f"File {self.src_path} is zero bytes. Download considered complete.")
            self.finish() # Call finish directly
            return await asyncio.wrap_future(self.future)

        self.fhandle = lookup_result.fhandle
        self.started_at = time.time()
        self.last_write_at = time.time()
        self.sendReadRequests()
        return await asyncio.wrap_future(self.future)
    except Exception as e:
        if not self.future.done():
            self.fail_download(f"NfsDownload.start() failed: {str(e)}")
        raise

  def setFilename(self, dst_path=""):
    self.dst_path = dst_path
    if os.path.exists(self.dst_path):
      logging.warning(f"File {self.dst_path} already exists, will be overwritten.")
    dirname = os.path.dirname(self.dst_path)
    if dirname:
      os.makedirs(dirname, exist_ok=True)
    self.download_file_handle = open(self.dst_path, "wb")
    self.type = NfsDownloadType.file

  def sendReadRequest(self, offset):
    if self.future.done() or self.type == NfsDownloadType.failed:
        return 0

    remaining = self.size - offset
    if remaining <= 0:
        return 0

    chunk = min(self.nfsclient.download_chunk_size, remaining)
    self.in_flight += 1
    task = asyncio.create_task(self.nfsclient.NfsReadData(self.host, self.fhandle, offset, chunk))
    self.active_read_tasks.add(task) # Track active task
    task.add_done_callback(functools.partial(self.readCallback, offset, task))
    return chunk

  def sendReadRequests(self):
    if self.future.done() or self.type == NfsDownloadType.failed or self.write_offset == self.size:
        return

    if self.write_offset < self.size and (self.write_offset not in self.blocks) and \
       self.last_write_at and (time.time() - self.last_write_at > self.single_request_timeout):
        if self.stuck_retry_count >= self.max_stuck_retries:
            self.fail_download(f"Download stuck at offset {self.write_offset} after {self.max_stuck_retries} checks. Timeouts likely occurred.")
            return
        else:
            logging.warning(f"Download may be stuck at offset {self.write_offset} (stuck check {self.stuck_retry_count + 1}/{self.max_stuck_retries}). Pausing sending new requests for this cycle.")
            self.stuck_retry_count += 1
            self.last_write_at = time.time()
            return

    while self.in_flight < self.max_in_flight and self.read_offset < self.size:
        if self.future.done() or self.type == NfsDownloadType.failed:
            break
        sent_bytes = self.sendReadRequest(self.read_offset)
        if sent_bytes == 0 and self.read_offset < self.size :
             logging.warning("sendReadRequest returned 0 bytes to send, but not at EOF.")
             break
        self.read_offset += sent_bytes
        if self.read_offset >= self.size:
             logging.debug("All necessary read requests have been dispatched.")
             break

  def readCallback(self, offset, task_ref, future_obj_from_partial):
    if task_ref in self.active_read_tasks: # Remove task from tracking
        self.active_read_tasks.remove(task_ref)

    self.in_flight = max(0, self.in_flight - 1)

    if self.future.done():
        return

    try:
        reply = task_ref.result()
        if offset == self.write_offset: # Successfully got the block we were waiting for
            self.stuck_retry_count = 0
    except asyncio.CancelledError:
        logging.debug(f"Read task for offset {offset} was cancelled.")
        # If cancellation was triggered by fail_download, future is already done.
        # If cancelled externally for some reason, this download might still be viable if other tasks complete.
        # However, usually, cancellation means the whole operation is stopping.
        # We don't call fail_download here to avoid recursion if fail_download initiated cancellation.
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
    else:
        logging.warning(f"Offset {offset} received but data already written past this point ({self.write_offset}). Ignoring.")

    self.writeBlocks()

    if self.future.done():
        return

    if self.write_offset == self.size:
        if self.in_flight == 0: # All acknowledged
            self.finish()
        else:
            logging.debug(f"All data written ({self.write_offset}/{self.size}), but {self.in_flight} requests still in flight. Waiting for them.")
    else:
      if not self.future.done(): # Check again before sending more
          self.sendReadRequests()

  def updateProgress(self, current_written_offset):
    if self.size == 0: return
    new_progress = int(100 * current_written_offset / self.size)
    if new_progress > self.progress + 3 or new_progress == 100 or self.progress == -3 :
      self.progress = new_progress
      current_time = time.time()
      if current_time > self.started_at:
        self.speed = current_written_offset / (current_time - self.started_at) / 1024 / 1024
      else:
        self.speed = 0
      logging.info("download progress %d%% (%d/%d Bytes, %.2f MiB/s)",
                   self.progress, current_written_offset, self.size, self.speed)

  def writeBlocks(self):
    while self.write_offset in self.blocks:
      data_block = self.blocks.pop(self.write_offset)
      expected_length = min(self.nfsclient.download_chunk_size, self.size - self.write_offset)
      if len(data_block) != expected_length:
        logging.warning("Received %d bytes for offset %d instead of %d. Using received length.",
                        len(data_block), self.write_offset, expected_length)

      if self.type == NfsDownloadType.buffer:
        self.download_buffer += data_block
      elif self.type == NfsDownloadType.file:
        if self.download_file_handle and not self.download_file_handle.closed:
            self.download_file_handle.write(data_block)
        else:
            logging.error("Attempted to write to a closed or non-existent file handle.")
            self.fail_download("File handle error during write.")
            return # Stop writing if file handle is bad

      self.write_offset += len(data_block)
      self.last_write_at = time.time()
      self.updateProgress(self.write_offset)

  def downloadToFileHandler(self, data):
    if self.download_file_handle and not self.download_file_handle.closed:
        self.download_file_handle.write(data)

  def downloadToBufferHandler(self, data):
    self.download_buffer += data

  def finish(self):
    if not self.future.done():
        if self.write_offset != self.size and self.size != 0 : # Allow finish for 0-byte files even if write_offset is 0
            self.fail_download(f"Finish called but not all data written: {self.write_offset}/{self.size}")
            return

        logging.info("finished downloading %s to %s, %d bytes, %.2f MiB/s",
                     self.src_path, self.dst_path, self.write_offset, self.speed)
        if self.in_flight > 0:
            logging.warning("Finishing download of %s with %d packets still nominally in flight (these will be cancelled or timeout).", self.src_path, self.in_flight)
            # Cancel remaining tasks as a cleanup, though they shouldn't block success if all data is here.
            for task in list(self.active_read_tasks):
                if not task.done():
                    task.cancel()
            self.active_read_tasks.clear()


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

        # Cancel any outstanding read tasks
        # logging.debug(f"fail_download: Cancelling {len(self.active_read_tasks)} active tasks.")
        for task in list(self.active_read_tasks): # Iterate over a copy
            if not task.done():
                # logging.debug(f"Cancelling task: {task}")
                task.cancel()
        self.active_read_tasks.clear()

        self.future.set_exception(RuntimeError(message))

        if self.type == NfsDownloadType.file and self.download_file_handle and not self.download_file_handle.closed:
            try:
                self.download_file_handle.close()
            except Exception as e:
                logging.error(f"Error closing download file handle during fail_download: {e}")
    else:
        # Only log if the message is different, to avoid spam if fail_download is called multiple times
        # for the same underlying issue by different callbacks before future.done() propagates.
        # This is hard to check perfectly without storing the original exception on self.future.
        logging.debug("fail_download() called for '%s' (msg: %s) but future was already done. State: %s", self.src_path, message, self.future)

def generic_file_download_done_callback(future):
  if future.exception() is not None:
    logging.error("download failed (callback): %s", future.exception())
