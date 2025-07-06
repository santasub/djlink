# Python ProDJ Link

![single player screenshot](screenshot-single.png)

This is a set of python scripts to participate in a Pioneer ProDJ Link system.
It is particularly useful to monitor whats happening on the players, but can also help by syncing other devices using midi clock.
The code to detect your own mac and ip addresses is os dependant, but thanks to the netifaces it should also work on platforms other than linux (testing required).

The Qt GUI is useful to perform light shows and react to events in the music.

## Getting Started

These instructions describe necessary work to be done before being able to run the project.

### Prerequisites

python-prodj-link is written in Python 3. It requires
[Construct](https://pypi.python.org/pypi/construct) **(Version 2.9 or later)**,
[PyQt5](https://pypi.python.org/pypi/PyQt5),
[PyOpenGL](https://pypi.org/project/PyOpenGL/) and
[netifaces](https://pypi.org/project/netifaces).

Use your distributions package management to install these, e.g. on Arch Linux:

```
pacman -S python-construct python-pyqt5 python-netifaces python-opengl
```

Alternatively, you can use pip to install the required dependencies, preferrably in a virtualenv:
```
python3 -m venv venv
source venv/bin/activate # On Linux/macOS
# venv\Scripts\activate # On Windows
pip install -r requirements.txt
```
The `requirements.txt` file includes `python-rtmidi` for cross-platform MIDI support. For Linux, `alsaseq` can also be used by `midiclock.py` if installed, but `python-rtmidi` is the default for `midiclock-qt.py` on non-Linux systems.

**macOS Users:** This project is now compatible with macOS. Ensure you have Python 3 and pip installed (e.g., via Homebrew). The `python-rtmidi` library requires a C++ compiler to build its extension; on macOS, this usually means having Xcode Command Line Tools installed (`xcode-select --install`).

**Note:** Construct v2.9 changed a lot of its internal APIs.
If you still need to use version 2.8, you can find an unmaintained version in the branch [construct-compat](../../../tree/construct-compat).

### Testing
```
python3 test_runner.py
```

### Network configuration

You need to be on the same Ethernet network as the players are discovered using broadcasts.
The players will aquire IPs using DHCP if a server is available, otherwise they fall back to IPv4 autoconfiguration.
If there is no DHCP server on your network, make sure you assign a IP address inside 169.254.0.0/16 to yourself, for example using NetworkManager or avahi-autoipd.

You can test your setup using wireshark or tcpdump to see if you receive keepalive broadcast on port 50000.

## Usage

### Qt GUI

The most useful part is the Qt GUI.
It displays some information about the tracks on every player, including metadata, artwork, current BPM, waveform and preview waveform.
Waveforms are rendered using OpenGL through Qt, thus you need an OpenGL 2.0 compatible graphics driver.
It is also possible to browse media and load these tracks into players remotely.
Additionally, you can download tracks from remote players, either directly when loaded or from the media browser.

    ./monitor-qt.py

or when using virtualenv:

    venv/bin/python3 monitor-qt.py

The `monitor-qt.py` application supports a `--loglevel` argument (e.g., `--loglevel debug`, `--loglevel info`) to control logging verbosity.

**Note on Playback Controls:** The "Start playback" and "Stop playback" menu items in `monitor-qt.py` use the ProDJ Link Fader Start mechanism. This feature typically requires a compatible Pioneer DJM mixer to be present on the network and correctly configured to relay these commands to the players. If no mixer is present, these controls will be disabled in the UI.

![two players screenshot with browser](screenshot-full.png)

### MIDI Clock Utilities

Two MIDI clock utilities are provided:

1.  **`midiclock.py` (Command-Line)**
    *   This script opens a MIDI output port and sends MIDI clock packets matching the current master BPM from the ProDJ Link network.
    *   It can also optionally send MIDI note events on beats.
    *   On Linux, it can use either `alsaseq` (if installed, for potentially more precise timing) or `python-rtmidi`. On other platforms like macOS and Windows, it uses `python-rtmidi`.
    *   Use `./midiclock.py --help` to see available options, including port listing and backend selection.

2.  **`midiclock-qt.py` (Graphical UI)**
    *   A new Qt-based graphical interface for MIDI clock functionality.
    *   **Features:**
        *   Displays active players from the ProDJ Link network with their BPM and master status.
        *   Allows selection of a specific player to source the BPM for the MIDI clock. Defaults to the network master.
        *   **BPM Coasting:** If the selected BPM source is lost, the clock continues at the last known valid BPM.
        *   **Manual BPM Control:** Includes a slider and a Tap Tempo button to manually set the MIDI clock BPM.
        *   MIDI output port selection via a dropdown.
        *   Start/Stop button for MIDI clock output.
        *   Settings dialog (mainly for Linux users to prefer ALSA or rtmidi backend).
    *   Like `monitor-qt.py`, it supports a `--loglevel` argument for controlling log output.
    *   **Usage:**
        ```bash
        ./midiclock-qt.py
        # or with virtualenv
        venv/bin/python3 midiclock-qt.py
        ```

**General MIDI Clock Notes:**
*   For `midiclock.py` on Linux using `alsaseq`: Depending on your distribution, you may need privileges to access `/dev/snd/seq` (e.g., membership in the `audio` group on Arch Linux).
*   Ensure `python-rtmidi` is installed (via `requirements.txt`) for `midiclock-qt.py` and as a fallback/default for `midiclock.py`.

## Bugs & Contributing

This is still early beta software!
It can freeze your players, although that has not happened to me with the recent versions yet.
Be careful when using it in an live environment!

If you experience any errors or have additional features, feel free to open an issue or pull request.
I have already **successfully tested** the script against the following players/mixers:

* Pioneer CDJ 2000
* Pioneer CDJ 2000 Nexus
* Pioneer CDJ 2000 NXS2
* Pioneer XDJ 1000
* Pioneer DJM 900 Nexus
* Pioneer DJM 900 NXS2

It may occur that I cannot parse some network packets that I have not seen yet, especially on players other than my XDJ-1000s or if Rekordbox is involved.
Please include debug output when reporting bugs and a network dump (i.e. wireshark) if possible.

## Acknowledgments

* A lot of information from [dysentery](https://github.com/brunchboy/dysentery)
* And some info from Austin Wright's [libpdjl](https://bitbucket.org/awwright/libpdjl)

## License

Licensed under the Apache License, Version 2.0 (the "License").
You may obtain a copy of the License inside the "LICENSE" file or at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
