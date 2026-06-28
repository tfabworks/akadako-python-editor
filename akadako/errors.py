"""
Exception classes for the akadako library.

These mirror the error classes of the JavaScript library (akadako.js):
AkaDako.NotSupportedError / DisconnectedError / InvalidValueError /
InvalidConnectorError / BusyError.
"""


class AkaDakoError(Exception):
    """Base class for all akadako errors."""


class NotSupportedError(AkaDakoError):
    """The requested feature is not supported.

    Mirrors AkaDako.NotSupportedError ("利用できない機能です").
    """

    def __init__(self, message="利用できない機能です"):
        super().__init__(message)


class DisconnectedError(AkaDakoError):
    """The board is disconnected so the operation cannot be performed.

    Mirrors AkaDako.DisconnectedError ("デバイスは切断されています").
    """

    def __init__(self, message="デバイスは切断されています"):
        super().__init__(message)


class InvalidValueError(AkaDakoError):
    """An invalid value was given.

    Mirrors AkaDako.InvalidValueError ("'value'は使用できません").
    """

    def __init__(self, value):
        self.value = value
        super().__init__(f"'{value}'は使用できません")


class InvalidConnectorError(AkaDakoError):
    """An invalid connector was specified.

    Mirrors AkaDako.InvalidConnectorError ("コネクター'connector'は使用できません").
    """

    def __init__(self, connector):
        self.connector = connector
        super().__init__(f"コネクター'{connector}'は使用できません")


class BusyError(AkaDakoError):
    """The device is busy.

    Mirrors AkaDako.BusyError ("'device'は使用中です").
    """

    def __init__(self, device):
        self.device = device
        super().__init__(f"'{device}'は使用中です")
