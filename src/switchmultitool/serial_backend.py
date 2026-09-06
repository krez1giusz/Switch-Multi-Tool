import os
import re
import time
import threading
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any

from .raw_win_serial import RawWinSerial

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # allows the app to show a nice error if pyserial is missing
    serial = None
    list_ports = None


@dataclass
class PortInfo:
    device: str
    description: str = ""
    hwid: str = ""
    manufacturer: str = ""
    product: str = ""
    serial_number: str = ""
    vid: str = ""
    pid: str = ""
    location: str = ""


def list_serial_ports() -> List[PortInfo]:
    if list_ports is None:
        return []

    ports: List[PortInfo] = []
    for p in list_ports.comports():
        ports.append(
            PortInfo(
                device=p.device or "",
                description=p.description or "",
                hwid=p.hwid or "",
                manufacturer=getattr(p, "manufacturer", None) or "",
                product=getattr(p, "product", None) or "",
                serial_number=getattr(p, "serial_number", None) or "",
                vid=f"0x{p.vid:04X}" if getattr(p, "vid", None) is not None else "",
                pid=f"0x{p.pid:04X}" if getattr(p, "pid", None) is not None else "",
                location=getattr(p, "location", None) or "",
            )
        )
    return ports


class SerialConsole:
    MAX_BUFFER_CHARS = 512_000

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        on_rx: Optional[Callable[[str], None]] = None,
        on_dev: Optional[Callable[[str], None]] = None,
        log_dir: str = "logs",
        flow_profile: str = "none",
        open_retries: int = 2,
    ):
        if serial is None:
            raise RuntimeError("pyserial is not installed. Run: pip install -r requirements.txt")

        self.port = port
        self.baudrate = baudrate
        self.on_rx = on_rx or (lambda text: None)
        self.on_dev = on_dev or (lambda text: None)
        self.buffer = ""
        self._lock = threading.Lock()
        self._closed = False

        Path(log_dir).mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_port = port.replace(":", "").replace("/", "_").replace("\\", "_")
        self.log_path = str(Path(log_dir) / f"{safe_port}_{ts}.log")

        self.flow_profile = flow_profile
        self.ser = self._open_serial_with_retries(open_retries=open_retries)

    def _flow_flags(self) -> Dict[str, bool]:
        """Return pyserial flow-control flags for common console-cable profiles."""
        profiles = {
            # Najbezpieczniejszy default dla Cisco console + większości adapterów USB-serial.
            "none": {"xonxoff": False, "rtscts": False, "dsrdtr": False},
            # Czasem używany w starszych opisach Cisco / terminalach.
            "xonxoff": {"xonxoff": True, "rtscts": False, "dsrdtr": False},
            # Awaryjnie dla kabli/konwerterów wymagających kontroli sprzętowej.
            "rtscts": {"xonxoff": False, "rtscts": True, "dsrdtr": False},
        }
        return profiles.get(self.flow_profile, profiles["none"])

    def _normalize_port_name(self, port: str) -> str:
        # PySerial zwykle ogarnia COM10+, ale forma \\.\COMx pomaga niektórym sterownikom.
        if port.upper().startswith("COM"):
            try:
                n = int(port[3:])
                if n >= 10:
                    return "\\\\.\\" + port
            except ValueError:
                pass
        return port

    def _open_serial_with_retries(self, open_retries: int):
        flags = self._flow_flags()
        port_to_open = self._normalize_port_name(self.port)
        last_exc = None

        if self.flow_profile == "raw-win-only":
            self._try_windows_mode_config()
            return self._open_raw_win()

        for attempt in range(1, open_retries + 2):
            try:
                self.on_dev(
                    f"OPEN_ATTEMPT {attempt}: port={self.port}, baud={self.baudrate}, "
                    f"flow={self.flow_profile}, flags={flags}"
                )
                ser = serial.Serial(
                    port=port_to_open,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=0.15,
                    write_timeout=1,
                    **flags,
                )

                # Niektóre adaptery nie lubią aktywnego DTR/RTS zaraz po otwarciu.
                try:
                    ser.setDTR(False)
                    ser.setRTS(False)
                except Exception as line_exc:
                    self.on_dev(f"WARN: nie udało się ustawić DTR/RTS=False: {line_exc}")

                try:
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                except Exception:
                    pass

                self.on_dev("OPEN_OK: port otwarty przez pyserial")
                return ser

            except Exception as exc:
                last_exc = exc
                self.on_dev(f"OPEN_FAIL {attempt}: {type(exc).__name__}: {exc!r}")
                time.sleep(0.6)

        # Windows/niektóre sterowniki USB-serial potrafią odrzucić SetCommState
        # w pyserialu błędem WinError 31, mimo że terminale typu Tera Term działają.
        # Próbujemy najpierw ustawić port komendą systemową `mode`, potem jeszcze
        # raz pyserial, a na końcu surowy fallback WinAPI bez SetCommState.
        if os.name == "nt":
            self._try_windows_mode_config()

            try:
                self.on_dev("OPEN_ATTEMPT after MODE: ponowna próba pyserial")
                ser = serial.Serial(
                    port=port_to_open,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=0.15,
                    write_timeout=1,
                    **flags,
                )
                self.on_dev("OPEN_OK: pyserial po konfiguracji MODE")
                return ser
            except Exception as exc:
                last_exc = exc
                self.on_dev(f"OPEN_FAIL after MODE: {type(exc).__name__}: {exc!r}")

            if self.flow_profile in ("none", "raw-win-fallback"):
                try:
                    return self._open_raw_win()
                except Exception as exc:
                    last_exc = exc
                    self.on_dev(f"RAW_WIN_FAIL: {type(exc).__name__}: {exc!r}")

        hint = diagnose_open_error(self.port, last_exc, self.flow_profile)
        raise RuntimeError(hint) from last_exc

    def _try_windows_mode_config(self):
        if os.name != "nt":
            return
        try:
            cmd = [
                "cmd", "/c", "mode", f"{self.port}:",
                f"BAUD={self.baudrate}", "PARITY=N", "DATA=8", "STOP=1"
            ]
            self.on_dev("MODE_ATTEMPT: " + " ".join(cmd))
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            out = (completed.stdout or "") + (completed.stderr or "")
            out = out.strip().replace("\r", "")
            self.on_dev(f"MODE_EXIT={completed.returncode}: {out[:500] if out else '<brak wyjścia>'}")
        except Exception as exc:
            self.on_dev(f"MODE_FAIL: {type(exc).__name__}: {exc!r}")

    def _open_raw_win(self):
        self.on_dev("RAW_WIN_ATTEMPT: otwieram COM przez CreateFile/ReadFile bez pyserial SetCommState")
        ser = RawWinSerial(self.port)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        self.on_dev("RAW_WIN_OK: port otwarty surowym WinAPI po próbie ustawienia parametrów przez Windows MODE.")
        return ser

    def close(self):
        self._closed = True
        try:
            self.ser.close()
        except Exception:
            pass

    def write(self, text: str):
        if self._closed:
            return
        wire = text if text.endswith("\r") else text + "\r"
        self.ser.write(wire.encode("ascii", errors="ignore"))
        self.ser.flush()
        display = f"\n>>> {text}\n" if text else "\n>>> <ENTER>\n"
        self._append_log(display)
        self.on_rx(display)

    def read_some(self) -> str:
        if self._closed:
            return ""
        data = self.ser.read(4096)
        if not data:
            return ""
        text = data.decode("utf-8", errors="replace")
        with self._lock:
            self.buffer += text
            if len(self.buffer) > self.MAX_BUFFER_CHARS:
                self.buffer = self.buffer[-self.MAX_BUFFER_CHARS:]
        self._append_log(text)
        self.on_rx(text)
        return text

    def clear_buffer(self):
        with self._lock:
            self.buffer = ""

    def get_buffer(self) -> str:
        with self._lock:
            return self.buffer

    def wait_for(self, patterns: List[str], timeout: float) -> Optional[str]:
        compiled = [re.compile(p, re.IGNORECASE | re.MULTILINE | re.DOTALL) for p in patterns]
        deadline = time.time() + timeout
        while time.time() < deadline and not self._closed:
            self.read_some()
            buf = self.get_buffer()
            for raw, rx in zip(patterns, compiled):
                if rx.search(buf):
                    return raw
            time.sleep(0.05)
        return None

    def _append_log(self, text: str):
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)


def diagnose_open_error(port: str, exc: Exception, flow_profile: str = "none") -> str:
    base = f"Nie mogę otworzyć {port}. PySerial/Windows zwrócił: {type(exc).__name__}: {exc!r}."
    msg = str(exc).lower()
    hints = []
    if "access is denied" in msg or "permissionerror" in type(exc).__name__.lower() or "odmowa" in msg:
        hints.append("Port jest prawdopodobnie zajęty przez Tera Term, PuTTY, poprzednią instancję aplikacji albo zawieszony sterownik.")
    if "nie działa" in msg or "31" in msg or "attached to the system is not functioning" in msg:
        hints.append("Windows zgłasza WinError 31 podczas konfiguracji portu. To często nie oznacza uszkodzenia switcha, tylko sterownik USB-serial odrzuca ustawienia DCB/SetCommState używane przez pyserial.")
        hints.append("Spróbuj Flow = raw-win-only. Ten tryb omija konfigurację pyserial i czyta/pisze po COM przez surowe WinAPI.")
    if flow_profile != "none":
        hints.append("Spróbuj profilu Flow control = none. Dla Cisco console to najlepszy default w tym prototypie.")
    hints.append("Sprawdź w Menedżerze urządzeń, czy COM nie ma żółtego ostrzeżenia i czy numer COM jest ten sam co w aplikacji.")
    hints.append("Po zamknięciu Tera Term poczekaj 2-3 sekundy przed startem testu; niektóre sterowniki zwalniają uchwyt z opóźnieniem.")
    return base + "\n\nSugestie:\n- " + "\n- ".join(hints)


class DemoConsole:
    """Small fake console for UI testing without hardware."""

    def __init__(self, scenario: str, on_rx=None, on_dev=None, log_dir="logs"):
        self.scenario = scenario
        self.on_rx = on_rx or (lambda text: None)
        self.on_dev = on_dev or (lambda text: None)
        self.buffer = ""
        self._closed = False
        Path(log_dir).mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = str(Path(log_dir) / f"DEMO_{scenario}_{ts}.log")
        self._booted = False
        self._enabled = False
        self._config = False

    def close(self):
        self._closed = True

    def write(self, text: str):
        if self._closed:
            return
        display = f"\n>>> {text}\n" if text else "\n>>> <ENTER>\n"
        self._emit(display)
        cmd = text.strip().lower()
        time.sleep(0.2)

        if not self._booted:
            self._booted = True
            if self.scenario == "console_password":
                self._emit("\r\nUser Access Verification\r\nPassword: ")
            else:
                self._emit("\r\n--- System Configuration Dialog ---\r\nWould you like to enter the initial configuration dialog? [yes/no]: ")
            return

        if cmd == "no":
            self._emit("\r\nPress RETURN to get started!\r\n")
            return

        if cmd == "":
            self._emit("\r\nSwitch> ")
            return

        if cmd == "enable":
            if self.scenario == "enable_password":
                self._emit("\r\nPassword: ")
            else:
                self._enabled = True
                self._emit("\r\nSwitch# ")
            return

        if cmd in ("configure terminal", "conf t"):
            if self._enabled:
                self._config = True
                self._emit("\r\nEnter configuration commands, one per line. End with CNTL/Z.\r\nSwitch(config)# ")
            else:
                self._emit("\r\n% Privilege level insufficient\r\nSwitch> ")
            return

        if cmd == "end":
            self._config = False
            self._emit("\r\nSwitch# ")
            return

        self._emit("\r\nSwitch# ")

    def read_some(self):
        return ""

    def clear_buffer(self):
        self.buffer = ""

    def get_buffer(self):
        return self.buffer

    def wait_for(self, patterns: List[str], timeout: float) -> Optional[str]:
        compiled = [re.compile(p, re.IGNORECASE | re.MULTILINE | re.DOTALL) for p in patterns]
        deadline = time.time() + timeout
        while time.time() < deadline and not self._closed:
            buf = self.buffer
            for raw, rx in zip(patterns, compiled):
                if rx.search(buf):
                    return raw
            time.sleep(0.05)
        return None

    def _emit(self, text: str):
        self.buffer += text
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
        self.on_rx(text)


def probe_port_open(port: str, baudrate: int, on_dev: Callable[[str], None], flow_profile: str = "none"):
    """Developer diagnostic: try several open paths and report exact failing layer."""
    on_dev(f"PROBE_START: port={port}, baud={baudrate}, flow={flow_profile}")
    ports = list_serial_ports()
    match = next((p for p in ports if p.device.upper() == port.upper()), None)
    if match:
        on_dev(
            "PROBE_PORT_INFO: "
            f"device={match.device}, desc={match.description}, manufacturer={match.manufacturer}, "
            f"vid={match.vid}, pid={match.pid}, serial={match.serial_number}, hwid={match.hwid}"
        )
    else:
        on_dev("PROBE_PORT_INFO: portu nie ma na liście pyserial.tools.list_ports")

    if os.name == "nt":
        try:
            cmd = ["cmd", "/c", "mode", f"{port}:", f"BAUD={baudrate}", "PARITY=N", "DATA=8", "STOP=1"]
            on_dev("PROBE_MODE_CMD: " + " ".join(cmd))
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            out = ((completed.stdout or "") + (completed.stderr or "")).strip().replace("\r", "")
            on_dev(f"PROBE_MODE_EXIT={completed.returncode}: {out[:800] if out else '<brak wyjścia>'}")
        except Exception as exc:
            on_dev(f"PROBE_MODE_EXCEPTION: {type(exc).__name__}: {exc!r}")

    flags = {
        "none": {"xonxoff": False, "rtscts": False, "dsrdtr": False},
        "xonxoff": {"xonxoff": True, "rtscts": False, "dsrdtr": False},
        "rtscts": {"xonxoff": False, "rtscts": True, "dsrdtr": False},
    }.get(flow_profile, {"xonxoff": False, "rtscts": False, "dsrdtr": False})

    if serial is not None:
        try:
            on_dev(f"PROBE_PYSERIAL_ATTEMPT: flags={flags}")
            ser = serial.Serial(port=port, baudrate=baudrate, bytesize=8, parity="N", stopbits=1, timeout=0.2, write_timeout=1, **flags)
            on_dev("PROBE_PYSERIAL_OK: otwarcie przez pyserial działa")
            ser.close()
        except Exception as exc:
            on_dev(f"PROBE_PYSERIAL_FAIL: {type(exc).__name__}: {exc!r}")

    if os.name == "nt":
        try:
            on_dev("PROBE_RAW_WIN_ATTEMPT")
            raw = RawWinSerial(port)
            on_dev("PROBE_RAW_WIN_OK: CreateFile/ReadFile fallback działa")
            raw.close()
        except Exception as exc:
            on_dev(f"PROBE_RAW_WIN_FAIL: {type(exc).__name__}: {exc!r}")

    on_dev("PROBE_DONE")
