#!/usr/bin/env python3

import logging
import sys
import argparse

from prodj.core.prodj import ProDj

parser = argparse.ArgumentParser(description='Python ProDJ Link Midi Clock')
notes_group = parser.add_mutually_exclusive_group()
notes_group.add_argument('-n', '--notes', action='store_true', help='Send four different note on events depending on the beat')
notes_group.add_argument('-s', '--single-note', action='store_true', help='Send the same note on event on every beat')
parser.add_argument('-l', '--list-ports', action='store_true', help='List available midi ports')
parser.add_argument('-d', '--device', help='MIDI device to use (default: first available device)')
parser.add_argument('-p', '--port', help='MIDI port to use (default: 0)', type=int, default=0)
parser.add_argument('-q', '--quiet', action='store_const', dest='loglevel', const=logging.WARNING, help='Display warning messages only', default=logging.INFO)
parser.add_argument('-D', '--debug', action='store_const', dest='loglevel', const=logging.DEBUG, help='Display verbose debugging information')
parser.add_argument('--note-base', type=int, default=60, help='Note value for first beat')
parser.add_argument('--rtmidi', action='store_true', help='Force use of rtmidi backend (e.g., on Linux if ALSA issues occur). Default on non-Linux.')
parser.add_argument('--alsa', action='store_true', help='Force use of ALSA backend (Linux only).')
args = parser.parse_args()

logging.basicConfig(level=args.loglevel, format='%(levelname)s: %(message)s')

MidiClock = None
alsa_available = False
rtmidi_available = False
selected_backend = None # For logging

# Try ALSA first (if on Linux and not overridden)
if sys.platform.startswith('linux') and not args.rtmidi:
    try:
        from prodj.midi.midiclock_alsaseq import MidiClock as AlsaMidiClock
        alsa_available = True
        if args.alsa or not MidiClock: # Use ALSA if forced or if it's the first choice
            MidiClock = AlsaMidiClock
            selected_backend = "ALSA"
            logging.info("Using ALSA MIDI backend.")
    except ImportError:
        logging.warning("ALSA backend selected or preferred, but 'alsaseq' library not found. Trying rtmidi.")
        if args.alsa: # If ALSA was forced, error out
            logging.error("Exiting: ALSA backend was forced (--alsa) but could not be loaded.")
            sys.exit(1)

# Try rtmidi if ALSA wasn't loaded or if rtmidi is forced
if MidiClock is None or args.rtmidi:
    try:
        from prodj.midi.midiclock_rtmidi import MidiClock as RtMidiClock
        rtmidi_available = True
        MidiClock = RtMidiClock # This will overwrite ALSA if --rtmidi is true
        selected_backend = "rtmidi"
        logging.info("Using rtmidi MIDI backend.")
    except ImportError:
        logging.warning("'rtmidi' library not found.")
        if args.rtmidi: # If rtmidi was forced, error out
            logging.error("Exiting: rtmidi backend was forced (--rtmidi) but could not be loaded.")
            sys.exit(1)

if MidiClock is None:
  logging.error("No suitable MIDI backend could be loaded. Please install 'python-rtmidi' (all platforms) or 'alsaseq' (Linux).")
  sys.exit(1)

c = MidiClock()

if args.list_ports:
  if selected_backend == "ALSA" and alsa_available:
    try:
      # iter_alsa_seq_clients is a method of AlsaMidiClock instance,
      # but it's defined in the class and can be called if we have an instance.
      # However, the original code called it on `c` which was already an instance.
      # Let's ensure `c` is of the correct type or handle it.
      # The original c.iter_alsa_seq_clients() implies c must be an AlsaMidiClock instance.
      if hasattr(c, 'iter_alsa_seq_clients'):
        logging.info("Available ALSA MIDI devices:")
        for id, name, ports in c.iter_alsa_seq_clients():
          logging.info("  Device %d: %s, ports: %s",
            id, name, ', '.join([str(x) for x in ports]))
      else:
        logging.warning("ALSA backend was selected, but port listing method is missing.")
    except Exception as e:
        logging.error(f"Could not list ALSA MIDI ports: {e}")
  elif selected_backend == "rtmidi" and rtmidi_available:
    try:
      midi_out = c.midiout # rtmidi.MidiOut() is instantiated in MidiClock init
      ports = midi_out.get_ports()
      if ports:
        logging.info("Available rtmidi output ports:")
        for i, port_name in enumerate(ports):
          logging.info(f"  Port {i}: {port_name}")
      else:
        logging.info("No rtmidi output ports found.")
    except Exception as e:
        logging.error(f"Could not list rtmidi MIDI ports: {e}")
  else:
    logging.warning("No available backend to list MIDI ports.")
  sys.exit(0)

c.open(args.device, args.port)

p = ProDj()
p.cl.log_played_tracks = False
p.cl.auto_request_beatgrid = False

bpm = 128 # default bpm until reported from player
beat = 0
c.setBpm(bpm)

def update_master(player_number):
  global bpm, beat, p
  client = p.cl.getClient(player_number)
  if client is None or not 'master' in client.state:
    return
  if (args.notes or args.single_note) and beat != client.beat:
    note = args.base_note
    if args.notes:
      note += client.beat
    c.send_note(note)
  newbpm = client.bpm*client.actual_pitch
  if bpm != newbpm:
    c.setBpm(newbpm)
    bpm = newbpm

p.set_client_change_callback(update_master)

try:
  p.start()
  p.vcdj_enable()
  c.start()
  p.join()
except KeyboardInterrupt:
  logging.info("Shutting down...")
  c.stop()
  p.stop()
