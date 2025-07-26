import unittest
from unittest.mock import Mock, MagicMock, patch
import sys

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

# Add the project root to the python path
sys.path.insert(0, '.')

from prodj.gui.midiclock_widgets import MidiClockMainWindow

class TestMidiClockUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication(sys.argv)

    def setUp(self):
        with patch('prodj.gui.midiclock_widgets.AlsaMidiClock', new=MagicMock()), \
             patch('prodj.gui.midiclock_widgets.RtMidiClock', new=MagicMock()):
            self.mock_prodj = Mock()
            self.mock_prodj.cl.clients = []
            self.mock_signal_bridge = Mock()
            self.window = MidiClockMainWindow(self.mock_prodj, self.mock_signal_bridge)

    def tearDown(self):
        self.window.close()

    def test_pitch_adjustment(self):
        self.assertEqual(self.window.pitch_offset, 0)
        self.window.pitch_amount_spinbox.setValue(50)
        self.window.pitch_up_button.click()
        self.assertEqual(self.window.pitch_offset, 50)
        self.assertEqual(self.window.pitch_label.text(), "Pitch: 50 ms")
        self.window.pitch_down_button.click()
        self.assertEqual(self.window.pitch_offset, 0)
        self.assertEqual(self.window.pitch_label.text(), "Pitch: 0 ms")

    def test_led_blink(self):
        self.assertEqual(self.window.midi_led.styleSheet(), "background-color: #505050; border-radius: 10px;")
        self.window.beat_received()
        self.assertEqual(self.window.midi_led.styleSheet(), "background-color: #00FF00; border-radius: 10px;")

        # Use a timer to check if the color resets
        QTimer.singleShot(100, self.check_led_color)

    def check_led_color(self):
        self.assertEqual(self.window.midi_led.styleSheet(), "background-color: #505050; border-radius: 10px;")

if __name__ == '__main__':
    unittest.main()
