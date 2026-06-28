"""
akadako - Python library for the AkaDako board (TFabWorks).

API design is adapted from the official JavaScript library (akadako.js):
https://akadako.com/javascript/
The Python API is synchronous (blocking); the JS library is async only because
the browser has no blocking I/O.

Example:
    from akadako import AkaDako

    board = AkaDako.connect()
    try:
        print(board.fetch_temperature())
    finally:
        board.disconnect()
"""

from akadako.board import AkaDako
from akadako.constants import (
    Color,
    Rainbow,
    ColorLed,
    DigitalRead,
    DigitalReadPin,
    DigitalWrite,
    AnalogRead,
    PwmWrite,
    ServoWrite,
    PinBias,
    IrRemoteWrite,
)
from akadako.errors import (
    AkaDakoError,
    NotSupportedError,
    DisconnectedError,
    InvalidValueError,
    InvalidConnectorError,
    BusyError,
)

__version__ = "1.0.0"
__all__ = [
    "AkaDako",
    "Color",
    "Rainbow",
    "ColorLed",
    "DigitalRead",
    "DigitalReadPin",
    "DigitalWrite",
    "AnalogRead",
    "PwmWrite",
    "ServoWrite",
    "PinBias",
    "IrRemoteWrite",
    "AkaDakoError",
    "NotSupportedError",
    "DisconnectedError",
    "InvalidValueError",
    "InvalidConnectorError",
    "BusyError",
]
