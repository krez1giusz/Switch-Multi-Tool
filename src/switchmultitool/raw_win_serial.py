import os
import ctypes
from ctypes import wintypes


class RawWinSerial:
    """Minimal Windows COM reader/writer that avoids pyserial's SetCommState path.

    It opens the COM device and uses ReadFile/WriteFile directly. It intentionally
    does not change baud/8N1 settings; use Device Manager, `mode COMx: ...`, or a
    terminal once if you need to force settings. This is mainly a fallback for
    USB-serial drivers that reject pyserial's SetCommState with WinError 31.
    """

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PURGE_RXCLEAR = 0x0008
    PURGE_TXCLEAR = 0x0004

    class COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [
            ("ReadIntervalTimeout", wintypes.DWORD),
            ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
            ("ReadTotalTimeoutConstant", wintypes.DWORD),
            ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
            ("WriteTotalTimeoutConstant", wintypes.DWORD),
        ]

    def __init__(self, port: str, read_timeout_ms: int = 120, write_timeout_ms: int = 1000):
        if os.name != "nt":
            raise OSError("RawWinSerial działa tylko na Windows.")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.port = self._normalize(port)
        self.handle = self.kernel32.CreateFileW(
            self.port,
            self.GENERIC_READ | self.GENERIC_WRITE,
            0,
            None,
            self.OPEN_EXISTING,
            0,
            None,
        )
        if self.handle == self.INVALID_HANDLE_VALUE:
            self._raise_last_error("CreateFileW")

        timeouts = self.COMMTIMEOUTS(
            ReadIntervalTimeout=read_timeout_ms,
            ReadTotalTimeoutMultiplier=0,
            ReadTotalTimeoutConstant=read_timeout_ms,
            WriteTotalTimeoutMultiplier=0,
            WriteTotalTimeoutConstant=write_timeout_ms,
        )
        self.kernel32.SetCommTimeouts(self.handle, ctypes.byref(timeouts))

    def _normalize(self, port: str) -> str:
        p = port.strip()
        if p.upper().startswith("COM"):
            return "\\\\.\\" + p
        return p

    def _raise_last_error(self, where: str):
        err = ctypes.get_last_error()
        raise OSError(err, f"{where} failed with WinError {err}")

    def read(self, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        read = wintypes.DWORD(0)
        ok = self.kernel32.ReadFile(self.handle, buf, size, ctypes.byref(read), None)
        if not ok:
            self._raise_last_error("ReadFile")
        return buf.raw[: read.value]

    def write(self, data: bytes) -> int:
        written = wintypes.DWORD(0)
        ok = self.kernel32.WriteFile(self.handle, data, len(data), ctypes.byref(written), None)
        if not ok:
            self._raise_last_error("WriteFile")
        return written.value

    def flush(self):
        self.kernel32.FlushFileBuffers(self.handle)

    def reset_input_buffer(self):
        self.kernel32.PurgeComm(self.handle, self.PURGE_RXCLEAR)

    def reset_output_buffer(self):
        self.kernel32.PurgeComm(self.handle, self.PURGE_TXCLEAR)

    def close(self):
        if getattr(self, "handle", None) and self.handle != self.INVALID_HANDLE_VALUE:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
