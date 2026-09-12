import unittest
from unittest.mock import Mock, MagicMock, patch
import sys

# Use qtpy so the test works under PySide2, PySide6 or PyQt5
from qtpy.QtWidgets import QApplication
from qtpy.QtCore import QObject, Signal, QTimer

sys.path.insert(0, '.')

from prodj.gui.midiclock_widgets import MidiClockMainWindow


class _FakeSignalBridge(QObject):
    """Minimal signal bridge that satisfies MidiClockMainWindow._connect_signals."""
    client_change_signal     = Signal(int)
    master_change_signal     = Signal(int)
    beat_signal              = Signal()
    prodj_beat_signal        = Signal(int, int)
    prodj_beat_timing_signal = Signal(int, int, object)
    metadata_ready_signal    = Signal()


class TestMidiClockUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        with patch('prodj.gui.midiclock_widgets.AlsaMidiClock', new=None), \
             patch('prodj.gui.midiclock_widgets.RtMidiClock', new=MagicMock()):
            self.mock_prodj = Mock()
            self.mock_prodj.cl.clients = []
            self.mock_prodj.cl.getClient = Mock(return_value=None)
            self.bridge = _FakeSignalBridge()
            self.window = MidiClockMainWindow(self.mock_prodj, self.bridge)

    def tearDown(self):
        self.window.close()

    # ------------------------------------------------------------------
    # Precision pitch
    # ------------------------------------------------------------------

    def test_pitch_initial_zero(self):
        self.assertEqual(self.window.precision_pitch_offset, 0.0)
        self.assertEqual(self.window.pitch_label.text(), "Pitch: +0.0 ms")

    def test_pitch_up(self):
        self.window.pitch_amount_spinbox.setValue(2.5)
        self.window.pitch_up_button.click()
        self.assertAlmostEqual(self.window.precision_pitch_offset, 2.5)
        self.assertEqual(self.window.pitch_label.text(), "Pitch: +2.5 ms")

    def test_pitch_down(self):
        self.window.pitch_amount_spinbox.setValue(1.0)
        self.window.pitch_up_button.click()   # +1.0
        self.window.pitch_down_button.click() # -1.0 → back to 0
        self.assertAlmostEqual(self.window.precision_pitch_offset, 0.0)

    def test_pitch_reset(self):
        self.window.pitch_amount_spinbox.setValue(5.0)
        self.window.pitch_up_button.click()
        self.window.reset_precision_pitch()
        self.assertEqual(self.window.precision_pitch_offset, 0.0)
        self.assertEqual(self.window.pitch_label.text(), "Pitch: +0.0 ms")

    # ------------------------------------------------------------------
    # Manual BPM mode
    # ------------------------------------------------------------------

    def test_manual_mode_clears_cdj_metrics(self):
        """Activating manual BPM mode must clear stale CDJ beat/phase data
        so the metrics panel shows — instead of the last CDJ values."""
        # Simulate prior CDJ data
        self.window._last_beat_number = 42
        self.window._sparkline = [1.2, -0.3, 0.8]
        self.window.phase_error_ms = 3.5

        self.window.manual_mode_button.click()  # activate manual mode

        self.assertEqual(self.window._last_beat_number, 0)
        self.assertEqual(self.window._sparkline, [])
        self.assertEqual(self.window.phase_error_ms, 0.0)
        # metrics panel BAR.BEAT should show —
        self.assertEqual(self.window._metrics_beat_label.text(), "—")
        # metrics panel PHASE ERR should show —
        self.assertEqual(self.window._metrics_phase_hist_label.text(), "—")
        # metrics panel CDJ DELAY should show —
        self.assertEqual(self.window._metrics_latency_label.text(), "—")

    def test_manual_bpm_toggle_enables_controls(self):
        self.assertFalse(self.window.manual_bpm_mode_active)
        self.window.manual_mode_button.click()  # activate
        self.assertTrue(self.window.manual_bpm_mode_active)
        self.assertTrue(self.window.manual_bpm_slider.isEnabled())
        self.assertTrue(self.window.tap_tempo_button.isEnabled())
        self.assertEqual(self.window.manual_mode_button.text(), "Auto BPM")

    def test_manual_bpm_toggle_back(self):
        self.window.manual_mode_button.click()  # on
        self.window.manual_mode_button.click()  # off
        self.assertFalse(self.window.manual_bpm_mode_active)
        self.assertFalse(self.window.manual_bpm_slider.isEnabled())
        self.assertEqual(self.window.manual_mode_button.text(), "Manual BPM")

    def test_tap_tempo_activates_manual_mode(self):
        self.assertFalse(self.window.manual_bpm_mode_active)
        # Tap twice so a BPM can be calculated
        self.window.handle_tap_tempo_clicked()
        self.window.handle_tap_tempo_clicked()
        self.assertTrue(self.window.manual_bpm_mode_active)

    # ------------------------------------------------------------------
    # Source radio / combo
    # ------------------------------------------------------------------

    def test_source_radio_master_default(self):
        self.assertTrue(self.window.source_master_radio.isChecked())
        self.assertIsNone(self.window.selected_player_source)

    def test_source_radio_lock_to_player(self):
        self.window.source_player_radio.setChecked(True)
        self.assertIsNotNone(self.window.selected_player_source)
        self.assertEqual(
            self.window.selected_player_source,
            int(self.window.source_player_combo.currentText()),
        )

    def test_source_radio_fires_once_per_toggle(self):
        """Fix 2f: switching radios should call update_midi_clock_source_logic
        exactly once, not twice (old bug: both toggled(False) and toggled(True)
        fired the slot).
        """
        call_count = [0]
        orig = self.window.update_midi_clock_source_logic
        def counted():
            call_count[0] += 1
            orig()
        self.window.update_midi_clock_source_logic = counted

        self.window.source_player_radio.setChecked(True)   # one toggle event
        self.assertEqual(call_count[0], 1, "expected exactly 1 call, got %d" % call_count[0])

        self.window.source_master_radio.setChecked(True)   # back to master
        self.assertEqual(call_count[0], 2)

    # ------------------------------------------------------------------
    # Auto-sync toggle clears sparkline
    # ------------------------------------------------------------------

    def test_auto_sync_off_clears_sparkline(self):
        self.window._sparkline = [1.0, 2.0, -0.5]
        self.window.auto_sync_button.setChecked(False)
        self.window.toggle_auto_sync()
        self.assertEqual(self.window._sparkline, [])

    def test_auto_sync_on_clears_sparkline(self):
        self.window._sparkline = [1.0, 2.0]
        self.window.auto_sync_button.setChecked(True)
        self.window.toggle_auto_sync()
        self.assertEqual(self.window._sparkline, [])

    # ------------------------------------------------------------------
    # _short_port_name helper
    # ------------------------------------------------------------------

    def test_short_port_name_windows(self):
        from prodj.gui.midiclock_widgets import _short_port_name
        self.assertEqual(_short_port_name("CH345:CH345 MIDI 1 28:0"), "CH345")

    def test_short_port_name_linux(self):
        from prodj.gui.midiclock_widgets import _short_port_name
        self.assertEqual(_short_port_name("CH345 MIDI 1"), "CH345 MIDI 1")

    # ------------------------------------------------------------------
    # Track info bar
    # ------------------------------------------------------------------

    def test_track_info_empty_when_no_source(self):
        """No CDJs — bar should show idle state."""
        self.window._refresh_track_info()
        self.assertEqual(self.window._track_title_label.text(), "No track")
        self.assertEqual(self.window._track_artist_label.text(), "")
        self.assertEqual(self.window._track_key_label.text(), "")

    def test_track_info_with_metadata(self):
        """When a master CDJ has metadata, the bar shows title/artist/key/duration."""
        from unittest.mock import Mock
        client = Mock()
        client.player_number = 1
        client.type = "cdj"
        client.state = ["master"]
        client.play_state = "playing"
        client.bpm = 124.0
        client.actual_pitch = 1.0
        client.track_number = 1
        client.track_analyze_type = "rekordbox"
        client.key = "Am"
        client.key_shift = None
        client.metadata = {
            "title": "Test Track",
            "artist": "Test Artist",
            "key": "Am",
            "duration": 332,  # 5:32
        }
        self.mock_prodj.cl.clients = [client]
        self.mock_prodj.cl.getClient = lambda pn: client if pn == 1 else None

        # Create tile so _get_active_source_player_number works
        from prodj.gui.midiclock_widgets import PlayerTileWidget
        tile = PlayerTileWidget(1)
        self.window.player_tiles[1] = tile

        self.window._refresh_track_info()

        self.assertEqual(self.window._track_title_label.text(), "Test Track")
        self.assertEqual(self.window._track_artist_label.text(), "Test Artist")
        self.assertEqual(self.window._track_key_label.text(), "Am")
        self.assertEqual(self.window._track_duration_label.text(), "5:32")
        self.assertEqual(self.window._track_player_label.text(), "P1")

    def test_track_info_playing_led_green(self):
        from unittest.mock import Mock
        client = Mock()
        client.player_number = 2
        client.type = "cdj"
        client.state = ["master"]
        client.play_state = "playing"
        client.bpm = 130.0
        client.actual_pitch = 1.0
        client.track_number = 5
        client.track_analyze_type = "rekordbox"
        client.key = None
        client.key_shift = None
        client.metadata = None
        self.mock_prodj.cl.clients = [client]
        self.mock_prodj.cl.getClient = lambda pn: client if pn == 2 else None

        from prodj.gui.midiclock_widgets import PlayerTileWidget
        tile = PlayerTileWidget(2)
        self.window.player_tiles[2] = tile

        self.window._refresh_track_info()
        # playing → green LED
        self.assertIn("#10b981", self.window._track_state_led.styleSheet())

    def test_track_info_paused_led_amber(self):
        from unittest.mock import Mock
        client = Mock()
        client.player_number = 3
        client.type = "cdj"
        client.state = ["master"]
        client.play_state = "paused"
        client.bpm = 128.0
        client.actual_pitch = 1.0
        client.track_number = 2
        client.track_analyze_type = "rekordbox"
        client.key = None
        client.key_shift = None
        client.metadata = None
        self.mock_prodj.cl.clients = [client]
        self.mock_prodj.cl.getClient = lambda pn: client if pn == 3 else None

        from prodj.gui.midiclock_widgets import PlayerTileWidget
        tile = PlayerTileWidget(3)
        self.window.player_tiles[3] = tile

        self.window._refresh_track_info()
        self.assertIn("#f59e0b", self.window._track_state_led.styleSheet())

    def test_track_info_duration_format(self):
        """Duration 332 seconds → '5:32', 60 → '1:00', 59 → '0:59'."""
        from unittest.mock import Mock
        for secs, expected in [(332, "5:32"), (60, "1:00"), (59, "0:59"), (3661, "61:01")]:
            client = Mock()
            client.player_number = 1
            client.type = "cdj"
            client.state = ["master"]
            client.play_state = "playing"
            client.bpm = 120.0
            client.actual_pitch = 1.0
            client.track_number = 1
            client.track_analyze_type = "rekordbox"
            client.key = None
            client.key_shift = None
            client.metadata = {"title": "T", "artist": "", "key": "", "duration": secs}
            self.mock_prodj.cl.clients = [client]
            self.mock_prodj.cl.getClient = lambda pn, c=client: c if pn == 1 else None
            from prodj.gui.midiclock_widgets import PlayerTileWidget
            self.window.player_tiles[1] = PlayerTileWidget(1)
            self.window._refresh_track_info()
            self.assertEqual(
                self.window._track_duration_label.text(), expected,
                f"{secs}s should format as {expected!r}"
            )

    # ------------------------------------------------------------------
    # _resolve_bpm_for_seed
    # ------------------------------------------------------------------

    def test_resolve_bpm_seed_defaults_120(self):
        """No CDJs, no history — should fall back to 120."""
        self.window.last_known_good_bpm = 0.0
        self.assertEqual(self.window._resolve_bpm_for_seed(), 120.0)

    def test_resolve_bpm_seed_uses_last_known(self):
        self.window.last_known_good_bpm = 134.5
        self.assertAlmostEqual(self.window._resolve_bpm_for_seed(), 134.5)

    def test_resolve_bpm_seed_manual_mode(self):
        self.window.manual_bpm_mode_active = True
        self.window.manual_bpm_value = 145.0
        self.assertAlmostEqual(self.window._resolve_bpm_for_seed(), 145.0)

    # ------------------------------------------------------------------
    # _bpm_from_client
    # ------------------------------------------------------------------

    def test_bpm_from_client_valid(self):
        c = Mock(bpm=124.0, actual_pitch=1.0)
        self.assertAlmostEqual(MidiClockMainWindow._bpm_from_client(c), 124.0)

    def test_bpm_from_client_with_pitch(self):
        c = Mock(bpm=100.0, actual_pitch=1.06)
        self.assertAlmostEqual(MidiClockMainWindow._bpm_from_client(c), 106.0)

    def test_bpm_from_client_zero_bpm(self):
        c = Mock(bpm=0.0, actual_pitch=1.0)
        self.assertIsNone(MidiClockMainWindow._bpm_from_client(c))

    def test_bpm_from_client_none(self):
        c = Mock(bpm=None, actual_pitch=1.0)
        self.assertIsNone(MidiClockMainWindow._bpm_from_client(c))

    def test_bpm_from_client_invalid_string(self):
        c = Mock(bpm="--.--", actual_pitch=1.0)
        self.assertIsNone(MidiClockMainWindow._bpm_from_client(c))


if __name__ == '__main__':
    unittest.main()
