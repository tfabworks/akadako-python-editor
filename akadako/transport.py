"""
MIDI transport for AkaDako board.

Implements Firmata protocol communication over MIDI,
matching the JavaScript MidiDakoTransport class in xcx-g2s.
"""

# Default MIDI port name patterns, mirroring the midiPortFilters in
# xcx-g2s akadako-connector.js. Individual boards present different MIDI
# port names ("STEAM BOX", "MidiDako", "AkaDako") depending on the model.
DEFAULT_NAME_FILTERS = ("STEAM BOX", "MidiDako", "AkaDako")


def _matching_port(ports, filters):
    """Return the index of the first port whose name contains any filter."""
    for i, name in enumerate(ports):
        if any(f in name for f in filters):
            return i
    return None


class MidiTransport:
    """MIDI transport layer for Firmata communication with AkaDako."""

    def __init__(self, midi_in, midi_out):
        self._midi_in = midi_in
        self._midi_out = midi_out
        self._callback = None

    @classmethod
    def find_and_open(cls, name_filter=None):
        """Find and open an AkaDako MIDI device.

        Args:
            name_filter: A substring (or iterable of substrings) to match in
                MIDI port names. Defaults to the known AkaDako port names
                (``DEFAULT_NAME_FILTERS``: "STEAM BOX", "MidiDako", "AkaDako").

        Returns:
            MidiTransport: Connected transport instance.

        Raises:
            RuntimeError: If no matching MIDI device is found.
        """
        try:
            import rtmidi
        except ImportError as exc:
            raise RuntimeError(
                "the 'python-rtmidi' package is required to connect to an "
                "AkaDako board; install with: pip install akadako"
            ) from exc

        if name_filter is None:
            filters = list(DEFAULT_NAME_FILTERS)
        elif isinstance(name_filter, str):
            filters = [name_filter]
        else:
            filters = list(name_filter)

        midi_in = rtmidi.MidiIn()
        midi_out = rtmidi.MidiOut()

        in_port = _matching_port(midi_in.get_ports(), filters)
        out_port = _matching_port(midi_out.get_ports(), filters)

        if in_port is None or out_port is None:
            raise RuntimeError(
                f"AkaDako MIDI device not found. "
                f"Available inputs: {midi_in.get_ports()}, "
                f"outputs: {midi_out.get_ports()}"
            )

        midi_in.open_port(in_port)
        midi_out.open_port(out_port)

        transport = cls(midi_in, midi_out)
        midi_in.set_callback(transport._on_midi_message)
        return transport

    def set_data_callback(self, callback):
        """Set callback for received Firmata data.

        Args:
            callback: Function(bytes) called with received data.
        """
        self._callback = callback

    def _on_midi_message(self, event, data=None):
        """Handle incoming MIDI message."""
        message, _delta_time = event
        # convertFromReceived: pass through as-is (same as JS)
        if self._callback and message:
            self._callback(bytes(message))

    def write(self, data):
        """Send Firmata data over MIDI.

        Applies the same conversions as JS MidiDakoTransport.convertToSend().

        Args:
            data: bytes or list of Firmata protocol bytes.
        """
        data = list(data)

        if not data:
            return

        if data[0] == 0xF9:
            # report version - skip (WebMIDI reserved)
            return

        if data[0] == 0xF4:
            # set PinMode - remap (WebMIDI reserved 0xF4 -> 0xA0)
            self._midi_out.send_message([0xA0, data[1], data[2]])
            return

        if data[0] == 0xF0 and len(data) > 1 and data[1] == 0x79:
            # query firmware - skip (board freezes)
            return

        if len(data) == 3 and data[0] == 0xF0 and data[2] == 0xF7:
            # one byte SysEx - add dummy byte
            data.insert(2, 0x00)
            self._midi_out.send_message(data)
            return

        self._midi_out.send_message(data)

    def close(self):
        """Close MIDI ports."""
        self._midi_in.close_port()
        self._midi_out.close_port()
