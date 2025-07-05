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
    self.last_write_at = None
    self.speed = 0
    self.future = Future()

    self.max_in_flight = 4 # values > 4 did not increase read speed in my tests
    self.in_flight = 0
    self.single_request_timeout = 2 # retry read after n seconds
    self.max_read_retries = 5
    self.read_retries = 0

    self.size = 0
    self.read_offset = 0
    self.write_offset = 0
    self.type = NfsDownloadType.buffer
    self.download_buffer = b""
    self.download_file_handle = None

    # maps offset -> data of blocks, written
    # when continuously available
    self.blocks = dict()

  async def start(self):
    lookup_result = await self.nfsclient.NfsLookupPath(self.host, self.mount_handle, self.src_path)
    self.size = lookup_result.attrs.size
    self.fhandle = lookup_result.fhandle
    self.started_at = time.time()
    self.sendReadRequests()
    # Ensure the future is awaited or handled to prevent "never retrieved" if start() itself raises an exception early.
    try:
        return await asyncio.wrap_future(self.future)
    except Exception as e:
        # If handle_download (and thus start()) itself fails before or during sendReadRequests,
        # ensure the future is set. fail_download is usually called from callbacks.
        if not self.future.done():
            self.fail_download(f"NfsDownload.start() failed: {str(e)}")
        raise # Re-raise the exception


  def setFilename(self, dst_path=""):
    self.dst_path = dst_path

    if os.path.exists(self.dst_path):
      raise FileExistsError(f"file already exists: {self.dst_path}")

    # create download directory if nonexistent
    dirname = os.path.dirname(self.dst_path)
    if dirname:
      os.makedirs(dirname, exist_ok=True)

    self.download_file_handle = open(self.dst_path, "wb")
    self.type = NfsDownloadType.file

  def sendReadRequest(self, offset):
    remaining = self.size - offset
    chunk = min(self.nfsclient.download_chunk_size, remaining)
    # logging.debug("sending read request @ %d for %d bytes [%d in flight]", offset, chunk, self.in_flight)
    self.in_flight += 1
    task = asyncio.create_task(self.nfsclient.NfsReadData(self.host, self.fhandle, offset, chunk))
    task.add_done_callback(functools.partial(self.readCallback, offset))
    return chunk

  def sendReadRequests(self):
    if self.future.done() or self.type == NfsDownloadType.failed: # Stop sending if already done/failed
        return

    # Check for overall timeout on a specific block being waited for
    if self.last_write_at is not None and \
       self.write_offset < self.size and \
       (self.write_offset not in self.blocks) and \
       (self.last_write_at + self.single_request_timeout * (self.read_retries + 1) < time.time()):
        # This condition means we've been waiting for self.write_offset for too long across potential retries.
        # The original retry logic was a bit simplistic. Let's refine.
        # If a block (self.write_offset) hasn't arrived and we've exceeded total timeout for it.
        if self.read_retries >= self.max_read_retries:
            self.fail_download(f"Read for offset {self.write_offset} ultimately timed out after {self.max_read_retries +1} attempts period.")
            return
        else:
            logging.warning(f"Read at offset {self.write_offset} appears stuck or timed out. Issuing explicit retry {self.read_retries + 1}/{self.max_read_retries + 1}.")
            # Re-request the specific block that's missing, if no request for it is currently in_flight
            # This is complex because in_flight is just a count. We don't track *which* offset is in_flight.
            # For now, we'll just increment retry count and let the pipeline try to fill.
            # A more advanced retry would re-send self.sendReadRequest(self.write_offset)
            # but we must be careful not to increment in_flight if one is already pending for this offset.
            # The current retry logic in original code was based on any last_write_at, not specific block.
            # Let's stick to a simpler global retry count for now, but log better.
            # The original code's retry: self.sendReadRequest(self.write_offset) - this could over-request.
            # Let's assume for now the pipeline fills and eventually the timed out XID from RpcReceiver handles it.
            # The primary goal of this section is to ensure we don't *indefinitely* send new requests if stuck.
            self.read_retries += 1 # Count this as a retry attempt for the overall download
            # The RpcReceiver handles individual RPC timeouts. This is more about NfsDownload progress.


    # Fill pipeline
    while self.in_flight < self.max_in_flight and self.read_offset < self.size:
      if self.future.done() or self.type == NfsDownloadType.failed: # Check again inside loop
          break
      self.read_offset += self.sendReadRequest(self.read_offset)

  def readCallback(self, offset, task):
    self.in_flight = max(0, self.in_flight - 1) # Decrement when callback is entered

    if self.future.done(): # If already failed or finished by another callback, do nothing more.
        # logging.debug(f"readCallback for offset {offset}: future already done. Current in_flight: {self.in_flight}")
        return

    try:
        reply = task.result() # This is where ReceiveTimeout would be raised
        # If successful, reset read_retries for the *overall* download progress if this was the current write_offset
        if offset == self.write_offset:
            self.read_retries = 0
    except Exception as e:
        logging.warning(f"Read request for offset {offset} failed: {e}")
        # Check if this failure should terminate the download
        # For now, we let RpcReceiver timeouts propagate and potentially fail the whole download via fail_download.
        # If it's a critical failure (e.g. too many retries managed by RpcReceiver or a fatal NFS error):
        self.fail_download(f"Critical failure on read at offset {offset}: {str(e)}")
        return

    # Only process block if it's relevant and not already processed
    if offset >= self.write_offset : # Process if it's the current one or a future one
        if offset in self.blocks:
             logging.warning(f"Offset {offset} received twice (data already in blocks dict), ignoring new data.")
        else:
            self.blocks[offset] = reply.data
    else: # offset < self.write_offset
      logging.warning(f"Offset {offset} received but already written past this point ({self.write_offset}), ignoring.")

    self.writeBlocks() # Attempt to write any contiguous blocks

    if self.future.done(): # Check again, as writeBlocks might have called finish/fail
        return

    if self.write_offset == self.size:
        # All data has been written.
        if self.in_flight == 0: # All sent requests have been acknowledged (success or fail)
            self.finish()
        else:
            # All data is written, but some requests are outstanding (e.g. for already processed blocks due to retries or aggressive pipelining)
            # These should eventually timeout or complete without affecting the result if data is all there.
            logging.debug(f"All data written ({self.write_offset}/{self.size}), but {self.in_flight} requests still nominally in flight. Waiting for them to clear.")
    else: # Not all data written yet
      self.sendReadRequests() # Try to send more requests if pipeline has space

  def updateProgress(self, offset):
    new_progress = int(100*offset/self.size)
    if new_progress > self.progress+3 or new_progress == 100:
      self.progress = new_progress
      self.speed = offset/(time.time()-self.started_at)/1024/1024
      logging.info("download progress %d%% (%d/%d Bytes, %.2f MiB/s)",
        self.progress, offset, self.size, self.speed)

  def writeBlocks(self):
    # logging.debug("writing %d blocks @ %d [%d in flight]",
    #   len(self.blocks), self.write_offset, self.in_flight)
    while self.write_offset in self.blocks:
      data = self.blocks.pop(self.write_offset)
      expected_length = min(self.nfsclient.download_chunk_size, self.size-self.write_offset)
      if len(data) != expected_length:
        logging.warning("Received %d bytes instead %d as requested. Try to decrease "\
          "the download chunk size!", len(data), expected_length)
      if self.type == NfsDownloadType.buffer:
        self.downloadToBufferHandler(data)
      elif self.type == NfsDownloadType.file:
        self.downloadToFileHandler(data)
      else:
        logging.debug("dropping write @ %d", self.write_offset)
      self.write_offset += len(data)
      self.last_write_at = time.time()
    if len(self.blocks) > 0:
      # To get an arbitrary key if needed for debugging, convert to list first
      # For example: list(self.blocks.keys())[0]
      # However, the primary logic relies on checking `self.write_offset in self.blocks`
      logging.debug("%d blocks still in queue, next expected is %d",
        len(self.blocks), self.write_offset)

  def downloadToFileHandler(self, data):
    self.download_file_handle.write(data)

  def downloadToBufferHandler(self, data):
    self.download_buffer += data

  def finish(self):
    logging.info("finished downloading %s to %s, %d bytes, %.2f MiB/s",
      self.src_path, self.dst_path, self.write_offset, self.speed)
    if self.in_flight > 0:
      logging.error("BUG: finishing download of %s but packets are still in flight (%d)", self.src_path, self.in_flight)

    if not self.future.done():
        if self.type == NfsDownloadType.buffer:
            self.future.set_result(self.download_buffer)
        elif self.type == NfsDownloadType.file:
            if self.download_file_handle and not self.download_file_handle.closed:
                self.download_file_handle.close()
            self.future.set_result(self.dst_path)
        # If type is failed, fail_download should have set the exception.
    else:
        logging.warning("finish() called but future was already done. State: %s", self.future)


  def fail_download(self, message="Unknown error"):
    if not self.future.done():
        logging.error("Download failed: %s", message) # Log the actual failure reason
        self.type = NfsDownloadType.failed
        self.future.set_exception(RuntimeError(message))
        # Optionally, attempt to close file handle if it was open
        if self.type == NfsDownloadType.file and self.download_file_handle and not self.download_file_handle.closed:
            try:
                self.download_file_handle.close()
            except Exception as e:
                logging.error(f"Error closing download file handle during fail_download: {e}")
    else:
        logging.warning("fail_download() called for '%s' but future was already done. State: %s", message, self.future)


def generic_file_download_done_callback(future):
  if future.exception() is not None:
    logging.error("download failed: %s", future.exception())
