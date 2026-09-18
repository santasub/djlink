#!/usr/bin/env python3

import threading
from threading import Thread
import time
import math
import alsaseq
import logging
import re

class MidiClock(Thread):
  def __init__(self):
    super().__init__()
    self.keep_running = True
    self.client_id = None
    self.client_port = None
    self.time_s = 0
    self.time_ns = 0
    self._bpm_lock = threading.Lock()    # guards _delay, add_s, add_ns
    self._delay = 60.0/120.0/24.0       # Default 120 BPM
    self.add_s = 0
    self.add_ns = math.floor(1e9 * self._delay)
    self.enqueue_at_once = 24
    self.beat_callback = None

    # this call causes /proc/asound/seq/clients to be created
    alsaseq.client('MidiClock', 0, 1, True)

  @property
  def delay(self):
    """Thread-safe read of the current tick delay (seconds)."""
    with self._bpm_lock:
      return self._delay

  # this may only be called after creating this object
  def iter_alsa_seq_clients(self):
    client_re = re.compile('Client[ ]+(\d+) : "(.*)"')
    port_re = re.compile('  Port[ ]+(\d+) : "(.*)"')
    try:
      with open("/proc/asound/seq/clients", "r") as f:
        id = None
        name = ""
        ports = []
        for line in f:
          match = client_re.match(line)
          if match:
            if id:
              yield (id, name, ports)
            id = int(match.groups()[0])
            name = match.groups()[1]
            ports = []
          else:
            match = port_re.match(line)
            if match:
              ports += [int(match.groups()[0])]
        if id:
          yield (id, name, ports)
    except FileNotFoundError:
      pass

  def open(self, preferred_name=None, preferred_port=0):
    clients_found = False
    for id, name, ports in self.iter_alsa_seq_clients():
      clients_found = True
      logging.debug("midi device %d: %s [%s]", id, name, ','.join([str(x) for x in ports]))
      if (preferred_name is None and name != "Midi Through") or name == preferred_name:
        self.client_id = id
        if preferred_port not in ports:
          preferred_port = ports[0]
          logging.warning("Preferred port not found, using %d", preferred_port)
        self.client_port = preferred_port
        break
    if self.client_id is None:
      if clients_found:
        raise RuntimeError(f"Requested device {preferred_name} not found")
      else:
        raise RuntimeError("No sequencers found")
    logging.info("Using device %s at %d:%d", name, self.client_id, self.client_port)
    alsaseq.connectto(0, self.client_id, self.client_port)

  def advance_time(self):
    with self._bpm_lock:
      add_s = self.add_s
      add_ns = self.add_ns
    self.time_ns += add_ns
    if self.time_ns >= 1000000000:
      self.time_s += 1
      self.time_ns -= 1000000000
    self.time_s = self.time_s + add_s

  def set_beat_callback(self, callback):
      self.beat_callback = callback

  def enqueue_events(self):
    fire_beat = False
    for i in range(self.enqueue_at_once):
      send = (36, 1, 0, 0, (self.time_s, self.time_ns), (128,0), (self.client_id, self.client_port), None)
      alsaseq.output(send)
      if i % 24 == 0:
        fire_beat = True  # note: beat boundary hit; callback fired after loop
      self.advance_time()
    # Fire beat callback outside the enqueue loop so any I/O or locking
    # inside the callback cannot stall the ALSA output queue fill.
    if fire_beat and self.beat_callback:
      self.beat_callback()

  def send_note(self, note):
    alsaseq.output((6, 0, 0, 0, (0,0), (128,0), (self.client_id, self.client_port), (0,note,127,0,0)))

  def run(self):
    logging.info("Starting MIDI clock queue")
    self.enqueue_events()
    alsaseq.start()
    while self.keep_running:
      # not using alsaseq.syncoutput() here, as we would not be fast enough to enqueue more events after
      # the queue has flushed, thus sleep for half the approximate time the queue will need to drain
      time.sleep(self.enqueue_at_once / 2 * self.delay)
      status, time_t, events = alsaseq.status()
      if events >= self.enqueue_at_once:
        #logging.info("more than 24*4 events queued, skipping enqueue")
        continue
      self.enqueue_events()
    alsaseq.stop()
    logging.info("MIDI clock queue stopped")

  def stop(self):
    self.keep_running = False
    self.join()

  def setBpm(self, bpm, pitch_offset=0):
    if bpm <= 0:
      logging.warning("Ignoring zero bpm")
      return
    new_delay = (60.0 / bpm / 24.0) - (pitch_offset / 1000.0)
    if new_delay < 0:
      new_delay = 0.0
    new_add_s = math.floor(new_delay)
    new_add_ns = math.floor(1e9 * (new_delay - new_add_s))
    # Write all three fields atomically under the lock so advance_time
    # never sees a partially-updated (delay, add_s, add_ns) triple.
    with self._bpm_lock:
      self._delay = new_delay
      self.add_s = new_add_s
      self.add_ns = new_add_ns
    logging.debug("alsaseq: BPM=%d pitch_offset=%.2fms delay=%.9fs", bpm, pitch_offset, new_delay)

  def adjust_phase(self, ms):
    """Shifts the MIDI clock grid by ms milliseconds (positive = later, negative = sooner)."""
    with self._bpm_lock:
      delta_ns = int(ms * 1_000_000)
      self.time_ns += delta_ns
      while self.time_ns >= 1_000_000_000:
        self.time_s += 1
        self.time_ns -= 1_000_000_000
      while self.time_ns < 0:
        self.time_s -= 1
        self.time_ns += 1_000_000_000
    logging.debug("alsaseq: phase adjusted %.3f ms (time %d.%09d)", ms, self.time_s, self.time_ns)

  def send_start(self):
    """Send MIDI Start (0xFA) — tells slaved devices to begin playback from position 0."""
    # ALSA sequencer event type 10 = SND_SEQ_EVENT_START
    # data field requires a 6-element tuple (alsaseq C extension requirement)
    alsaseq.output((10, 0, 0, 0, (0, 0), (0, 0), (self.client_id, self.client_port), (0, 0, 0, 0, 0, 0)))
    logging.info("alsaseq: MIDI Start (0xFA) sent")

  def send_stop(self):
    """Send MIDI Stop (0xFC) — tells slaved devices to stop playback."""
    # ALSA sequencer event type 12 = SND_SEQ_EVENT_STOP
    # data field requires a 6-element tuple (alsaseq C extension requirement)
    alsaseq.output((12, 0, 0, 0, (0, 0), (0, 0), (self.client_id, self.client_port), (0, 0, 0, 0, 0, 0)))
    logging.info("alsaseq: MIDI Stop (0xFC) sent")

if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
  mc = MidiClock()
  mc.open("CH345", 0)
  mc.setBpm(175)
  mc.start()
  try:
    mc.join()
  except KeyboardInterrupt:
    mc.stop()
