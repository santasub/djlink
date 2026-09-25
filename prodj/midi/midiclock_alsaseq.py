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
    self.last_beat_wall_time = None      # wall-clock when beat tick was enqueued
    self._last_beat_queue_s = 0         # queue-time seconds of that beat tick
    self._last_beat_queue_ns = 0        # queue-time nanoseconds of that beat tick
    self._queue_start_wall = None       # wall-clock when alsaseq.start() was called
    self._queue_start_ns = 0            # queue-time at start (always 0)

    # this call causes /proc/asound/seq/clients to be created
    alsaseq.client('MidiClock', 0, 1, True)

  @property
  def delay(self):
    """Thread-safe read of the current tick delay (seconds)."""
    with self._bpm_lock:
      return self._delay

  @property
  def queue_latency_ms(self) -> float:
    """Approximate latency introduced by the ALSA pre-queue in milliseconds.
    The queue holds enqueue_at_once ticks ahead; at current BPM that equals
    enqueue_at_once * delay seconds of lookahead."""
    with self._bpm_lock:
      return self.enqueue_at_once * self._delay * 1000.0

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
      if i % 24 == 0:
        fire_beat = True
        # Capture the queue-time of this beat tick BEFORE advancing
        # This is when this beat actually plays out of the MIDI port
        self._last_beat_queue_s = self.time_s
        self._last_beat_queue_ns = self.time_ns
      send = (36, 1, 0, 0, (self.time_s, self.time_ns), (128,0), (self.client_id, self.client_port), None)
      alsaseq.output(send)
      self.advance_time()
    if fire_beat:
      self.last_beat_wall_time = time.time()
      if self.beat_callback:
        self.beat_callback()

  def send_note(self, note):
    alsaseq.output((6, 0, 0, 0, (0,0), (128,0), (self.client_id, self.client_port), (0,note,127,0,0)))

  def _queue_wall_time_for(self, queue_s: int, queue_ns: int) -> float:
    """Convert an ALSA queue timestamp to wall-clock time.

    We calibrate once at start: wall_start = wall clock when queue starts,
    queue_start = 0. Then any queue time t maps to:
        wall = wall_start + (t - queue_start) = wall_start + t
    """
    if self._queue_start_wall is None:
      return time.time()
    queue_s_total = queue_s + queue_ns / 1e9
    return self._queue_start_wall + queue_s_total

  def next_beat_wall_time(self) -> float:
    """Return wall-clock time of the NEXT upcoming MIDI beat output.

    _last_beat_queue_s/ns is the queue-time of the most recently enqueued
    beat tick.  Since the callback fires ~0.5 beat-periods AFTER that tick
    has already played, the truly next beat is at:

        queue_start_wall + _last_beat_queue_s + beat_period

    We find the smallest N such that
        queue_start_wall + _last_beat_queue_s + N*beat_period > now
    """
    if self._queue_start_wall is None:
      return time.time()
    with self._bpm_lock:
      beat_period_s = self._delay * 24.0
    last_beat_wall = self._queue_wall_time_for(self._last_beat_queue_s,
                                               self._last_beat_queue_ns)
    now = time.time()
    if beat_period_s <= 0:
      return now
    # Find next beat boundary strictly after now
    n = max(1, int((now - last_beat_wall) / beat_period_s) + 1)
    return last_beat_wall + n * beat_period_s

  def run(self):
    logging.info("Starting MIDI clock queue")
    self.enqueue_events()
    self._queue_start_wall = time.time()   # calibration point
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
    """Shift the MIDI clock grid by ms milliseconds using ALSA QUEUE_SKEW.

    Instead of modifying the internal time counter (which only affects future
    enqueue calls and has no effect on already-queued events), we send a
    SND_SEQ_EVENT_QUEUE_SKEW event directly into the queue. ALSA processes
    this event in sequence — when it is reached, the queue speed is changed
    for one tick interval and then restored, producing a real time-shift that
    is audible immediately on the connected device.

    For larger shifts (>= 1 ms) we use SETPOS_TIME to jump the queue clock
    directly, which is instant but causes a brief glitch. For small corrections
    we use SKEW so the shift is smooth (gradual tempo nudge).
    """
    # Also update internal time counter so future enqueue calls are aligned
    with self._bpm_lock:
      delta_ns = int(ms * 1_000_000)
      self.time_ns += delta_ns
      while self.time_ns >= 1_000_000_000:
        self.time_s += 1
        self.time_ns -= 1_000_000_000
      while self.time_ns < 0:
        self.time_s -= 1
        self.time_ns += 1_000_000_000
      current_s = self.time_s
      current_ns = self.time_ns
      delay = self._delay

    abs_ms = abs(ms)

    if abs_ms < 0.1:
      # negligible — skip
      return
    elif abs_ms <= 20.0:
      # Small correction: use QUEUE_SKEW for smooth gradual nudge.
      # Skew ratio: run the queue faster/slower for one beat period,
      # producing a net shift of ms over beat_period_ms.
      # skew = (beat_period + delta) / beat_period  as integer fraction
      beat_period_ns = int(delay * 24 * 1e9)
      if beat_period_ns <= 0:
        return
      delta_ns_total = int(ms * 1_000_000)
      # skew value/base: value = base + delta_ticks
      # Use base=10000 for good resolution
      base = 10000
      skew_val = base + int(delta_ns_total * base / beat_period_ns)
      skew_val = max(1, skew_val)  # must be positive
      # SND_SEQ_EVENT_QUEUE_SKEW = 35
      # data = (skew_value, skew_base, 0, 0, 0, 0)
      alsaseq.output((35, 1, 0, 0, (0, 0), (0, 0),
                      (self.client_id, self.client_port),
                      (skew_val, base, 0, 0, 0, 0)))
      logging.debug("alsaseq: phase skew %.3f ms (skew %d/%d)", ms, skew_val, base)
    else:
      # Large correction: use SETPOS_TIME for immediate jump.
      # SND_SEQ_EVENT_SETPOS_TIME = 31
      # data = (seconds, nanoseconds, 0, 0, 0, 0)
      alsaseq.output((31, 1, 0, 0, (0, 0), (0, 0),
                      (self.client_id, self.client_port),
                      (current_s, current_ns, 0, 0, 0, 0)))
      logging.debug("alsaseq: phase jump %.3f ms -> %d.%09d s", ms, current_s, current_ns)

  def send_start(self):
    """Send MIDI Start (0xFA) scheduled at the next beat boundary in the ALSA queue.

    We schedule the START event at the timestamp of the *next* beat tick
    (i.e. the current queue time_s/time_ns which points to the next tick to
    be enqueued).  This ensures 0xFA arrives at the downstream device exactly
    on the beat, not some random ms later due to Python/GUI thread latency.
    """
    with self._bpm_lock:
      # time_s/time_ns is the timestamp of the NEXT tick to be enqueued.
      # Round back to the nearest beat boundary (multiple of 24 ticks).
      beat_period_ns = int(self._delay * 24 * 1e9)
      t_ns = self.time_s * 1_000_000_000 + self.time_ns
      if beat_period_ns > 0:
        # align to previous beat boundary
        t_ns = (t_ns // beat_period_ns) * beat_period_ns
      sched_s  = t_ns // 1_000_000_000
      sched_ns = t_ns %  1_000_000_000
    # SND_SEQ_EVENT_START = 10, flags=1 (scheduled/tick-time)
    alsaseq.output((10, 1, 0, 0, (sched_s, sched_ns), (0, 0),
                    (self.client_id, self.client_port), (0, 0, 0, 0, 0, 0)))
    logging.info("alsaseq: MIDI Start (0xFA) scheduled at %d.%09d", sched_s, sched_ns)

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
