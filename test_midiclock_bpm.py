#!/usr/bin/env python3
"""
Visual BPM verification test for the rtmidi clock.

Sends MIDI clock at a sequence of BPMs and prints a visible marker
on every beat (every 24 ticks) with a timestamp so you can:
  a) see the beat interval in the terminal
  b) watch your MIDI device LED change speed

Usage:
    python test_midiclock_bpm.py [port_index]

Default port_index=0. Run without args to see available ports,
then pass the index of your USB MIDI device.
"""

import sys
import time
import threading
from prodj.midi.midiclock_rtmidi import MidiClock, list_ports


def run_test(port_index: int):
    ports = list_ports()
    if not ports:
        print("ERROR: No MIDI output ports found.")
        sys.exit(1)

    print("Available ports:")
    for i, p in enumerate(ports):
        marker = " <-- will open" if i == port_index else ""
        print(f"  [{i}] {p}{marker}")
    print()

    clock = MidiClock()
    clock.daemon = True

    beat_times = []
    beat_lock = threading.Lock()

    def on_beat():
        now = time.perf_counter()
        with beat_lock:
            beat_times.append(now)
            n = len(beat_times)
            if n >= 2:
                interval_ms = (beat_times[-1] - beat_times[-2]) * 1000
                expected_ms = (60.0 / current_bpm) * 1000
                error_ms = interval_ms - expected_ms
                bar = "|" * int(min(interval_ms / 50, 40))
                print(f"  Beat {n:4d} | {interval_ms:7.1f}ms "
                      f"(target {expected_ms:.1f}ms, err {error_ms:+.1f}ms) {bar}")
            else:
                print(f"  Beat {n:4d} | -- first beat --")

    clock.set_beat_callback(on_beat)
    clock.open(preferred_port=port_index)

    # Test sequence: (bpm, duration_seconds)
    test_sequence = [
        (120, 4),   # 4 beats @ 120 — baseline
        (60,  4),   # should be clearly half speed
        (30,  6),   # very slow — 2s per beat, obvious on LED
        (120, 4),   # back to 120 — instant response check
        (140, 4),   # faster
        (174, 4),   # typical DnB tempo
    ]

    global current_bpm
    current_bpm = test_sequence[0][0]

    clock.setBpm(current_bpm)
    clock.start()

    print(f"Clock started on port [{port_index}] {ports[port_index]}")
    print("Watch your MIDI device LED — it should change speed at each step.\n")

    for bpm, duration in test_sequence:
        current_bpm = bpm
        clock.setBpm(bpm)
        print(f"\n{'='*60}")
        print(f"  SET BPM = {bpm}  (beat every {60000/bpm:.0f}ms)")
        print(f"{'='*60}")
        time.sleep(duration)

    print("\nTest complete. Stopping clock.")
    clock.stop()


if __name__ == "__main__":
    port_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    run_test(port_index)
