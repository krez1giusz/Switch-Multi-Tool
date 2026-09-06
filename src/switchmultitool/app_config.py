from configparser import ConfigParser
from pathlib import Path


CONFIG_PATH = Path("config") / "switchmultitool.ini"


DEFAULT_CONFIG = {
    "ui": {
        "terminal_max_chars": "180000",
    },
    "serial": {
        "default_baud": "9600",
        "flow_profiles": "none,xonxoff,rtscts,raw-win-fallback,raw-win-only",
        "log_dir": "logs",
    },
    "paths": {
        "results_csv": "results/check_results.csv",
        "order_excel_backup_dir": "backups/order_excel",
    },
    "detector": {
        "boot_max_timeout": "360",
        "boot_idle_timeout": "55",
        "nudge_timeout": "35",
        "prompt_timeout": "75",
        "enable_timeout": "15",
        "config_mode_timeout": "12",
    },
    "set_test_password": {
        "enable_secret": "Test123!",
        "prompt_timeout": "45",
        "enable_timeout": "12",
        "config_timeout": "10",
        "end_timeout": "8",
        "write_memory_timeout": "15",
        "disable_timeout": "6",
    },
    "recovery": {
        "total_steps": "5",
        "switch_prompt_timeout": "300",
        "flash_init_timeout": "60",
        "load_helper_timeout": "15",
        "dir_flash_timeout": "20",
        "delete_timeout": "15",
        "boot_max_timeout": "360",
        "boot_idle_timeout": "65",
        "post_boot_nudge_timeout": "35",
        "user_prompt_timeout": "40",
        "priv_prompt_timeout": "10",
        "delete_files": "\nconfig.text|required\nvlan.dat|optional\nprivate-config.text|optional\nconfig.old|optional",
    },
    "adapter:0x1A86:0x7523": {
        "name": "WCH CH340 / CH341",
        "recommended_flow": "raw-win-only",
    },
    "adapter:0x0403:0x6001": {
        "name": "FTDI FT232",
        "recommended_flow": "none",
    },
    "adapter:0x067B:0x2303": {
        "name": "Prolific PL2303",
        "recommended_flow": "none",
    },
    "recovery_instruction:WS-C2960C": {
        "text": (
            "Instrukcja dla Cisco Catalyst 2960-C / WS-C2960C\n"
            "1. Odepnij zasilanie switcha.\n"
            "2. Wcisnij i trzymaj przycisk MODE.\n"
            "3. Podepnij zasilanie, caly czas trzymajac MODE.\n"
            "4. Pusc MODE dopiero gdy dioda SYST mignie pomaranczowo dwa razy.\n"
            "5. Po chwili dioda SYST powinna swiecic stalym zielonym.\n"
            "Aplikacja nasluchuje promptu switch: i przejdzie dalej automatycznie."
        ),
    },
    "recovery_instruction:default": {
        "text": (
            "Instrukcja ogolna, model nie zostal jeszcze odczytany\n"
            "1. Wylacz zasilanie switcha.\n"
            "2. Wcisnij i trzymaj przycisk MODE.\n"
            "3. Wlacz zasilanie, caly czas trzymajac MODE.\n"
            "4. Pusc MODE dopiero gdy switch wejdzie w tryb recovery/boot loader.\n"
            "Aplikacja nasluchuje promptu switch: i przejdzie dalej automatycznie."
        ),
    },
    "command_profile:Cisco IOS generic": {
        "terminal_length_command": "terminal length 0",
        "read_info_commands": "show version",
    },
    "command_profile:Cisco Catalyst 2960 / 2960-C": {
        "terminal_length_command": "terminal length 0",
        "read_info_commands": "show version",
    },
    "command_profile:Cisco Catalyst 2960X": {
        "terminal_length_command": "terminal length 0",
        "read_info_commands": "show version",
    },
}


class AppConfig:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.parser = ConfigParser()
        self.parser.read_dict(DEFAULT_CONFIG)
        self.parser.read(path, encoding="utf-8")

    def get(self, section: str, option: str, fallback: str = "") -> str:
        return self.parser.get(section, option, fallback=fallback)

    def getint(self, section: str, option: str, fallback: int) -> int:
        try:
            return self.parser.getint(section, option, fallback=fallback)
        except ValueError:
            return fallback

    def getlist(self, section: str, option: str, fallback: list[str]) -> list[str]:
        raw = self.get(section, option, "")
        if not raw:
            return fallback
        values = []
        for line in raw.replace(",", "\n").splitlines():
            value = line.strip()
            if value:
                values.append(value)
        return values or fallback

    @property
    def terminal_max_chars(self) -> int:
        return self.getint("ui", "terminal_max_chars", 180_000)

    @property
    def default_baud(self) -> str:
        return self.get("serial", "default_baud", "9600")

    @property
    def flow_profiles(self) -> list[str]:
        return self.getlist("serial", "flow_profiles", ["none", "xonxoff", "rtscts", "raw-win-fallback", "raw-win-only"])

    @property
    def serial_log_dir(self) -> str:
        return self.get("serial", "log_dir", "logs")

    @property
    def results_csv(self) -> str:
        return self.get("paths", "results_csv", "results/check_results.csv")

    @property
    def order_excel_backup_dir(self) -> str:
        return self.get("paths", "order_excel_backup_dir", "backups/order_excel")

    @property
    def test_password(self) -> str:
        return self.get("set_test_password", "enable_secret", "Test123!")

    def known_adapter_hints(self) -> dict[tuple[str, str], dict[str, str]]:
        hints = {}
        for section in self.parser.sections():
            if not section.lower().startswith("adapter:"):
                continue
            parts = section.split(":", 2)
            if len(parts) != 3:
                continue
            vid = parts[1]
            pid = parts[2]
            hints[(vid, pid)] = {
                "name": self.get(section, "name", ""),
                "recommended_flow": self.get(section, "recommended_flow", "none"),
            }
        return hints

    def recovery_delete_files(self) -> list[tuple[str, bool]]:
        raw = self.get("recovery", "delete_files", "")
        files = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                filename, mode = [part.strip() for part in line.split("|", 1)]
            else:
                filename, mode = line, "optional"
            if filename:
                files.append((filename, mode.lower() in {"required", "true", "yes", "1"}))
        return files or [
            ("config.text", True),
            ("vlan.dat", False),
            ("private-config.text", False),
            ("config.old", False),
        ]

    def recovery_instructions(self) -> tuple[dict[str, str], str]:
        instructions = {}
        default_text = self.get("recovery_instruction:default", "text", "")
        for section in self.parser.sections():
            if not section.lower().startswith("recovery_instruction:"):
                continue
            key = section.split(":", 1)[1]
            text = self.get(section, "text", "")
            if key.lower() == "default":
                default_text = text
            elif key and text:
                instructions[key] = text
        return instructions, default_text

    def command_profile(self, profile_name: str) -> dict[str, list[str] | str]:
        section = f"command_profile:{profile_name}"
        if not self.parser.has_section(section):
            section = "command_profile:Cisco IOS generic"
        return {
            "terminal_length_command": self.get(section, "terminal_length_command", "terminal length 0"),
            "read_info_commands": self.getlist(section, "read_info_commands", ["show version"]),
        }


APP_CONFIG = AppConfig()
