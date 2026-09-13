#!/usr/bin/env python3
"""
Regression tests for MidiClock BPM accuracy and live BPM changes.

No real MIDI device required — midiout.send_message is mocked so the
clock records tick timestamps instead of transmitting to hardware.

Run:
    python -m pytest test_midiclock_bpm.py -v
    # or
    python -m unittest test_midiclock_bpm
"""

import sys
import time
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from prodj.midi.midiclock_rtmidi import MidiClock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOLERANCE_MS = 5.0   # ± 5 ms acceptable for a software clock under CI load
WARMUP_BEATS = 2     # beats to discard after each BPM change before measuring
MEASURE_BEATS = 3    # beats to measure for convergence check


def _make_clock():
    """Return a MidiClock with midiout stubbed out — no real MIDI device needed."""
    clock = MidiClock()
    clock.midiout = MagicMock()
    return clock


def _collect_beat_times(clock, n_beats, timeout=30.0):
    """Register a beat callback, start the clock, collect *n_beats* timestamps.

    Returns a list of perf_counter timestamps, one per beat callback fire.
    Raises AssertionError if not enough beats arrive within *timeout* seconds.
    """
    times = []
    done = threading.Event()

    def _on_beat():
        times.append(time.perf_counter())
        if len(times) >= n_beats:
            done.set()

    clock.set_beat_callback(_on_beat)
    clock.start()
    done.wait(timeout=timeout)
    return times


def _intervals_ms(times):
    """Convert a list of timestamps (seconds) to consecutive intervals in ms."""
    return [(times[i] - times[i - 1]) * 1000.0 for i in range(1, len(times))]


# ---------------------------------------------------------------------------
# Tests: single-BPM accuracy
# ---------------------------------------------------------------------------

class TestMidiClockBpmAccuracy(unittest.TestCase):
    """Verify that the measured beat interval matches the requested BPM."""

    def _assert_converges(self, clock, bpm, warmup=WARMUP_BEATS, measure=MEASURE_BEATS):
        """After *warmup* beats, the next *measure* beat intervals must be
        within TOLERANCE_MS of the expected beat period at *bpm*."""
        expected_ms = 60_000.0 / bpm
        n_total = warmup + measure
        timeout = max(n_total * expected_ms / 1000.0 * 2, 10.0)
        times = _collect_beat_times(clock, n_total, timeout=timeout)
        self.assertEqual(
            len(times), n_total,
            f"Expected {n_total} beats but only got {len(times)} within timeout",
        )
        for i, iv in enumerate(_intervals_ms(times[warmup:])):
            self.assertAlmostEqual(
                iv, expected_ms, delta=TOLERANCE_MS,
                msg=(
                    f"BPM={bpm}: beat interval #{warmup + i + 1} "
                    f"was {iv:.2f} ms, expected {expected_ms:.2f} ± {TOLERANCE_MS} ms"
                ),
            )

    def _run_single_bpm(self, bpm):
        clock = _make_clock()
        clock.setBpm(bpm)
        try:
            self._assert_converges(clock, bpm)
        finally:
            clock.keep_running = False
            clock.join(timeout=3.0)

    def test_bpm_30(self):
        self._run_single_bpm(30)

    def test_bpm_60(self):
        self._run_single_bpm(60)

    def test_bpm_120(self):
        self._run_single_bpm(120)

    def test_bpm_140(self):
        self._run_single_bpm(140)

    def test_bpm_174(self):
        self._run_single_bpm(174)


# ---------------------------------------------------------------------------
# Tests: live BPM changes while the clock is running
# ---------------------------------------------------------------------------

class TestMidiClockLiveBpmChange(unittest.TestCase):
    """Verify that setBpm() while running takes effect within WARMUP_BEATS.

    This is the reported regression: after a stop/start the LED rate changed,
    but live changes during playback were silently dropped.
    """

    def _run_transition(self, bpm_from, bpm_to):
        """Start clock at *bpm_from*, stabilise, then change to *bpm_to* and
        confirm the new interval is reached within WARMUP_BEATS beats."""
        expected_ms = 60_000.0 / bpm_to
        clock = _make_clock()
        clock.setBpm(bpm_from)

        # Phase 1: stabilise at initial BPM
        stabilise_times = []
        phase1_done = threading.Event()

        def _phase1():
            stabilise_times.append(time.perf_counter())
            if len(stabilise_times) >= WARMUP_BEATS:
                phase1_done.set()

        clock.set_beat_callback(_phase1)
        clock.start()
        stabilise_timeout = max(WARMUP_BEATS * 60_000.0 / bpm_from / 1000.0 * 2, 10.0)
        phase1_done.wait(timeout=stabilise_timeout)
        self.assertGreaterEqual(
            len(stabilise_times), WARMUP_BEATS,
            f"Clock did not stabilise at {bpm_from} BPM in time",
        )

        # Phase 2: change BPM and measure convergence
        post_times = []
        phase2_done = threading.Event()
        n_target = WARMUP_BEATS + MEASURE_BEATS

        def _phase2():
            post_times.append(time.perf_counter())
            if len(post_times) >= n_target:
                phase2_done.set()

        clock.set_beat_callback(_phase2)
        clock.setBpm(bpm_to)   # ← the live change under test

        phase2_timeout = max(n_target * expected_ms / 1000.0 * 2, 10.0)
        phase2_done.wait(timeout=phase2_timeout)
        clock.keep_running = False
        clock.join(timeout=3.0)

        self.assertGreaterEqual(
            len(post_times), n_target,
            f"Only {len(post_times)}/{n_target} beats after live change to {bpm_to}",
        )
        for i, iv in enumerate(_intervals_ms(post_times[WARMUP_BEATS:])):
            self.assertAlmostEqual(
                iv, expected_ms, delta=TOLERANCE_MS,
                msg=(
                    f"After live change {bpm_from}→{bpm_to}: interval #{WARMUP_BEATS + i + 1} "
                    f"was {iv:.2f} ms, expected {expected_ms:.2f} ± {TOLERANCE_MS} ms"
                ),
            )

    def test_transition_120_to_60(self):
        self._run_transition(120, 60)

    def test_transition_120_to_140(self):
        self._run_transition(120, 140)

    def test_transition_60_to_174(self):
        self._run_transition(60, 174)

    def test_transition_174_to_30(self):
        self._run_transition(174, 30)

    def test_repeated_same_bpm_does_not_stall(self):
        """Calling setBpm(x) twice in a row must not stall the clock.
        Regression: the old equality guard could leave _delay_changed=False
        so the run loop never re-anchored after the second call."""
        clock = _make_clock()
        clock.setBpm(120)
        clock.setBpm(120)  # duplicate — must not break anything
        times = _collect_beat_times(clock, WARMUP_BEATS + MEASURE_BEATS, timeout=15.0)
        self.assertEqual(len(times), WARMUP_BEATS + MEASURE_BEATS)
        for iv in _intervals_ms(times[WARMUP_BEATS:]):
            self.assertAlmostEqual(iv, 500.0, delta=TOLERANCE_MS)
        clock.keep_running = False
        clock.join(timeout=3.0)

    def test_setBpm_zero_ignored(self):
        """setBpm(0) must be silently ignored and the clock must keep running."""
        clock = _make_clock()
        clock.setBpm(120)
        times = []
        done = threading.Event()

        def _on_beat():
            times.append(time.perf_counter())
            if len(times) == 2:
                clock.setBpm(0)  # must be silently ignored
            if len(times) >= 5:
                done.set()

        clock.set_beat_callback(_on_beat)
        clock.start()
        done.wait(timeout=15.0)
        self.assertGreaterEqual(len(times), 5, "Clock stopped after setBpm(0)")
        clock.keep_running = False
        clock.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Tests: delay property and setBpm internals (no running clock needed)
# ---------------------------------------------------------------------------

class TestMidiClockDelayProperty(unittest.TestCase):
    """Unit tests for the delay property and setBpm internals."""

    def test_delay_reflects_setBpm(self):
        clock = _make_clock()
        clock.setBpm(120)
        self.assertAlmostEqual(clock.delay, 60.0 / 120.0 / 24.0, places=10)

    def test_delay_reflects_pitch_offset(self):
        clock = _make_clock()
        clock.setBpm(120, pitch_offset=1.0)
        expected = (60.0 / 120.0 / 24.0) - (1.0 / 1000.0)
        self.assertAlmostEqual(clock.delay, expected, places=10)

    def test_delay_clamps_to_zero(self):
        """A huge positive pitch_offset must not produce a negative delay."""
        clock = _make_clock()
        clock.setBpm(1, pitch_offset=99999.0)
        self.assertEqual(clock.delay, 0.0)

    def test_delay_changed_always_set_on_repeat_call(self):
        """_delay_changed must be True after every setBpm call, even when the
        computed delay is numerically identical to the previous value.
        (Regression: equality guard silently dropped the flag.)"""
        clock = _make_clock()
        clock.setBpm(120)
        # Simulate the run loop draining the flag
        with clock._bpm_lock:
            clock._delay_changed = False
        # Second identical call — flag must still be raised
        clock.setBpm(120)
        with clock._bpm_lock:
            self.assertTrue(
                clock._delay_changed,
                "_delay_changed must be True after every setBpm call",
            )

    def test_lock_exists_and_is_lock(self):
        clock = _make_clock()
        # threading.Lock() returns a _thread.lock; Lock and RLock share no ABC,
        # but both expose acquire/release.
        self.assertTrue(hasattr(clock._bpm_lock, "acquire"))
        self.assertTrue(hasattr(clock._bpm_lock, "release"))

    def test_internal_delay_field_updated(self):
        clock = _make_clock()
        clock.setBpm(174)
        with clock._bpm_lock:
            self.assertAlmostEqual(clock._delay, 60.0 / 174.0 / 24.0, places=10)


# ---------------------------------------------------------------------------
# Visual / manual runner (invoked when a port index is passed on the CLI)
# ---------------------------------------------------------------------------

def _visual_run(port_index: int):
    """Interactive terminal test — requires a real MIDI device.

    Phase A — PROOF MODE (1 byte/beat, like the handover proof script):
      Sends exactly one 0xF8 per beat with a busy-wait loop.
      Device LED should blink visibly and change speed between BPMs.
      This is the REFERENCE — if the LED does NOT blink here, the
      hardware itself does not respond to 0xF8, which is a different bug.

    Phase B — APP MODE (24 ticks/beat, correct MIDI clock):
      Uses MidiClock thread — same as what the app sends.
      Device LED will blink at 24x the beat rate — looks constant.
      Terminal flash shows measured beat intervals for accuracy check.

    Metrics printed for both phases:
      - Target BPM
      - Measured beat interval (ms) and error vs target
      - Tick interval (ms) — how fast individual bytes hit the wire
      - Min/max jitter over last 8 beats
    """
    import rtmidi
    from prodj.midi.midiclock_rtmidi import list_ports

    ports = list_ports()
    if not ports:
        print("ERROR: No MIDI output ports found.")
        sys.exit(1)

    print("Available ports:")
    for i, p in enumerate(ports):
        marker = " <-- will open" if i == port_index else ""
        print(f"  [{i}] {p}{marker}")
    print()

    # ── Phase A: proof mode — 1 byte per beat ────────────────────────────
    print("=" * 60)
    print("PHASE A — PROOF MODE (1 byte/beat, busy-wait loop)")
    print("  Watch device LED: should blink visibly and change speed.")
    print("  This matches the handover proof script exactly.")
    print("=" * 60)
    time.sleep(1.0)

    proof_out = rtmidi.MidiOut()
    proof_out.open_port(port_index)

    proof_sequence = [(30, 4), (60, 6), (120, 8), (30, 4)]
    for bpm, beats in proof_sequence:
        delay = 60.0 / bpm
        print(f"  {bpm:>3d} BPM — {beats} beats — LED should blink every {delay*1000:.0f} ms")
        t = time.perf_counter()
        for _ in range(beats):
            proof_out.send_message([0xF8])
            t += delay
            while time.perf_counter() < t:
                pass
        print(f"           done.")

    del proof_out
    print("\nPhase A complete. Note LED behaviour above.")
    print("Pausing 2s before Phase B...")
    time.sleep(2.0)

    # ── Phase B: app mode — 24 ticks/beat via MidiClock thread ───────────
    print()
    print("=" * 60)
    print("PHASE B — APP MODE (24 ticks/beat, MidiClock thread)")
    print("  Device LED will look CONSTANT — that is expected.")
    print("  Watch TERMINAL FLASH for beat accuracy instead.")
    print("  Tick interval shown = how fast bytes hit the wire.")
    print("=" * 60)
    time.sleep(1.0)

    clock = MidiClock()

            # Beat state — written only from the clock thread, read from main thread.
    # We avoid a lock in on_beat() entirely so the clock thread is never
    # blocked by terminal I/O in the display loop.
    # Python's GIL makes simple attribute reads/writes effectively atomic for
    # the types used here (int, float, list reference swap).
    state = {
        "beat": 0,
        "last_t": None,
        "intervals": [],   # rolling last-8 beat intervals in ms
        "target_bpm": 120,
    }

    def on_beat():
        """Runs in the clock thread — must not block.  No I/O, no locks."""
        now = time.perf_counter()
        beat = state["beat"] + 1
        last = state["last_t"]
        ivs  = state["intervals"]
        if last is not None:
            ivs = ivs[-7:] + [(now - last) * 1000.0]  # build new list, no mutation
        state["intervals"] = ivs
        state["beat"]   = beat
        state["last_t"] = now

    clock.set_beat_callback(on_beat)
    clock.open(preferred_port=port_index)

    test_sequence = [
        (120, 6),
        (60,  6),
        (30,  8),
        (120, 6),
        (174, 6),
    ]

    state["target_bpm"] = test_sequence[0][0]
    clock.setBpm(state["target_bpm"])
    clock.start()

    print("\033[2J\033[H", end="")  # clear screen
    print(f"Port [{port_index}]: {ports[port_index]}")
    print("Phase A done. Now Phase B — 24 ticks/beat (correct MIDI clock).")
    print("Device LED looks constant = CORRECT.  Watch terminal for beat flash + metrics.")
    print()

    FLASH_HOLD_S = 0.10

    try:
        for bpm, duration in test_sequence:
            clock.setBpm(bpm)
            # Reset display state — write new list object so on_beat() always
            # sees a consistent reference (no in-place .clear() race).
            state["target_bpm"] = bpm
            state["intervals"]  = []
            deadline = time.perf_counter() + duration

            while time.perf_counter() < deadline:
                # Snapshot — cheap reads, no lock needed
                beat_n    = state["beat"]
                intervals = state["intervals"]   # reference copy
                target    = state["target_bpm"]
                last_t    = state["last_t"]

                target_ms  = 60_000.0 / target if target > 0 else 0.0
                tick_ms    = target_ms / 24.0

                age = time.perf_counter() - (last_t or time.perf_counter())
                lit = age < FLASH_HOLD_S

                if intervals:
                    last_iv  = intervals[-1]
                    err_ms   = last_iv - target_ms
                    min_iv   = min(intervals)
                    max_iv   = max(intervals)
                    jitter   = max_iv - min_iv
                    err_col  = "\033[32m" if abs(err_ms) < 2 else ("\033[33m" if abs(err_ms) < 5 else "\033[31m")
                    meas_s   = (f"last={last_iv:>7.1f} ms  "
                                f"err={err_col}{err_ms:>+6.1f}\033[0m ms  "
                                f"jitter={jitter:>5.1f} ms  "
                                f"beat #{beat_n}")
                else:
                    meas_s = "(waiting for first beat...)"

                block = "\033[42m## BEAT ##\033[0m" if lit else "          "

                print(
                    f"\033[5;0H"
                    f"  Target  : {target:>3d} BPM   beat every {target_ms:>6.0f} ms   "
                    f"tick every {tick_ms:>5.2f} ms\n"
                    f"  Flash   : {block}\n"
                    f"  Measured: {meas_s}   \n"
                    f"  LED note: at {target} BPM, device LED gets {24} pulses/beat "
                    f"({24*target/60:.0f} Hz) — looks solid, is CORRECT.   \n",
                    end="", flush=True
                )

                time.sleep(0.02)

    except KeyboardInterrupt:
        pass

    print("\n\nTest complete. Stopping clock.")
    clock.stop()


if __name__ == "__main__":
    # If a numeric argument is given, run the visual test against real hardware.
    # Otherwise, run the unittest suite.
    if len(sys.argv) > 1 and sys.argv[1].lstrip("-").isdigit():
        _visual_run(int(sys.argv[1]))
    else:
        unittest.main()
