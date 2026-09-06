import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from pathlib import Path

from .serial_backend import list_serial_ports, SerialConsole, probe_port_open
from .detector import DetectionResult, PasswordDetector, parse_boot_info
from .wizard import RecoveryWizard, SetTestPasswordAction, TEST_PASSWORD
from .order_excel import create_order_workbook_backup, mark_unlocked_in_workbook
from .app_config import APP_CONFIG
from .session_store import (
    KnownDevicesDB,
    SessionEntry,
    append_session_csv,
    new_session_id,
    write_report_xlsx,
)


APP_NAME = "SwitchMultiTool"
APP_VERSION = "1.0.0"
TERMINAL_MAX_CHARS = APP_CONFIG.terminal_max_chars
QUEUE_PROCESS_BUDGET_SEC = 0.035
SERIAL_TOKEN_RE = re.compile(r"\b[A-Z0-9]{6,}\b", re.IGNORECASE)


def _terminal_display_text(text: str) -> str:
    """Keep the live terminal readable and cheap to redraw during IOS boot spam."""
    if not text:
        return text
    text = re.sub(r"@{120,}", lambda m: f"@ x{len(m.group(0))}", text)
    cleaned = []
    for ch in text:
        if ch in "\r\n\t\b":
            cleaned.append(ch)
        elif ch == "\x00":
            continue
        elif ord(ch) < 32:
            cleaned.append(".")
        else:
            cleaned.append(ch)
    return "".join(cleaned)


def _extract_serial_from_text(text: str) -> str:
    if not text:
        return ""
    if ":" in text:
        text = text.split(":", 1)[1]
    text = re.sub(r"_x[0-9A-Fa-f]{4}_", "", text)
    serial_match = re.search(r"\b(?:serial(?:\s+number)?|s/n|sn)\s*[:=]?\s*([A-Z0-9]{6,})\b", text, re.IGNORECASE)
    if serial_match:
        return serial_match.group(1).upper()
    matches = SERIAL_TOKEN_RE.findall(text.upper())
    ignored = {
        "UNKNOWN",
        "UNTIL",
        "DEVICE",
        "READ",
        "ADAPTER",
        "SERIAL",
        "SHOWN",
        "ABOVE",
        "SWITCH",
        "SELECTED",
        "CONNECTION",
        "MANUFACTURER",
        "PRODUCT",
        "ODHACZONO",
        "ARKUSZU",
        "WIERSZ",
        "COPIED",
        "CLIPBOARD",
        "STATUS",
        "UNLOCKED",
    }
    for match in matches:
        if match not in ignored and not match.startswith("VID") and not match.startswith("PID"):
            return match
    return ""

# Instrukcje procedury MODE per model switcha.
# Klucz = fragment PID z boot loga (case-insensitive contains).
# Fallback = RECOVERY_INSTRUCTIONS_DEFAULT.
RECOVERY_INSTRUCTIONS, RECOVERY_INSTRUCTIONS_DEFAULT = APP_CONFIG.recovery_instructions()

STATUS_COLORS = {
    "UNLOCKED":      "#4caf50",
    "FACTORY_RESET": "#4caf50",
    "WIZARD_OK":     "#4caf50",
    "PWD_SET":       "#4caf50",
    "LOCKED":        "#f44336",
    "WIZARD_FAIL":   "#f44336",
    "ERROR":         "#f44336",
    "RUNNING":       "#4fc3f7",
    "WIZARD_WAIT":   "#4fc3f7",
    "SET_PWD":       "#4fc3f7",
    "PROBING":       "#4fc3f7",
    "PROBE_DONE":    "#90caf9",
    "PARTIAL_ACCESS":"#ff9800",
    "UNKNOWN":       "#ff9800",
    "ABORTED":       "#9e9e9e",
    "STOPPED":       "#9e9e9e",
    "IDLE":          "#9e9e9e",
}

KNOWN_ADAPTER_HINTS = APP_CONFIG.known_adapter_hints()


class SwitchMultiToolApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1460x860")
        self.minsize(1220, 760)

        self.event_queue = queue.Queue()
        self._rx_pending = []
        self._boot_info_buffer = ""
        self._device_info = {}
        self._device_info_port = ""
        self.current_console = None
        self.worker = None
        self.ports = []
        self.last_result = None
        self.last_log_path = ""
        self.session_counter = 0
        self._wizard_instance = None
        self._wizard_operator_event = threading.Event()
        self.order_excel_path = ""
        self.order_excel_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._order_excel_backups = {}
        self.active_session_id = ""
        self.session_entries = []
        self.last_excel_status = ""
        self.known_devices = KnownDevicesDB()
        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value=APP_CONFIG.default_baud)
        self.flow_var = tk.StringVar(value="none")
        self.profile_var = tk.StringVar(value="Cisco Catalyst 2960 / 2960-C")
        self.connection_info_var = tk.StringVar(value="Port: not selected")
        self.profile_info_var = tk.StringVar(value="Profile: Cisco Catalyst 2960 / 2960-C")
        self.adapter_hint_var = tk.StringVar(value="Adapter: unknown")

        self._build_ui()
        self._bind_shortcuts()
        self.refresh_ports()
        self.after(80, self._process_queue)
        self.protocol("WM_DELETE_WINDOW", self.safe_quit)

    def _build_ui(self):
        self._build_menu()
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # Lewy panel ma dużo kontrolek i na mniejszych ekranach potrafił ucinać sekcję Status.
        # Dlatego jest teraz scrollowalny: UX zostaje jednoekranowy, a nic nie znika poza oknem.
        left_outer = ttk.Frame(self)
        left_outer.grid(row=0, column=0, sticky="ns")
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_outer, width=315, highlightthickness=0, borderwidth=0)
        left_scroll = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left_canvas.configure(yscrollcommand=left_scroll.set)
        left_canvas.grid(row=0, column=0, sticky="ns")
        left_scroll.grid(row=0, column=1, sticky="ns")

        left = ttk.Frame(left_canvas, padding=10)
        left.columnconfigure(0, weight=1)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw", width=315)

        def _sync_left_scroll(_event=None):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def _sync_left_width(event):
            left_canvas.itemconfigure(left_window, width=event.width)

        def _left_mousewheel(event):
            if getattr(event, "num", None) == 4:
                left_canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                left_canvas.yview_scroll(3, "units")
            else:
                left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mousewheel_recursive(widget):
            widget.bind("<MouseWheel>", _left_mousewheel, add="+")
            widget.bind("<Button-4>", _left_mousewheel, add="+")
            widget.bind("<Button-5>", _left_mousewheel, add="+")
            for child in widget.winfo_children():
                _bind_mousewheel_recursive(child)

        left.bind("<Configure>", _sync_left_scroll)
        left_canvas.bind("<Configure>", _sync_left_width)
        left_canvas.bind("<MouseWheel>", _left_mousewheel)
        left_canvas.bind("<Button-4>", _left_mousewheel)
        left_canvas.bind("<Button-5>", _left_mousewheel)
        # Bindujemy po zbudowaniu całego lewego panelu — patrz koniec _build_ui
        self._bind_left_scroll = _bind_mousewheel_recursive
        self._left_frame = left

        conn_box = ttk.LabelFrame(left, text="1. Connection", padding=10)
        conn_box.grid(row=1, column=0, sticky="ew", pady=6)
        conn_box.columnconfigure(0, weight=1)
        ttk.Label(conn_box, textvariable=self.connection_info_var, wraplength=340).grid(row=0, column=0, sticky="w")
        ttk.Label(conn_box, textvariable=self.adapter_hint_var, wraplength=340, foreground="#555").grid(row=1, column=0, sticky="w", pady=(4, 0))

        profile_box = ttk.LabelFrame(left, text="2. Active profile", padding=10)
        profile_box.grid(row=2, column=0, sticky="ew", pady=6)
        profile_box.columnconfigure(0, weight=1)
        ttk.Label(profile_box, textvariable=self.profile_info_var, wraplength=340).grid(row=0, column=0, sticky="w")


        device_box = ttk.LabelFrame(left, text="3. Current device", padding=10)
        device_box.grid(row=3, column=0, sticky="ew", pady=6)
        device_box.columnconfigure(1, weight=1)
        self.dev_vendor_var = tk.StringVar(value="Vendor: unknown")
        self.dev_model_var = tk.StringVar(value="Model: unknown")
        self.dev_serial_var = tk.StringVar(value="Serial/ID: not read yet")
        self.dev_status_var = tk.StringVar(value="Password: not checked")
        for i, var in enumerate([self.dev_vendor_var, self.dev_model_var, self.dev_serial_var, self.dev_status_var]):
            label = ttk.Label(device_box, textvariable=var, wraplength=340)
            label.grid(row=i, column=0, columnspan=2, sticky="w", pady=1)
            if var is self.dev_serial_var:
                self._bind_serial_copy_label(label, var, use_device_info=True)

        action_box = ttk.LabelFrame(left, text="4. Actions", padding=10)
        action_box.grid(row=4, column=0, sticky="ew", pady=6)
        action_box.columnconfigure(0, weight=1)

        self.read_info_btn = ttk.Button(action_box, text="Read Device Info  (F4)", command=self.start_read_device_info)
        self.read_info_btn.grid(row=0, column=0, sticky="ew", pady=3)
        ttk.Button(action_box, text="Check password  (F5)", command=self.start_real_check).grid(row=1, column=0, sticky="ew", pady=3)
        ttk.Button(action_box, text="Diagnose COM  (F6)", command=self.start_port_probe).grid(row=2, column=0, sticky="ew", pady=3)
        self.recovery_btn = ttk.Button(action_box, text="Recovery Wizard  (F7)", command=self.start_recovery_wizard)
        self.recovery_btn.grid(row=3, column=0, sticky="ew", pady=3)
        self.set_pwd_btn = ttk.Button(action_box, text=f"Set Test Password  ({TEST_PASSWORD})", command=self.start_set_password)
        self.set_pwd_btn.grid(row=4, column=0, sticky="ew", pady=3)
        ttk.Button(action_box, text="Stop / close port  (Esc)", command=self.stop_current).grid(row=5, column=0, sticky="ew", pady=3)

        status_box = ttk.LabelFrame(left, text="5. Status", padding=10)
        status_box.grid(row=5, column=0, sticky="ew", pady=6)
        status_box.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="IDLE")
        self.reason_var = tk.StringVar(value="Ready. Wybierz COM i kliknij Check password.")
        self.log_var = tk.StringVar(value="")

        self.status_label = ttk.Label(status_box, textvariable=self.status_var, font=("Segoe UI", 20, "bold"))
        self.status_label.grid(row=0, column=0, sticky="w")
        reason_label = ttk.Label(status_box, textvariable=self.reason_var, wraplength=340)
        reason_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._bind_serial_copy_label(reason_label, self.reason_var, use_device_info=False)
        ttk.Label(status_box, textvariable=self.log_var, wraplength=340, foreground="#555").grid(row=2, column=0, sticky="w", pady=(6, 0))

        self.order_excel_var = tk.StringVar(value="No order file selected")
        self.order_excel_status_var = tk.StringVar(value="UNLOCKED will mark the matched serial row green.")

        # Right side: top dashboard + terminal/devlog + recent table
        right_main = ttk.Frame(self, padding=(0, 10, 10, 10))
        right_main.grid(row=0, column=1, sticky="nsew")
        right_main.columnconfigure(0, weight=1)
        right_main.rowconfigure(1, weight=1)
        right_main.rowconfigure(2, weight=0)

        self._dashboard = ttk.Frame(right_main)
        self._dashboard.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._dashboard.columnconfigure(0, weight=1)
        self._dashboard.columnconfigure(1, weight=1)

        self.connection_summary = tk.StringVar(value="Connection: not selected")
        self.current_summary = tk.StringVar(value="Current: no active test")
        self.next_steps = tk.StringVar(value="Next: connect console + power, then Check password")
        for col, (title, var) in enumerate([
            ("Current test", self.current_summary),
            ("Operator hint", self.next_steps),
        ]):
            box = ttk.LabelFrame(self._dashboard, text=title, padding=8)
            box.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0))
            label = ttk.Label(box, textvariable=var, wraplength=300)
            label.grid(row=0, column=0, sticky="w")
            if title == "Operator hint":
                self._bind_serial_copy_label(label, var, use_device_info=False)

        # Wizard overlay — ukryty domyślnie, pojawia się zamiast dashboardu
        self._wizard_panel = ttk.Frame(right_main)
        self._wizard_panel.columnconfigure(0, weight=1)
        self._build_wizard_panel(self._wizard_panel)

        paned = ttk.PanedWindow(right_main, orient=tk.HORIZONTAL)
        paned.grid(row=1, column=0, sticky="nsew")

        term_frame = ttk.LabelFrame(paned, text="Live terminal serial", padding=6)
        term_frame.rowconfigure(0, weight=1)
        term_frame.columnconfigure(0, weight=1)
        self.terminal = tk.Text(term_frame, wrap="none", font=("Consolas", 10), bg="#050505", fg="#d9ffd9", insertbackground="#d9ffd9")
        self.terminal.grid(row=0, column=0, sticky="nsew")
        term_scroll = ttk.Scrollbar(term_frame, orient="vertical", command=self.terminal.yview)
        term_scroll.grid(row=0, column=1, sticky="ns")
        self.terminal.configure(yscrollcommand=term_scroll.set)

        dev_frame = ttk.LabelFrame(paned, text="Developer / state machine", padding=6)
        dev_frame.rowconfigure(0, weight=1)
        dev_frame.columnconfigure(0, weight=1)
        self.devlog = tk.Text(dev_frame, wrap="word", font=("Consolas", 10), bg="#101820", fg="#f0f0f0", insertbackground="#f0f0f0")
        self.devlog.grid(row=0, column=0, sticky="nsew")
        dev_scroll = ttk.Scrollbar(dev_frame, orient="vertical", command=self.devlog.yview)
        dev_scroll.grid(row=0, column=1, sticky="ns")
        self.devlog.configure(yscrollcommand=dev_scroll.set)

        paned.add(term_frame, weight=3)
        paned.add(dev_frame, weight=2)

        recent_frame = ttk.LabelFrame(right_main, text="Session devices", padding=6)
        recent_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        recent_frame.columnconfigure(0, weight=1)
        self.recent_tree = ttk.Treeview(
            recent_frame,
            columns=("num", "time", "port", "serial", "model", "status", "excel", "note", "log"),
            show="headings",
            height=4,
        )
        headers = {
            "num": "#",
            "time": "Time",
            "port": "Port",
            "serial": "Switch serial",
            "model": "Model",
            "status": "Password status",
            "excel": "Excel",
            "note": "Note",
            "log": "Log",
        }
        widths = {"num": 45, "time": 80, "port": 70, "serial": 130, "model": 150, "status": 120, "excel": 210, "note": 220, "log": 220}
        for col, label in headers.items():
            self.recent_tree.heading(col, text=label)
            self.recent_tree.column(col, width=widths[col], anchor="w")
        self.recent_tree.grid(row=0, column=0, sticky="ew")
        self.recent_tree.bind("<Button-1>", self._recent_tree_click, add="+")
        self.recent_tree.bind("<Double-1>", self._recent_tree_copy_serial, add="+")
        self.recent_tree.bind("<Button-3>", self._recent_tree_copy_serial, add="+")
        recent_scroll = ttk.Scrollbar(recent_frame, orient="vertical", command=self.recent_tree.yview)
        recent_scroll.grid(row=0, column=1, sticky="ns")
        self.recent_tree.configure(yscrollcommand=recent_scroll.set)
        self.recent_tree.tag_configure("green",  background="#1b3a1b", foreground="#a5d6a7")
        self.recent_tree.tag_configure("red",    background="#3a1b1b", foreground="#ef9a9a")
        self.recent_tree.tag_configure("orange", background="#3a2a1b", foreground="#ffcc80")
        self.recent_tree.tag_configure("blue",   background="#1b2a3a", foreground="#90caf9")

        # Binduj scroll kółka myszki do wszystkich dzieci lewego panelu
        self.after(100, lambda: self._bind_left_scroll(self._left_frame))

    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=False)
        order_menu = tk.Menu(file_menu, tearoff=False)
        order_menu.add_command(label="Otworz...", command=self.select_order_excel)
        file_menu.add_cascade(label="Zamowienie", menu=order_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Zamknij", command=self.safe_quit)
        menubar.add_cascade(label="Plik", menu=file_menu)

        connection_menu = tk.Menu(menubar, tearoff=False)
        connection_menu.add_command(label="Konfiguracja...", command=self.open_connection_settings)
        connection_menu.add_command(label="Refresh COM (F2)", command=self.refresh_ports)
        menubar.add_cascade(label="Polaczenie", menu=connection_menu)

        device_menu = tk.Menu(menubar, tearoff=False)
        device_menu.add_command(label="Profil urzadzenia...", command=self.open_profile_settings)
        device_menu.add_separator()
        device_menu.add_command(label="Read Device Info (F4)", command=self.start_read_device_info)
        menubar.add_cascade(label="Urzadzenie", menu=device_menu)

        self.session_menu = tk.Menu(menubar, tearoff=False)
        self.session_menu.add_command(label="Rozpocznij sesje (inactive)", command=self.start_session)
        self.session_menu.add_command(label="Zakoncz sesje / raport (inactive)", command=self.end_session, state="disabled")
        self.session_menu.add_separator()
        self.session_menu.add_command(label="Manual override...", command=self.manual_override)
        menubar.add_cascade(label="Sesja", menu=self.session_menu)

        log_menu = tk.Menu(menubar, tearoff=False)
        log_menu.add_command(label="Open last log", command=self.open_last_log)
        log_menu.add_command(label="View log...", command=self.view_last_log)
        menubar.add_cascade(label="Log", menu=log_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About / paths", command=self.show_about_paths)
        menubar.add_cascade(label="Pomoc", menu=help_menu)

        self.config(menu=menubar)

    def _build_wizard_panel(self, parent):
        parent.columnconfigure(0, weight=1)

        step_row = ttk.Frame(parent)
        step_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        step_row.columnconfigure(1, weight=1)
        step_row.columnconfigure(2, weight=0)

        self._wiz_step_var   = tk.StringVar(value="")
        self._wiz_title_var  = tk.StringVar(value="")
        self._wiz_detail_var = tk.StringVar(value="")
        self._wiz_elapsed_var = tk.StringVar(value="")

        ttk.Label(step_row, textvariable=self._wiz_step_var,
                  font=("Segoe UI", 14, "bold"), foreground="#4fc3f7").grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Label(step_row, textvariable=self._wiz_title_var,
                  font=("Segoe UI", 14, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(step_row, textvariable=self._wiz_elapsed_var,
                  font=("Segoe UI", 11), foreground="#aaa").grid(row=0, column=2, sticky="e", padx=(16, 0))

        self._wiz_progress = ttk.Progressbar(parent, mode="determinate", maximum=5)
        self._wiz_progress.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        ttk.Label(parent, textvariable=self._wiz_detail_var,
                  font=("Segoe UI", 11), wraplength=900, justify="left").grid(row=2, column=0, sticky="w", pady=(0, 6))

        btn_row = ttk.Frame(parent)
        btn_row.grid(row=3, column=0, sticky="w")

        self._wiz_ok_btn = ttk.Button(btn_row, text="Gotowe — trzymam MODE",
                                      command=self._wizard_operator_ready, width=28)
        self._wiz_ok_btn.grid(row=0, column=0, padx=(0, 8))
        self._wiz_ok_btn.grid_remove()

        ttk.Button(btn_row, text="Przerwij (Abort)",
                   command=self._wizard_abort, width=18).grid(row=0, column=1)

        self._wiz_start_time = None
        self._wiz_tick_id = None

    def _show_wizard(self):
        self._dashboard.grid_remove()
        self._wizard_panel.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._wiz_start_time = datetime.now()
        self._wiz_elapsed_var.set("0s")
        self._wiz_tick_elapsed()

    def _hide_wizard(self):
        self._wizard_panel.grid_remove()
        self._dashboard.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        if self._wiz_tick_id:
            self.after_cancel(self._wiz_tick_id)
            self._wiz_tick_id = None

    def _wiz_tick_elapsed(self):
        if self._wiz_start_time and self._wizard_panel.winfo_ismapped():
            elapsed = int((datetime.now() - self._wiz_start_time).total_seconds())
            m, s = divmod(elapsed, 60)
            self._wiz_elapsed_var.set(f"{m}:{s:02d}" if m else f"{s}s")
            self._wiz_tick_id = self.after(1000, self._wiz_tick_elapsed)

    def _wizard_set_step(self, num, total, title, detail):
        self._wiz_step_var.set(f"KROK {num} / {total}")
        self._wiz_title_var.set(title)
        self._wiz_detail_var.set(detail)
        self._wiz_progress["maximum"] = total
        self._wiz_progress["value"] = num

    def _wizard_show_ok_btn(self, show: bool):
        if show:
            self._wiz_ok_btn.grid()
        else:
            self._wiz_ok_btn.grid_remove()

    def _bind_shortcuts(self):
        self.bind("<F2>", lambda e: self.refresh_ports())
        self.bind("<F4>", lambda e: self.start_read_device_info())
        self.bind("<F5>", lambda e: self.start_real_check())
        self.bind("<F6>", lambda e: self.start_port_probe())
        self.bind("<F7>", lambda e: self.start_recovery_wizard())
        self.bind("<Escape>", lambda e: self.stop_current())

    def _set_status(self, status: str):
        self.status_var.set(status)
        key = status.split()[0] if " " in status else status
        color = STATUS_COLORS.get(key, STATUS_COLORS.get(status, "#e0e0e0"))
        if getattr(self, "_last_status_color", None) != color:
            self._last_status_color = color
            self.status_label.configure(foreground=color)

    def _copy_to_clipboard(self, text: str, source: str = "serial") -> bool:
        value = (text or "").strip()
        if not value or value.lower() in {"not read", "unknown", "-"}:
            return False
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self._append_dev(f"COPY_{source.upper()}: {value}")
        return True

    def _bind_serial_copy_label(self, label, text_var, use_device_info: bool = False):
        label.configure(cursor="hand2")

        def copy_from_label(_event=None):
            text = text_var.get()
            serial = ""
            serial_match = re.search(r"\bserial\s*=\s*([^\s|]+)", text, re.IGNORECASE)
            if serial_match:
                serial = serial_match.group(1)
            if not serial:
                serial = _extract_serial_from_text(text)
            if not serial and use_device_info:
                serial = self._device_info.get("serial") or ""
            if self._copy_to_clipboard(serial, "serial"):
                self.next_steps.set(f"Copied serial to clipboard: {serial}")

        label.bind("<Button-1>", copy_from_label, add="+")
        label.bind("<Button-3>", copy_from_label, add="+")

    def open_connection_settings(self):
        self.refresh_ports()
        win = tk.Toplevel(self)
        win.title("Konfiguracja polaczenia")
        win.transient(self)
        win.grab_set()
        win.columnconfigure(1, weight=1)

        ttk.Label(win, text="Port:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        port_combo = ttk.Combobox(win, textvariable=self.port_var, values=[p.device for p in self.ports], state="readonly", width=28)
        port_combo.grid(row=0, column=1, sticky="ew", padx=10, pady=(10, 4))
        port_combo.bind("<<ComboboxSelected>>", lambda _e: self.show_selected_port_info())

        ttk.Label(win, text="Baud:").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        ttk.Entry(win, textvariable=self.baud_var, width=12).grid(row=1, column=1, sticky="w", padx=10, pady=4)

        ttk.Label(win, text="Flow:").grid(row=2, column=0, sticky="w", padx=10, pady=4)
        ttk.Combobox(win, textvariable=self.flow_var, values=APP_CONFIG.flow_profiles, state="readonly", width=20).grid(row=2, column=1, sticky="w", padx=10, pady=4)

        info = ttk.Label(win, textvariable=self.connection_info_var, wraplength=420, foreground="#555")
        info.grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))
        ttk.Label(win, textvariable=self.adapter_hint_var, wraplength=420, foreground="#555").grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=4)

        btns = ttk.Frame(win)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", padx=10, pady=10)
        ttk.Button(btns, text="Refresh COM", command=lambda: (self.refresh_ports(), port_combo.configure(values=[p.device for p in self.ports]))).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="Close", command=win.destroy).grid(row=0, column=1)

    def open_profile_settings(self):
        win = tk.Toplevel(self)
        win.title("Profil urzadzenia")
        win.transient(self)
        win.grab_set()
        win.columnconfigure(0, weight=1)
        ttk.Label(win, text="Profile:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        values = [
            "Cisco Catalyst 2960 / 2960-C",
            "Cisco Catalyst 2960X",
            "Cisco IOS generic",
            "Generic serial console",
        ]
        combo = ttk.Combobox(win, textvariable=self.profile_var, values=values, state="readonly", width=34)
        combo.grid(row=1, column=0, sticky="ew", padx=10)

        def close():
            self.profile_info_var.set(f"Profile: {self.profile_var.get()}")
            win.destroy()

        ttk.Button(win, text="Close", command=close).grid(row=2, column=0, sticky="e", padx=10, pady=10)

    def refresh_ports(self):
        self.ports = list_serial_ports()
        values = [p.device for p in self.ports]
        current = self.port_var.get()
        if values and current not in values:
            self.port_var.set(values[0])
        elif not values:
            self.port_var.set("")
        self.show_selected_port_info()
        self.dev(f"SCAN_PORTS: wykryto {len(values)} portów: {', '.join(values) if values else 'brak'}")

    def _selected_port(self):
        selected = self.port_var.get()
        return next((p for p in self.ports if p.device == selected), None)

    def show_selected_port_info(self):
        port = self._selected_port()
        if port:
            if self._device_info_port != port.device:
                self._device_info_port = port.device
                self._boot_info_buffer = ""
                self._device_info = {}
            adapter_key = (port.vid, port.pid)
            hint = KNOWN_ADAPTER_HINTS.get(adapter_key)
            if hint:
                self.adapter_hint_var.set(f"Adapter: {hint['name']} | flow={self.flow_var.get() or '-'}")
                if self.flow_var.get() != hint["recommended_flow"]:
                    self.flow_var.set(hint["recommended_flow"])
                    self.adapter_hint_var.set(f"Adapter: {hint['name']} | flow={hint['recommended_flow']}")
                    self.dev(f"ADAPTER_HINT: {port.device} {hint['name']} -> auto Flow={hint['recommended_flow']}")
            else:
                self.adapter_hint_var.set(f"Adapter: {port.description or 'unknown'} | flow={self.flow_var.get() or '-'}")

            info = (
                f"Port: {port.device} | {port.description or '-'}\n"
                f"Device: {port.manufacturer or '-'} / {port.product or '-'}\n"
                f"VID:PID={port.vid or '-'}:{port.pid or '-'}"
            )
            self.connection_info_var.set(info)
            self.connection_summary.set(info)
            self.dev_vendor_var.set("Vendor: unknown until device info read")
            self.dev_model_var.set("Model: unknown until device info read")
            self.dev_serial_var.set("Serial/ID: adapter serial shown above, switch serial not read yet")
        else:
            self.connection_info_var.set("Port: no COM port selected")
            self.adapter_hint_var.set("Adapter: none")
            self.connection_summary.set("Connection: no COM port selected")

    def _display_path(self, path: str, max_len: int = 42) -> str:
        if not path:
            return ""
        text = str(path)
        if len(text) <= max_len:
            return text
        return "..." + text[-(max_len - 3):]

    def _ensure_order_excel_backup(self, path: str):
        if not path:
            return None
        try:
            backup_key = str(Path(path).resolve()).lower()
        except Exception:
            backup_key = str(path).lower()
        if backup_key in self._order_excel_backups:
            return self._order_excel_backups[backup_key]

        result = create_order_workbook_backup(path, self.order_excel_session_id)
        if result.success:
            self._order_excel_backups[backup_key] = result.backup_path
            self._append_dev(f"ORDER_EXCEL_BACKUP: {result.backup_path}")
        else:
            self._append_dev(f"ORDER_EXCEL_BACKUP_FAIL: {result.message}")
        return result.backup_path if result.success else None

    def select_order_excel(self):
        path = filedialog.askopenfilename(
            title="Select order Excel",
            filetypes=[
                ("Excel workbook", "*.xlsx *.xlsm"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.order_excel_path = path
        self.order_excel_var.set(f"Order: {self._display_path(path)}")
        backup_path = self._ensure_order_excel_backup(path)
        if backup_path:
            self.order_excel_status_var.set(f"Backup ready: {self._display_path(backup_path)}")
        else:
            self.order_excel_status_var.set("Backup failed; Excel will not be modified.")
        self.dev(f"ORDER_EXCEL: selected {path}")

    def _mark_order_excel_for_result(self, result):
        self.last_excel_status = ""
        if result.status != "UNLOCKED":
            self.last_excel_status = "Not applicable"
            return
        if not self.order_excel_path:
            self.last_excel_status = "No order file selected"
            self.order_excel_status_var.set("No order file selected; Excel not updated.")
            self._append_dev("ORDER_EXCEL: skipped, no file selected")
            return

        self._update_device_from_current_console()
        serial = self._device_info.get("serial") or ""
        backup_path = self._ensure_order_excel_backup(self.order_excel_path)
        if not backup_path:
            message = "Nie odhaczam Excela, bo nie udalo sie utworzyc backupu."
            self.last_excel_status = message
            self.order_excel_status_var.set(message)
            self._append_dev(f"ORDER_EXCEL: {message}")
            self.next_steps.set(message)
            return

        mark_result = mark_unlocked_in_workbook(
            self.order_excel_path,
            serial,
            tested_at=datetime.now(),
        )
        self.last_excel_status = mark_result.message
        self.order_excel_status_var.set(mark_result.message)
        prefix = "updated" if mark_result.success else "not updated"
        self._append_dev(f"ORDER_EXCEL: {prefix}: {mark_result.message}")
        if mark_result.success:
            self.next_steps.set(mark_result.message)
        else:
            current_hint = self.next_steps.get()
            self.next_steps.set(f"{current_hint} | Excel: {mark_result.message}")

    def _get_baud(self):
        try:
            return int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showwarning("Zły baud", "Baud musi być liczbą, np. 9600.")
            return None

    def start_real_check(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Brak portu", "Wybierz port COM/tty.")
            return
        baud = self._get_baud()
        if baud is None:
            return
        flow = self.flow_var.get().strip() or "none"
        self._start_check(lambda: SerialConsole(port, baudrate=baud, flow_profile=flow, on_rx=self.rx, on_dev=self.dev, log_dir=APP_CONFIG.serial_log_dir), port)

    def start_read_device_info(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Brak portu", "Wybierz port COM/tty.")
            return
        baud = self._get_baud()
        if baud is None:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Pracuję", "Procedura już trwa. Najpierw kliknij STOP.")
            return
        flow = self.flow_var.get().strip() or "none"
        self._set_status("RUNNING")
        self.reason_var.set("Odczytuję informacje o urządzeniu...")
        self.current_summary.set(f"Read Device Info: {port}")
        self.next_steps.set("Wysyłam show version — poczekaj chwilę")

        def task():
            try:
                import re
                import time as _time

                console = SerialConsole(port, baudrate=baud, flow_profile=flow, on_rx=self.rx, on_dev=self.dev, log_dir=APP_CONFIG.serial_log_dir)
                self.current_console = console
                RE_PRIV = re.compile(r"(?m)^[A-Za-z0-9._()/-]+#\s*$")
                RE_USER = re.compile(r"(?m)^[A-Za-z0-9._()/-]+>\s*$")
                RE_CONF = re.compile(r"(?m)^[A-Za-z0-9._()/-]+\(config[^)]*\)#\s*$")
                RE_MORE = re.compile(r"--\s*MORE\s*--", re.IGNORECASE)
                RE_DIALOG = re.compile(r"\[yes/no\]", re.IGNORECASE)
                RE_PLEASE_NO = re.compile(r"Please answer 'yes' or 'no'", re.IGNORECASE)
                RE_PRESS_RET = re.compile(r"Press RETURN to get started", re.IGNORECASE)

                def drain_input(timeout=1.5, quiet=0.35):
                    deadline = _time.time() + timeout
                    quiet_deadline = _time.time() + quiet
                    while _time.time() < deadline:
                        chunk = console.read_some()
                        if chunk:
                            quiet_deadline = _time.time() + quiet
                        elif _time.time() >= quiet_deadline:
                            break
                        _time.sleep(0.05)

                def tail_has_prompt(buf):
                    tail = buf.rstrip()[-250:]
                    return RE_PRIV.search(tail) or RE_USER.search(tail)

                def settle_prompt(timeout=12):
                    deadline = _time.time() + timeout
                    while _time.time() < deadline:
                        console.read_some()
                        buf = console.get_buffer()
                        if RE_PLEASE_NO.search(buf) or RE_DIALOG.search(buf):
                            console.clear_buffer()
                            console.write("no")
                            drain_input(timeout=1.2)
                            continue
                        if RE_PRESS_RET.search(buf):
                            console.clear_buffer()
                            console.write("")
                            drain_input(timeout=1.2)
                            continue
                        if RE_MORE.search(buf):
                            console.clear_buffer()
                            console.write(" ")
                            drain_input(timeout=1.2)
                            continue
                        if RE_CONF.search(buf.rstrip()[-250:]):
                            console.clear_buffer()
                            console.write("end")
                            drain_input(timeout=1.2)
                            continue
                        if tail_has_prompt(buf):
                            return True
                        _time.sleep(0.08)
                    return False
                # Obudź konsolę
                for _ in range(2):
                    console.write("")
                    drain_input(timeout=1.0)
                settle_prompt(timeout=8)

                known_info_text = self._boot_info_buffer
                profile_commands = APP_CONFIG.command_profile(self.profile_var.get())
                terminal_length_command = profile_commands.get("terminal_length_command") or "terminal length 0"
                read_info_commands = profile_commands.get("read_info_commands") or ["show version"]
                console.clear_buffer()
                console.write(str(terminal_length_command))
                settle_prompt(timeout=6)

                console.clear_buffer()
                console.write(str(read_info_commands[0]))
                # Czekaj na prompt (do 20s), obsługuj --MORE--
                # Czekaj na dane z show version, nie na stary prompt z wake-up.
                saw_version_data = False
                deadline = _time.time() + 45
                while _time.time() < deadline:
                    console.read_some()
                    buf = console.get_buffer()
                    lowered = buf.lower()
                    if "processor board id" in lowered or "system serial number" in lowered or "model number" in lowered:
                        saw_version_data = True
                    if RE_MORE.search(buf):
                        console.write(" ")
                        _time.sleep(0.2)
                        continue
                    if saw_version_data and tail_has_prompt(buf):
                        break
                    _time.sleep(0.1)
                info = parse_boot_info(known_info_text + "\n" + console.get_buffer())
                if not info and self._device_info:
                    info = dict(self._device_info)
                self.event_queue.put(("READ_INFO_DONE", info))
            except Exception as exc:
                self.event_queue.put(("ERROR", str(exc)))
            finally:
                if self.current_console:
                    try:
                        self.current_console.close()
                    except Exception:
                        pass

        self.terminal.delete("1.0", tk.END)
        self.devlog.delete("1.0", tk.END)
        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def start_port_probe(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Brak portu", "Wybierz port COM/tty.")
            return
        baud = self._get_baud()
        if baud is None:
            return
        flow = self.flow_var.get().strip() or "none"
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Pracuję", "Procedura już trwa. Najpierw kliknij STOP.")
            return
        self.devlog.delete("1.0", tk.END)
        self._set_status("PROBING")
        self.reason_var.set("Diagnostyka otwarcia portu")
        self.current_summary.set(f"Probe: {port}, {baud}, flow={flow}")
        self.next_steps.set("Czekaj na wynik PROBE_RAW_WIN_OK / PROBE_PYSERIAL_OK w Developer logu")

        def task():
            try:
                probe_port_open(port, baud, self.dev, flow)
                self.event_queue.put(("STATE", ("PROBE_DONE", "Diagnostyka zakończona — sprawdź panel Developer")))
            except Exception as exc:
                self.event_queue.put(("ERROR", f"Probe failed: {exc}"))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def _start_check(self, console_factory, port_label):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Pracuję", "Procedura już trwa. Najpierw kliknij STOP.")
            return

        self.terminal.delete("1.0", tk.END)
        self.devlog.delete("1.0", tk.END)
        self._set_status("RUNNING")
        self.reason_var.set("Start procedury detekcji")
        self.log_var.set("")
        self.current_summary.set(f"Running password check on {port_label}")
        self.next_steps.set("Nie odpinaj konsoli. Obserwuj Live terminal i Developer log.")
        self.dev_status_var.set("Password: checking...")

        def task():
            try:
                self.current_console = console_factory()
                self.dev(f"OPEN: {port_label}")
                detector = PasswordDetector(self.current_console, port_label, on_state=self.state)
                result = detector.run()
                self.event_queue.put(("RESULT", result))
            except Exception as exc:
                self.event_queue.put(("ERROR", str(exc)))
            finally:
                if self.current_console:
                    try:
                        self.current_console.close()
                    except Exception:
                        pass

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------
    # Set Test Password
    # ------------------------------------------------------------------

    def start_set_password(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Brak portu", "Wybierz port COM.")
            return
        baud = self._get_baud()
        if baud is None:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Pracuję", "Procedura już trwa.")
            return
        flow = self.flow_var.get().strip() or "none"

        self.terminal.delete("1.0", tk.END)
        self.devlog.delete("1.0", tk.END)
        self._set_status("SET_PWD")
        self.reason_var.set(f"Ustawiam hasło testowe: {TEST_PASSWORD}")
        self.current_summary.set(f"Set Test Password: {port}")
        self.next_steps.set("Czekaj — aplikacja ustawia enable secret na switchu")

        def task():
            try:
                console = SerialConsole(port, baudrate=baud, flow_profile=flow, on_rx=self.rx, on_dev=self.dev, log_dir=APP_CONFIG.serial_log_dir)
                self.current_console = console
                action = SetTestPasswordAction(console, on_event=lambda k, v: self.event_queue.put((k, v)))
                result = action.run()
                self.event_queue.put(("STP_RESULT", result))
            except Exception as exc:
                self.event_queue.put(("ERROR", str(exc)))
            finally:
                if self.current_console:
                    try:
                        self.current_console.close()
                    except Exception:
                        pass

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------
    # Recovery Wizard
    # ------------------------------------------------------------------

    def _current_device_model(self) -> str:
        model = self._device_info.get("model") or self._device_info.get("pid") or ""
        if model:
            return model.strip()
        model_raw = self.dev_model_var.get()
        return model_raw.replace("Model:", "").strip()

    def _recovery_instructions_for_model(self) -> str:
        model_display = self._current_device_model()
        model = model_display.upper()
        for key, text in RECOVERY_INSTRUCTIONS.items():
            if key.upper() in model:
                if model_display and "UNKNOWN" not in model:
                    return f"Wykryty switch: {model_display}\n\n{text}"
                return text
        if model_display and "UNKNOWN" not in model:
            return f"Wykryty switch: {model_display}\n\n{RECOVERY_INSTRUCTIONS_DEFAULT}"
        return RECOVERY_INSTRUCTIONS_DEFAULT

    def start_recovery_wizard(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Brak portu", "Wybierz port COM.")
            return
        baud = self._get_baud()
        if baud is None:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Pracuję", "Procedura już trwa.")
            return

        self._show_wizard()
        self._wizard_set_step(1, 5,
            "Czekam na switch: — port otwarty i nasłuchuję",
            self._recovery_instructions_for_model()
        )
        self._wizard_show_ok_btn(False)
        self._set_status("WIZARD_WAIT")
        self.reason_var.set("Nasłuchuję na switch: — wykonaj kroki po prawej")

        flow = self.flow_var.get().strip() or "none"

        def task():
            try:
                console = SerialConsole(port, baudrate=baud, flow_profile=flow, on_rx=self.rx, on_dev=self.dev, log_dir=APP_CONFIG.serial_log_dir)
                self.current_console = console
                # Słuchamy od razu — nie czekamy na kliknięcie
                wizard = RecoveryWizard(console, port, on_event=lambda k, v: self.event_queue.put((k, v)))
                self._wizard_instance = wizard
                result = wizard.run()
                self.event_queue.put(("WIZARD_RESULT", result))
            except Exception as exc:
                self.event_queue.put(("ERROR", str(exc)))
            finally:
                if self.current_console:
                    try:
                        self.current_console.close()
                    except Exception:
                        pass

        self.terminal.delete("1.0", tk.END)
        self.devlog.delete("1.0", tk.END)
        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def _wizard_operator_ready(self):
        self._wizard_show_ok_btn(False)
        self._wizard_operator_event.set()
        self.dev("WIZARD: operator kliknął Gotowe — trzyma MODE")

    def _wizard_abort(self):
        if self._wizard_instance:
            self._wizard_instance.abort()
        self._wizard_operator_event.set()  # odblokuj wątek jeśli czeka
        if self.current_console:
            try:
                self.current_console.close()
            except Exception:
                pass
        self._hide_wizard()
        self._set_status("ABORTED")
        self.reason_var.set("Recovery Wizard przerwany przez operatora")
        self.current_summary.set("Wizard aborted")
        self.dev("WIZARD: abort przez operatora")

    def stop_current(self):
        if self._wizard_instance:
            self._wizard_instance.abort()
        self._wizard_operator_event.set()
        if self.current_console:
            self.current_console.close()
            self.dev("STOP: zamknięto aktualny port/sesję")
        self._hide_wizard()
        self._set_status("STOPPED")
        self.reason_var.set("Przerwano lub zamknięto port")
        self.current_summary.set("Stopped")
        self.next_steps.set("Możesz kliknąć Check password ponownie albo Diagnose COM.")

    def open_last_log(self):
        path = self.last_log_path or self.log_var.get().replace("Log: ", "").strip()
        if not path:
            messagebox.showinfo("Brak loga", "Nie ma jeszcze loga do otwarcia.")
            return
        p = Path(path)
        if not p.exists():
            messagebox.showwarning("Brak pliku", f"Nie znalazłem pliku:\n{p}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif os.uname().sysname == "Darwin":
                os.system(f"open {str(p)!r}")
            else:
                os.system(f"xdg-open {str(p)!r}")
        except Exception as exc:
            messagebox.showwarning("Nie udało się otworzyć", str(exc))

    def _refresh_session_buttons(self):
        if not hasattr(self, "session_menu"):
            return
        if self.active_session_id:
            short_id = self.active_session_id
            self.session_menu.entryconfig(0, label=f"Sesja aktywna: {short_id}", state="disabled")
            self.session_menu.entryconfig(1, label="Zakoncz sesje / raport", state="normal")
        else:
            self.session_menu.entryconfig(0, label="Rozpocznij sesje (inactive)", state="normal")
            self.session_menu.entryconfig(1, label="Zakoncz sesje / raport (inactive)", state="disabled")

    def start_session(self):
        if self.active_session_id and self.session_entries:
            if not messagebox.askyesno("Nowa sesja", "Aktualna sesja ma juz wpisy. Zaczac nowa sesje?"):
                return
        self.active_session_id = new_session_id()
        self.session_entries = []
        self.session_counter = 0
        self.last_excel_status = ""
        for item in self.recent_tree.get_children():
            self.recent_tree.delete(item)
        self.current_summary.set(f"Session: {self.active_session_id}")
        self.next_steps.set("Sesja aktywna. Podlacz switch i kliknij Check password.")
        self._append_dev(f"SESSION_START: {self.active_session_id}")
        self._refresh_session_buttons()

    def end_session(self):
        if not self.active_session_id:
            messagebox.showinfo("Brak sesji", "Nie ma aktywnej sesji.")
            return
        if not self.session_entries:
            messagebox.showinfo("Pusta sesja", "Sesja nie ma jeszcze wpisow.")
            return
        report_path = self._finalize_session()
        if report_path:
            messagebox.showinfo("Raport sesji", f"Zapisano raport:\n{report_path}")

    def _finalize_session(self):
        if not self.active_session_id or not self.session_entries:
            return None
        session_id = self.active_session_id
        try:
            report_path = write_report_xlsx(session_id, self.session_entries)
        except Exception as exc:
            messagebox.showwarning("Raport", f"Nie udalo sie utworzyc raportu:\n{exc}")
            return None
        self._append_dev(f"SESSION_END: {session_id} report={report_path}")
        self.current_summary.set(f"Session ended: {session_id}")
        self.next_steps.set(f"Report: {report_path}")
        self.active_session_id = ""
        self._refresh_session_buttons()
        return report_path

    def safe_quit(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Zamknac aplikacje?", "Procedura nadal trwa. Przerwac port i zamknac aplikacje?"):
                return
        report_path = None
        if self.active_session_id and self.session_entries:
            report_path = self._finalize_session()
        elif self.active_session_id:
            self._append_dev(f"SESSION_END empty: {self.active_session_id}")
            self.active_session_id = ""
            self._refresh_session_buttons()
        if self._wizard_instance:
            self._wizard_instance.abort()
        self._wizard_operator_event.set()
        if self.current_console:
            try:
                self.current_console.close()
            except Exception:
                pass
        if report_path:
            self._append_dev(f"SAFE_QUIT report={report_path}")
        self.destroy()

    def manual_override(self):
        if not self.active_session_id:
            messagebox.showinfo("Brak aktywnej sesji", "Kliknij Start session, zeby zapisac manual override w sesji.")
            return
        win = tk.Toplevel(self)
        win.title("Manual override")
        win.transient(self)
        win.grab_set()
        win.columnconfigure(0, weight=1)
        ttk.Label(win, text="Status:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        status_var = tk.StringVar(value="SKIP")
        ttk.Combobox(
            win,
            textvariable=status_var,
            values=["UNLOCKED", "LOCKED", "SKIP", "DAMAGED", "NO_CONSOLE", "UNKNOWN"],
            state="readonly",
            width=24,
        ).grid(row=1, column=0, sticky="ew", padx=10)
        ttk.Label(win, text="Note:").grid(row=2, column=0, sticky="w", padx=10, pady=(10, 2))
        note_var = tk.StringVar()
        ttk.Entry(win, textvariable=note_var, width=46).grid(row=3, column=0, sticky="ew", padx=10)

        def save():
            status = status_var.get().strip()
            note = note_var.get().strip()
            result = DetectionResult(
                status=status,
                reason=note or "Manual override",
                log_path=self.last_log_path,
                port=self.port_var.get().strip(),
                timestamp=datetime.now().isoformat(timespec="seconds"),
            )
            self.last_result = result
            self.last_excel_status = "Manual"
            self._set_status(status)
            self.reason_var.set(result.reason)
            self.dev_status_var.set(f"Password: {status}")
            self._append_dev(f"MANUAL_OVERRIDE: {status} | {note}")
            self._add_recent_result(result, source="manual", note=note)
            win.destroy()

        btns = ttk.Frame(win)
        btns.grid(row=4, column=0, sticky="e", padx=10, pady=10)
        ttk.Button(btns, text="Cancel", command=win.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="Save", command=save).grid(row=0, column=1)

    def view_last_log(self):
        path = self.last_log_path or self.log_var.get().replace("Log: ", "").strip()
        if not path:
            messagebox.showinfo("Brak loga", "Nie ma jeszcze loga do pokazania.")
            return
        p = Path(path)
        if not p.exists():
            messagebox.showwarning("Brak pliku", f"Nie znalazlem pliku:\n{p}")
            return
        win = tk.Toplevel(self)
        win.title(f"Log viewer - {p.name}")
        win.geometry("980x620")
        win.rowconfigure(1, weight=1)
        win.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(win, padding=6)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Filter:").grid(row=0, column=0, sticky="w")
        filter_var = tk.StringVar()
        filter_combo = ttk.Combobox(
            toolbar,
            textvariable=filter_var,
            values=["", "Password", "Switch>", "Switch#", "ERROR", "PID", "SN", "--More--"],
        )
        filter_combo.grid(row=0, column=1, sticky="ew", padx=6)
        text = tk.Text(win, wrap="none", font=("Consolas", 10))
        text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        raw = p.read_text(encoding="utf-8", errors="replace")

        def render(_event=None):
            pattern = filter_var.get().strip()
            text.delete("1.0", tk.END)
            if pattern:
                lines = [line for line in raw.splitlines() if pattern.lower() in line.lower()]
                text.insert("1.0", "\n".join(lines))
            else:
                text.insert("1.0", raw)

        def copy_summary():
            info = parse_boot_info(raw)
            summary = (
                f"Log: {p}\n"
                f"Model: {info.get('model') or info.get('pid') or '-'}\n"
                f"Serial: {info.get('serial') or '-'}\n"
                f"IOS: {info.get('ios') or '-'}\n"
                f"Last result: {self.status_var.get()} | {self.reason_var.get()}"
            )
            self._copy_to_clipboard(summary, "log_summary")

        ttk.Button(toolbar, text="Apply", command=render).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(toolbar, text="Copy summary", command=copy_summary).grid(row=0, column=3)
        filter_combo.bind("<<ComboboxSelected>>", render)
        render()

    def show_about_paths(self):
        text = (
            f"{APP_NAME} {APP_VERSION}\n\n"
            f"Config: {APP_CONFIG.path}\n"
            f"Logs: {APP_CONFIG.serial_log_dir}\n"
            f"Results CSV: {APP_CONFIG.results_csv}\n"
            f"Order backups: {APP_CONFIG.order_excel_backup_dir}\n"
            f"Sessions: sessions\n"
            f"Reports: reports\n"
            f"Known devices DB: data/known_devices.sqlite3\n\n"
            f"Active session: {self.active_session_id or '-'}"
        )
        messagebox.showinfo("About / paths", text)

    def rx(self, text):
        self.event_queue.put(("RX", text))

    def dev(self, text):
        self.event_queue.put(("DEV", text))

    def state(self, state, detail):
        self.event_queue.put(("STATE", (state, detail)))

    def _process_queue(self):
        start = time.monotonic()
        processed = 0
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                processed += 1
                if kind == "RX":
                    self._rx_pending.append(payload)
                    self._ingest_boot_info(payload)
                elif kind == "DEV":
                    self._append_dev(payload)
                elif kind == "STATE":
                    state, detail = payload
                    self._set_status(state)
                    self.reason_var.set(detail)
                    self.current_summary.set(f"State: {state}")
                    self._append_dev(f"STATE {state}: {detail}")
                    if state == "BOOT_INFO":
                        self._update_device_from_boot_info(detail)
                elif kind == "RESULT":
                    result = payload
                    self.last_result = result
                    self.last_log_path = result.log_path
                    self._set_status(result.status)
                    self.reason_var.set(result.reason)
                    self.log_var.set(f"Log: {result.log_path}")
                    self.dev_status_var.set(f"Password: {result.status}")
                    self.current_summary.set(f"Last result: {result.status} on {result.port}")
                    self.next_steps.set(self._next_hint_for_result(result.status))
                    self._append_dev(f"RESULT: {result.status} | {result.reason} | {result.log_path}")
                    if self.current_console:
                        try:
                            info = parse_boot_info(self.current_console.get_buffer())
                            if info:
                                self._update_device_from_boot_info_dict(info)
                        except Exception:
                            pass
                    self._mark_order_excel_for_result(result)
                    self._add_recent_result(result)
                elif kind == "READ_INFO_DONE":
                    info = payload
                    if info:
                        self._update_device_from_boot_info_dict(info)
                        self._set_status("PROBE_DONE")
                        self.reason_var.set("Dane urządzenia odczytane.")
                        self.next_steps.set("Informacje zaktualizowane w panelu Device.")
                        self._append_dev(f"READ_INFO: {info}")
                    else:
                        self._set_status("UNKNOWN")
                        self.reason_var.set("Nie udało się odczytać danych — sprawdź czy switch jest na prompcie.")
                        self.next_steps.set("Upewnij się że switch jest zabootowany i na prompcie Switch> lub Switch#")
                elif kind == "WIZARD_STEP":
                    num, total, title, detail = payload
                    if num == 1:
                        detail = self._recovery_instructions_for_model()
                    self._wizard_set_step(num, total, title, detail)
                    self._set_status(f"WIZARD {num}/{total}")
                    self.reason_var.set(title)
                    self._append_dev(f"WIZARD KROK {num}/{total}: {title} — {detail}")
                elif kind == "WIZARD_LOG":
                    self._append_dev(f"WIZARD: {payload}")
                elif kind == "WIZARD_DONE":
                    success, message = payload
                    self._update_device_from_current_console()
                    self._hide_wizard()
                    self._wizard_instance = None
                    status = "WIZARD_OK" if success else "WIZARD_FAIL"
                    self._set_status(status)
                    self.reason_var.set(message)
                    self.current_summary.set(status)
                    self.next_steps.set("Reset zakończony. Switch gotowy — możesz podłączyć się normalnie." if success else "Sprawdź log i spróbuj ponownie.")
                    self._append_dev(f"WIZARD_DONE success={success}: {message}")
                    if success:
                        port = self.port_var.get().strip()
                        from .detector import DetectionResult
                        self.last_excel_status = ""
                        self._add_recent_result(DetectionResult(
                            status="FACTORY_RESET",
                            reason=message,
                            log_path="",
                            port=port,
                            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ))
                        self.dev_status_var.set("Password: FACTORY RESET — brak konfiguracji")
                elif kind == "WIZARD_RESULT":
                    pass
                elif kind == "STP_LOG":
                    self._append_dev(f"STP: {payload}")
                elif kind == "STP_RESULT":
                    result = payload
                    self._update_device_from_current_console()
                    status = "PWD_SET" if result.success else "PWD_FAIL"
                    self._set_status(status)
                    self.reason_var.set(result.message)
                    self.current_summary.set(status)
                    self.next_steps.set("Uruchom Check password — switch powinien być teraz LOCKED." if result.success else "Sprawdź połączenie i stan switcha.")
                    self._append_dev(f"STP_RESULT: {result.message}")
                    if result.success:
                        self.dev_status_var.set(f"Password: SET ({TEST_PASSWORD})")
                elif kind == "ERROR":
                    self._hide_wizard()
                    self._set_status("ERROR")
                    self.reason_var.set(payload)
                    self.dev_status_var.set("Password: ERROR / unknown")
                    self.current_summary.set("Error during current operation")
                    self.next_steps.set("Kliknij Diagnose COM albo sprawdź kabel/port. Pełny błąd jest w Developer logu.")
                    self._append_dev(f"ERROR: {payload}")
                if processed >= 250 or time.monotonic() - start > QUEUE_PROCESS_BUDGET_SEC:
                    break
        except queue.Empty:
            pass
        self._flush_rx_pending()
        self.after(80, self._process_queue)

    def _flush_rx_pending(self):
        if not self._rx_pending:
            return
        text = _terminal_display_text("".join(self._rx_pending))
        self._rx_pending.clear()
        if not text:
            return
        self.terminal.insert(tk.END, text)
        chars = self.terminal.count("1.0", tk.END, "chars")
        if chars:
            extra = int(chars[0]) - TERMINAL_MAX_CHARS
            if extra > 0:
                self.terminal.delete("1.0", f"1.0+{extra}c")
        self.terminal.see(tk.END)

    def _next_hint_for_result(self, status):
        if status == "UNLOCKED":
            return "Switch bez hasła — dostęp wolny. Możesz ustawić hasło testowe lub przejść dalej."
        if status == "LOCKED":
            return "Switch zabezpieczony hasłem. Użyj Recovery Wizard żeby wykonać reset fabryczny."
        if status == "PARTIAL_ACCESS":
            return "Masz część dostępu. Zapisz log i sprawdź prompt / uprawnienia ręcznie."
        return "Wynik niejednoznaczny. Spróbuj Retry/Check password albo Diagnose COM."

    def _session_entry_from_result(self, result, source="auto", note=""):
        self._update_device_from_current_console()
        self.session_counter += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        switch_serial = self._device_info.get("serial") or "not read"
        model = self._device_info.get("model") or self._device_info.get("pid") or ""
        return SessionEntry(
            num=self.session_counter,
            timestamp=ts,
            port=result.port,
            serial=switch_serial,
            model=model,
            ios=self._device_info.get("ios") or "",
            mac=self._device_info.get("mac") or "",
            profile=self.profile_var.get(),
            status=result.status,
            reason=result.reason,
            excel_status=self.last_excel_status or "",
            note=note,
            log_path=result.log_path,
            source=source,
        )

    def _add_recent_result(self, result, source="auto", note=""):
        entry = self._session_entry_from_result(result, source=source, note=note)
        known = self.known_devices.get(entry.serial)
        if self.active_session_id and known and known.get("last_session") == self.active_session_id:
            entry.note = (entry.note + " | " if entry.note else "") + "Duplicate in this session"
        elif known:
            self.next_steps.set(f"Known device: {entry.serial}, last status {known.get('last_status') or '-'}")
        _green  = {"UNLOCKED", "FACTORY_RESET", "WIZARD_OK", "PWD_SET"}
        _red    = {"LOCKED", "WIZARD_FAIL", "ERROR", "PWD_FAIL"}
        _orange = {"PARTIAL_ACCESS", "UNKNOWN", "SKIP", "DAMAGED", "NO_CONSOLE"}
        s = entry.status
        tag = "green" if s in _green else "red" if s in _red else "orange" if s in _orange else "blue"
        self.recent_tree.insert(
            "",
            0,
            values=(
                entry.num,
                entry.timestamp.split(" ")[1],
                entry.port,
                entry.serial,
                entry.model,
                entry.status,
                entry.excel_status,
                entry.note,
                entry.log_path,
            ),
            tags=(tag,),
        )
        if self.active_session_id:
            self.session_entries.append(entry)
            append_session_csv(self.active_session_id, entry)
            self.known_devices.upsert(entry, self.active_session_id)
        else:
            self._append_dev("SESSION: inactive, result shown only in table")
        children = self.recent_tree.get_children()
        for item in children[80:]:
            self.recent_tree.delete(item)

    def _recent_tree_click(self, event):
        if not self.recent_tree.identify_row(event.y):
            self.recent_tree.selection_remove(self.recent_tree.selection())
            self.recent_tree.focus("")

    def _recent_tree_copy_serial(self, event):
        row_id = self.recent_tree.identify_row(event.y)
        column_id = self.recent_tree.identify_column(event.x)
        if not row_id:
            return
        columns = self.recent_tree["columns"]
        try:
            column_name = columns[int(column_id.lstrip("#")) - 1]
        except Exception:
            return
        if column_name != "serial":
            return
        values = self.recent_tree.item(row_id, "values")
        serial = values[columns.index("serial")] if values else ""
        if self._copy_to_clipboard(serial, "serial"):
            self.next_steps.set(f"Copied serial to clipboard: {serial}")

    def _append_dev(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.devlog.insert(tk.END, f"[{ts}] {text}\n")
        self.devlog.see(tk.END)

    def _ingest_boot_info(self, text: str):
        if not text:
            return
        self._boot_info_buffer = (self._boot_info_buffer + text)[-512_000:]
        info = parse_boot_info(self._boot_info_buffer)
        if info:
            self._update_device_from_boot_info_dict(info)

    def _update_device_from_current_console(self):
        if not self.current_console:
            return
        try:
            info = parse_boot_info(self.current_console.get_buffer())
        except Exception:
            return
        if info:
            self._update_device_from_boot_info_dict(info)

    def _refresh_wizard_instructions_if_step_one(self):
        if not self._wizard_panel.winfo_ismapped():
            return
        if not self._wiz_step_var.get().startswith("KROK 1"):
            return
        self._wiz_detail_var.set(self._recovery_instructions_for_model())

    def _update_device_from_boot_info(self, detail: str):
        """Wywoływane ze STATE BOOT_INFO — detail to repr słownika."""
        try:
            import ast
            info = ast.literal_eval(detail.split("Dane z boot loga: ", 1)[-1])
            self._update_device_from_boot_info_dict(info)
        except Exception:
            pass

    def _update_device_from_boot_info_dict(self, info: dict):
        if not info:
            return
        changed = False
        for key, value in info.items():
            if value and self._device_info.get(key) != value:
                self._device_info[key] = value
                changed = True
        info = self._device_info
        model = info.get("model") or info.get("pid")
        serial = info.get("serial")
        mac = info.get("mac")
        ios = info.get("ios")
        if model:
            self.dev_vendor_var.set("Vendor: Cisco")
            self.dev_model_var.set(f"Model: {model}")
            model_upper = model.upper()
            if "2960X" in model_upper:
                detected_profile = "Cisco Catalyst 2960X"
            elif "2960" in model_upper:
                detected_profile = "Cisco Catalyst 2960 / 2960-C"
            elif "CISCO" in model_upper or model_upper.startswith(("WS-", "C")):
                detected_profile = "Cisco IOS generic"
            else:
                detected_profile = ""
            if detected_profile and self.profile_var.get() != detected_profile:
                self.profile_var.set(detected_profile)
                self.profile_info_var.set(f"Profile: {detected_profile}")
                self._append_dev(f"PROFILE_AUTO: {detected_profile}")
        if serial:
            self.dev_serial_var.set(f"Serial: {serial}")
        parts = []
        if mac:
            parts.append(f"MAC: {mac}")
        if ios:
            parts.append(f"IOS: {ios}")
        if parts:
            current = self.dev_status_var.get()
            if current.startswith("Password:") or current == "Password: not checked":
                pass
        if changed:
            detail = []
            if model:
                detail.append(f"model={model}")
            if serial:
                detail.append(f"serial={serial}")
            if mac:
                detail.append(f"mac={mac}")
            if ios:
                detail.append(f"ios={ios}")
            self._append_dev("DEVICE_INFO: " + " | ".join(detail))
            self._refresh_wizard_instructions_if_step_one()


def main():
    app = SwitchMultiToolApp()
    app.mainloop()
