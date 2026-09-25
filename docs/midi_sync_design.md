# MIDI Clock Sync — Design & Anforderungen

## Ziel

Die App verhält sich wie ein CDJ im Master-Auto-Sync-Modus.
Output ist ein stabiler MIDI Clock für Synthesizer und Drummaschinen.
Input ist der ProDJ Link vom CDJ (Master).

---

## 1. BPM-Quelle

- CDJ Master BPM × actual_pitch = unser setBpm
- Schwellwert für "großen" Tempowechsel: > 2 BPM Differenz
- Bei großem Tempowechsel: Auto-Stop + Re-Sync auf nächsten Bar Beat 1
- Koasting: wenn kein CDJ Signal → letztes bekanntes BPM beibehalten

---

## 2. Start & Sync

- User drückt "Start & Sync"
- App wartet auf nächsten CDJ Bar Beat 1 (Countdown 1–4)
- Auf Beat 1: einmaliger harter Snap (ALSA SETPOS_TIME) + MIDI Start (0xFA)
- Danach: Clock läuft stabil, kein weiterer harter Snap

---

## 3. Auto Sync (default ON, manuell deaktivierbar)

### Phasen-Referenz
- ProDJ Link liefert Beatgrid-Daten mit `next_beat_ms`
- CDJ Beat-Paket Ankunftszeit (wall clock) = echter Beat-Zeitpunkt
- Aus beiden kann berechnet werden wann der nächste Beat kommen sollte
- XDJ-700 sendet `next_beat_ms = 500` als Dummy → nur Ankunftszeit verwenden

### Korrektur-Mechanismus
- Vergleich: letzter CDJ Beat (Ankunftszeit) vs letzter MIDI Beat (`last_beat_wall_time`)
- Fehler = Differenz modulo beat_period
- Korrektur: **nur ALSA QUEUE_SKEW** (graduell, kein Sprung)
- Max Korrektur pro Beat: **±2ms** (als Sound Engineer: unterhalb der Wahrnehmungsschwelle für Synths/Drummaschinen, typisch < 3ms für stabile Sync)
- Smoothing über 4 Beats bevor Korrektur angewendet wird

### Was Auto Sync NICHT macht
- Keinen SETPOS_TIME während Playback (würde Geräte aus dem Takt bringen)
- Keine Korrektur während Pitch-Erkennung / Re-Sync

---

## 4. Pitch / Tempo-Änderung Erkennung

- Wenn BPM-Änderung > 2 BPM innerhalb eines Beats erkannt:
  - MIDI Stop (0xFC) senden
  - Warten bis BPM für 2 Beats stabil ist (< 0.5 BPM Varianz)
  - Dann: automatischer Re-Sync auf nächsten Bar Beat 1
  - MIDI Start (0xFA) senden

---

## 5. Slip Mode

- CDJ sendet Slip-Mode Flag im Status-Paket
- Während Slip Mode aktiv: BPM-Änderungen ignorieren
- Clock läuft weiter auf letztem stabilem BPM
- Nach Slip Mode Ende: einmaliger sanfter Re-Snap (SKEW, kein Stop/Start)

---

## 6. Manual BPM Override

- User aktiviert Manual Mode
- Startwert = letztes bekanntes CDJ-BPM (wenn vorhanden), sonst 120
- Clock läuft weiter auf diesem Wert ohne Änderung
- Auto Sync deaktiviert während Manual Mode
- Tap Tempo verfügbar
- Rückkehr zu Auto: letztes CDJ-BPM wieder übernehmen + _beat_snap_pending setzen

---

## 7. Grid Shift (manuell)

- Benutzer kann Phase manuell verschieben (±1/5/10/25ms)
- Wird als persistenter Offset gespeichert (_grid_offset_ms)
- Auto Sync rechnet diesen Offset ein (Ziel-Fehler = 0 bei offset)
- Reset-Button setzt Offset auf 0 + _beat_snap_pending

---

## 8. Ausgabe-Stabilität (Priorität)

**Der MIDI Clock Output muss für angeschlossene Geräte absolut stabil sein.**
- Keine harten Zeitsprünge während Playback
- Tempo-Änderungen nur via setBpm (graduell durch ALSA Queue)
- Phase-Korrekturen nur via SKEW (graduell)
- Harter Snap (SETPOS_TIME) nur beim initialen Start

---

## Hardware

- Input: Pioneer XDJ-700 (ProDJ Link)
- Output: MIDI über ALSA Sequencer (Raspberry Pi / reTerminal)
- Angeschlossene Geräte: Synthesizer, Drummaschinen

## Bekannte Eigenheiten

- XDJ-700 sendet `next_beat_ms = 500` als fixen Dummy-Wert → unbrauchbar für Phasenmessung
- CDJ Beat-Paket Ankunftszeit (wall clock) ist der echte Beat-Anker
- ALSA QUEUE_SKEW = gradueller Tempo-Nudge (gut)
- ALSA SETPOS_TIME = sofortiger Zeitsprung (nur beim Start verwenden)
