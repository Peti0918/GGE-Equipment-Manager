import asyncio
import json
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from equipment_core import EquipmentBotSession
from app_paths import (
    ACCOUNTS_PATH,
    APP_DATA_DIR,
    APP_VERSION,
    migrate_legacy_loadouts,
    program_dir,
)

PROGRAM_DIR = program_dir()
LEGACY_PROFILE_PATHS = [
    APP_DATA_DIR / "profiles.json",
    PROGRAM_DIR / "profiles.json",
]
GAME_URL = "ep-live-mz-hu1-skn1-gr1-lt1-game.goodgamestudios.com"
GAME_SERVER = "EmpireEx_12"

# Dark blue palette inspired by the familiar Empire Automation layout.
BG = "#08111f"
TOPBAR = "#0a1424"
PANEL = "#101c2e"
PANEL_ALT = "#0d1829"
PANEL_HOVER = "#162641"
BORDER = "#22344e"
ACCENT = "#2f6fed"
ACCENT_HOVER = "#3d7bf4"
SUCCESS = "#19c98b"
WARNING = "#f3a823"
DANGER = "#f45b69"
TEXT = "#e7eef8"
MUTED = "#8da2be"
SUBTLE = "#5f7390"
LOG_BG = "#091321"
LOG_TEXT = "#d9e4f2"


class AsyncWorker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class EquipmentBotGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GGE Equipment Manager")
        self.geometry("1120x900")
        self.minsize(960, 760)
        self.configure(bg=BG)

        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.worker = AsyncWorker()
        self.session = EquipmentBotSession(
            log_callback=self.log_queue.put,
            progress_callback=self.progress_queue.put,
        )
        self.busy = False
        self._last_connected = False

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.connection_var = tk.StringVar(value="Offline")
        self.player_var = tk.StringVar(value="-")
        self.commanders_var = tk.StringVar(value="-")
        self.equipped_var = tk.StringVar(value="-")
        self.moving_var = tk.StringVar(value="-")
        self.snapshot_var = tk.StringVar(value="-")
        self.storage_var = tk.StringVar(value="- / -")
        self.storage_free_var = tk.StringVar(value="-")
        self.unequip_required_var = tk.StringVar(value="-")
        self.storage_after_var = tk.StringVar(value="-")
        self.storage_message_var = tk.StringVar(value="Storage data will appear after connecting.")
        self.operation_var = tk.StringVar(value="No active operation")
        self.progress_text_var = tk.StringVar(value="")
        self.progress_value = tk.DoubleVar(value=0)
        self.auto_scroll_var = tk.BooleanVar(value=True)

        self.profiles = {}  # username-only records; passwords are never persisted
        self.last_username = ""
        self._autocomplete_job = None

        self._configure_styles()
        self._build_ui()
        self._load_accounts()

        self.after(60, self._poll_logs)
        self.after(80, self._poll_progress)
        self.after(400, self._auto_refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Dark.TEntry",
            fieldbackground=PANEL_ALT,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(10, 8),
        )
        style.map(
            "Dark.TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
        )

        style.configure(
            "Dark.TCombobox",
            fieldbackground=PANEL_ALT,
            background=PANEL_ALT,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(10, 8),
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", PANEL_ALT), ("!disabled", PANEL_ALT)],
            foreground=[("readonly", TEXT), ("!disabled", TEXT)],
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
        )

        for name, bg, active in (
            ("Primary.TButton", ACCENT, ACCENT_HOVER),
            ("Secondary.TButton", PANEL_HOVER, "#203554"),
            ("Danger.TButton", "#b83d4a", DANGER),
            ("Success.TButton", "#147e65", "#199a78"),
            ("Warning.TButton", "#9a6719", "#b5791c"),
        ):
            style.configure(
                name,
                background=bg,
                foreground=TEXT,
                borderwidth=0,
                focusthickness=0,
                padding=(14, 10),
                font=("Segoe UI Semibold", 10),
            )
            style.map(
                name,
                background=[("active", active), ("disabled", "#182437")],
                foreground=[("disabled", "#60718a")],
            )

        style.configure(
            "Blue.Horizontal.TProgressbar",
            troughcolor="#17253a",
            background=ACCENT,
            bordercolor="#17253a",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=8,
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background="#273952",
            troughcolor=LOG_BG,
            bordercolor=LOG_BG,
            arrowcolor=MUTED,
        )
        style.map("Dark.Vertical.TScrollbar", background=[("active", "#365171")])

    def _build_ui(self):
        self._build_topbar()

        # Split the window into a scrollable dashboard and a persistent live-log
        # pane. This keeps the log visible even when the application is resized
        # smaller, while the upper controls can still be reached by scrolling.
        self.main_pane = tk.PanedWindow(
            self,
            orient="vertical",
            bg=BG,
            bd=0,
            sashwidth=6,
            sashrelief="flat",
            showhandle=False,
        )
        self.main_pane.pack(fill="both", expand=True, padx=20, pady=(16, 20))

        dashboard_host = tk.Frame(self.main_pane, bg=BG)
        log_host = tk.Frame(self.main_pane, bg=BG)
        self.main_pane.add(dashboard_host, minsize=360, stretch="always")
        self.main_pane.add(log_host, minsize=180, stretch="always")

        self.dashboard_canvas = tk.Canvas(
            dashboard_host,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        dashboard_scrollbar = ttk.Scrollbar(
            dashboard_host,
            orient="vertical",
            command=self.dashboard_canvas.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.dashboard_canvas.configure(yscrollcommand=dashboard_scrollbar.set)
        self.dashboard_canvas.pack(side="left", fill="both", expand=True)
        dashboard_scrollbar.pack(side="right", fill="y")

        body = tk.Frame(self.dashboard_canvas, bg=BG)
        self.dashboard_window = self.dashboard_canvas.create_window((0, 0), window=body, anchor="nw")

        body.bind("<Configure>", self._on_dashboard_content_configure)
        self.dashboard_canvas.bind("<Configure>", self._on_dashboard_canvas_configure)
        self.dashboard_canvas.bind("<Enter>", self._bind_dashboard_mousewheel)
        self.dashboard_canvas.bind("<Leave>", self._unbind_dashboard_mousewheel)

        self._build_account_panel(body)
        self._build_status_cards(body)
        self._build_storage_panel(body)
        self._build_actions(body)
        self._build_log_panel(log_host)

        # Give the log a useful initial height. PanedWindow minsize values keep
        # both areas usable during later resizes.
        self.after(120, self._set_initial_sash_position)

    def _on_dashboard_content_configure(self, _event=None):
        self.dashboard_canvas.configure(scrollregion=self.dashboard_canvas.bbox("all"))

    def _on_dashboard_canvas_configure(self, event):
        self.dashboard_canvas.itemconfigure(self.dashboard_window, width=event.width)

    def _bind_dashboard_mousewheel(self, _event=None):
        self.bind_all("<MouseWheel>", self._on_dashboard_mousewheel)

    def _unbind_dashboard_mousewheel(self, _event=None):
        self.unbind_all("<MouseWheel>")

    def _on_dashboard_mousewheel(self, event):
        if not self.dashboard_canvas.winfo_exists():
            return
        delta = -1 if event.delta > 0 else 1
        self.dashboard_canvas.yview_scroll(delta * 3, "units")

    def _set_initial_sash_position(self):
        try:
            height = self.main_pane.winfo_height()
            # Prefer roughly 240 px for the log, while respecting pane minima.
            self.main_pane.sash_place(0, 0, max(360, height - 240))
        except tk.TclError:
            pass

    def _build_topbar(self):
        bar = tk.Frame(self, bg=TOPBAR, height=72, highlightthickness=0)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        brand = tk.Frame(bar, bg=TOPBAR)
        brand.pack(side="left", padx=28, pady=14)

        logo = tk.Canvas(brand, width=32, height=32, bg=TOPBAR, highlightthickness=0)
        logo.pack(side="left", padx=(0, 12))
        logo.create_rectangle(3, 15, 9, 28, fill=ACCENT, outline="")
        logo.create_rectangle(13, 8, 19, 28, fill=ACCENT, outline="")
        logo.create_rectangle(23, 12, 29, 28, fill=ACCENT, outline="")
        logo.create_rectangle(5, 11, 7, 15, fill=ACCENT, outline="")
        logo.create_rectangle(15, 4, 17, 8, fill=ACCENT, outline="")
        logo.create_rectangle(25, 8, 27, 12, fill=ACCENT, outline="")

        title_box = tk.Frame(brand, bg=TOPBAR)
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text="GGE EQUIPMENT",
            bg=TOPBAR,
            fg=TEXT,
            font=("Segoe UI Semibold", 15),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text=f"Commander equipment manager  •  v{APP_VERSION}",
            bg=TOPBAR,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        right = tk.Frame(bar, bg=TOPBAR)
        right.pack(side="right", padx=28)
        self.status_dot = tk.Canvas(right, width=14, height=14, bg=TOPBAR, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 7))
        self.status_dot_id = self.status_dot.create_oval(3, 3, 11, 11, fill=SUBTLE, outline="")
        tk.Label(
            right,
            textvariable=self.connection_var,
            bg=TOPBAR,
            fg=MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left")

    def _panel(self, parent, **kwargs):
        return tk.Frame(
            parent,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            **kwargs,
        )

    def _section_header(self, parent, title, subtitle=None):
        box = tk.Frame(parent, bg=PANEL)
        box.pack(fill="x", padx=20, pady=(16, 10))
        tk.Label(box, text=title, bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 12)).pack(anchor="w")
        if subtitle:
            tk.Label(box, text=subtitle, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))

    def _build_account_panel(self, parent):
        panel = self._panel(parent)
        panel.pack(fill="x")
        self._section_header(
            panel,
            "Connection",
            "The bot stays in one live session until you disconnect. Only usernames are remembered; passwords are never saved.",
        )

        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill="x", padx=20, pady=(0, 18))
        row.columnconfigure(1, weight=1)
        row.columnconfigure(3, weight=1)

        tk.Label(row, text="Username", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 9)).grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        self.username_combo = ttk.Combobox(
            row,
            textvariable=self.username_var,
            style="Dark.TCombobox",
        )
        self.username_combo.grid(row=0, column=1, sticky="ew", padx=(0, 22))
        self.username_combo.bind("<KeyRelease>", self._on_username_keyrelease)
        self.username_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        self.username_combo.bind("<FocusIn>", lambda _event: self._refresh_profile_values())
        self.username_combo.bind("<Button-1>", lambda _event: self._refresh_profile_values())

        tk.Label(row, text="Password", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 9)).grid(
            row=0, column=2, sticky="w", padx=(0, 10)
        )
        ttk.Entry(row, textvariable=self.password_var, show="•", style="Dark.TEntry").grid(
            row=0, column=3, sticky="ew", padx=(0, 22)
        )

        self.connect_button = ttk.Button(row, text="Connect", style="Primary.TButton", command=self._connect)
        self.connect_button.grid(row=0, column=4, padx=(0, 8))

        self.disconnect_button = ttk.Button(
            row, text="Disconnect", style="Danger.TButton", command=self._disconnect, state="disabled"
        )
        self.disconnect_button.grid(row=0, column=5, padx=(0, 8))

        self.forget_account_button = ttk.Button(
            row, text="Forget account", style="Secondary.TButton", command=self._forget_account
        )
        self.forget_account_button.grid(row=0, column=6)

    def _make_stat_card(self, parent, title, variable, accent=None):
        card = tk.Frame(parent, bg=PANEL_ALT, highlightbackground=BORDER, highlightthickness=1)
        tk.Label(card, text=title, bg=PANEL_ALT, fg=MUTED, font=("Segoe UI", 9)).pack(
            anchor="w", padx=16, pady=(12, 3)
        )
        tk.Label(
            card,
            textvariable=variable,
            bg=PANEL_ALT,
            fg=accent or TEXT,
            font=("Segoe UI Semibold", 15),
        ).pack(anchor="w", padx=16, pady=(0, 12))
        return card

    def _build_status_cards(self, parent):
        section = tk.Frame(parent, bg=BG)
        section.pack(fill="x", pady=(16, 0))
        for i in range(5):
            section.columnconfigure(i, weight=1)

        cards = [
            self._make_stat_card(section, "Player ID", self.player_var),
            self._make_stat_card(section, "Commanders", self.commanders_var),
            self._make_stat_card(section, "Equipped items", self.equipped_var, ACCENT),
            self._make_stat_card(section, "Moving", self.moving_var, WARNING),
            self._make_stat_card(section, "Saved snapshot", self.snapshot_var, SUCCESS),
        ]
        for i, card in enumerate(cards):
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 4 else 6))

    def _build_storage_panel(self, parent):
        panel = self._panel(parent)
        panel.pack(fill="x", pady=(16, 0))
        self._section_header(
            panel,
            "Equipment storage",
            "Live storage capacity from the game server. Unequip is blocked automatically if there is not enough room.",
        )

        stats = tk.Frame(panel, bg=PANEL)
        stats.pack(fill="x", padx=20)
        for i in range(4):
            stats.columnconfigure(i, weight=1)

        cards = [
            self._make_stat_card(stats, "Used / capacity", self.storage_var, ACCENT),
            self._make_stat_card(stats, "Free slots", self.storage_free_var, SUCCESS),
            self._make_stat_card(stats, "Required to unequip", self.unequip_required_var, WARNING),
            self._make_stat_card(stats, "Slots after unequip", self.storage_after_var),
        ]
        for i, card in enumerate(cards):
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6))

        footer = tk.Frame(panel, bg=PANEL)
        footer.pack(fill="x", padx=20, pady=(12, 16))
        self.storage_health_dot = tk.Canvas(footer, width=14, height=14, bg=PANEL, highlightthickness=0)
        self.storage_health_dot.pack(side="left", padx=(0, 7))
        self.storage_health_dot_id = self.storage_health_dot.create_oval(3, 3, 11, 11, fill=SUBTLE, outline="")
        self.storage_health_label = tk.Label(
            footer,
            textvariable=self.storage_message_var,
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.storage_health_label.pack(side="left")

    def _build_actions(self, parent):
        panel = self._panel(parent)
        panel.pack(fill="x", pady=(16, 0))
        self._section_header(panel, "Equipment actions", "All actions use the current live session; no reconnect is required.")

        buttons = tk.Frame(panel, bg=PANEL)
        buttons.pack(fill="x", padx=20)
        for i in range(3):
            buttons.columnconfigure(i, weight=1)

        self.save_button = ttk.Button(
            buttons, text="Save equipment", style="Primary.TButton", command=self._save_snapshot, state="disabled"
        )
        self.save_button.grid(row=0, column=0, sticky="ew", padx=(0, 7))

        self.unequip_button = ttk.Button(
            buttons, text="Unequip all", style="Warning.TButton", command=self._unequip, state="disabled"
        )
        self.unequip_button.grid(row=0, column=1, sticky="ew", padx=7)

        self.restore_button = ttk.Button(
            buttons,
            text="Restore equipment",
            style="Success.TButton",
            command=self._restore,
            state="disabled",
        )
        self.restore_button.grid(row=0, column=2, sticky="ew", padx=(7, 0))

        progress_row = tk.Frame(panel, bg=PANEL)
        progress_row.pack(fill="x", padx=20, pady=(16, 18))
        progress_row.columnconfigure(0, weight=1)

        top = tk.Frame(progress_row, bg=PANEL)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        tk.Label(top, textvariable=self.operation_var, bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 9)).pack(side="left")
        tk.Label(top, textvariable=self.progress_text_var, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")

        self.progress_bar = ttk.Progressbar(
            progress_row,
            variable=self.progress_value,
            maximum=100,
            mode="determinate",
            style="Blue.Horizontal.TProgressbar",
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew")

    def _build_log_panel(self, parent):
        panel = self._panel(parent)
        panel.pack(fill="both", expand=True, pady=(16, 0))

        head = tk.Frame(panel, bg=PANEL)
        head.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(head, text="Live log", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 12)).pack(side="left")

        clear = tk.Label(head, text="Clear log", bg=PANEL, fg=MUTED, cursor="hand2", font=("Segoe UI", 9))
        clear.pack(side="right")
        clear.bind("<Button-1>", lambda _event: self._clear_log())

        auto = tk.Checkbutton(
            head,
            text="Auto-scroll",
            variable=self.auto_scroll_var,
            bg=PANEL,
            fg=SUCCESS,
            activebackground=PANEL,
            activeforeground=SUCCESS,
            selectcolor=PANEL_ALT,
            highlightthickness=0,
            bd=0,
            font=("Segoe UI", 9),
        )
        auto.pack(side="right", padx=(0, 18))

        console = tk.Frame(panel, bg=LOG_BG, highlightbackground=BORDER, highlightthickness=1)
        console.pack(fill="both", expand=True, padx=18, pady=(0, 18))

        self.log_text = tk.Text(
            console,
            height=16,
            wrap="word",
            state="disabled",
            bg=LOG_BG,
            fg=LOG_TEXT,
            insertbackground=TEXT,
            selectbackground="#24466d",
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=12,
            font=("Consolas", 10),
        )
        scrollbar = ttk.Scrollbar(
            console, orient="vertical", command=self.log_text.yview, style="Dark.Vertical.TScrollbar"
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log_text.tag_configure("timestamp", foreground="#657b99")
        self.log_text.tag_configure("normal", foreground=LOG_TEXT)
        self.log_text.tag_configure("success", foreground=SUCCESS)
        self.log_text.tag_configure("warning", foreground=WARNING)
        self.log_text.tag_configure("error", foreground=DANGER)
        self.log_text.tag_configure("accent", foreground="#72a7ff")

    def _profile_records(self):
        return sorted(
            (record for record in self.profiles.values() if record.get("username")),
            key=lambda record: record["username"].casefold(),
        )

    def _find_profile(self, username):
        key = str(username or "").strip().casefold()
        return self.profiles.get(key)

    def _refresh_profile_values(self, prefix=""):
        prefix = str(prefix or "").strip().casefold()
        usernames = [record["username"] for record in self._profile_records()]
        if prefix:
            filtered = [name for name in usernames if name.casefold().startswith(prefix)]
        else:
            filtered = usernames
        if hasattr(self, "username_combo"):
            self.username_combo.configure(values=filtered or usernames)
        return filtered

    def _write_accounts(self):
        """Persist usernames only. No password field is ever written to disk."""
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "last_username": self.last_username,
            "accounts": [record["username"] for record in self._profile_records()],
        }
        ACCOUNTS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            ACCOUNTS_PATH.chmod(0o600)
        except OSError:
            pass

    def _remember_username(self, username, *, log=True):
        username = str(username or "").strip()
        if not username:
            return False
        key = username.casefold()
        previous = self.profiles.get(key)
        changed = previous is None or previous.get("username") != username
        self.profiles[key] = {"username": username}
        self.last_username = username
        self._write_accounts()
        self._refresh_profile_values()
        if log and changed:
            self._append_log(f"[SUCCESS] Account '{username}' remembered. Password was not saved.")
        return changed

    @staticmethod
    def _read_json_file(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _import_usernames_from_legacy_profile(self, path):
        """Import only usernames from an older profiles.json and scrub passwords."""
        if not path.exists():
            return 0, ""
        data = self._read_json_file(path)
        if not isinstance(data, dict):
            return 0, ""

        imported = 0
        raw_profiles = data.get("profiles", {})
        if isinstance(raw_profiles, dict):
            for record in raw_profiles.values():
                if not isinstance(record, dict):
                    continue
                username = str(record.get("username", "")).strip()
                if username and username.casefold() not in self.profiles:
                    self.profiles[username.casefold()] = {"username": username}
                    imported += 1
                # Security migration: remove any previously persisted password.
                if "password" in record:
                    record["password"] = ""
                if "PW" in record:
                    record["PW"] = ""

        # This file belongs to older versions of this app. After importing the
        # usernames, remove it so its historical plaintext passwords cannot be
        # copied with the application. If deletion fails, overwrite passwords
        # with empty strings as a fallback.
        try:
            path.unlink()
            self._append_log(f"[SUCCESS] Removed legacy password store '{path.name}'.")
        except OSError:
            try:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                self._append_log(
                    f"[WARNING] Could not remove or scrub legacy credential file '{path}': {exc}"
                )

        return imported, str(data.get("last_username", "")).strip()

    def _load_accounts(self):
        """Load username history and migrate old password-saving builds safely."""
        self.profiles.clear()
        self.last_username = ""

        # 1) Current username-only store.
        if ACCOUNTS_PATH.exists():
            data = self._read_json_file(ACCOUNTS_PATH)
            if isinstance(data, dict):
                raw_accounts = data.get("accounts", [])
                if isinstance(raw_accounts, list):
                    for value in raw_accounts:
                        username = str(value or "").strip()
                        if username:
                            self.profiles[username.casefold()] = {"username": username}
                self.last_username = str(data.get("last_username", "")).strip()

        # 2) Import old V4/V5/V6 profile files, but NEVER import passwords.
        imported = 0
        seen_paths = set()
        for path in LEGACY_PROFILE_PATHS:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen_paths or not path.exists():
                continue
            seen_paths.add(resolved)
            try:
                count, last = self._import_usernames_from_legacy_profile(path)
                imported += count
                if not self.last_username and last:
                    self.last_username = last
            except Exception as exc:
                self._append_log(f"[WARNING] Could not migrate legacy profiles from '{path}': {exc}")

        # Migrate old equipment snapshots into per-user AppData as well.
        try:
            copied = migrate_legacy_loadouts()
            if copied:
                self._append_log(
                    f"[SUCCESS] Migrated {copied} equipment snapshot{'s' if copied != 1 else ''} to the app data folder."
                )
        except Exception as exc:
            self._append_log(f"[WARNING] Could not migrate old equipment snapshots: {exc}")

        if self.profiles:
            if not self.last_username or self.last_username.casefold() not in self.profiles:
                self.last_username = self._profile_records()[0]["username"]
            try:
                self._write_accounts()
            except Exception as exc:
                self._append_log(f"[WARNING] Could not save remembered usernames: {exc}")

        if imported:
            self._append_log(
                f"[SUCCESS] Imported {imported} saved username{'s' if imported != 1 else ''}. Legacy passwords were not imported."
            )

        self._refresh_profile_values()
        record = self._find_profile(self.last_username)
        if record:
            self.username_var.set(record["username"])
        self.password_var.set("")

    def _forget_account(self):
        username = self.username_var.get().strip()
        record = self._find_profile(username)
        if not record:
            messagebox.showinfo("Forget account", "This username is not in the remembered account list.")
            return
        if not messagebox.askyesno(
            "Forget account",
            f"Remove '{record['username']}' from the remembered username list?\n\nNo password is stored by this app.",
        ):
            return
        self.profiles.pop(record["username"].casefold(), None)
        if self.last_username.casefold() == record["username"].casefold():
            self.last_username = self._profile_records()[0]["username"] if self.profiles else ""
        try:
            self._write_accounts()
        except Exception as exc:
            messagebox.showerror("Save error", str(exc))
            return
        self.username_var.set(self.last_username)
        self.password_var.set("")
        self._refresh_profile_values()
        self._append_log(f"Account '{record['username']}' removed from remembered usernames.")

    def _on_profile_selected(self, _event=None):
        record = self._find_profile(self.username_var.get())
        if not record:
            return
        self.username_var.set(record["username"])
        self.password_var.set("")
        self.last_username = record["username"]
        try:
            self._write_accounts()
        except Exception:
            pass

    def _on_username_keyrelease(self, event):
        if event.keysym in {"Up", "Down", "Left", "Right", "Home", "End", "Tab", "Shift_L", "Shift_R"}:
            return
        if self._autocomplete_job is not None:
            try:
                self.after_cancel(self._autocomplete_job)
            except Exception:
                pass
            self._autocomplete_job = None

        typed = self.username_var.get().strip()
        if not typed:
            self.password_var.set("")
            self._refresh_profile_values()
            return

        matches = self._refresh_profile_values(typed)
        if len(matches) == 1 and not self._find_profile(typed):
            self._autocomplete_job = self.after(300, lambda prefix=typed: self._autocomplete_unique_profile(prefix))

    def _autocomplete_unique_profile(self, prefix):
        self._autocomplete_job = None
        current = self.username_var.get().strip()
        if current.casefold() != prefix.casefold():
            return
        matches = self._refresh_profile_values(prefix)
        if len(matches) != 1:
            return
        full_name = matches[0]
        if not full_name.casefold().startswith(prefix.casefold()):
            return
        record = self._find_profile(full_name)
        if not record:
            return
        self.username_var.set(record["username"])
        self.password_var.set("")
        try:
            self.username_combo.icursor(len(prefix))
            self.username_combo.selection_range(len(prefix), "end")
        except tk.TclError:
            pass

    def _current_config(self):
        return {
            "username": self.username_var.get().strip(),
            "password": self.password_var.get(),
            "game_url": GAME_URL,
            "game_server": GAME_SERVER,
        }

    def _log_tag_for(self, text):
        upper = text.upper()
        if "[ERROR]" in upper or "FAILED" in upper:
            return "error"
        if "[WARNING]" in upper or "RATE LIMIT" in upper or "SKIPPED" in upper:
            return "warning"
        if "[SUCCESS]" in upper or "CONNECTED" in upper or "LOGIN SUCCESSFUL" in upper:
            return "success"
        if "STARTING" in upper or "CONNECTING" in upper or "LOADING STATE" in upper:
            return "accent"
        return "normal"

    def _append_log(self, text):
        text = str(text).rstrip()
        self.log_text.configure(state="normal")
        match = re.match(r"^(\[[0-9]{2}:[0-9]{2}:[0-9]{2}\])\s?(.*)$", text)
        tag = self._log_tag_for(text)
        if match:
            self.log_text.insert("end", match.group(1) + " ", "timestamp")
            self.log_text.insert("end", match.group(2) + "\n", tag)
        else:
            self.log_text.insert("end", text + "\n", tag)
        if self.auto_scroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _poll_logs(self):
        try:
            while True:
                self._append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(60, self._poll_logs)

    def _poll_progress(self):
        try:
            while True:
                event = self.progress_queue.get_nowait()
                current = event.get("current", 0)
                total = event.get("total", 0)
                active = event.get("active", False)
                mode = event.get("mode")
                message = event.get("message", "")

                if mode == "restore":
                    title = "Restore equipment"
                elif mode == "unequip":
                    title = "Unequip all"
                else:
                    title = "Operation"

                if total > 0:
                    pct = max(0.0, min(100.0, current / total * 100.0))
                    self.progress_value.set(pct)
                    self.progress_text_var.set(f"{current}/{total}  •  {pct:.0f}%")
                else:
                    self.progress_value.set(0)
                    self.progress_text_var.set(message)

                self.operation_var.set(title if active else (message or "No active operation"))
        except queue.Empty:
            pass
        self.after(80, self._poll_progress)

    def _auto_refresh_status(self):
        connected = bool(self.session.connected)
        if connected:
            self._update_status(self.session.status())
        elif self._last_connected:
            self._after_disconnect()

        if connected != self._last_connected:
            self._set_connected_ui(connected)
            self._last_connected = connected

        self.after(500, self._auto_refresh_status)

    def _set_connection_indicator(self, connected, busy=False):
        if busy:
            color = WARNING
            text = self.connection_var.get()
        elif connected:
            color = SUCCESS
            text = "Online"
        else:
            color = SUBTLE
            text = "Offline"
        self.status_dot.itemconfigure(self.status_dot_id, fill=color)
        if not busy:
            self.connection_var.set(text)

    def _update_action_button_states(self, status=None):
        connected = bool(self.session.connected)
        if self.busy or not connected:
            for button in (self.save_button, self.unequip_button, self.restore_button):
                button.configure(state="disabled")
            return

        status = status or self.session.status()
        snapshot_exists = bool(status.get("snapshot_exists"))
        required = status.get("unequip_required")
        can_unequip = status.get("can_unequip")

        self.save_button.configure(state="normal")
        self.restore_button.configure(state="normal" if snapshot_exists else "disabled")
        self.unequip_button.configure(
            state="normal"
            if snapshot_exists and can_unequip is True and isinstance(required, int) and required > 0
            else "disabled"
        )

    def _set_connected_ui(self, connected):
        self.connect_button.configure(state="disabled" if connected else "normal")
        self.disconnect_button.configure(state="normal" if connected and not self.busy else "disabled")
        self._update_action_button_states()
        self._set_connection_indicator(connected, self.busy)

    def _set_busy(self, busy, text=None):
        self.busy = busy
        connected = self.session.connected
        if busy and text:
            self.connection_var.set(text)
        self.connect_button.configure(state="disabled" if connected or busy else "normal")
        self.disconnect_button.configure(state="disabled" if busy or not connected else "normal")
        self._update_action_button_states()
        self._set_connection_indicator(connected, busy)

    def _submit(self, coro, on_success=None, busy_text="Working..."):
        self._set_busy(True, busy_text)
        future = self.worker.submit(coro)

        def done(f):
            def finish_on_ui():
                try:
                    result = f.result()
                except Exception as exc:
                    self._append_log(f"[ERROR] {exc}")
                    messagebox.showerror("Error", str(exc))
                else:
                    if on_success:
                        on_success(result)
                finally:
                    self._set_busy(False)
                    self._set_connected_ui(self.session.connected)
                    if self.session.connected:
                        self._update_status(self.session.status())
            self.after(0, finish_on_ui)

        future.add_done_callback(done)

    def _connect(self):
        config = self._current_config()
        if not config["username"] or not config["password"]:
            messagebox.showwarning("Missing data", "Enter the username and password.")
            return
        self.progress_value.set(0)
        self.progress_text_var.set("")
        self.operation_var.set("Connecting...")
        self._submit(self.session.connect(config), self._after_connect, "Connecting...")

    def _after_connect(self, status):
        self._last_connected = True
        self._set_connected_ui(True)
        self._update_status(status)
        self.operation_var.set("No active operation")

        # Remember only the username after a successful login. The password is
        # deliberately cleared from the UI and is never written to disk.
        username = self.username_var.get().strip()
        try:
            changed = self._remember_username(username, log=False)
            if changed:
                self._append_log(
                    f"[SUCCESS] Account '{username}' added to remembered usernames. Password was not saved."
                )
        except Exception as exc:
            self._append_log(f"[WARNING] Login succeeded, but the username could not be remembered: {exc}")
        finally:
            self.password_var.set("")

    def _disconnect(self):
        self._submit(self.session.disconnect(), lambda _: self._after_disconnect(), "Disconnecting...")

    def _after_disconnect(self):
        self._last_connected = False
        self._set_connected_ui(False)
        self.password_var.set("")
        self.player_var.set("-")
        self.commanders_var.set("-")
        self.equipped_var.set("-")
        self.moving_var.set("-")
        self.snapshot_var.set("-")
        self.storage_var.set("- / -")
        self.storage_free_var.set("-")
        self.unequip_required_var.set("-")
        self.storage_after_var.set("-")
        self.storage_message_var.set("Storage data will appear after connecting.")
        if hasattr(self, "storage_health_dot"):
            self.storage_health_dot.itemconfigure(self.storage_health_dot_id, fill=SUBTLE)
            self.storage_health_label.configure(fg=MUTED)
        self.operation_var.set("No active operation")
        self.progress_text_var.set("")
        self.progress_value.set(0)

    def _update_status(self, status):
        self.player_var.set(str(status.get("player_id") or "-"))
        self.commanders_var.set(str(status.get("commanders", "-")))
        self.equipped_var.set(str(status.get("equipped_items", "-")))
        self.moving_var.set(str(status.get("moving", "-")))
        snapshot_exists = bool(status.get("snapshot_exists"))
        self.snapshot_var.set("Yes" if snapshot_exists else "No")

        used = status.get("storage_used")
        capacity = status.get("storage_capacity")
        free = status.get("storage_free")
        required = status.get("unequip_required")
        remaining = status.get("remaining_after_unequip")
        can_unequip = status.get("can_unequip")

        self.storage_var.set(
            f"{used} / {capacity}"
            if used is not None and capacity is not None
            else "- / -"
        )
        self.storage_free_var.set(str(free) if free is not None else "-")
        self.unequip_required_var.set(str(required) if required is not None else "-")
        self.storage_after_var.set(str(remaining) if remaining is not None else "-")

        if capacity is None or free is None:
            message = "Storage data is unavailable; unequip is disabled for safety."
            color = WARNING
        elif not snapshot_exists:
            message = "Storage detected. Save an equipment snapshot to calculate unequip requirements."
            color = ACCENT
        elif required == 0:
            message = "No currently removable equipment needs storage space."
            color = SUCCESS
        elif can_unequip is True:
            message = f"Enough space. {remaining} slots will remain after unequipping."
            color = SUCCESS
        else:
            missing = max(0, (required or 0) - free)
            message = f"Not enough space. Free {missing} more slot{'s' if missing != 1 else ''} before unequipping."
            color = DANGER

        self.storage_message_var.set(message)
        if hasattr(self, "storage_health_dot"):
            self.storage_health_dot.itemconfigure(self.storage_health_dot_id, fill=color)
            self.storage_health_label.configure(fg=color)

        self._update_action_button_states(status)

    def _save_snapshot(self):
        status = self.session.status()
        force = False
        if status.get("snapshot_exists"):
            force = messagebox.askyesno(
                "Existing snapshot",
                "A saved equipment snapshot already exists for this account.\n\n"
                "Do you want to overwrite it with the current equipment state?",
                icon="warning",
            )
            if not force:
                return

        def after(result):
            if result.get("ok"):
                messagebox.showinfo(
                    "Snapshot saved",
                    f"Saved {result['commanders']} commanders and {result['items']} equipment items.",
                )

        self.operation_var.set("Save equipment")
        self._submit(self.session.save_snapshot(force=force), after, "Saving...")

    def _unequip(self):
        status = self.session.status()
        if not status.get("snapshot_exists"):
            messagebox.showwarning("No snapshot", "Create an equipment snapshot for this account first.")
            return

        free = status.get("storage_free")
        capacity = status.get("storage_capacity")
        used = status.get("storage_used")
        required = status.get("unequip_required")
        can_unequip = status.get("can_unequip")

        if can_unequip is not True:
            if free is None or capacity is None:
                messagebox.showwarning(
                    "Storage unavailable",
                    "The equipment storage capacity could not be determined. Unequip is blocked for safety.",
                )
            else:
                missing = max(0, (required or 0) - free)
                messagebox.showwarning(
                    "Not enough storage space",
                    f"Storage: {used}/{capacity}\n"
                    f"Free slots: {free}\n"
                    f"Required: {required}\n\n"
                    f"Free {missing} more slot{'s' if missing != 1 else ''} before unequipping.",
                )
            return

        remaining = free - required
        if not messagebox.askyesno(
            "Unequip all",
            f"Storage: {used}/{capacity}\n"
            f"Free slots: {free}\n"
            f"Items to remove: {required}\n"
            f"Slots remaining afterwards: {remaining}\n\n"
            "Remove all currently equipped items from commanders that have a saved loadout?\n\n"
            "Commanders currently moving will be skipped.",
            icon="warning",
        ):
            return

        def after(result):
            if result.get("blocked"):
                if result.get("reason") == "not_enough_space":
                    messagebox.showwarning(
                        "Not enough storage space",
                        f"The live preflight found only {result.get('free')} free slots, "
                        f"but {result.get('required')} are required. Nothing was removed.",
                    )
                else:
                    messagebox.showwarning(
                        "Storage unavailable",
                        "The live storage check could not be completed. Nothing was removed.",
                    )

        self.progress_value.set(0)
        self.progress_text_var.set("")
        self._submit(self.session.unequip(), after, "Unequipping...")

    def _restore(self):
        if not self.session.status().get("snapshot_exists"):
            messagebox.showwarning("No snapshot", "There is no equipment snapshot to restore for this account.")
            return
        if not messagebox.askyesno(
            "Restore equipment",
            "Restore the previously saved equipment?\n\n"
            "The program restores by saved VIS position and skips commanders that are currently moving.",
        ):
            return
        self.progress_value.set(0)
        self.progress_text_var.set("")
        self._submit(self.session.restore(), lambda _: None, "Restoring...")

    def _on_close(self):
        if self.session.connected:
            if not messagebox.askyesno(
                "Exit", "The program is still connected. Disconnect and exit?"
            ):
                return
            future = self.worker.submit(self.session.disconnect(silent=True))
            try:
                future.result(timeout=3)
            except Exception:
                pass
        self.password_var.set("")
        self.worker.stop()
        self.destroy()


if __name__ == "__main__":
    app = EquipmentBotGUI()
    app.mainloop()
