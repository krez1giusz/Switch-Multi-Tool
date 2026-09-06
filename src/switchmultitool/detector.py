from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import time
import re
from typing import Callable, Optional, List

from .app_config import APP_CONFIG


@dataclass
class DetectionResult:
    status: str
    reason: str
    log_path: str
    port: str = ""
    timestamp: str = ""


# Ścisłe regexy promptów — muszą być na końcu linii, nie łapiemy losowych > z boot loga.
RE_PROMPT_USER = re.compile(r"(?m)^[A-Za-z0-9._()/-]+>\s*$")
RE_PROMPT_PRIV = re.compile(r"(?m)^[A-Za-z0-9._()/-]+#\s*$")
RE_PROMPT_CONF = re.compile(r"(?m)^[A-Za-z0-9._()/-]+\(config[^)]*\)#\s*$")
RE_DIALOG      = re.compile(r"\[yes/no\]", re.IGNORECASE)
RE_PLEASE_NO   = re.compile(r"Please answer 'yes' or 'no'", re.IGNORECASE)
RE_PRESS_RET   = re.compile(r"Press RETURN to get started", re.IGNORECASE)
RE_MORE        = re.compile(r"--\s*MORE\s*--", re.IGNORECASE)
RE_PASSWORD    = re.compile(r"Password:", re.IGNORECASE)
RE_UAV         = re.compile(r"User Access Verification", re.IGNORECASE)

# Parser boot loga — wyciąga dane urządzenia bez show version
_BOOT_PARSERS = [
    ("model",   re.compile(r"(?:Model number|cisco)\s*[:\s]+([A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+(?:-[A-Z0-9]+)*)", re.IGNORECASE)),
    ("serial",  re.compile(r"(?:System serial number|Processor board ID)\s*[:\s]+([A-Z0-9]+)", re.IGNORECASE)),
    ("serial",  re.compile(r"\bSN\s*:\s*([A-Z0-9]+)", re.IGNORECASE)),
    ("mac",     re.compile(r"Base [Ee]thernet MAC [Aa]ddress[:\s]+([0-9A-Fa-f]{2}(?:[:.][0-9A-Fa-f]{2}){5})")),
    ("ios",     re.compile(r"Cisco IOS Software.*?\bVersion\s+([^,\s]+)", re.IGNORECASE | re.DOTALL)),
    ("ios",     re.compile(r"\bVersion\s+(\d[\d.()A-Za-z]+)\s*,", re.IGNORECASE)),
    ("pid",     re.compile(r"PID\s*:\s*([A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+(?:-[A-Z0-9]+)*)", re.IGNORECASE)),
]


def parse_boot_info(buf: str) -> dict:
    info = {}
    for key, rx in _BOOT_PARSERS:
        m = rx.search(buf)
        if m and key not in info:
            info[key] = m.group(1).strip()
    return info


def _tail_has_prompt_user(buf: str) -> bool:
    last = buf.rstrip()[-200:]
    return bool(RE_PROMPT_USER.search(last))


def _tail_has_prompt_priv(buf: str) -> bool:
    last = buf.rstrip()[-200:]
    return bool(RE_PROMPT_PRIV.search(last))


def _buf_has_dialog(buf: str) -> bool:
    return bool(RE_DIALOG.search(buf))


class PasswordDetector:
    def __init__(self, console, port_label: str, on_state: Optional[Callable[[str, str], None]] = None):
        self.console = console
        self.port_label = port_label
        self.on_state = on_state or (lambda state, detail: None)
        self.boot_info: dict = {}

    def state(self, state: str, detail: str = ""):
        self.on_state(state, detail)

    def _dismiss_more(self):
        self.state("PAGER_MORE", "Konsola stoi na --More--, wysylam spacje")
        self.console.clear_buffer()
        self.console.write(" ")
        time.sleep(0.25)

    def run(self) -> DetectionResult:
        c = self.console
        try:
            # 1. Budź konsolę
            self.state("INIT", "Budzenie konsoli przez ENTER")
            for _ in range(3):
                c.write("")
                time.sleep(0.7)

            # 2. Czekaj na cokolwiek sensownego z boot loga
            self.state("WAIT_BOOT", "Czekam na boot/dialog/prompt. Timeout wydłuża się jeśli switch nadal nadaje.")
            hit = self._wait_for_boot_or_idle(
                max_timeout=APP_CONFIG.getint("detector", "boot_max_timeout", 360),
                idle_timeout=APP_CONFIG.getint("detector", "boot_idle_timeout", 55),
            )

            if hit is None:
                self.state("NUDGE", "Brak promptu, wysyłam ENTER i czekam jeszcze chwilę")
                c.clear_buffer()
                c.write("")
                hit = self._wait_any(timeout=APP_CONFIG.getint("detector", "nudge_timeout", 35))

            if hit is None:
                return self._result("UNKNOWN", "Nie wykryto promptu ani pytania o hasło po boocie/idle")

            # 3. Obsłuż dialog konfiguracyjny w pętli (może pojawić się wielokrotnie)
            hit = self._handle_dialogs(hit)
            if hit is None:
                return self._result("UNKNOWN", "Timeout po obsłudze initial config dialog")

            # 4. Parsuj boot info — uzupełniaj to co już zebraliśmy podczas boot wait
            # (clear_buffer w _handle_dialogs kasuje boot log, więc tu możemy mieć pusty bufor)
            extra = parse_boot_info(c.get_buffer())
            for k, v in extra.items():
                if k not in self.boot_info:
                    self.boot_info[k] = v
            if self.boot_info:
                self.state("BOOT_INFO", f"Dane z boot loga: {self.boot_info}")

            # 5. Sprawdź czy Password na konsoli (przed promptem)
            buf = c.get_buffer()
            if (RE_PASSWORD.search(buf[-300:]) or RE_UAV.search(buf[-300:])) \
               and not _tail_has_prompt_user(buf) and not _tail_has_prompt_priv(buf):
                self.state("LOCKED_CONSOLE", "Switch pyta o hasło na konsoli")
                return self._result("LOCKED", "Wymagane hasło konsoli/login")

            # 6. Jeśli już privileged — nie ufamy temu (może zostać z poprzedniej sesji).
            # Wysyłamy disable żeby wrócić do > i sprawdzić enable właściwie.
            if _tail_has_prompt_priv(c.get_buffer()):
                self.state("FORCE_USER_MODE", "Widzę # — wysyłam disable, żeby rzetelnie sprawdzić hasło enable")
                c.clear_buffer()
                c.write("disable")
                deadline = time.time() + 8
                while time.time() < deadline and not c._closed:
                    c.read_some()
                    if _tail_has_prompt_user(c.get_buffer()):
                        break
                    time.sleep(0.05)
                if not _tail_has_prompt_user(c.get_buffer()):
                    # disable nie zadziałało — testuj config mode bezpośrednio
                    self.state("ENABLE_MODE", "disable nie zwróciło >, testuję configure terminal")
                    return self._test_config_mode()

            # 7. Musi być user exec prompt
            if not _tail_has_prompt_user(c.get_buffer()):
                # Czekaj jeszcze chwilę
                self.state("WAIT_PROMPT", "Czekam na prompt użytkownika lub Password:")
                prompt_hit = self._wait_any(timeout=APP_CONFIG.getint("detector", "prompt_timeout", 75))
                if prompt_hit is None:
                    return self._result("UNKNOWN", "Nie udało się potwierdzić promptu po starcie")
                # Ponownie sprawdź dialog
                hit = self._handle_dialogs(prompt_hit)
                if hit is None:
                    return self._result("UNKNOWN", "Timeout po obsłudze dialogu (drugi raz)")
                buf = c.get_buffer()
                if (RE_PASSWORD.search(buf[-300:]) or RE_UAV.search(buf[-300:])) \
                   and not _tail_has_prompt_user(buf) and not _tail_has_prompt_priv(buf):
                    self.state("LOCKED_CONSOLE", "Switch pyta o hasło na konsoli")
                    return self._result("LOCKED", "Wymagane hasło konsoli/login")
                if _tail_has_prompt_priv(buf):
                    self.state("ENABLE_MODE", "Już jesteśmy w trybie privileged exec")
                    return self._test_config_mode()
                if not _tail_has_prompt_user(buf):
                    return self._result("UNKNOWN", "Brak rozpoznanego promptu po oczekiwaniu")

            # 8. User exec — wyślij enable
            self.state("USER_MODE", "Wykryto user exec prompt, wysyłam enable")
            c.clear_buffer()
            c.write("enable")

            enable_hit = self._wait_any(timeout=APP_CONFIG.getint("detector", "enable_timeout", 15))

            if enable_hit is None:
                return self._result("UNKNOWN", "Brak jasnej odpowiedzi po komendzie enable")

            buf = c.get_buffer()

            if RE_PASSWORD.search(buf[-200:]) and not _tail_has_prompt_priv(buf):
                self.state("LOCKED_ENABLE", "Komenda enable wymaga hasła")
                return self._result("LOCKED", "Wymagane hasło enable")

            if _tail_has_prompt_priv(buf):
                self.state("ENABLE_OK", "Enable bez hasła, testuję configure terminal")
                return self._test_config_mode()

            # enable dało nieoczekiwany wynik
            return self._result("LOCKED", "Enable zwrócił odmowę dostępu albo błąd hasła")

        except Exception as exc:
            return self._result("ERROR", f"Błąd procedury: {exc}")

    # ------------------------------------------------------------------
    # Obsługa initial config dialog w pętli
    # ------------------------------------------------------------------

    def _handle_dialogs(self, initial_hit: str) -> Optional[str]:
        """
        Dopóki bufor zawiera [yes/no] albo 'Please answer yes or no',
        wysyłaj 'no' i czekaj na następny stan.
        Zwraca ostatni hit albo None przy timeout.
        """
        c = self.console
        hit = initial_hit
        MAX_LOOPS = 6

        for _ in range(MAX_LOOPS):
            buf = c.get_buffer()

            # % Please answer 'yes' or 'no'. — najwyższy priorytet
            if RE_PLEASE_NO.search(buf):
                self.state("SEND_NO", "Switch prosi o yes/no — wysyłam no")
                c.clear_buffer()
                c.write("no")
                time.sleep(0.5)
                hit = self._wait_any(timeout=20)
                continue

            # [yes/no]: — dialog konfiguracyjny
            if RE_DIALOG.search(buf):
                self.state("INITIAL_DIALOG", "Wykryto initial configuration dialog — wysyłam no")
                c.clear_buffer()
                c.write("no")
                self.state("WAIT_PROMPT_AFTER_NO", "Czekam na prompt po odpowiedzi no")
                time.sleep(0.5)
                hit = self._wait_any(timeout=25)
                continue

            # Press RETURN to get started
            if RE_PRESS_RET.search(buf):
                self.state("PRESS_RETURN", "Wysyłam ENTER po komunikacie Press RETURN")
                c.clear_buffer()
                c.write("")
                time.sleep(0.8)
                hit = self._wait_any(timeout=25)
                continue

            # Dotarliśmy do promptu / Password — wychodzimy z pętli
            break

        return hit

    # ------------------------------------------------------------------
    # Pomocniki wait
    # ------------------------------------------------------------------

    def _wait_any(self, timeout: float) -> Optional[str]:
        """Czeka na dowolny rozpoznawalny stan ze switcha."""
        c = self.console
        deadline = time.time() + timeout
        while time.time() < deadline and not c._closed:
            c.read_some()
            buf = c.get_buffer()
            if RE_PLEASE_NO.search(buf):
                return "please_no"
            if RE_DIALOG.search(buf):
                return "dialog"
            if RE_PRESS_RET.search(buf):
                return "press_return"
            if RE_MORE.search(buf):
                self._dismiss_more()
                continue
            if RE_UAV.search(buf):
                return "uav_password"
            if RE_PASSWORD.search(buf[-200:]):
                return "password"
            if _tail_has_prompt_priv(buf):
                return "priv_prompt"
            if _tail_has_prompt_user(buf):
                return "user_prompt"
            time.sleep(0.05)
        return None

    def _wait_for_boot_or_idle(self, max_timeout: float = 360, idle_timeout: float = 55) -> Optional[str]:
        """
        Czeka na pierwszy rozpoznawalny sygnał ze switcha.
        Nie uznaje timeoutu dopóki przez ostatnie idle_timeout sekund płynęły dane.
        """
        c = self.console
        start = time.time()
        last_activity = time.time()
        last_len = len(c.get_buffer())
        last_state_note = 0

        while time.time() - start < max_timeout:
            c.read_some()
            buf = c.get_buffer()
            now = time.time()

            if len(buf) != last_len:
                last_len = len(buf)
                last_activity = now

            # Parsuj boot info na bieżąco — rób to przed jakimkolwiek clear_buffer
            if len(buf) > 200:
                partial = parse_boot_info(buf)
                if partial:
                    merged = {**partial, **self.boot_info}  # boot_info ma priorytet (pierwsze trafienie)
                    if merged != self.boot_info:
                        self.boot_info = merged
                        self.state("BOOT_INFO", f"Dane z boot loga: {self.boot_info}")

            # Sprawdź czy coś rozpoznawalnego już jest
            if RE_MORE.search(buf):
                self._dismiss_more()
                last_len = len(c.get_buffer())
                last_activity = now
                continue

            hit = self._check_buf(buf)
            if hit:
                return hit

            if now - last_state_note > 20:
                idle = int(now - last_activity)
                elapsed = int(now - start)
                self.state("WAIT_BOOT", f"Czekam; elapsed={elapsed}s, idle={idle}s")
                last_state_note = now

            if now - last_activity > idle_timeout:
                return None

            time.sleep(0.05)

        return None

    def _check_buf(self, buf: str) -> Optional[str]:
        if RE_PLEASE_NO.search(buf):
            return "please_no"
        if RE_DIALOG.search(buf):
            return "dialog"
        if RE_PRESS_RET.search(buf):
            return "press_return"
        if RE_UAV.search(buf):
            return "uav_password"
        if RE_PASSWORD.search(buf[-200:]):
            return "password"
        if _tail_has_prompt_priv(buf):
            return "priv_prompt"
        if _tail_has_prompt_user(buf):
            return "user_prompt"
        return None

    # ------------------------------------------------------------------
    # Test configure terminal
    # ------------------------------------------------------------------

    def _test_config_mode(self) -> DetectionResult:
        c = self.console
        self.state("CONF_TEST", "Wysyłam configure terminal")
        c.clear_buffer()
        c.write("configure terminal")

        deadline = time.time() + APP_CONFIG.getint("detector", "config_mode_timeout", 12)
        while time.time() < deadline and not c._closed:
            c.read_some()
            buf = c.get_buffer()
            if RE_PROMPT_CONF.search(buf):
                self.state("UNLOCKED", "Dostęp do config mode potwierdzony")
                c.write("end")
                return self._result("UNLOCKED", "Enable i configure terminal dostępne bez hasła")
            if re.search(r"% Invalid input|% Authorization failed|% Privilege", buf, re.IGNORECASE):
                return self._result("PARTIAL_ACCESS", "Enable dostępny, ale config mode odmówiony")
            time.sleep(0.05)

        return self._result("PARTIAL_ACCESS", "Enable dostępny, ale nie potwierdzono config mode (timeout)")

    # ------------------------------------------------------------------

    def _result(self, status: str, reason: str) -> DetectionResult:
        result = DetectionResult(
            status=status,
            reason=reason,
            log_path=getattr(self.console, "log_path", ""),
            port=self.port_label,
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )
        save_result(result)
        self.state("DONE", f"{status}: {reason}")
        return result


def save_result(result: DetectionResult, csv_path=None):
    csv_path = csv_path or APP_CONFIG.results_csv
    Path("results").mkdir(exist_ok=True)
    path = Path(csv_path)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "port", "status", "reason", "log_path"])
        if not exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": result.timestamp,
            "port": result.port,
            "status": result.status,
            "reason": result.reason,
            "log_path": result.log_path,
        })
