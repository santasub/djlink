#!/usr/bin/env python3

import curses
import logging

from prodj.core.prodj import ProDj
from prodj.curses.loghandler import CursesHandler

#default_loglevel=logging.DEBUG
default_loglevel=logging.INFO

import sys

# init curses
win = curses.initscr()
curses.start_color() # Enable color if needed, though not explicitly used in this snippet
curses.use_default_colors()
win.clear()

CLIENT_WIN_HEIGHT = 16
SEPARATOR_LINE_Y = CLIENT_WIN_HEIGHT
LOG_WIN_START_Y = SEPARATOR_LINE_Y + 1

if curses.LINES < LOG_WIN_START_Y + 2: # Minimum 2 lines for log_win (e.g. 1 for content, 1 for border/scroll)
    curses.endwin()
    print(f"Terminal too small. Minimum {LOG_WIN_START_Y + 2} lines required for the layout.")
    sys.exit(1)

if curses.COLS < 20: # Arbitrary minimum width
    curses.endwin()
    print(f"Terminal too small. Minimum 20 columns required.")
    sys.exit(1)

try:
    client_win = win.subwin(CLIENT_WIN_HEIGHT, curses.COLS, 0, 0)

    win.hline(SEPARATOR_LINE_Y, 0, "-", curses.COLS)

    log_win_height = curses.LINES - LOG_WIN_START_Y
    if log_win_height < 1: # Ensure log_win_height is at least 1
        log_win_height = 1
    log_win = win.subwin(log_win_height, curses.COLS, LOG_WIN_START_Y, 0)
    log_win.scrollok(True)

    win.refresh() # Refresh main window once after initial setup
except curses.error as e:
    curses.endwin()
    print(f"Curses error during window setup: {e}")
    print("Is your terminal window large enough?")
    sys.exit(1)


# init logging
ch = CursesHandler(log_win)
ch.setFormatter(logging.Formatter(fmt='%(levelname)s: %(message)s'))
logging.basicConfig(level=default_loglevel, handlers=[ch])

p = ProDj()
p.set_client_keepalive_callback(lambda n: update_clients(client_win))
p.set_client_change_callback(lambda n: update_clients(client_win))

def update_clients(client_win):
  try:
    client_win.clear()
    client_win.addstr(0, 0, "Detected Pioneer devices:\n")
    if len(p.cl.clients) == 0:
      client_win.addstr("  No devices detected\n")
    else:
      for c in p.cl.clients:
        client_win.addstr("Player {}: {} {} BPM Pitch {:.2f}% Beat {}/{} NextCue {}\n".format(
          c.player_number, c.model if c.fw=="" else "{}({})".format(c.model,c.fw),
          c.bpm, (c.pitch-1)*100, c.beat, c.beat_count, c.cue_distance))
        if c.status_packet_received:
          client_win.addstr("  {} ({}) Track {} from Player {},{} Actual Pitch {:.2f}%\n".format(
            c.play_state, ",".join(c.state), c.track_number, c.loaded_player_number,
            c.loaded_slot, (c.actual_pitch-1)*100))
    client_win.refresh()
  except Exception as e:
    logging.critical(str(e))

update_clients(client_win)

try:
  p.start()
  p.vcdj_enable()
  p.join()
except KeyboardInterrupt:
  logging.info("Shutting down...")
  p.stop()
#except:
#  curses.endwin()
#  raise
finally:
  curses.endwin()
