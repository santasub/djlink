#!/usr/bin/env python3

# Timing approach: absolute-deadline loop with sleep+busywait.
#
# Platform notes:
#   Windows  – default timer granularity ~15ms → we activate timeBeginPeriod(1)
#              to bring it to 1ms, then busywait the last 1ms.
#   macOS    – time.perf_counter() uses mach_absolute_time (nanosecond precision),
#              CoreMIDI scheduling is excellent; busywait guard of 0.1ms is enough.
#   Linux/Pi – prefer the alsaseq backend (kernel-level scheduling, no busywait
#              needed). If rtmidi is used anyway, 0.2ms guard is sufficient.

import sys
import ctypes
from threading import Thread
import time
import rtmidi
import logging

# --- Platform-specific timer setup ---

# Windows: winmm.timeBeginPeriod(1) raises scheduler resolution to 1ms
_winmm = None
if sys.platform == 'win32':
    try:
        _winmm = ctypes.WinDLL('winmm')
    except OSError:
        _winmm = None

def _timer_begin():
    if sys.platform == 'win32' and _winmm:
        _winmm.timeBeginPeriod(1)
        logging.debug("rtmidi: Windows timeBeginPeriod(1) activated")
    # macOS / Linux: nothing to do

def _timer_end():
    if sys.platform == 'win32' and _winmm:
        _winmm.timeEndPeriod(1)
        logging.debug("rtmidi: Windows timeEndPeriod(1) released")

# Busywait guard: how many seconds before the deadline we stop sleeping.
#   Windows  1.0ms  – sleep() granularity with timeBeginPeriod(1)
#   macOS    0.1ms  – mach_absolute_time is very precise, tiny guard needed
#   Linux    0.2ms  – CLOCK_REALTIME, generally fine for non-RT kernel
if sys.platform == 'win32':
    _BUSYWAIT_GUARD_S = 0.001
elif sys.platform == 'darwin':
    _BUSYWAIT_GUARD_S = 0.0001
else:
    _BUSYWAIT_GUARD_S = 0.0002


class MidiClock(Thread):
  def __init__(self, preferred_port=None):
    super().__init__(daemon=True)
    self.keep_running = True
    self.delay = 1.0           # seconds per MIDI tick (set via setBpm)
    self.midiout = rtmidi.MidiOut()
    self.beat_callback = None
    self._phase_offset_s = 0.0  # one-shot phase nudge in seconds

  def open(self, preferred_name=None, preferred_port=0):
    available_ports = self.midiout.get_ports()
    if not available_ports:
      raise Exception("No available midi ports")

    port_index = 0
    logging.debug("Available MIDI ports:")
    for index, port in enumerate(available_ports):
      logging.debug("  [%d] %s", index, port)
      port_split = port.split(':')
      name = port_split[0]
      port_num = port_split[-1]
      if preferred_name is None or (name == preferred_name and port_num == str(preferred_port)):
        port_index = index
    logging.info("rtmidi: opening port index %d (%s)", port_index, available_ports[port_index])
    self.midiout.open_port(port_index)

  def set_beat_callback(self, callback):
    self.beat_callback = callback

  def _sleep_until(self, deadline):
    """Sleep until deadline (perf_counter seconds), using sleep+busywait."""
    remaining = deadline - time.perf_counter()
    if remaining > _BUSYWAIT_GUARD_S:
      time.sleep(remaining - _BUSYWAIT_GUARD_S)
    # busy-wait the last guard interval for precision
    while time.perf_counter() < deadline:
      pass

  def run(self):
    _timer_begin()
    try:
      self._run_loop()
    finally:
      _timer_end()

  def _run_loop(self):
    beat_count = 0
    # Anchor: absolute time of the next tick's deadline
    next_deadline = time.perf_counter()

    while self.keep_running:
      # Send the MIDI clock tick
      self.midiout.send_message([0xF8])

      # Fire beat callback on quarter-note boundaries (every 24 ticks)
      if self.beat_callback and beat_count % 24 == 0:
        self.beat_callback()
      beat_count += 1

      # Advance deadline by one tick period, then apply any pending phase nudge
      next_deadline += self.delay
      phase = self._phase_offset_s
      if phase != 0.0:
        next_deadline += phase
        self._phase_offset_s = 0.0
        logging.debug("rtmidi: phase nudge applied: %.3f ms", phase * 1000)

      self._sleep_until(next_deadline)

  def stop(self):
    self.keep_running = False
    self.join(timeout=2.0)

  def setBpm(self, bpm, pitch_offset=0):
    if bpm <= 0:
      logging.warning("Ignoring zero or negative BPM")
      return
    self.delay = (60.0 / bpm / 24.0) - (pitch_offset / 1000.0)
    if self.delay < 0:
      self.delay = 0.0
    logging.info("rtmidi: BPM=%.2f pitch_offset=%.2fms tick_delay=%.6fs",
                 bpm, pitch_offset, self.delay)

  def adjust_phase(self, ms):
    """Shift the clock grid by ms milliseconds (positive=later, negative=earlier).
    Thread-safe: the value is consumed atomically in the run loop."""
    self._phase_offset_s += ms / 1000.0
    logging.debug("rtmidi: phase nudge scheduled: %.3f ms", ms)

if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
  mc = MidiClock("CH345:CH345 MIDI 1 28:0")
  mc.setBpm(175)
  mc.start()
  try:
    mc.join()
  except KeyboardInterrupt:
    mc.stop()
