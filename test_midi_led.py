#!/usr/bin/env python3
"""
Test whether the MIDI device LED responds to:
  a) MIDI Clock (0xF8) - what we send
  b) Note On/Off (0x90/0x80) - what many devices blink to
  c) Active Sensing (0xFE)

Usage: python test_midi_led.py [port_index]
"""
import sys
import time
from prodj.midi.midiclock_rtmidi import list_ports
import rtmidi

def test(port_index: int):
    ports = list_ports()
    print("Ports:", ports)

    out = rtmidi.MidiOut()
    out.open_port(port_index)
    print(f"Opened: {ports[port_index]}\n")

    bpms = [120, 60, 30]

    for bpm in bpms:
        tick_interval = 60.0 / bpm / 24.0
        print(f"--- {bpm} BPM (tick every {tick_interval*1000:.1f}ms, beat every {60000/bpm:.0f}ms) ---")
        print("  Sending MIDI Clock 0xF8 for 4 beats...")
        ticks = int(4 * 24)
        start = time.perf_counter()
        next_t = start
        for _ in range(ticks):
            out.send_message([0xF8])
            next_t += tick_interval
            remaining = next_t - time.perf_counter()
            if remaining > 0.001:
                time.sleep(remaining - 0.001)
            while time.perf_counter() < next_t:
                pass
        print(f"  Done. ({time.perf_counter()-start:.2f}s elapsed, expected {4*60/bpm:.2f}s)")

        print("  Now sending Note On/Off at same beat rate for 4 beats...")
        beat_interval = 60.0 / bpm
        next_t = time.perf_counter()
        for _ in range(4):
            out.send_message([0x90, 36, 100])  # Note On, C2, vel 100
            time.sleep(0.05)
            out.send_message([0x80, 36, 0])    # Note Off
            next_t += beat_interval
            remaining = next_t - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)

        print("  Done.\n")
        time.sleep(0.5)

    del out
    print("Test complete.")

if __name__ == "__main__":
    port_index = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    test(port_index)
