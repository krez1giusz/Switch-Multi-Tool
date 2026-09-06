import time
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .app_config import APP_CONFIG

RE_SWITCH_PROMPT = re.compile(r"switch\s*:", re.IGNORECASE)
RE_MORE          = re.compile(r"--\s*MORE\s*--", re.IGNORECASE)
RE_PROMPT_USER   = re.compile(r"(?m)^[A-Za-z0-9._()/-]+>\s*$")
RE_PROMPT_PRIV   = re.compile(r"(?m)^[A-Za-z0-9._()/-]+#\s*$")
RE_PROMPT_CONF   = re.compile(r"(?m)^[A-Za-z0-9._()/-]+\(config[^)]*\)#\s*$")
RE_DIALOG        = re.compile(r"\[yes/no\]", re.IGNORECASE)
RE_PLEASE_NO     = re.compile(r"Please answer 'yes' or 'no'", re.IGNORECASE)
RE_PRESS_RET     = re.compile(r"Press RETURN to get started", re.IGNORECASE)
RE_DEST_FNAME    = re.compile(r"Destination filename", re.IGNORECASE)
RE_COPY_OK       = re.compile(r"\[OK\]|bytes copied", re.IGNORECASE)

TEST_PASSWORD = APP_CONFIG.test_password
TOTAL_STEPS   = APP_CONFIG.getint("recovery", "total_steps", 5)


@dataclass
class WizardResult:
    success: bool
    message: str
    log_path: str = ""


class RecoveryWizard:
    def __init__(self, console, port_label: str, on_event: Callable[[str, object], None]):
        self.console     = console
        self.port_label  = port_label
        self.on_event    = on_event
        self._aborted    = False

    def abort(self):
        self._aborted = True

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> WizardResult:
        """
        Wywoływane po tym jak operator potwierdził trzymanie MODE.
        Zakłada że port jest już otwarty.
        """
        c = self.console

        # Krok 1 — czekaj na switch: (operator jeszcze może przygotowywać switch)
        self._step(1, "Czekam na switch:", "Wykonaj kroki: wyłącz → trzymaj MODE → włącz → puść po pomarańczowej diodzie")
        if not self._wait_switch_prompt(timeout=APP_CONFIG.getint("recovery", "switch_prompt_timeout", 300)):
            return self._fail(
                "Nie wykryto promptu switch: w ciągu 5 minut.\n"
                "Upewnij się że:\n"
                "- trzymasz MODE podczas włączania\n"
                "- puszczasz gdy dioda SYST zmienia kolor na pomarańczowy\n"
                "- kabel konsolowy jest podłączony"
            )

        # Krok 2 — komendy ROMMON
        self._step(2, "ROMMON: flash_init", "Inicjalizacja flash — może chwilę trwać...")
        c.clear_buffer()
        c.write("flash_init")
        if not self._wait_switch_prompt(timeout=APP_CONFIG.getint("recovery", "flash_init_timeout", 60)):
            return self._fail("flash_init nie zakończył się w czasie")

        self._step(2, "ROMMON: load_helper", "")
        c.clear_buffer()
        c.write("load_helper")
        if not self._wait_switch_prompt(timeout=APP_CONFIG.getint("recovery", "load_helper_timeout", 15)):
            time.sleep(2)  # load_helper może nic nie zwrócić — to OK

        self._step(2, "ROMMON: dir flash:", "Sprawdzam pliki na flash...")
        c.clear_buffer()
        c.write("dir flash:")
        if not self._wait_switch_prompt(timeout=APP_CONFIG.getint("recovery", "dir_flash_timeout", 20)):
            return self._fail("dir flash: nie odpowiedział")

        for filename, required in APP_CONFIG.recovery_delete_files():
            detail = "Kasuje konfiguracje..." if required else ""
            self._step(2, f"ROMMON: delete {filename}", detail)
            if not self._rommon_delete(filename, required=required):
                return self._fail(f"delete flash:{filename} nie powiodlo sie")

        # Krok 3 — boot
        self._step(3, "Wysyłam boot — switch restartuje się...", "Nie ruszaj kabla. Boot IOS trwa 2–3 min.")
        c.clear_buffer()
        c.write("boot")

        # Krok 4 — czekaj na boot IOS
        self._step(4, "Czekam na boot IOS...", "Switch wczytuje IOS...")
        time.sleep(6)
        c.clear_buffer()

        hit = self._wait_boot(
            max_timeout=APP_CONFIG.getint("recovery", "boot_max_timeout", 360),
            idle_timeout=APP_CONFIG.getint("recovery", "boot_idle_timeout", 65),
        )
        if hit is None:
            c.write("")
            hit = self._wait_any(timeout=APP_CONFIG.getint("recovery", "post_boot_nudge_timeout", 35))
        if hit is None:
            return self._fail("Brak sygnału z konsoli po restarcie. Sprawdź kabel.")

        hit = self._handle_dialogs(hit)
        if hit is None:
            return self._fail("Timeout podczas obsługi boot dialogu")

        # Krok 5 — czekaj na prompt i zakończ
        self._step(5, "Czekam na prompt switcha...", "Switch startuje bez konfiguracji i haseł")

        if hit not in ("user_prompt", "priv_prompt"):
            if not self._wait_user_prompt(timeout=APP_CONFIG.getint("recovery", "user_prompt_timeout", 40)):
                if not self._wait_priv(timeout=APP_CONFIG.getint("recovery", "priv_prompt_timeout", 10)):
                    return self._fail("Brak promptu po boot — sprawdź konsolę")

        return self._ok("Reset fabryczny zakończony!\nSwitch uruchomił się bez konfiguracji i haseł.")

    # ------------------------------------------------------------------
    # Dialog handling (identyczna logika jak w PasswordDetector)
    # ------------------------------------------------------------------

    def _handle_dialogs(self, initial_hit: str) -> Optional[str]:
        c = self.console
        hit = initial_hit
        for _ in range(8):
            if self._aborted:
                return None
            buf = c.get_buffer()
            if RE_PLEASE_NO.search(buf):
                self.on_event("WIZARD_LOG", "SEND_NO (please answer)")
                c.clear_buffer()
                c.write("no")
                time.sleep(0.5)
                hit = self._wait_any(timeout=20)
                continue
            if RE_DIALOG.search(buf):
                self.on_event("WIZARD_LOG", "SEND_NO (config dialog)")
                c.clear_buffer()
                c.write("no")
                time.sleep(0.5)
                hit = self._wait_any(timeout=25)
                continue
            if RE_PRESS_RET.search(buf):
                self.on_event("WIZARD_LOG", "SEND_ENTER (Press RETURN)")
                c.clear_buffer()
                c.write("")
                time.sleep(0.8)
                hit = self._wait_any(timeout=25)
                continue
            break
        return hit

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    def _rommon_delete(self, filename: str, required: bool = True) -> bool:
        """Wysyła delete flash:filename, obsługuje pytanie (y/n)?, czeka na switch:."""
        c = self.console
        c.clear_buffer()
        c.write(f"delete flash:{filename}")
        deadline = time.time() + APP_CONFIG.getint("recovery", "delete_timeout", 15)
        while time.time() < deadline and not self._aborted:
            c.read_some()
            buf = c.get_buffer()
            if RE_SWITCH_PROMPT.search(buf):
                return True
            if re.search(r"\(y/n\)\?", buf):
                c.write("y")
                continue
            time.sleep(0.05)
        return not required  # jeśli nie wymagany, milcząco OK

    def _wait_re(self, rx: re.Pattern, timeout: float) -> bool:
        c = self.console
        deadline = time.time() + timeout
        while time.time() < deadline and not self._aborted:
            c.read_some()
            if rx.search(c.get_buffer()):
                return True
            time.sleep(0.05)
        return False

    def _wait_switch_prompt(self, timeout: float) -> bool:
        """Czeka na switch: — automatycznie dismissuje -- MORE -- spacją."""
        c = self.console
        deadline = time.time() + timeout
        while time.time() < deadline and not self._aborted:
            c.read_some()
            buf = c.get_buffer()
            if RE_SWITCH_PROMPT.search(buf):
                return True
            if RE_MORE.search(buf):
                self.on_event("WIZARD_LOG", "MORE — wysyłam spację")
                c.write(" ")
                time.sleep(0.3)
                continue
            time.sleep(0.05)
        return False

    def _wait_priv(self, timeout: float) -> bool:
        c = self.console
        deadline = time.time() + timeout
        while time.time() < deadline and not self._aborted:
            c.read_some()
            if RE_PROMPT_PRIV.search(c.get_buffer().rstrip()[-150:]):
                return True
            time.sleep(0.05)
        return False

    def _wait_user_prompt(self, timeout: float) -> bool:
        c = self.console
        deadline = time.time() + timeout
        while time.time() < deadline and not self._aborted:
            c.read_some()
            if RE_PROMPT_USER.search(c.get_buffer().rstrip()[-150:]):
                return True
            time.sleep(0.05)
        return False

    def _wait_any(self, timeout: float) -> Optional[str]:
        c = self.console
        deadline = time.time() + timeout
        while time.time() < deadline and not self._aborted:
            c.read_some()
            buf = c.get_buffer()
            if RE_PLEASE_NO.search(buf):         return "please_no"
            if RE_DIALOG.search(buf):            return "dialog"
            if RE_PRESS_RET.search(buf):         return "press_return"
            if re.search(r"Password:", buf[-200:], re.IGNORECASE): return "password"
            if RE_PROMPT_PRIV.search(buf.rstrip()[-150:]): return "priv_prompt"
            if RE_PROMPT_USER.search(buf.rstrip()[-150:]): return "user_prompt"
            if RE_SWITCH_PROMPT.search(buf):     return "switch_prompt"
            time.sleep(0.05)
        return None

    def _wait_boot(self, max_timeout: float, idle_timeout: float) -> Optional[str]:
        c = self.console
        start = time.time()
        last_activity = time.time()
        last_len = len(c.get_buffer())
        last_note = 0

        while time.time() - start < max_timeout and not self._aborted:
            c.read_some()
            buf = c.get_buffer()
            now = time.time()
            if len(buf) != last_len:
                last_len = len(buf)
                last_activity = now
            hit = self._check_buf(buf)
            if hit:
                return hit
            if now - last_note > 20:
                elapsed = int(now - start)
                idle = int(now - last_activity)
                self.on_event("WIZARD_LOG", f"Czekam na boot IOS; elapsed={elapsed}s idle={idle}s")
                last_note = now
            if now - last_activity > idle_timeout:
                return None
            time.sleep(0.05)
        return None

    def _check_buf(self, buf: str) -> Optional[str]:
        if RE_PLEASE_NO.search(buf):                      return "please_no"
        if RE_DIALOG.search(buf):                         return "dialog"
        if RE_PRESS_RET.search(buf):                      return "press_return"
        if re.search(r"Password:", buf[-200:], re.I):     return "password"
        if RE_PROMPT_PRIV.search(buf.rstrip()[-150:]):    return "priv_prompt"
        if RE_PROMPT_USER.search(buf.rstrip()[-150:]):    return "user_prompt"
        return None

    # ------------------------------------------------------------------

    def _step(self, num: int, title: str, detail: str):
        self.on_event("WIZARD_STEP", (num, TOTAL_STEPS, title, detail))

    def _ok(self, message: str) -> WizardResult:
        self.on_event("WIZARD_DONE", (True, message))
        return WizardResult(True, message, getattr(self.console, "log_path", ""))

    def _fail(self, message: str) -> WizardResult:
        self.on_event("WIZARD_DONE", (False, message))
        return WizardResult(False, message, getattr(self.console, "log_path", ""))


# ------------------------------------------------------------------
# Set Test Password — osobna akcja, nie wizard
# ------------------------------------------------------------------

class SetTestPasswordAction:
    def __init__(self, console, on_event: Callable[[str, object], None]):
        self.console  = console
        self.on_event = on_event
        self._aborted = False

    def abort(self):
        self._aborted = True

    def run(self) -> WizardResult:
        c = self.console
        self.on_event("STP_LOG", "Budzenie konsoli...")

        for _ in range(2):
            c.write("")
            time.sleep(0.7)

        # Szybki boot wait — switch powinien już stać na prompcie
        deadline = time.time() + APP_CONFIG.getint("set_test_password", "prompt_timeout", 45)
        hit = None
        while time.time() < deadline and not self._aborted:
            c.read_some()
            buf = c.get_buffer()
            if RE_PLEASE_NO.search(buf):
                c.clear_buffer(); c.write("no"); time.sleep(0.5)
            elif RE_DIALOG.search(buf):
                c.clear_buffer(); c.write("no"); time.sleep(0.5)
            elif RE_PRESS_RET.search(buf):
                c.clear_buffer(); c.write(""); time.sleep(0.8)
            elif RE_PROMPT_USER.search(buf.rstrip()[-150:]):
                hit = "user"; break
            elif RE_PROMPT_PRIV.search(buf.rstrip()[-150:]):
                hit = "priv"; break
            time.sleep(0.05)

        if hit is None:
            return WizardResult(False, "Brak promptu switcha — sprawdź połączenie")

        if hit == "user":
            self.on_event("STP_LOG", "Wysyłam enable")
            c.clear_buffer()
            c.write("enable")
            deadline = time.time() + APP_CONFIG.getint("set_test_password", "enable_timeout", 12)
            while time.time() < deadline and not self._aborted:
                c.read_some()
                buf = c.get_buffer()
                if RE_PROMPT_PRIV.search(buf.rstrip()[-150:]):
                    hit = "priv"; break
                if re.search(r"Password:", buf[-100:], re.I):
                    return WizardResult(False, "Switch pyta o hasło enable — jest już zablokowany. Użyj Recovery Wizard.")
                time.sleep(0.05)
            if hit != "priv":
                return WizardResult(False, "Brak odpowiedzi na enable")

        # Jesteśmy w # — ustawiamy hasło
        self.on_event("STP_LOG", "Wchodzę w configure terminal")
        c.clear_buffer()
        c.write("configure terminal")
        if not self._wait_re(RE_PROMPT_CONF, timeout=APP_CONFIG.getint("set_test_password", "config_timeout", 10)):
            return WizardResult(False, "Nie udało się wejść w configure terminal")

        self.on_event("STP_LOG", f"Ustawiam enable secret {TEST_PASSWORD}")
        c.clear_buffer()
        c.write(f"enable secret {TEST_PASSWORD}")
        time.sleep(1)

        c.clear_buffer()
        c.write("end")
        if not self._wait_re(RE_PROMPT_PRIV, timeout=APP_CONFIG.getint("set_test_password", "end_timeout", 8)):
            return WizardResult(False, "Brak powrotu do # po end")

        self.on_event("STP_LOG", "write memory")
        c.clear_buffer()
        c.write("write memory")
        deadline2 = time.time() + APP_CONFIG.getint("set_test_password", "write_memory_timeout", 15)
        while time.time() < deadline2 and not self._aborted:
            c.read_some()
            if re.search(r"\[OK\]|Building configuration|bytes", c.get_buffer(), re.I):
                break
            time.sleep(0.1)

        # Wróć do user exec — żeby następny Check Password widział > a nie #
        c.clear_buffer()
        c.write("disable")
        deadline3 = time.time() + APP_CONFIG.getint("set_test_password", "disable_timeout", 6)
        while time.time() < deadline3 and not self._aborted:
            c.read_some()
            if RE_PROMPT_USER.search(c.get_buffer().rstrip()[-100:]):
                break
            time.sleep(0.05)

        return WizardResult(True, f"Hasło enable secret ustawione: {TEST_PASSWORD}\nUruchom Check password — switch powinien być LOCKED.")

    def _wait_re(self, rx: re.Pattern, timeout: float) -> bool:
        c = self.console
        deadline = time.time() + timeout
        while time.time() < deadline and not self._aborted:
            c.read_some()
            if rx.search(c.get_buffer()):
                return True
            time.sleep(0.05)
        return False
