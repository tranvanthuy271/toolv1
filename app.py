from __future__ import annotations

import binascii
import io
import os
import queue
import secrets
import struct
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

from sprite_core import (
    OUTPUT_ROOT,
    analyze_image_path,
    create_output_batch_dir,
    generate_batch,
    load_client_asset_image,
    save_background_removed_image,
    scale_image_for_client,
)
from arr_data_editor import (
    AnimationFrame,
    CharacterDataEntry,
    CharacterDataProcessorEntry,
    HEntry,
    load_arr_data_game,
    save_arr_data_game,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseWindow = TkinterDnD.Tk
    HAS_DND = True
except Exception:
    DND_FILES = None
    BaseWindow = tk.Tk
    HAS_DND = False

try:
    from Crypto.Cipher import AES as _AES_LIB
except ImportError:
    try:
        from Cryptodome.Cipher import AES as _AES_LIB
    except ImportError:
        _AES_LIB = None

_AES_PA = [79, 46, 15, 52,  8, 49, 12, 49, 29, 49, 22, 56, 30, 63, 52, 40]
_AES_MA = [14, 21,  5, 12,  3,  9,  6, 10, 18, 11,  7,  8,  4,  6, 13, 17]


def _build_aes_key() -> bytes:
    ch = [pa ^ ma for pa, ma in zip(_AES_PA, _AES_MA)]
    key = bytearray(16)
    key[0] = ch[0]
    for i in range(1, 16):
        key[i] = ch[i] ^ ch[i - 1]
    return bytes(key)


_AES_KEY = _build_aes_key()
_AES_MAGIC = b'\xCA\xFE'
_AES_HDR_SIZE = 22  # 2 + 4 + 16

APP_TITLE = "Weapon Sprite Adapter"
WINDOW_BG = "#1a1a2e"
PANEL_BG = "#16213e"
CARD_BG = "#111a33"
BORDER = "#263054"
TEXT = "#e0e0e0"
MUTED = "#8a8aa3"
ACCENT = "#7c3aed"
ACCENT_ALT = "#5b21b6"
SUCCESS = "#16a34a"
DANGER = "#dc2626"
IMAGE_TYPES = [
    ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp"),
    ("PNG files", "*.png"),
    ("All files", "*.*"),
]
IMAGE_NEAREST = Image.Resampling.NEAREST if hasattr(Image, "Resampling") else Image.NEAREST


def pick_image_path() -> str:
    return filedialog.askopenfilename(title="Choose image", filetypes=IMAGE_TYPES)


def parse_dropped_paths(root: tk.Misc, raw_data: str) -> list[Path]:
    try:
        candidates = root.tk.splitlist(raw_data)
    except Exception:
        candidates = [raw_data]

    results: list[Path] = []
    for candidate in candidates:
        path = Path(candidate.strip().strip("{}"))
        if path.exists():
            results.append(path)
    return results


def draw_checkerboard(canvas: tk.Canvas, width: int, height: int, tile_size: int = 16) -> None:
    color_a = "#24243f"
    color_b = "#313154"
    for top in range(0, height, tile_size):
        for left in range(0, width, tile_size):
            color = color_a if ((left // tile_size) + (top // tile_size)) % 2 == 0 else color_b
            canvas.create_rectangle(
                left,
                top,
                min(left + tile_size, width),
                min(top + tile_size, height),
                fill=color,
                outline=color,
            )


class ScrollableFrame(tk.Frame):
    def __init__(self, master: tk.Misc):
        super().__init__(master, bg=WINDOW_BG)
        self.canvas = tk.Canvas(self, bg=WINDOW_BG, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.content = tk.Frame(self.canvas, bg=WINDOW_BG)
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.content.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._resize_content)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)
        self.content.bind("<Enter>", self._bind_mousewheel)
        self.content.bind("<Leave>", self._unbind_mousewheel)

    def _bind_mousewheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _update_scrollregion(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_content(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _on_mousewheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def scroll_to_bottom(self) -> None:
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)


class ImageSlot(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        title: str,
        preview_size: tuple[int, int],
        on_change,
        info_mode: str = "analysis",
    ):
        super().__init__(master, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        self.preview_size = preview_size
        self.on_change = on_change
        self.info_mode = info_mode
        self.path: str | None = None
        self.analysis: dict | None = None
        self._photo: ImageTk.PhotoImage | None = None

        header = tk.Frame(self, bg=PANEL_BG)
        header.pack(fill="x", padx=10, pady=(10, 6))

        tk.Label(
            header,
            text=title,
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side="left")

        tk.Button(
            header,
            text="Clear",
            command=self.clear,
            bg=DANGER,
            fg="white",
            activebackground="#b91c1c",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=3,
            cursor="hand2",
        ).pack(side="right")

        self.preview_canvas = tk.Canvas(
            self,
            bg=CARD_BG,
            height=preview_size[1],
            highlightthickness=1,
            highlightbackground="#1f2a4c",
            cursor="hand2",
        )
        self.preview_canvas.pack(fill="x", padx=10, pady=(0, 8))
        self.preview_canvas.bind("<Button-1>", lambda _event: self.choose_image())
        self.preview_canvas.bind("<Configure>", lambda _event: self.render_preview())

        self.path_label = tk.Label(
            self,
            text="No file selected",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=360,
        )
        self.path_label.pack(fill="x", padx=10)

        self.info_label = tk.Label(
            self,
            text="",
            bg=PANEL_BG,
            fg="#b9b9d1",
            font=("Consolas", 9),
            anchor="w",
            justify="left",
        )
        self.info_label.pack(fill="x", padx=10, pady=(4, 6))

        tk.Button(
            self,
            text="Choose Image",
            command=self.choose_image,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_ALT,
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=8,
            cursor="hand2",
        ).pack(fill="x", padx=10, pady=(0, 10))

        if HAS_DND:
            self._register_drop_target(self.preview_canvas)
            self._register_drop_target(self)

        self.render_preview()

    def _register_drop_target(self, widget: tk.Misc) -> None:
        if hasattr(widget, "drop_target_register") and hasattr(widget, "dnd_bind"):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._handle_drop)

    def _handle_drop(self, event) -> str:
        for path in parse_dropped_paths(self, event.data):
            if path.is_file():
                self.set_path(str(path))
                break
        return "break"

    def choose_image(self) -> None:
        path = pick_image_path()
        if path:
            self.set_path(path)

    def set_path(self, path: str) -> None:
        self.path = path
        self.refresh_preview()
        self.on_change()

    def refresh_analysis(self, auto_remove_bg: bool, global_remove_bg: bool = False) -> None:
        if not self.path:
            self.analysis = None
            self.info_label.configure(text="")
            return

        self.analysis = analyze_image_path(self.path, auto_remove_bg=auto_remove_bg, global_remove=global_remove_bg)
        if self.info_mode == "dimensions":
            self.info_label.configure(
                text=f"{self.analysis['width']} x {self.analysis['height']} px"
            )
            return

        object_width, object_height = self.analysis["object_size"]
        center_x, center_y = self.analysis["center"]
        self.info_label.configure(
            text=(
                f"{self.analysis['width']} x {self.analysis['height']} px\n"
                f"Object box: {object_width} x {object_height} px\n"
                f"Center: {center_x}, {center_y}"
            )
        )

    def refresh_preview(self) -> None:
        self.render_preview()
        self.path_label.configure(text=Path(self.path).name if self.path else "No file selected")

    def render_preview(self) -> None:
        self.preview_canvas.delete("all")
        canvas_width = max(self.preview_canvas.winfo_width(), self.preview_size[0])
        canvas_height = self.preview_size[1]
        self.preview_canvas.configure(height=canvas_height)
        draw_checkerboard(self.preview_canvas, canvas_width, canvas_height)

        if not self.path:
            placeholder = "Drop image here or click to choose" if HAS_DND else "Click to choose image"
            self.preview_canvas.create_text(
                canvas_width / 2,
                canvas_height / 2,
                text=placeholder,
                fill=MUTED,
                font=("Segoe UI", 10),
                justify="center",
            )
            self._photo = None
            return

        with Image.open(self.path) as opened:
            preview_image = opened.convert("RGBA")
            preview_image.thumbnail((canvas_width - 16, canvas_height - 16))

        self._photo = ImageTk.PhotoImage(preview_image)
        self.preview_canvas.create_image(canvas_width / 2, canvas_height / 2, image=self._photo)

    def clear(self) -> None:
        self.path = None
        self.analysis = None
        self.info_label.configure(text="")
        self.refresh_preview()
        self.on_change()


class ResultCard(tk.Frame):
    def __init__(self, master: tk.Misc, title: str, preview_size: tuple[int, int]):
        super().__init__(master, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        self.preview_size = preview_size
        self.output_path: str | None = None
        self._photo: ImageTk.PhotoImage | None = None

        tk.Label(
            self,
            text=title,
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(anchor="w", padx=10, pady=(10, 6))

        self.preview_canvas = tk.Canvas(
            self,
            bg=CARD_BG,
            height=preview_size[1],
            highlightthickness=1,
            highlightbackground="#1f2a4c",
        )
        self.preview_canvas.pack(fill="x", padx=10, pady=(0, 8))
        self.preview_canvas.bind("<Configure>", lambda _event: self.render_preview())

        self.info_label = tk.Label(
            self,
            text="",
            bg=PANEL_BG,
            fg="#b9b9d1",
            font=("Consolas", 9),
            anchor="w",
            justify="left",
        )
        self.info_label.pack(fill="x", padx=10)

        self.open_button = tk.Button(
            self,
            text="Open File",
            command=self.open_file,
            bg=SUCCESS,
            fg="white",
            activebackground="#15803d",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=8,
            cursor="hand2",
            state="disabled",
        )
        self.open_button.pack(fill="x", padx=10, pady=(8, 10))

        self.render_preview()

    def set_result(self, result: dict | None) -> None:
        self.output_path = None if not result else result["output_path"]
        if not result:
            self.info_label.configure(text="")
            self.open_button.configure(state="disabled")
            self.render_preview()
            return

        self.info_label.configure(text=f"{result['width']} x {result['height']} px\n{Path(result['output_path']).name}")
        self.open_button.configure(state="normal")
        self.render_preview()

    def render_preview(self) -> None:
        self.preview_canvas.delete("all")
        canvas_width = max(self.preview_canvas.winfo_width(), self.preview_size[0])
        canvas_height = self.preview_size[1]
        self.preview_canvas.configure(height=canvas_height)
        draw_checkerboard(self.preview_canvas, canvas_width, canvas_height)

        if not self.output_path or not os.path.exists(self.output_path):
            self.preview_canvas.create_text(
                canvas_width / 2,
                canvas_height / 2,
                text="No output yet",
                fill=MUTED,
                font=("Segoe UI", 10),
            )
            self._photo = None
            return

        with Image.open(self.output_path) as opened:
            preview_image = opened.convert("RGBA")
            preview_image.thumbnail((canvas_width - 16, canvas_height - 16))

        self._photo = ImageTk.PhotoImage(preview_image)
        self.preview_canvas.create_image(canvas_width / 2, canvas_height / 2, image=self._photo)

    def open_file(self) -> None:
        if self.output_path and os.path.exists(self.output_path):
            os.startfile(self.output_path)


class WeaponSpriteAdapterApp(BaseWindow):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1240x860")
        self.minsize(980, 680)
        self.configure(bg=WINDOW_BG)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.auto_remove_bg = tk.BooleanVar(value=True)
        self.global_remove_bg = tk.BooleanVar(value=False)
        self.adapter_output_mode = tk.StringVar(value="separate")
        self.status_text = tk.StringVar(value="Choose a source image and at least one template image.")
        self.bg_status_text = tk.StringVar(value="Choose an image to remove its background.")
        self.bg_batch_paths: list[str] = []
        self.bg_resize_enabled = tk.BooleanVar(value=False)
        self.bg_resize_width = tk.IntVar(value=1990)
        self.bg_resize_height = tk.IntVar(value=1020)
        self.bg_numbering_enabled = tk.BooleanVar(value=False)
        self.bg_start_number = tk.IntVar(value=1)
        self.bg_out_dir_var = tk.StringVar(value="")
        self.bg_log_queue: queue.Queue = queue.Queue()
        self.bg_busy = False
        self.client_scale_output_mode = tk.StringVar(value="separate")
        self.client_scale_status_text = tk.StringVar(
            value="Drop or choose x4 PNG/.dnd files to export x4/x3/x2/x1 PNGs. Default: folder rieng."
        )
        self.encrypt_status_text = tk.StringVar(value="Choose file(s) or folder to encrypt/decrypt PNG.")
        self.idchar_status_text = tk.StringVar(value="Load an arr_data_game file to begin.")
        self.last_batch_dir: Path | None = None
        self.last_bg_batch_dir: Path | None = None
        self.last_client_scale_dir: Path | None = None
        self.last_encrypt_dir: Path | None = None
        self.encrypt_out_dir_var = tk.StringVar(value="")
        self._enc_out_dir_entry: tk.Entry | None = None
        self._enc_out_dir_browse_btn: tk.Button | None = None
        self._encrypt_log_queue: queue.Queue = queue.Queue()
        self._encrypt_busy = False
        self._enc_mode_snap: str = "auto"
        self._encrypt_action_btns: list[tk.Button] = []
        self.active_tab = "adapter"
        self.tab_buttons: dict[str, tk.Button] = {}
        self._arr_data = None
        self._idchar_selected_index: int | None = None
        self._idchar_filtered_indices: list[int] = []
        self._idchar_file_path: str | None = None
        self._idchar_active_processor_index: int | None = None
        self._idchar_processor_slot_refs: list[tuple[str, int, int]] = []
        self._idchar_visible_character_data_ids: list[int] = []
        self.idchar_icon_root_var = tk.StringVar(value=self._detect_default_icon_root())
        self._idchar_a0_icon_usage: list[dict[str, object]] = []
        self._idchar_icon_row_widgets: dict[int, dict[str, object]] = {}
        self._idchar_icon_preview_cache: dict[tuple[str, int], tuple[ImageTk.PhotoImage | None, str]] = {}

        self._build_tab_bar()

        self.content_host = tk.Frame(self, bg=WINDOW_BG)
        self.content_host.grid(row=1, column=0, sticky="nsew")
        self.content_host.grid_rowconfigure(0, weight=1)
        self.content_host.grid_columnconfigure(0, weight=1)

        self.adapter_frame = tk.Frame(self.content_host, bg=WINDOW_BG)
        self.adapter_frame.grid(row=0, column=0, sticky="nsew")
        self.adapter_frame.grid_rowconfigure(0, weight=1)
        self.adapter_frame.grid_columnconfigure(0, weight=1)

        self.remove_bg_frame = tk.Frame(self.content_host, bg=WINDOW_BG)
        self.remove_bg_frame.grid(row=0, column=0, sticky="nsew")
        self.remove_bg_frame.grid_rowconfigure(0, weight=1)
        self.remove_bg_frame.grid_columnconfigure(0, weight=1)

        self.client_scale_frame = tk.Frame(self.content_host, bg=WINDOW_BG)
        self.client_scale_frame.grid(row=0, column=0, sticky="nsew")
        self.client_scale_frame.grid_rowconfigure(0, weight=1)
        self.client_scale_frame.grid_columnconfigure(0, weight=1)

        self.encrypt_frame = tk.Frame(self.content_host, bg=WINDOW_BG)
        self.encrypt_frame.grid(row=0, column=0, sticky="nsew")
        self.encrypt_frame.grid_rowconfigure(0, weight=1)
        self.encrypt_frame.grid_columnconfigure(0, weight=1)

        self.idchar_frame = tk.Frame(self.content_host, bg=WINDOW_BG)
        self.idchar_frame.grid(row=0, column=0, sticky="nsew")
        self.idchar_frame.grid_rowconfigure(0, weight=1)
        self.idchar_frame.grid_columnconfigure(0, weight=1)

        self.scroll_frame = ScrollableFrame(self.adapter_frame)
        self.scroll_frame.grid(row=0, column=0, sticky="nsew")

        self._build_adapter_layout()
        self._build_adapter_footer()
        self._build_remove_bg_layout()
        self._build_remove_bg_footer()
        self._build_client_scale_layout()
        self._build_client_scale_footer()
        self._build_encrypt_layout()
        self._build_encrypt_footer()
        self._build_idchar_layout()
        self._build_idchar_footer()

        self.update_generate_button_state()
        self.update_remove_bg_button_state()
        self.refresh_all_analyses()
        self.show_tab("adapter")

    def _build_tab_bar(self) -> None:
        tab_bar = tk.Frame(self, bg=WINDOW_BG)
        tab_bar.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 0))

        for key, label in (("adapter", "Resize by Template"), ("remove_bg", "Remove Background"), ("client_scale", "Client X4 -> X1"), ("encrypt", "Encrypt/Decrypt PNG"), ("idchar_editor", "idChar Editor")):
            button = tk.Button(
                tab_bar,
                text=label,
                command=lambda value=key: self.show_tab(value),
                bg=PANEL_BG,
                fg=TEXT,
                activebackground="#22315a",
                activeforeground="white",
                relief="flat",
                padx=18,
                pady=10,
                cursor="hand2",
                font=("Segoe UI Semibold", 10),
            )
            button.pack(side="left", padx=(0, 10))
            self.tab_buttons[key] = button

    def show_tab(self, tab_name: str) -> None:
        self.active_tab = tab_name
        frames = {
            "adapter": self.adapter_frame,
            "remove_bg": self.remove_bg_frame,
            "client_scale": self.client_scale_frame,
            "encrypt": self.encrypt_frame,
            "idchar_editor": self.idchar_frame,
        }
        active_frame = frames.get(tab_name, self.adapter_frame)
        active_frame.tkraise()

        for key, button in self.tab_buttons.items():
            is_active = key == tab_name
            button.configure(
                bg=ACCENT if is_active else PANEL_BG,
                fg="white" if is_active else TEXT,
                activebackground=ACCENT_ALT if is_active else "#22315a",
            )

    def _build_adapter_layout(self) -> None:
        content = self.scroll_frame.content

        header = tk.Frame(content, bg=WINDOW_BG)
        header.pack(fill="x", padx=20, pady=(18, 10))

        tk.Label(
            header,
            text=APP_TITLE,
            bg=WINDOW_BG,
            fg="white",
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w")

        tk.Label(
            header,
            text="Desktop tool: resize anh theo vung doi tuong cua anh mau, xuat PNG nen trong suot.",
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 0))

        top = tk.Frame(content, bg=WINDOW_BG)
        top.pack(fill="x", padx=20)
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=2)

        left_panel = tk.Frame(top, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        tk.Label(
            left_panel,
            text="Source Image",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=12, pady=(12, 8))
        self.reference_slot = ImageSlot(left_panel, "Reference", (320, 300), self.on_slot_changed)
        self.reference_slot.pack(fill="x", padx=12, pady=(0, 12))

        right_panel = tk.Frame(top, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        right_panel.grid(row=0, column=1, sticky="nsew")
        tk.Label(
            right_panel,
            text="Template Images",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        grid = tk.Frame(right_panel, bg=PANEL_BG)
        grid.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.layout_slots: list[ImageSlot] = []
        for row in range(3):
            grid.grid_rowconfigure(row, weight=1)
        for column in range(2):
            grid.grid_columnconfigure(column, weight=1)

        for index in range(6):
            slot = ImageSlot(grid, f"Template {index + 1}", (260, 135), self.on_slot_changed)
            slot.grid(row=index // 2, column=index % 2, sticky="nsew", padx=6, pady=6)
            self.layout_slots.append(slot)

        self.results_panel = tk.Frame(content, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        self.results_panel.pack(fill="both", expand=True, padx=20, pady=(14, 18))
        tk.Label(
            self.results_panel,
            text="Generated Outputs",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        results_grid = tk.Frame(self.results_panel, bg=PANEL_BG)
        results_grid.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.result_cards: list[ResultCard] = []
        for row in range(2):
            results_grid.grid_rowconfigure(row, weight=1)
        for column in range(3):
            results_grid.grid_columnconfigure(column, weight=1)

        for index in range(6):
            card = ResultCard(results_grid, f"Output {index + 1}", (250, 150))
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=6, pady=6)
            self.result_cards.append(card)

    def _build_adapter_footer(self) -> None:
        footer = tk.Frame(self.adapter_frame, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        footer.grid(row=1, column=0, sticky="ew")

        options = tk.Frame(footer, bg=PANEL_BG)
        options.pack(side="left", padx=14, pady=12)

        tk.Checkbutton(
            options,
            text="Auto remove background",
            variable=self.auto_remove_bg,
            command=self.refresh_all_analyses,
            bg=PANEL_BG,
            fg=TEXT,
            activebackground=PANEL_BG,
            activeforeground=TEXT,
            selectcolor=WINDOW_BG,
            font=("Segoe UI", 10),
        ).pack(side="left")

        tk.Checkbutton(
            options,
            text="Remove enclosed background",
            variable=self.global_remove_bg,
            command=self.refresh_all_analyses,
            bg=PANEL_BG,
            fg=TEXT,
            activebackground=PANEL_BG,
            activeforeground=TEXT,
            selectcolor=WINDOW_BG,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(8, 0))

        tk.Label(
            options,
            text="Save:",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(12, 0))

        for value, label in (("separate", "Folder rieng"), ("common", "Folder chung")):
            tk.Radiobutton(
                options,
                text=label,
                variable=self.adapter_output_mode,
                value=value,
                bg=PANEL_BG,
                fg=TEXT,
                activebackground=PANEL_BG,
                activeforeground=TEXT,
                selectcolor=WINDOW_BG,
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(8, 0))

        dnd_text = "Drag & drop ready" if HAS_DND else "Drag & drop unavailable in current install"
        tk.Label(
            options,
            text=dnd_text,
            bg=PANEL_BG,
            fg="#c4b5fd" if HAS_DND else "#fca5a5",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(12, 0))

        buttons = tk.Frame(footer, bg=PANEL_BG)
        buttons.pack(side="right", padx=14, pady=12)

        self.generate_button = tk.Button(
            buttons,
            text="Generate Images",
            command=self.generate_images,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_ALT,
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        )
        self.generate_button.pack(side="left", padx=(0, 10))

        tk.Button(
            buttons,
            text="Open Outputs",
            command=self.open_output_directory,
            bg="#2d3a66",
            fg="#ddd6fe",
            activebackground="#37457a",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        ).pack(side="left")

        tk.Label(
            footer,
            textvariable=self.status_text,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=16)

    def _build_remove_bg_layout(self) -> None:
        content = tk.Frame(self.remove_bg_frame, bg=WINDOW_BG)
        content.grid(row=0, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)

        header = tk.Frame(content, bg=WINDOW_BG)
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))

        tk.Label(
            header,
            text="Remove Background (Batch)",
            bg=WINDOW_BG,
            fg="white",
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w")

        tk.Label(
            header,
            text="Xoa nen cho nhieu anh hoac ca thu muc, kem tinh nang resize va danh so thu tu.",
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(content, bg=WINDOW_BG)
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 18))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        source_panel = tk.Frame(body, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        source_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(
            source_panel,
            text="Input Settings",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # Input selection
        input_frame = tk.Frame(source_panel, bg=PANEL_BG)
        input_frame.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(
            input_frame, text="Choose File(s)", command=self._bg_browse_files,
            bg=ACCENT, fg="white", activebackground=ACCENT_ALT, activeforeground="white",
            relief="flat", padx=10, pady=4, cursor="hand2"
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            input_frame, text="Choose Folder", command=self._bg_browse_folder,
            bg=ACCENT, fg="white", activebackground=ACCENT_ALT, activeforeground="white",
            relief="flat", padx=10, pady=4, cursor="hand2"
        ).pack(side="left")
        
        self.bg_input_label = tk.Label(
            source_panel, text="No input selected", bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9)
        )
        self.bg_input_label.pack(anchor="w", padx=12, pady=(0, 12))

        # Resize Config
        resize_frame = tk.Frame(source_panel, bg=PANEL_BG)
        resize_frame.pack(fill="x", padx=12, pady=(0, 10))
        tk.Checkbutton(
            resize_frame, text="Resize Image", variable=self.bg_resize_enabled,
            bg=PANEL_BG, fg=TEXT, activebackground=PANEL_BG, activeforeground=TEXT, selectcolor=WINDOW_BG
        ).pack(side="left")
        tk.Label(resize_frame, text="W:", bg=PANEL_BG, fg=TEXT).pack(side="left", padx=(8, 2))
        tk.Entry(resize_frame, textvariable=self.bg_resize_width, width=6, bg=CARD_BG, fg=TEXT, relief="flat", insertbackground=TEXT).pack(side="left")
        tk.Label(resize_frame, text="H:", bg=PANEL_BG, fg=TEXT).pack(side="left", padx=(8, 2))
        tk.Entry(resize_frame, textvariable=self.bg_resize_height, width=6, bg=CARD_BG, fg=TEXT, relief="flat", insertbackground=TEXT).pack(side="left")

        # Numbering Config
        num_frame = tk.Frame(source_panel, bg=PANEL_BG)
        num_frame.pack(fill="x", padx=12, pady=(0, 10))
        tk.Checkbutton(
            num_frame, text="Sequential Numbering (e.g. 1.png, 2.png)", variable=self.bg_numbering_enabled,
            bg=PANEL_BG, fg=TEXT, activebackground=PANEL_BG, activeforeground=TEXT, selectcolor=WINDOW_BG
        ).pack(side="left")
        tk.Label(num_frame, text="Start:", bg=PANEL_BG, fg=TEXT).pack(side="left", padx=(8, 2))
        tk.Entry(num_frame, textvariable=self.bg_start_number, width=6, bg=CARD_BG, fg=TEXT, relief="flat", insertbackground=TEXT).pack(side="left")

        # Output Folder Config
        out_frame = tk.Frame(source_panel, bg=PANEL_BG)
        out_frame.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(out_frame, text="Output Folder:", bg=PANEL_BG, fg=TEXT).pack(side="left", padx=(0, 8))
        tk.Entry(out_frame, textvariable=self.bg_out_dir_var, bg=CARD_BG, fg=TEXT, state="readonly", readonlybackground=CARD_BG, relief="flat").pack(side="left", fill="x", expand=True)
        tk.Button(out_frame, text="Browse", command=self._bg_browse_output, bg=ACCENT, fg="white", activebackground=ACCENT_ALT, activeforeground="white", relief="flat", padx=6, cursor="hand2").pack(side="left", padx=(4, 0))
        tk.Button(out_frame, text="Default", command=lambda: self.bg_out_dir_var.set(""), bg="#374151", fg=TEXT, activebackground="#4b5563", activeforeground=TEXT, relief="flat", padx=6, cursor="hand2").pack(side="left", padx=(4, 0))

        output_panel = tk.Frame(body, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        output_panel.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        tk.Label(
            output_panel,
            text="Execution Log",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        self.bg_log = tk.Text(
            output_panel,
            bg=CARD_BG,
            fg=TEXT,
            font=("Consolas", 10),
            wrap="word",
            state="disabled",
            highlightthickness=1,
            highlightbackground="#1f2a4c",
            relief="flat",
        )
        self.bg_log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _build_remove_bg_footer(self) -> None:
        footer = tk.Frame(self.remove_bg_frame, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        footer.grid(row=1, column=0, sticky="ew")

        tk.Label(
            footer,
            text="Tab nay chi xoa nen va luu PNG trong suot.",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=14, pady=12)

        buttons = tk.Frame(footer, bg=PANEL_BG)
        buttons.pack(side="right", padx=14, pady=12)

        tk.Checkbutton(
            buttons,
            text="Remove enclosed background",
            variable=self.global_remove_bg,
            bg=PANEL_BG,
            fg=TEXT,
            activebackground=PANEL_BG,
            activeforeground=TEXT,
            selectcolor=WINDOW_BG,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 16))

        self.remove_bg_button = tk.Button(
            buttons,
            text="Remove Background",
            command=self.remove_background_only,
            bg=SUCCESS,
            fg="white",
            activebackground="#15803d",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        )
        self.remove_bg_button.pack(side="left", padx=(0, 10))

        tk.Button(
            buttons,
            text="Open Outputs",
            command=self.open_background_output_directory,
            bg="#2d3a66",
            fg="#ddd6fe",
            activebackground="#37457a",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        ).pack(side="left")

        tk.Label(
            footer,
            textvariable=self.bg_status_text,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=16)

    def _build_client_scale_layout(self) -> None:
        content = tk.Frame(self.client_scale_frame, bg=WINDOW_BG)
        content.grid(row=0, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(2, weight=1)

        header = tk.Frame(content, bg=WINDOW_BG)
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))

        tk.Label(
            header,
            text="Client Scale Export",
            bg=WINDOW_BG,
            fg="white",
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w")

        tk.Label(
            header,
            text=(
                "Client dung asset goc x4, sau do scale theo zoomLevel/4. "
                "Tab nay xuat PNG cho x4, x3, x2, x1 va tu giai ma .dnd neu can."
            ),
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        drop_panel = tk.Frame(content, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        drop_panel.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))

        drop_title = tk.Label(
            drop_panel,
            text="X4 Input",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        )
        drop_title.pack(anchor="w", padx=12, pady=(12, 6))

        mode_frame = tk.Frame(drop_panel, bg=PANEL_BG)
        mode_frame.pack(fill="x", padx=12, pady=(0, 10))

        tk.Label(
            mode_frame,
            text="Save mode",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left")

        for value, label in (
            ("separate", "Folder rieng: x4/x3/x2/x1"),
            ("common", "Folder chung: all_sizes"),
        ):
            tk.Radiobutton(
                mode_frame,
                text=label,
                variable=self.client_scale_output_mode,
                value=value,
                bg=PANEL_BG,
                fg=TEXT,
                activebackground=PANEL_BG,
                activeforeground=TEXT,
                selectcolor=CARD_BG,
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(12, 0))

        drop_hint = (
            "Keo file .png, .dnd hoac folder x4 vao day.\n"
            "App se xuat x4/x3/x2/x1 dang PNG va giu nguyen ten file."
            if HAS_DND
            else "Drag & drop chua kha dung trong moi truong hien tai. Dung cac nut ben duoi de chon file hoac folder."
        )
        self.client_scale_drop_hint = tk.Label(
            drop_panel,
            text=drop_hint,
            bg=CARD_BG,
            fg=TEXT if HAS_DND else "#fca5a5",
            justify="center",
            font=("Segoe UI", 10),
            padx=16,
            pady=18,
            highlightthickness=1,
            highlightbackground="#1f2a4c",
            relief="flat",
        )
        self.client_scale_drop_hint.pack(fill="x", padx=12, pady=(0, 12))

        if HAS_DND:
            self._register_client_scale_drop_target(drop_panel)
            self._register_client_scale_drop_target(drop_title)
            self._register_client_scale_drop_target(self.client_scale_drop_hint)

        log_panel = tk.Frame(content, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        log_panel.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 18))

        tk.Label(
            log_panel,
            text="Export Log",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        self.client_scale_log = tk.Text(
            log_panel,
            bg=CARD_BG,
            fg=TEXT,
            font=("Consolas", 10),
            wrap="word",
            state="disabled",
            highlightthickness=1,
            highlightbackground="#1f2a4c",
            relief="flat",
        )
        self.client_scale_log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _build_client_scale_footer(self) -> None:
        footer = tk.Frame(self.client_scale_frame, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        footer.grid(row=1, column=0, sticky="ew")

        tk.Label(
            footer,
            textvariable=self.client_scale_status_text,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=14, pady=12)

        buttons = tk.Frame(footer, bg=PANEL_BG)
        buttons.pack(side="right", padx=14, pady=12)

        tk.Button(
            buttons,
            text="Choose File(s)",
            command=self._client_scale_pick_files,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_ALT,
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(0, 10))

        tk.Button(
            buttons,
            text="Choose Folder",
            command=self._client_scale_pick_folder,
            bg="#b45309",
            fg="white",
            activebackground="#92400e",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
            font=("Segoe UI Semibold", 10),
        ).pack(side="left", padx=(0, 10))

        tk.Button(
            buttons,
            text="Open Outputs",
            command=self._client_scale_open_outputs,
            bg="#2d3a66",
            fg="#ddd6fe",
            activebackground="#37457a",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        ).pack(side="left")

    def on_slot_changed(self) -> None:
        self.refresh_all_analyses()
        self.update_generate_button_state()

    def on_bg_source_changed(self) -> None:
        self.bg_result_card.set_result(None)
        try:
            self.bg_source_slot.refresh_analysis(auto_remove_bg=False)
            if self.bg_source_slot.path:
                self.bg_status_text.set("Ready to remove background and save a transparent PNG.")
            else:
                self.bg_status_text.set("Choose an image to remove its background.")
        except Exception as exc:
            self.bg_status_text.set(f"Image analysis failed: {exc}")
        finally:
            self.update_remove_bg_button_state()

    def refresh_all_analyses(self) -> None:
        auto_remove_bg = self.auto_remove_bg.get()
        global_remove_bg = self.global_remove_bg.get()
        try:
            self.reference_slot.refresh_analysis(auto_remove_bg, global_remove_bg)
            for slot in self.layout_slots:
                slot.refresh_analysis(auto_remove_bg, global_remove_bg)
            mode = "on" if auto_remove_bg else "off"
            self.status_text.set(f"Ready. Background removal is {mode}.")
        except Exception as exc:
            self.status_text.set(f"Analysis failed: {exc}")

    def update_generate_button_state(self) -> None:
        has_reference = bool(self.reference_slot.path)
        has_layout = any(slot.path for slot in self.layout_slots)
        self.generate_button.configure(state="normal" if has_reference and has_layout else "disabled")

    def update_remove_bg_button_state(self) -> None:
        self.remove_bg_button.configure(state="normal" if self.bg_batch_paths and not self.bg_busy else "disabled")

    def generate_images(self) -> None:
        if not self.reference_slot.path:
            messagebox.showwarning(APP_TITLE, "Please choose a source image first.")
            return

        layout_paths = [slot.path for slot in self.layout_slots]
        if not any(layout_paths):
            messagebox.showwarning(APP_TITLE, "Please choose at least one template image.")
            return

        self.generate_button.configure(state="disabled")
        self.status_text.set("Generating images...")
        self.update_idletasks()

        try:
            batch_dir, results = generate_batch(
                self.reference_slot.path,
                layout_paths,
                auto_remove_bg=self.auto_remove_bg.get(),
                global_remove=self.global_remove_bg.get(),
                output_mode=self.adapter_output_mode.get(),
            )
            self.last_batch_dir = batch_dir
            for card, result in zip(self.result_cards, results):
                card.set_result(result)

            generated_count = sum(1 for result in results if result)
            self.status_text.set(f"Generated {generated_count} image(s) in {batch_dir}")
            self.after(10, self.scroll_frame.scroll_to_bottom)
            messagebox.showinfo(APP_TITLE, f"Generated {generated_count} image(s).\n\nSaved to:\n{batch_dir}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Generation failed:\n{exc}")
            self.status_text.set(f"Generation failed: {exc}")
        finally:
            self.update_generate_button_state()

    def _bg_browse_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose Images", filetypes=IMAGE_TYPES)
        if paths:
            self.bg_batch_paths = list(paths)
            self.bg_input_label.config(text=f"Selected {len(self.bg_batch_paths)} file(s)")
            self.update_remove_bg_button_state()

    def _bg_browse_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose Folder")
        if folder:
            paths = []
            for entry in os.scandir(folder):
                if entry.is_file() and entry.name.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp')):
                    paths.append(entry.path)
            self.bg_batch_paths = paths
            self.bg_input_label.config(text=f"Selected folder: {Path(folder).name} ({len(self.bg_batch_paths)} images)")
            self.update_remove_bg_button_state()

    def _bg_browse_output(self) -> None:
        folder = filedialog.askdirectory(title="Choose Output Folder")
        if folder:
            self.bg_out_dir_var.set(folder)

    def _bg_log_append(self, msg: str) -> None:
        self.bg_log.configure(state="normal")
        self.bg_log.insert("end", msg + "\n")
        self.bg_log.see("end")
        self.bg_log.configure(state="disabled")

    def _bg_log_clear(self) -> None:
        self.bg_log.configure(state="normal")
        self.bg_log.delete("1.0", "end")
        self.bg_log.configure(state="disabled")

    def _process_single_bg_task(self, path: str, output_dir: Path, resize: bool, w: int, h: int, seq_num: int | None) -> str:
        try:
            from sprite_core import load_rgba_image, remove_background
            img = load_rgba_image(path)
            img = remove_background(img, global_remove=self.global_remove_bg.get())
            
            if resize:
                from PIL import Image
                img = img.resize((w, h), Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.ANTIALIAS)
                
            orig_name = Path(path).stem
            if seq_num is not None:
                new_name = f"{seq_num}.png"
            else:
                new_name = f"{orig_name}_rm_bg.png"
                
            out_path = output_dir / new_name
            # Ensure unique name if not sequentially numbering (though numbering should guarantee uniqueness)
            if seq_num is None:
                suffix = 2
                while out_path.exists():
                    out_path = output_dir / f"{orig_name}_rm_bg_{suffix}.png"
                    suffix += 1
            
            img.save(out_path, "PNG")
            return f"Success: {Path(path).name} -> {out_path.name}"
        except Exception as e:
            return f"Error ({Path(path).name}): {e}"

    def remove_background_only(self) -> None:
        if not self.bg_batch_paths:
            messagebox.showwarning(APP_TITLE, "Please choose file(s) or a folder first.")
            return

        self.bg_busy = True
        self.update_remove_bg_button_state()
        self._bg_log_clear()
        
        custom_out = self.bg_out_dir_var.get()
        if custom_out:
            output_dir = Path(custom_out)
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            output_dir = create_output_batch_dir("rm_bg", OUTPUT_ROOT)
            
        self.last_bg_batch_dir = output_dir
        
        resize = self.bg_resize_enabled.get()
        rw = self.bg_resize_width.get()
        rh = self.bg_resize_height.get()
        
        numbering = self.bg_numbering_enabled.get()
        start_num = self.bg_start_number.get()

        self._bg_log_append(f"Starting batch process ({len(self.bg_batch_paths)} images)...")
        self._bg_log_append(f"Output directory: {output_dir}")
        self.bg_status_text.set(f"Processing {len(self.bg_batch_paths)} image(s)...")
        
        def _worker() -> None:
            processed = 0
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 1) + 4)) as executor:
                futures = {}
                current_num = start_num
                for path in self.bg_batch_paths:
                    seq_num = current_num if numbering else None
                    if numbering:
                        current_num += 1
                    futures[executor.submit(self._process_single_bg_task, path, output_dir, resize, rw, rh, seq_num)] = path
                
                for future in as_completed(futures):
                    res = future.result()
                    self.bg_log_queue.put(res)
                    processed += 1
            self.bg_log_queue.put(f"__DONE__:{processed}")

        threading.Thread(target=_worker, daemon=True).start()
        self._bg_poll_queue()

    def _bg_poll_queue(self) -> None:
        try:
            while True:
                msg = self.bg_log_queue.get_nowait()
                if msg.startswith("__DONE__:"):
                    total = msg.split(":")[1]
                    self._bg_log_append(f"\nFinished processing {total} images.")
                    self.bg_status_text.set(f"Done. Processed {total} images.")
                    self.bg_busy = False
                    self.update_remove_bg_button_state()
                    messagebox.showinfo(APP_TITLE, f"Batch process complete.\n\nSaved to:\n{self.last_bg_batch_dir}")
                    return
                else:
                    self._bg_log_append(msg)
        except queue.Empty:
            pass
        self.after(100, self._bg_poll_queue)

    def open_output_directory(self) -> None:
        target = self.last_batch_dir if self.last_batch_dir else OUTPUT_ROOT
        target.mkdir(exist_ok=True, parents=True)
        os.startfile(target)

    def open_background_output_directory(self) -> None:
        target = self.last_bg_batch_dir if self.last_bg_batch_dir else OUTPUT_ROOT
        target.mkdir(exist_ok=True, parents=True)
        os.startfile(target)

    # ── Client Scale Export tab ───────────────────────────────

    def _client_scale_log_append(self, msg: str) -> None:
        self.client_scale_log.configure(state="normal")
        self.client_scale_log.insert("end", msg + "\n")
        self.client_scale_log.see("end")
        self.client_scale_log.configure(state="disabled")
        self.update_idletasks()

    def _client_scale_log_clear(self) -> None:
        self.client_scale_log.configure(state="normal")
        self.client_scale_log.delete("1.0", "end")
        self.client_scale_log.configure(state="disabled")

    def _register_client_scale_drop_target(self, widget: tk.Misc) -> None:
        if hasattr(widget, "drop_target_register") and hasattr(widget, "dnd_bind"):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._handle_client_scale_drop)

    @staticmethod
    def _collect_client_scale_jobs(paths: list[Path]) -> list[tuple[Path, Path]]:
        jobs: list[tuple[Path, Path]] = []
        seen: set[Path] = set()

        for path in paths:
            if path.is_file() and path.suffix.lower() in {".png", ".dnd"}:
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    jobs.append((path, Path(path.name)))
                continue

            if path.is_dir():
                for pattern in ("*.png", "*.dnd"):
                    for child in sorted(path.rglob(pattern)):
                        if not child.is_file():
                            continue
                        resolved = child.resolve()
                        if resolved in seen:
                            continue
                        seen.add(resolved)
                        jobs.append((child, child.relative_to(path)))

        return jobs

    def _handle_client_scale_drop(self, event) -> str:
        dropped_paths = parse_dropped_paths(self, event.data)
        if not dropped_paths:
            self.client_scale_status_text.set("Khong doc duoc du lieu keo-tha.")
            return "break"

        jobs = self._collect_client_scale_jobs(dropped_paths)
        if not jobs:
            self.client_scale_status_text.set("Khong tim thay file .png hoac .dnd hop le.")
            messagebox.showwarning(APP_TITLE, "Khong tim thay file .png hoac .dnd hop le trong du lieu keo-tha.")
            return "break"

        self._run_client_scale_batch(jobs, prefix="[DROP]")
        return "break"

    def _client_scale_pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose x4 PNG/.dnd file(s)",
            filetypes=[("Client image files", "*.png *.dnd"), ("All files", "*.*")],
        )
        if not paths:
            return

        jobs = [(Path(path), Path(Path(path).name)) for path in paths]
        self._run_client_scale_batch(jobs)

    def _client_scale_pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose folder with x4 PNG/.dnd files")
        if not folder:
            return

        source_dir = Path(folder)
        jobs = self._collect_client_scale_jobs([source_dir])
        if not jobs:
            messagebox.showwarning(APP_TITLE, "No .png or .dnd files found in the selected folder.")
            return

        self._run_client_scale_batch(jobs, prefix=f"Folder: {source_dir}")

    def _run_client_scale_batch(
        self,
        jobs: list[tuple[Path, Path]],
        prefix: str | None = None,
    ) -> None:
        self._client_scale_log_clear()
        if prefix:
            self._client_scale_log_append(prefix)
        self._client_scale_log_append(
            f"Found {len(jobs)} file(s). Exporting x4/x3/x2/x1 PNGs in {self._client_scale_save_mode_label()}..."
        )

        output_root = OUTPUT_ROOT / "client_scale"
        batch_dir = create_output_batch_dir("client_scale", output_root)
        self.last_client_scale_dir = batch_dir

        processed = 0
        failed = 0
        for source_path, relative_path in jobs:
            if self._process_client_scale_file(source_path, relative_path, batch_dir):
                processed += 1
            else:
                failed += 1

        msg = f"Client scale export done: {processed} processed, {failed} failed."
        self._client_scale_log_append(msg)
        self.client_scale_status_text.set(
            f"{msg} Saved to {batch_dir} ({self._client_scale_save_mode_label()})."
        )
        messagebox.showinfo(APP_TITLE, f"{msg}\n\nSaved to:\n{batch_dir}")

    def _client_scale_save_mode_label(self) -> str:
        if self.client_scale_output_mode.get() == "common":
            return "folder chung"
        return "folder rieng"

    @staticmethod
    def _client_scale_unique_path(dest_path: Path) -> Path:
        if not dest_path.exists():
            return dest_path

        suffix = 2
        while True:
            candidate = dest_path.with_name(f"{dest_path.stem}_{suffix}{dest_path.suffix}")
            if not candidate.exists():
                return candidate
            suffix += 1

    def _build_client_scale_output_path(
        self,
        batch_dir: Path,
        output_relative: Path,
        scale_level: int,
    ) -> Path:
        if self.client_scale_output_mode.get() == "common":
            relative_target = output_relative.with_name(
                f"{output_relative.stem}_x{scale_level}{output_relative.suffix}"
            )
            target = batch_dir / "all_sizes" / relative_target
        else:
            target = batch_dir / f"x{scale_level}" / output_relative
        return self._client_scale_unique_path(target)

    def _process_client_scale_file(
        self,
        src_path: Path,
        relative_path: Path,
        batch_dir: Path,
    ) -> bool:
        source_image: Image.Image | None = None
        try:
            source_image, source_kind = load_client_asset_image(src_path)
            output_relative = relative_path.with_suffix(".png")

            for scale_level in (4, 3, 2, 1):
                scaled_image = scale_image_for_client(source_image, scale_level)
                dest_path = self._build_client_scale_output_path(
                    batch_dir,
                    output_relative,
                    scale_level,
                )
                dest_path.parent.mkdir(exist_ok=True, parents=True)
                try:
                    scaled_image.save(dest_path, "PNG")
                finally:
                    scaled_image.close()

            self._client_scale_log_append(
                f"  OK: {src_path.name} [{source_kind}] -> {output_relative.as_posix()}"
            )
            return True
        except Exception as exc:
            self._client_scale_log_append(f"  ERROR: {src_path.name}: {exc}")
            return False
        finally:
            if source_image is not None:
                source_image.close()

    def _client_scale_open_outputs(self) -> None:
        target = self.last_client_scale_dir if self.last_client_scale_dir else OUTPUT_ROOT / "client_scale"
        target.mkdir(exist_ok=True, parents=True)
        os.startfile(target)

    # ── Encrypt / Decrypt PNG tab ──────────────────────────────

    _PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

    @staticmethod
    def _reverse_first_bytes(data: bytearray) -> bytearray:
        """Reverse the first 51 bytes – symmetric encrypt/decrypt."""
        if len(data) < 51:
            return data
        tmp = bytearray(51)
        for i in range(51):
            tmp[50 - i] = data[i]
        for i in range(51):
            data[i] = tmp[i]
        return data

    @staticmethod
    def _aes_encrypt_bytes(plaintext: bytes) -> bytes:
        if _AES_LIB is None:
            raise RuntimeError("pycryptodome not installed. Run: pip install pycryptodome")
        nonce = secrets.token_bytes(16)
        cipher = _AES_LIB.new(_AES_KEY, _AES_LIB.MODE_CTR, nonce=b'', initial_value=nonce)
        ct = cipher.encrypt(plaintext)
        crc = binascii.crc32(ct) & 0xFFFFFFFF
        header = _AES_MAGIC + struct.pack('>I', crc) + nonce
        return header + ct

    @staticmethod
    def _aes_decrypt_bytes(data: bytes) -> bytes:
        if _AES_LIB is None:
            raise RuntimeError("pycryptodome not installed. Run: pip install pycryptodome")
        if len(data) < _AES_HDR_SIZE or data[0:2] != _AES_MAGIC:
            raise ValueError("Not a v1 AES-encrypted asset")
        stored_crc = struct.unpack('>I', data[2:6])[0]
        nonce = data[6:22]
        ct = data[_AES_HDR_SIZE:]
        crc = binascii.crc32(ct) & 0xFFFFFFFF
        if crc != stored_crc:
            raise ValueError(f"CRC32 mismatch: stored={stored_crc:#010x} computed={crc:#010x}")
        cipher = _AES_LIB.new(_AES_KEY, _AES_LIB.MODE_CTR, nonce=b'', initial_value=nonce)
        return cipher.decrypt(ct)

    @classmethod
    def _is_normal_png(cls, data: bytes | bytearray) -> bool:
        """Return True if data starts with the standard PNG header."""
        return len(data) >= 8 and data[:8] == cls._PNG_MAGIC

    def _build_encrypt_layout(self) -> None:
        content = tk.Frame(self.encrypt_frame, bg=WINDOW_BG)
        content.grid(row=0, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(5, weight=1)

        header = tk.Frame(content, bg=WINDOW_BG)
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))

        tk.Label(
            header,
            text="Encrypt / Decrypt PNG",
            bg=WINDOW_BG,
            fg="white",
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w")

        tk.Label(
            header,
            text=(
                "Chọn thuật toán mã hóa bên dưới:\n"
                "• Đảo 51 byte: đảo ngược 51 byte đầu PNG (đối xứng).  |  • AES-128-CTR: mã hóa AES chuẩn.\n"
                "Auto-detect tự nhận biết file cần encrypt hay decrypt."
            ),
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # Algorithm selector
        algo_frame = tk.Frame(content, bg=WINDOW_BG)
        algo_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 6))

        tk.Label(
            algo_frame,
            text="Thuật toán:",
            bg=WINDOW_BG,
            fg=TEXT,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 12))

        self.encrypt_algo = tk.StringVar(value="swap51")
        for value, label in (
            ("swap51", "Đảo 51 byte (PNG)"),
            ("aes", "AES-128-CTR"),
        ):
            tk.Radiobutton(
                algo_frame,
                text=label,
                variable=self.encrypt_algo,
                value=value,
                bg=WINDOW_BG,
                fg=TEXT,
                activebackground=WINDOW_BG,
                activeforeground=TEXT,
                selectcolor=CARD_BG,
                font=("Segoe UI", 10),
            ).pack(side="left", padx=(0, 16))

        # Mode selector
        mode_frame = tk.Frame(content, bg=WINDOW_BG)
        mode_frame.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))

        self.encrypt_mode = tk.StringVar(value="auto")
        for value, label in (
            ("auto", "Auto-detect"),
            ("encrypt", "Force Encrypt"),
            ("decrypt", "Force Decrypt"),
        ):
            tk.Radiobutton(
                mode_frame,
                text=label,
                variable=self.encrypt_mode,
                value=value,
                bg=WINDOW_BG,
                fg=TEXT,
                activebackground=WINDOW_BG,
                activeforeground=TEXT,
                selectcolor=CARD_BG,
                font=("Segoe UI", 10),
            ).pack(side="left", padx=(0, 16))

        self.encrypt_overwrite = tk.BooleanVar(value=False)
        tk.Checkbutton(
            mode_frame,
            text="Ghi đè file gốc (không tạo bản sao)",
            variable=self.encrypt_overwrite,
            bg=WINDOW_BG,
            fg=TEXT,
            activebackground=WINDOW_BG,
            activeforeground=TEXT,
            selectcolor=CARD_BG,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 16))

        self.encrypt_overwrite.trace_add("write", lambda *_: self._encrypt_toggle_out_dir())

        # Output directory selector
        out_dir_frame = tk.Frame(content, bg=WINDOW_BG)
        out_dir_frame.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 8))

        row1 = tk.Frame(out_dir_frame, bg=WINDOW_BG)
        row1.pack(fill="x")

        tk.Label(
            row1,
            text="Output folder:",
            bg=WINDOW_BG,
            fg=TEXT,
            font=("Segoe UI", 10),
        ).pack(side="left", padx=(0, 8))

        self._enc_out_dir_entry = tk.Entry(
            row1,
            textvariable=self.encrypt_out_dir_var,
            bg=CARD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 9),
            state="readonly",
            readonlybackground=CARD_BG,
        )
        self._enc_out_dir_entry.pack(side="left", fill="x", expand=True, ipady=4)

        self._enc_out_dir_browse_btn = tk.Button(
            row1,
            text="Browse…",
            command=self._encrypt_browse_output,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_ALT,
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            font=("Segoe UI", 9),
        )
        self._enc_out_dir_browse_btn.pack(side="left", padx=(6, 0))

        tk.Button(
            row1,
            text="Mặc định",
            command=lambda: self.encrypt_out_dir_var.set(""),
            bg="#374151",
            fg=TEXT,
            activebackground="#4b5563",
            activeforeground=TEXT,
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(4, 0))

        tk.Label(
            out_dir_frame,
            text="Để trống = mặc định (outputs/encrypt_decrypt). Bị khóa khi ‘Ghi đè file gốc’ được chọn.",
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(2, 0))

        drop_panel = tk.Frame(content, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        drop_panel.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 10))

        drop_title = tk.Label(
            drop_panel,
            text="Drop Zone",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        )
        drop_title.pack(anchor="w", padx=12, pady=(12, 6))

        dnd_hint = (
            "Keo file .png, .dnd hoac ca folder vao day.\n"
            "Mode Auto se tu nhan biet file can encrypt hay decrypt."
            if HAS_DND
            else "Drag & drop chua kha dung trong moi truong hien tai. Dung cac nut ben duoi de chon file."
        )
        self.encrypt_drop_hint = tk.Label(
            drop_panel,
            text=dnd_hint,
            bg=CARD_BG,
            fg=TEXT if HAS_DND else "#fca5a5",
            justify="center",
            font=("Segoe UI", 10),
            padx=16,
            pady=18,
            highlightthickness=1,
            highlightbackground="#1f2a4c",
            relief="flat",
        )
        self.encrypt_drop_hint.pack(fill="x", padx=12, pady=(0, 12))

        if HAS_DND:
            self._register_encrypt_drop_target(drop_panel)
            self._register_encrypt_drop_target(drop_title)
            self._register_encrypt_drop_target(self.encrypt_drop_hint)

        # Log area
        log_panel = tk.Frame(content, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        log_panel.grid(row=5, column=0, sticky="nsew", padx=20, pady=(0, 18))

        tk.Label(
            log_panel,
            text="Processing Log",
            bg=PANEL_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=12, pady=(12, 8))

        self.encrypt_log = tk.Text(
            log_panel,
            bg=CARD_BG,
            fg=TEXT,
            font=("Consolas", 10),
            wrap="word",
            state="disabled",
            highlightthickness=1,
            highlightbackground="#1f2a4c",
            relief="flat",
        )
        self.encrypt_log.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _build_encrypt_footer(self) -> None:
        footer = tk.Frame(self.encrypt_frame, bg=PANEL_BG, highlightbackground=BORDER, highlightthickness=1)
        footer.grid(row=1, column=0, sticky="ew")

        tk.Label(
            footer,
            textvariable=self.encrypt_status_text,
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=14, pady=12)

        buttons = tk.Frame(footer, bg=PANEL_BG)
        buttons.pack(side="right", padx=14, pady=12)

        self._encrypt_action_btns.clear()
        for text, cmd, bg, abg in (
            ("Encrypt File(s)", lambda: self._encrypt_pick_files("encrypt"), DANGER, "#b91c1c"),
            ("Decrypt File(s)", lambda: self._encrypt_pick_files("decrypt"), SUCCESS, "#15803d"),
            ("Encrypt Folder",  lambda: self._encrypt_pick_folder("encrypt"), "#b45309", "#92400e"),
            ("Decrypt Folder",  lambda: self._encrypt_pick_folder("decrypt"), ACCENT, ACCENT_ALT),
        ):
            btn = tk.Button(
                buttons,
                text=text,
                command=cmd,
                bg=bg,
                fg="white",
                activebackground=abg,
                activeforeground="white",
                relief="flat",
                padx=18,
                pady=10,
                cursor="hand2",
                font=("Segoe UI Semibold", 10),
            )
            btn.pack(side="left", padx=(0, 10))
            self._encrypt_action_btns.append(btn)

        tk.Button(
            buttons,
            text="Open Outputs",
            command=self._encrypt_open_outputs,
            bg="#2d3a66",
            fg="#ddd6fe",
            activebackground="#37457a",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        ).pack(side="left")

    def _encrypt_log_append(self, msg: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self._encrypt_log_queue.put(msg)
            return
        self.encrypt_log.configure(state="normal")
        self.encrypt_log.insert("end", msg + "\n")
        self.encrypt_log.see("end")
        self.encrypt_log.configure(state="disabled")
        self.update_idletasks()

    def _encrypt_log_clear(self) -> None:
        self.encrypt_log.configure(state="normal")
        self.encrypt_log.delete("1.0", "end")
        self.encrypt_log.configure(state="disabled")

    def _register_encrypt_drop_target(self, widget: tk.Misc) -> None:
        if hasattr(widget, "drop_target_register") and hasattr(widget, "dnd_bind"):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._handle_encrypt_drop)

    def _handle_encrypt_drop(self, event) -> str:
        dropped_paths = parse_dropped_paths(self, event.data)
        if not dropped_paths:
            self.encrypt_status_text.set("Khong doc duoc du lieu keo-tha.")
            return "break"

        source_files = self._collect_encrypt_source_files(dropped_paths)
        if not source_files:
            self.encrypt_status_text.set("Khong tim thay file .png hoac .dnd hop le trong du lieu keo-tha.")
            messagebox.showwarning(APP_TITLE, "Khong tim thay file .png hoac .dnd hop le trong du lieu keo-tha.")
            return "break"

        overwrite = self.encrypt_overwrite.get()
        out_dir: Path | None = None
        src_root: Path | None = None
        if not overwrite:
            custom = self.encrypt_out_dir_var.get().strip()
            if custom:
                out_dir = Path(custom)
            else:
                out_dir = OUTPUT_ROOT / "encrypt_decrypt" / "dropped"
            out_dir.mkdir(exist_ok=True, parents=True)
            # Determine src_root from dropped directories for preserving folder structure
            dropped_dirs = [p for p in dropped_paths if p.is_dir()]
            if len(dropped_dirs) == 1:
                src_root = dropped_dirs[0]

        mode = self.encrypt_mode.get()
        action = mode if mode in {"encrypt", "decrypt"} else "auto"
        self._run_encrypt_batch(source_files, out_dir, action, prefix="[DROP]", src_root=src_root)
        return "break"

    @staticmethod
    def _collect_encrypt_source_files(paths: list[Path]) -> list[Path]:
        collected: set[Path] = set()
        for path in paths:
            if path.is_file() and path.suffix.lower() in {".png", ".dnd"}:
                collected.add(path)
                continue

            if path.is_dir():
                for pattern in ("*.png", "*.dnd"):
                    for child in path.rglob(pattern):
                        if child.is_file():
                            collected.add(child)

        return sorted(collected)

    def _decide_action(self, data: bytearray, btn_action: str) -> str:
        """Decide 'encrypt' or 'decrypt' based on mode + file header.

        Returns 'encrypt', 'decrypt', or 'skip'.
        """
        is_normal = self._is_normal_png(data)
        mode = self._enc_mode_snap  # snapshotted before spawning threads

        if mode == "encrypt":
            return "encrypt"

        if mode == "decrypt":
            return "decrypt"

        if btn_action == "encrypt":
            return "encrypt" if is_normal else "skip"

        if btn_action == "decrypt":
            return "decrypt" if not is_normal else "skip"

        return "skip"

    def _decide_action_aes(self, data: bytes | bytearray, btn_action: str) -> str:
        """Decide 'encrypt' or 'decrypt' for AES mode based on mode + AES magic header."""
        is_aes_encrypted = len(data) >= _AES_HDR_SIZE and data[0:2] == _AES_MAGIC
        mode = self._enc_mode_snap  # snapshotted before spawning threads
        if mode == "encrypt":
            return "encrypt"
        if mode == "decrypt":
            return "decrypt"
        if btn_action == "encrypt":
            return "encrypt" if not is_aes_encrypted else "skip"
        if btn_action == "decrypt":
            return "decrypt" if is_aes_encrypted else "skip"
        return "skip"

    @staticmethod
    def _validate_png_bytes(data: bytes | bytearray) -> tuple[bool, str | None]:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return True, None
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _describe_input_state(data: bytes | bytearray) -> str:
        return "normal PNG" if WeaponSpriteAdapterApp._is_normal_png(data) else "encrypted/non-PNG"

    def _build_encrypt_output_path(self, src_path: Path, out_root: Path, action: str,
                                     src_root: Path | None = None,
                                     name_suffix: str = "") -> Path:
        action_dir = out_root / ("encrypted" if action == "encrypt" else "decrypted")
        if src_root is not None:
            try:
                rel = src_path.parent.relative_to(src_root)
                if rel != Path("."):
                    action_dir = action_dir / rel
            except ValueError:
                pass
        action_dir.mkdir(exist_ok=True, parents=True)

        return action_dir / f"{src_path.stem}{name_suffix}.png"

    def _build_failed_decrypt_output_path(self, src_path: Path, out_dir: Path | None,
                                          src_root: Path | None = None) -> Path:
        if out_dir is not None:
            return self._build_encrypt_output_path(
                src_path,
                out_dir,
                "decrypt",
                src_root=src_root,
                name_suffix="_1",
            )
        return src_path.with_name(f"{src_path.stem}_1.png")

    def _save_failed_decrypt_output(self, src_path: Path, transformed: bytes | bytearray,
                                    out_dir: Path | None, reason: str,
                                    src_root: Path | None = None) -> bool:
        dest = self._build_failed_decrypt_output_path(src_path, out_dir, src_root=src_root)
        dest.write_bytes(bytes(transformed))
        self._encrypt_log_append(f"  FAIL [DECRYPT]: {src_path.name} — {reason}")
        self._encrypt_log_append(f"  SAVE [DECRYPT-FAILED] → {dest}")
        return True

    def _run_encrypt_batch(
        self,
        source_files: list[Path],
        out_dir: Path | None,
        btn_action: str,
        prefix: str | None = None,
        src_root: Path | None = None,
    ) -> None:
        if self._encrypt_busy:
            return

        # Snapshot all Tkinter-bound values — must be read on the main thread
        algo = self.encrypt_algo.get()
        self._enc_mode_snap = self.encrypt_mode.get()
        overwrite = self.encrypt_overwrite.get()

        self._set_encrypt_busy(True)
        self._encrypt_log_clear()
        label = btn_action.upper()
        total = len(source_files)
        n_workers = min(32, (os.cpu_count() or 1) * 2)
        if prefix:
            self._encrypt_log_append(prefix)
        self._encrypt_log_append(
            f"[{label}] Found {total} file(s). Processing with {n_workers} workers..."
        )

        def _worker() -> None:
            ok = 0
            skipped = 0
            try:
                with ThreadPoolExecutor(max_workers=n_workers) as pool:
                    if algo == "aes":
                        futs = {
                            pool.submit(
                                self._process_aes_file, f, out_dir, btn_action, src_root, overwrite
                            ): f
                            for f in source_files
                        }
                    else:
                        futs = {
                            pool.submit(
                                self._process_encrypt_file, f, out_dir, btn_action, src_root, overwrite
                            ): f
                            for f in source_files
                        }
                    for fut in as_completed(futs):
                        try:
                            if fut.result():
                                ok += 1
                            else:
                                skipped += 1
                        except Exception as exc:
                            self._encrypt_log_queue.put(
                                f"  ERROR (future): {futs[fut].name}: {exc}"
                            )
                            skipped += 1
            finally:
                if out_dir:
                    self.last_encrypt_dir = out_dir
                msg = f"[{label}] Done: {ok} processed, {skipped} skipped, {total} total."
                self._encrypt_log_queue.put(("__DONE__", msg))

        threading.Thread(target=_worker, daemon=True).start()
        self._encrypt_poll_log()

    def _process_encrypt_file(self, src_path: Path, out_dir: Path | None,
                              btn_action: str, src_root: Path | None = None,
                              overwrite: bool | None = None) -> bool:
        """Process a single PNG file. Returns True on success."""
        try:
            _ow = overwrite if overwrite is not None else self.encrypt_overwrite.get()
            data = bytearray(src_path.read_bytes())
            if len(data) < 51:
                self._encrypt_log_append(f"  SKIP (too small, <51 bytes): {src_path.name}")
                return False

            source_state = self._describe_input_state(data)
            action = self._decide_action(data, btn_action)
            self._encrypt_log_append(f"  INPUT: {src_path.name} [{source_state}]")

            if action == "skip":
                reason = "already encrypted" if btn_action == "encrypt" else "already normal PNG"
                self._encrypt_log_append(f"  SKIP ({reason}): {src_path.name}")
                return False

            tag = "ENCRYPT" if action == "encrypt" else "DECRYPT"
            transformed = self._reverse_first_bytes(bytearray(data))

            # Validate the result
            result_is_png = self._is_normal_png(transformed)
            if action == "decrypt" and not result_is_png:
                return self._save_failed_decrypt_output(
                    src_path,
                    transformed,
                    out_dir,
                    "reverse xong khong ra PNG header, file nay khong dung format client.",
                    src_root=src_root,
                )

            if action == "decrypt":
                is_valid_png, error = self._validate_png_bytes(transformed)
                if not is_valid_png:
                    return self._save_failed_decrypt_output(
                        src_path,
                        transformed,
                        out_dir,
                        f"header da dung nhung PIL khong mo duoc PNG: {error}",
                        src_root=src_root,
                    )
            elif result_is_png:
                self._encrypt_log_append(
                    f"  WARN [{tag}]: {src_path.name} — result van con PNG header (khong dung voi file da ma hoa)"
                )

            if _ow:
                src_path.write_bytes(transformed)
                if action == "encrypt" and src_path.suffix.lower() == ".png":
                    self._encrypt_log_append(
                        f"  WARN [{tag}]: overwrite giu duoi .png, file sau khi ma hoa se KHONG mo duoc nhu anh binh thuong."
                    )
                self._encrypt_log_append(f"  OK [{tag}] (overwrite): {src_path.name}")
            else:
                dest = self._build_encrypt_output_path(src_path, out_dir, action, src_root=src_root)
                dest.write_bytes(transformed)
                self._encrypt_log_append(f"  OK [{tag}] → {dest}")

            return True
        except Exception as exc:
            self._encrypt_log_append(f"  ERROR: {src_path.name}: {exc}")
            return False

    def _process_aes_file(self, src_path: Path, out_dir: Path | None,
                          btn_action: str, src_root: Path | None = None,
                          overwrite: bool | None = None) -> bool:
        """Process a single file with AES-128-CTR. Returns True on success."""
        try:
            _ow = overwrite if overwrite is not None else self.encrypt_overwrite.get()
            if _AES_LIB is None:
                self._encrypt_log_append(
                    "  ERROR: pycryptodome not installed. Run: pip install pycryptodome"
                )
                return False

            raw = src_path.read_bytes()
            action = self._decide_action_aes(raw, btn_action)
            self._encrypt_log_append(f"  INPUT: {src_path.name}")

            if action == "skip":
                is_enc = len(raw) >= _AES_HDR_SIZE and raw[0:2] == _AES_MAGIC
                reason = "already AES-encrypted" if is_enc else "not AES-encrypted"
                self._encrypt_log_append(f"  SKIP ({reason}): {src_path.name}")
                return False

            tag = "AES-ENC" if action == "encrypt" else "AES-DEC"
            try:
                if action == "encrypt":
                    result = self._aes_encrypt_bytes(raw)
                else:
                    result = self._aes_decrypt_bytes(raw)
            except (ValueError, RuntimeError) as exc:
                self._encrypt_log_append(f"  ERR [{tag}] {src_path.name}: {exc}")
                return False

            if _ow:
                src_path.write_bytes(result)
                self._encrypt_log_append(f"  OK [{tag}] (overwrite): {src_path.name}")
            else:
                if out_dir is not None:
                    action_dir = out_dir / ("encrypted" if action == "encrypt" else "decrypted")
                    if src_root is not None:
                        try:
                            rel = src_path.parent.relative_to(src_root)
                            if rel != Path("."):
                                action_dir = action_dir / rel
                        except ValueError:
                            pass
                    action_dir.mkdir(parents=True, exist_ok=True)
                    dest = action_dir / src_path.name
                else:
                    dest = src_path.parent / src_path.name
                dest.write_bytes(result)
                self._encrypt_log_append(f"  OK [{tag}] → {dest}")

            return True
        except Exception as exc:
            self._encrypt_log_append(f"  ERROR: {src_path.name}: {exc}")
            return False

    def _encrypt_pick_files(self, btn_action: str) -> None:
        algo = self.encrypt_algo.get()
        if algo == "aes":
            title = "Choose file(s) to AES-encrypt" if btn_action == "encrypt" \
                else "Choose AES-encrypted file(s) to decrypt"
            filetypes = [("All files", "*.*"), ("PNG files", "*.png")]
        elif btn_action == "encrypt":
            title = "Choose PNG file(s) to encrypt"
            filetypes = [("PNG files", "*.png"), ("All files", "*.*")]
        else:
            title = "Choose encrypted file(s) (.dnd or obfuscated .png) to decrypt"
            filetypes = [("Encrypted files", "*.dnd *.png"), ("All files", "*.*")]
        paths = filedialog.askopenfilenames(
            title=title,
            filetypes=filetypes,
        )
        if not paths:
            return

        self._encrypt_log_clear()
        overwrite = self.encrypt_overwrite.get()
        out_dir: Path | None = None
        if not overwrite:
            custom = self.encrypt_out_dir_var.get().strip()
            out_dir = Path(custom) if custom else OUTPUT_ROOT / "encrypt_decrypt"
            out_dir.mkdir(exist_ok=True, parents=True)
        self._run_encrypt_batch([Path(p) for p in paths], out_dir, btn_action)

    def _encrypt_pick_folder(self, btn_action: str) -> None:
        title = "Choose folder with PNG files" if btn_action == "encrypt" \
            else "Choose folder with encrypted files (.dnd or obfuscated .png)"
        folder = filedialog.askdirectory(title=title)
        if not folder:
            return

        src_dir = Path(folder)
        if self.encrypt_algo.get() == "aes":
            source_files = sorted(p for p in src_dir.rglob("*") if p.is_file())
        else:
            patterns = ["*.png"] if btn_action == "encrypt" else ["*.dnd", "*.png"]
            source_files = sorted({path for pattern in patterns for path in src_dir.rglob(pattern) if path.is_file()})
        if not source_files:
            expected = ".png" if btn_action == "encrypt" else ".dnd or encrypted .png"
            messagebox.showwarning(APP_TITLE, f"No {expected} files found in the selected folder.")
            return

        self._encrypt_log_clear()
        overwrite = self.encrypt_overwrite.get()
        out_dir: Path | None = None
        if not overwrite:
            custom = self.encrypt_out_dir_var.get().strip()
            if custom:
                out_dir = Path(custom)
            else:
                out_dir = OUTPUT_ROOT / "encrypt_decrypt" / src_dir.name
            out_dir.mkdir(exist_ok=True, parents=True)
        self._run_encrypt_batch(source_files, out_dir, btn_action, prefix=f"Folder: {src_dir}", src_root=src_dir)

    def _encrypt_open_outputs(self) -> None:
        target = self.last_encrypt_dir if self.last_encrypt_dir else OUTPUT_ROOT / "encrypt_decrypt"
        target.mkdir(exist_ok=True, parents=True)
        os.startfile(target)

    def _encrypt_browse_output(self) -> None:
        folder = filedialog.askdirectory(title="Chọn thư mục output cho Encrypt/Decrypt")
        if folder:
            self.encrypt_out_dir_var.set(folder)

    def _encrypt_toggle_out_dir(self) -> None:
        if self._enc_out_dir_entry is None or self._enc_out_dir_browse_btn is None:
            return
        locked = self.encrypt_overwrite.get()
        state = "disabled" if locked else "readonly"
        btn_state = "disabled" if locked else "normal"
        self._enc_out_dir_entry.configure(state=state)
        self._enc_out_dir_browse_btn.configure(state=btn_state)

    def _set_encrypt_busy(self, busy: bool) -> None:
        self._encrypt_busy = busy
        state = "disabled" if busy else "normal"
        for btn in self._encrypt_action_btns:
            btn.configure(state=state)
        if busy:
            self.encrypt_status_text.set("Processing… please wait.")

    def _encrypt_poll_log(self) -> None:
        """Drain the log queue and, on completion, re-enable the UI."""
        try:
            while True:
                item = self._encrypt_log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__DONE__":
                    msg = item[1]
                    self._encrypt_log_append(msg)
                    self.encrypt_status_text.set(msg)
                    self._set_encrypt_busy(False)
                    messagebox.showinfo(APP_TITLE, msg)
                    return
                else:
                    self._encrypt_log_append(str(item))
        except queue.Empty:
            pass
        self.after(40, self._encrypt_poll_log)

    def _detect_default_icon_root(self) -> str:
        workspace_root = Path(__file__).resolve().parents[2]
        candidates = [
            workspace_root / "Client_base" / "langla-client" / "langla_data" / "unified",
            workspace_root / "langlabase" / "langla_data" / "unified",
        ]
        for candidate in candidates:
            if candidate.is_dir():
                return str(candidate)
        return ""

    # ------------------------------------------------------------------
    # idChar Editor tab
    # ------------------------------------------------------------------

    def _build_idchar_layout(self) -> None:
        header = tk.Frame(self.idchar_frame, bg=WINDOW_BG)
        header.pack(fill="x", padx=20, pady=(18, 10))

        tk.Label(
            header, text="idChar Editor", bg=WINDOW_BG, fg="white",
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Open arr_data_game to view / add / edit H[] entries (SkillLevelCalculator).",
            bg=WINDOW_BG, fg=MUTED, font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 0))

        # --- File selector bar ---
        file_bar = tk.Frame(self.idchar_frame, bg=PANEL_BG,
                            highlightbackground=BORDER, highlightthickness=1)
        file_bar.pack(fill="x", padx=20, pady=(0, 10))

        tk.Button(
            file_bar, text="Browse", command=self._idchar_browse,
            bg=ACCENT, fg="white", activebackground=ACCENT_ALT,
            activeforeground="white", relief="flat", padx=12, pady=6,
            cursor="hand2",
        ).pack(side="left", padx=(10, 6), pady=8)

        self.idchar_path_label = tk.Label(
            file_bar, text="No file loaded", bg=PANEL_BG, fg=MUTED,
            font=("Segoe UI", 9), anchor="w",
        )
        self.idchar_path_label.pack(side="left", fill="x", expand=True, padx=6, pady=8)

        tk.Button(
            file_bar, text="Load", command=self._idchar_load,
            bg=SUCCESS, fg="white", activebackground="#15803d",
            activeforeground="white", relief="flat", padx=12, pady=6,
            cursor="hand2",
        ).pack(side="right", padx=(6, 10), pady=8)

        icon_bar = tk.Frame(
            self.idchar_frame,
            bg=PANEL_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        icon_bar.pack(fill="x", padx=20, pady=(0, 10))

        tk.Label(
            icon_bar,
            text="Unified icon folder",
            bg=PANEL_BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(10, 8), pady=8)

        tk.Entry(
            icon_bar,
            textvariable=self.idchar_icon_root_var,
            bg=CARD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Consolas", 10),
        ).pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=4, pady=8)

        tk.Button(
            icon_bar,
            text="Browse Icons",
            command=self._idchar_browse_icon_root,
            bg=PANEL_BG,
            fg=TEXT,
            activebackground="#22315a",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side="right", padx=(0, 10), pady=8)

        tk.Button(
            icon_bar,
            text="Refresh Previews",
            command=self._idchar_refresh_icon_previews,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_ALT,
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side="right", padx=(0, 8), pady=8)

        # --- Main area: entry list + detail + icon replacer ---
        main = tk.Frame(self.idchar_frame, bg=WINDOW_BG)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=2)
        main.grid_columnconfigure(2, weight=3)
        main.grid_rowconfigure(0, weight=1)

        # Left panel – entry list
        left = tk.Frame(main, bg=PANEL_BG,
                        highlightbackground=BORDER, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        tk.Label(
            left, text="Entries", bg=PANEL_BG, fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=10, pady=(10, 4))

        search_frame = tk.Frame(left, bg=PANEL_BG)
        search_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.idchar_search_var = tk.StringVar()
        self.idchar_search_var.trace_add("write", lambda *_: self._idchar_filter_list())
        tk.Entry(
            search_frame, textvariable=self.idchar_search_var,
            bg=CARD_BG, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Consolas", 10),
        ).pack(fill="x", ipady=4)

        list_frame = tk.Frame(left, bg=PANEL_BG)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.idchar_listbox = tk.Listbox(
            list_frame, bg=CARD_BG, fg=TEXT, selectbackground=ACCENT,
            selectforeground="white", font=("Consolas", 10),
            activestyle="none", highlightthickness=0, relief="flat",
        )
        lb_scroll = tk.Scrollbar(list_frame, orient="vertical",
                                 command=self.idchar_listbox.yview)
        self.idchar_listbox.configure(yscrollcommand=lb_scroll.set)
        self.idchar_listbox.pack(side="left", fill="both", expand=True)
        lb_scroll.pack(side="right", fill="y")
        self.idchar_listbox.bind("<<ListboxSelect>>", self._idchar_on_select)

        # Center panel – entry detail + inspector
        right = tk.Frame(main, bg=PANEL_BG,
                 highlightbackground=BORDER, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 8))

        tk.Label(
            right, text="Entry Detail", bg=PANEL_BG, fg="#c4b5fd",
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=10, pady=(10, 4))

        self.idchar_index_label = tk.Label(
            right, text="Select an entry", bg=PANEL_BG, fg=MUTED,
            font=("Segoe UI", 10),
        )
        self.idchar_index_label.pack(anchor="w", padx=10)

        editor_frame = tk.Frame(right, bg=PANEL_BG)
        editor_frame.pack(fill="x")

        for label_text, attr_name in (
            ("a[0] — processorLookupTable[0]:", "idchar_a0_text"),
            ("a[1] — processorLookupTable[1]:", "idchar_a1_text"),
            ("a[2] — processorLookupTable[2]:", "idchar_a2_text"),
        ):
            tk.Label(
                editor_frame, text=label_text, bg=PANEL_BG, fg=TEXT,
                font=("Segoe UI Semibold", 9),
            ).pack(anchor="w", padx=10, pady=(8, 2))
            text_w = tk.Text(
                editor_frame, bg=CARD_BG, fg=TEXT, insertbackground=TEXT,
                relief="flat", font=("Consolas", 10), height=3, wrap="word",
            )
            text_w.pack(fill="x", padx=10, pady=(0, 4))
            setattr(self, attr_name, text_w)

        action_row = tk.Frame(editor_frame, bg=PANEL_BG)
        action_row.pack(fill="x", padx=10, pady=(8, 10))

        tk.Button(
            action_row, text="Apply Changes", command=self._idchar_apply_changes,
            bg=ACCENT, fg="white", activebackground=ACCENT_ALT,
            activeforeground="white", relief="flat", padx=12, pady=6,
            cursor="hand2",
        ).pack(side="left")

        tk.Label(
            action_row,
            text="Text editor for raw processor ids. Use the A0 icon tool in the right column to clone a new idChar and swap iconId only.",
            bg=PANEL_BG,
            fg=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=10)

        replacer_panel = tk.Frame(
            main,
            bg=WINDOW_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        replacer_panel.grid(row=0, column=2, sticky="nsew")

        replacer_header = tk.Frame(replacer_panel, bg=WINDOW_BG)
        replacer_header.pack(fill="x", padx=10, pady=(10, 6))

        tk.Label(
            replacer_header,
            text="A0 Icon Replacer",
            bg=WINDOW_BG,
            fg="#c4b5fd",
            font=("Segoe UI Semibold", 10),
        ).pack(side="left")

        tk.Button(
            replacer_header,
            text="Clone A0 -> New idChar",
            command=self._idchar_clone_a0_icon_changes,
            bg=SUCCESS,
            fg="white",
            activebackground="#15803d",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side="right")

        self.idchar_icon_tool_summary_label = tk.Label(
            replacer_panel,
            text="Select an idChar to scan unique iconId values used by a0 processors.",
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            justify="left",
            anchor="w",
        )
        self.idchar_icon_tool_summary_label.pack(fill="x", padx=10, pady=(0, 8))

        self.idchar_icon_tool_scroll = ScrollableFrame(replacer_panel)
        self.idchar_icon_tool_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.idchar_icon_tool_scroll.content.configure(bg=WINDOW_BG)

        inspector = tk.Frame(
            right,
            bg=WINDOW_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        inspector.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        inspector.grid_columnconfigure(0, weight=1)
        inspector.grid_columnconfigure(1, weight=2)
        inspector.grid_rowconfigure(1, weight=1)

        toolbar = tk.Frame(inspector, bg=WINDOW_BG)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 8))

        tk.Label(
            toolbar, text="Processor", bg=WINDOW_BG, fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left")
        self.idchar_processor_var = tk.StringVar()
        tk.Entry(
            toolbar, textvariable=self.idchar_processor_var,
            bg=CARD_BG, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Consolas", 10), width=8,
        ).pack(side="left", padx=(8, 6), ipady=4)
        tk.Button(
            toolbar, text="Open", command=self._idchar_open_processor,
            bg=PANEL_BG, fg=TEXT, activebackground="#22315a",
            activeforeground="white", relief="flat", padx=10, pady=6,
            cursor="hand2",
        ).pack(side="left", padx=(0, 16))

        tk.Label(
            toolbar, text="Find iconId", bg=WINDOW_BG, fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left")
        self.idchar_icon_search_var = tk.StringVar()
        tk.Entry(
            toolbar, textvariable=self.idchar_icon_search_var,
            bg=CARD_BG, fg=TEXT, insertbackground=TEXT, relief="flat",
            font=("Consolas", 10), width=10,
        ).pack(side="left", padx=(8, 6), ipady=4)
        tk.Button(
            toolbar, text="Find", command=self._idchar_find_icon,
            bg=PANEL_BG, fg=TEXT, activebackground="#22315a",
            activeforeground="white", relief="flat", padx=10, pady=6,
            cursor="hand2",
        ).pack(side="left")

        slot_panel = tk.Frame(inspector, bg=WINDOW_BG)
        slot_panel.grid(row=1, column=0, sticky="nsew", padx=(10, 8), pady=(0, 10))

        tk.Label(
            slot_panel, text="Processor Slots", bg=WINDOW_BG, fg="#c4b5fd",
            font=("Segoe UI Semibold", 10),
        ).pack(anchor="w", pady=(0, 4))

        slot_list_frame = tk.Frame(slot_panel, bg=WINDOW_BG)
        slot_list_frame.pack(fill="both", expand=True)
        self.idchar_processor_slot_listbox = tk.Listbox(
            slot_list_frame,
            bg=CARD_BG,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            font=("Consolas", 10),
            activestyle="none",
            highlightthickness=0,
            relief="flat",
        )
        slot_scroll = tk.Scrollbar(
            slot_list_frame,
            orient="vertical",
            command=self.idchar_processor_slot_listbox.yview,
        )
        self.idchar_processor_slot_listbox.configure(yscrollcommand=slot_scroll.set)
        self.idchar_processor_slot_listbox.pack(side="left", fill="both", expand=True)
        slot_scroll.pack(side="right", fill="y")
        self.idchar_processor_slot_listbox.bind(
            "<<ListboxSelect>>",
            self._idchar_on_processor_slot_select,
        )

        detail_panel = tk.Frame(inspector, bg=WINDOW_BG)
        detail_panel.grid(row=1, column=1, sticky="nsew", padx=(0, 10), pady=(0, 10))

        tk.Label(
            detail_panel, text="Processor Inspector", bg=WINDOW_BG, fg="#c4b5fd",
            font=("Segoe UI Semibold", 10),
        ).pack(anchor="w")

        self.idchar_processor_summary_label = tk.Label(
            detail_panel,
            text="Select a processor slot or enter a processor index.",
            bg=WINDOW_BG,
            fg=MUTED,
            font=("Segoe UI", 9),
            justify="left",
            anchor="w",
        )
        self.idchar_processor_summary_label.pack(fill="x", pady=(4, 6))

        tk.Label(
            detail_panel, text="characterDataIds", bg=WINDOW_BG, fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        self.idchar_processor_ids_text = tk.Text(
            detail_panel,
            bg=CARD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Consolas", 10),
            height=2,
            wrap="word",
        )
        self.idchar_processor_ids_text.pack(fill="x", pady=(2, 8))

        tk.Label(
            detail_panel, text="CharacterData", bg=WINDOW_BG, fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        char_list_frame = tk.Frame(detail_panel, bg=WINDOW_BG)
        char_list_frame.pack(fill="both", expand=False, pady=(2, 8))
        self.idchar_character_data_listbox = tk.Listbox(
            char_list_frame,
            bg=CARD_BG,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            font=("Consolas", 10),
            activestyle="none",
            highlightthickness=0,
            relief="flat",
            height=5,
        )
        char_scroll = tk.Scrollbar(
            char_list_frame,
            orient="vertical",
            command=self.idchar_character_data_listbox.yview,
        )
        self.idchar_character_data_listbox.configure(yscrollcommand=char_scroll.set)
        self.idchar_character_data_listbox.pack(side="left", fill="both", expand=True)
        char_scroll.pack(side="right", fill="y")
        self.idchar_character_data_listbox.bind(
            "<<ListboxSelect>>",
            self._idchar_on_character_data_select,
        )

        tk.Label(
            detail_panel, text="Frame Detail", bg=WINDOW_BG, fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        self.idchar_character_data_detail_text = tk.Text(
            detail_panel,
            bg=CARD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Consolas", 10),
            height=9,
            wrap="word",
        )
        self.idchar_character_data_detail_text.pack(fill="both", expand=True, pady=(2, 8))

        tk.Label(
            detail_panel, text="iconId Search Results", bg=WINDOW_BG, fg=TEXT,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        self.idchar_icon_search_results_text = tk.Text(
            detail_panel,
            bg=CARD_BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Consolas", 10),
            height=6,
            wrap="word",
        )
        self.idchar_icon_search_results_text.pack(fill="both", expand=True, pady=(2, 0))

        self._idchar_set_text(self.idchar_processor_ids_text, "")
        self._idchar_set_text(self.idchar_character_data_detail_text, "")
        self._idchar_set_text(self.idchar_icon_search_results_text, "")

    def _build_idchar_footer(self) -> None:
        footer = tk.Frame(self.idchar_frame, bg=WINDOW_BG)
        footer.pack(fill="x", padx=20, pady=(0, 16))

        left_btns = tk.Frame(footer, bg=WINDOW_BG)
        left_btns.pack(side="left")

        tk.Button(
            left_btns, text="Add Empty", command=self._idchar_add_empty,
            bg=PANEL_BG, fg=TEXT, activebackground="#22315a",
            activeforeground="white", relief="flat", padx=12, pady=8,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            left_btns, text="Clone Selected", command=self._idchar_clone,
            bg=PANEL_BG, fg=TEXT, activebackground="#22315a",
            activeforeground="white", relief="flat", padx=12, pady=8,
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        self.idchar_count_label = tk.Label(
            footer, text="0 entries", bg=WINDOW_BG, fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.idchar_count_label.pack(side="left", padx=12)

        self.idchar_backup_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            footer, text="Create .bak", variable=self.idchar_backup_var,
            bg=WINDOW_BG, fg=TEXT, selectcolor=CARD_BG,
            activebackground=WINDOW_BG, activeforeground=TEXT,
        ).pack(side="right", padx=(8, 0))

        tk.Button(
            footer, text="Save", command=self._idchar_save,
            bg=SUCCESS, fg="white", activebackground="#15803d",
            activeforeground="white", relief="flat", padx=20, pady=8,
            cursor="hand2", font=("Segoe UI Semibold", 10),
        ).pack(side="right")

        tk.Label(
            footer, textvariable=self.idchar_status_text,
            bg=WINDOW_BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(side="right", padx=12)

    def _idchar_browse_icon_root(self) -> None:
        folder = filedialog.askdirectory(title="Choose unified icon folder")
        if not folder:
            return
        self.idchar_icon_root_var.set(folder)
        self._idchar_icon_preview_cache = {}
        self._idchar_refresh_icon_previews()

    def _idchar_find_icon_file(self, root: Path, icon_id: int) -> Path | None:
        for filename in (
            f"char_icon_{icon_id}.dnd",
            f"char_icon_{icon_id}.png",
            f"client_icon_{icon_id}.dnd",
            f"client_icon_{icon_id}.png",
        ):
            candidate = root / filename
            if candidate.is_file():
                return candidate
        return None

    def _idchar_get_icon_preview(self, icon_id: int) -> tuple[ImageTk.PhotoImage | None, str]:
        root_text = self.idchar_icon_root_var.get().strip()
        cache_key = (root_text, icon_id)
        cached = self._idchar_icon_preview_cache.get(cache_key)
        if cached is not None:
            return cached

        if not root_text:
            result = (None, "No icon folder selected.")
            self._idchar_icon_preview_cache[cache_key] = result
            return result

        root = Path(root_text)
        if not root.is_dir():
            result = (None, "Icon folder does not exist.")
            self._idchar_icon_preview_cache[cache_key] = result
            return result

        icon_file = self._idchar_find_icon_file(root, icon_id)
        if icon_file is None:
            result = (None, f"Icon file not found for {icon_id}.")
            self._idchar_icon_preview_cache[cache_key] = result
            return result

        try:
            data = icon_file.read_bytes()
            if len(data) >= 51 and not self._is_normal_png(data):
                data = bytes(self._reverse_first_bytes(bytearray(data)))
            with Image.open(io.BytesIO(data)) as image:
                preview_image = image.convert("RGBA")
                preview_image.thumbnail((72, 72), resample=IMAGE_NEAREST)
            result = (ImageTk.PhotoImage(preview_image), icon_file.name)
        except Exception as exc:
            result = (None, f"Preview error: {exc}")

        self._idchar_icon_preview_cache[cache_key] = result
        return result

    def _idchar_render_icon_canvas(
        self,
        canvas: tk.Canvas,
        icon_id: int | None,
        fallback_text: str,
    ) -> str:
        canvas.delete("all")
        draw_checkerboard(canvas, 88, 88, tile_size=11)

        if icon_id is None:
            canvas.create_text(
                44,
                44,
                text=fallback_text,
                fill=MUTED,
                width=76,
                justify="center",
                font=("Segoe UI", 8),
            )
            canvas.image = None
            return fallback_text

        photo, status = self._idchar_get_icon_preview(icon_id)
        if photo is None:
            canvas.create_text(
                44,
                44,
                text=fallback_text,
                fill="#fca5a5",
                width=76,
                justify="center",
                font=("Segoe UI", 8),
            )
            canvas.image = None
            return status

        canvas.create_image(44, 44, image=photo)
        canvas.image = photo
        return status

    def _idchar_clear_a0_icon_tool(self) -> None:
        self._idchar_a0_icon_usage = []
        self._idchar_icon_row_widgets = {}
        for child in self.idchar_icon_tool_scroll.content.winfo_children():
            child.destroy()
        self.idchar_icon_tool_summary_label.configure(
            text="Select an idChar to scan unique iconId values used by a0 processors.",
            fg=MUTED,
        )

    def _idchar_collect_a0_icon_usage(self, entry: HEntry) -> list[dict[str, object]]:
        if not self._arr_data:
            return []

        icon_usage: dict[int, dict[str, object]] = {}
        for slot_index, processor_index in enumerate(entry.a0):
            if processor_index < 0 or processor_index >= len(self._arr_data.character_data_processors):
                continue
            processor = self._arr_data.character_data_processors[processor_index]
            for char_data_id in processor.character_data_ids:
                if char_data_id < 0 or char_data_id >= len(self._arr_data.character_data):
                    continue
                char_data = self._arr_data.character_data[char_data_id]
                for frame_index, frame in enumerate(char_data.animation_frames):
                    if frame.icon_id == 0:
                        continue
                    usage = icon_usage.setdefault(
                        frame.icon_id,
                        {
                            "icon_id": frame.icon_id,
                            "processor_indexes": set(),
                            "slot_indexes": set(),
                            "character_data_ids": set(),
                            "frame_refs": [],
                        },
                    )
                    usage["processor_indexes"].add(processor_index)
                    usage["slot_indexes"].add(slot_index)
                    usage["character_data_ids"].add(char_data_id)
                    usage["frame_refs"].append((processor_index, char_data_id, frame_index))

        return sorted(icon_usage.values(), key=lambda item: int(item["icon_id"]))

    def _idchar_refresh_a0_icon_tool(self, entry: HEntry) -> None:
        self._idchar_clear_a0_icon_tool()
        if not self._arr_data:
            return

        icon_usage = self._idchar_collect_a0_icon_usage(entry)
        self._idchar_a0_icon_usage = icon_usage
        if not icon_usage:
            self.idchar_icon_tool_summary_label.configure(
                text="a0 does not reference any non-zero iconId values.",
                fg="#fca5a5",
            )
            return

        self.idchar_icon_tool_summary_label.configure(
            text=(
                f"a0 slots={len(entry.a0)}  |  unique processors={len(set(entry.a0))}  |  "
                f"unique iconId={len(icon_usage)}. Edit the right column, then clone to append a new idChar."
            ),
            fg=TEXT,
        )

        for usage in icon_usage:
            icon_id = int(usage["icon_id"])
            processor_indexes = sorted(int(value) for value in usage["processor_indexes"])
            slot_indexes = sorted(int(value) for value in usage["slot_indexes"])
            character_data_ids = sorted(int(value) for value in usage["character_data_ids"])
            frame_total = len(usage["frame_refs"])
            slot_preview = ", ".join(f"a0[{value}]" for value in slot_indexes[:6])
            if len(slot_indexes) > 6:
                slot_preview += ", ..."

            row = tk.Frame(
                self.idchar_icon_tool_scroll.content,
                bg=PANEL_BG,
                highlightbackground=BORDER,
                highlightthickness=1,
            )
            row.pack(fill="x", pady=(0, 8))

            old_canvas = tk.Canvas(
                row,
                width=88,
                height=88,
                bg=CARD_BG,
                highlightthickness=1,
                highlightbackground="#1f2a4c",
                relief="flat",
            )
            old_canvas.pack(side="left", padx=(10, 12), pady=10)

            info = tk.Frame(row, bg=PANEL_BG)
            info.pack(side="left", fill="both", expand=True, pady=10)

            tk.Label(
                info,
                text=f"Current iconId {icon_id}",
                bg=PANEL_BG,
                fg=TEXT,
                font=("Segoe UI Semibold", 10),
            ).pack(anchor="w")

            tk.Label(
                info,
                text=(
                    f"processors={processor_indexes}  |  charData={character_data_ids}  |  frames={frame_total}\n"
                    f"used at {slot_preview}"
                ),
                bg=PANEL_BG,
                fg=MUTED,
                justify="left",
                font=("Consolas", 9),
            ).pack(anchor="w", pady=(4, 0))

            replace_panel = tk.Frame(row, bg=PANEL_BG)
            replace_panel.pack(side="right", padx=(12, 10), pady=10)

            tk.Label(
                replace_panel,
                text="New iconId",
                bg=PANEL_BG,
                fg=TEXT,
                font=("Segoe UI Semibold", 9),
            ).pack(anchor="w")

            replace_var = tk.StringVar(value=str(icon_id))
            tk.Entry(
                replace_panel,
                textvariable=replace_var,
                bg=CARD_BG,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                font=("Consolas", 10),
                width=12,
            ).pack(anchor="w", ipady=4, pady=(4, 8))

            new_canvas = tk.Canvas(
                replace_panel,
                width=88,
                height=88,
                bg=CARD_BG,
                highlightthickness=1,
                highlightbackground="#1f2a4c",
                relief="flat",
            )
            new_canvas.pack(anchor="w")

            status_label = tk.Label(
                replace_panel,
                text="",
                bg=PANEL_BG,
                fg=MUTED,
                justify="left",
                font=("Segoe UI", 8),
                wraplength=210,
            )
            status_label.pack(anchor="w", pady=(6, 0))

            self._idchar_icon_row_widgets[icon_id] = {
                "var": replace_var,
                "old_canvas": old_canvas,
                "new_canvas": new_canvas,
                "status_label": status_label,
            }
            replace_var.trace_add(
                "write",
                lambda *_args, current_icon_id=icon_id: self._idchar_update_icon_row(current_icon_id),
            )
            self._idchar_update_icon_row(icon_id)

    def _idchar_update_icon_row(self, old_icon_id: int) -> None:
        row = self._idchar_icon_row_widgets.get(old_icon_id)
        if not row:
            return

        old_status = self._idchar_render_icon_canvas(
            row["old_canvas"],
            old_icon_id,
            f"iconId\n{old_icon_id}",
        )

        raw_value = row["var"].get().strip()
        if not raw_value:
            self._idchar_render_icon_canvas(row["new_canvas"], None, "Keep")
            row["status_label"].configure(
                text=f"Current preview: {old_status}\nBlank means keep original iconId.",
                fg=MUTED,
            )
            return

        try:
            new_icon_id = int(raw_value)
        except ValueError:
            self._idchar_render_icon_canvas(row["new_canvas"], None, "Invalid")
            row["status_label"].configure(
                text="New iconId must be an integer.",
                fg="#fca5a5",
            )
            return

        new_status = self._idchar_render_icon_canvas(
            row["new_canvas"],
            new_icon_id,
            f"iconId\n{new_icon_id}",
        )
        if new_icon_id == old_icon_id:
            status_text = f"Current preview: {old_status}\nKeeping original iconId {old_icon_id}."
            status_color = MUTED
        else:
            status_text = f"Current preview: {old_status}\nNew preview: {new_status}\nReplace {old_icon_id} -> {new_icon_id}."
            status_color = TEXT
        row["status_label"].configure(text=status_text, fg=status_color)

    def _idchar_refresh_icon_previews(self) -> None:
        if not self._idchar_icon_row_widgets:
            self.idchar_status_text.set("Select an idChar first to build A0 previews.")
            return
        self._idchar_icon_preview_cache = {}
        for icon_id in list(self._idchar_icon_row_widgets):
            self._idchar_update_icon_row(icon_id)
        self.idchar_status_text.set("Refreshed A0 icon previews.")

    def _idchar_parse_icon_replacements(self) -> dict[int, int]:
        replacements: dict[int, int] = {}
        for old_icon_id, row in self._idchar_icon_row_widgets.items():
            raw_value = row["var"].get().strip()
            if not raw_value:
                continue
            try:
                new_icon_id = int(raw_value)
            except ValueError as exc:
                raise ValueError(f"iconId {old_icon_id}: new value must be an integer.") from exc
            if not 0 <= new_icon_id <= 65535:
                raise ValueError(f"iconId {old_icon_id}: new value must be between 0 and 65535.")
            if new_icon_id != old_icon_id:
                replacements[old_icon_id] = new_icon_id
        return replacements

    def _idchar_select_index(self, index: int) -> None:
        if not self._arr_data or not (0 <= index < len(self._arr_data.h_entries)):
            return
        self.idchar_search_var.set("")
        self._idchar_populate_list()
        if index not in self._idchar_filtered_indices:
            return
        list_index = self._idchar_filtered_indices.index(index)
        self.idchar_listbox.selection_clear(0, tk.END)
        self.idchar_listbox.selection_set(list_index)
        self.idchar_listbox.activate(list_index)
        self.idchar_listbox.see(list_index)
        self._idchar_on_select()

    def _idchar_clone_a0_icon_changes(self) -> None:
        if self._idchar_selected_index is None or not self._arr_data:
            self.idchar_status_text.set("Select an idChar before cloning A0 icon changes.")
            return

        source_index = self._idchar_selected_index
        source_entry = self._arr_data.h_entries[source_index]
        if not source_entry.a0:
            self.idchar_status_text.set("Selected idChar has an empty a0 table.")
            return

        try:
            replacements = self._idchar_parse_icon_replacements()
        except ValueError as exc:
            messagebox.showerror("Invalid iconId", str(exc))
            return

        if not replacements:
            self.idchar_status_text.set("No iconId changes entered. Edit at least one New iconId first.")
            return

        old_to_new_character_data: dict[int, int] = {}
        old_to_new_processor: dict[int, int] = {}
        new_a0: list[int] = []
        cloned_character_data_count = 0
        cloned_processor_count = 0

        try:
            for processor_index in source_entry.a0:
                if processor_index not in old_to_new_processor:
                    if processor_index < 0 or processor_index >= len(self._arr_data.character_data_processors):
                        raise ValueError(f"Processor {processor_index} in a0 is out of range.")

                    source_processor = self._arr_data.character_data_processors[processor_index]
                    new_character_data_ids: list[int] = []
                    for char_data_id in source_processor.character_data_ids:
                        if char_data_id not in old_to_new_character_data:
                            if char_data_id < 0 or char_data_id >= len(self._arr_data.character_data):
                                raise ValueError(
                                    f"CharacterData {char_data_id} referenced by processor {processor_index} is out of range."
                                )

                            source_character_data = self._arr_data.character_data[char_data_id]
                            cloned_frames = [
                                AnimationFrame(
                                    icon_id=replacements.get(frame.icon_id, frame.icon_id),
                                    rotation_frame=frame.rotation_frame,
                                    hue_flag=frame.hue_flag,
                                    offset_x=frame.offset_x,
                                    offset_y=frame.offset_y,
                                )
                                for frame in source_character_data.animation_frames
                            ]
                            self._arr_data.character_data.append(
                                CharacterDataEntry(
                                    part_type=source_character_data.part_type,
                                    animation_frames=cloned_frames,
                                )
                            )
                            old_to_new_character_data[char_data_id] = len(self._arr_data.character_data) - 1
                            cloned_character_data_count += 1

                        new_character_data_ids.append(old_to_new_character_data[char_data_id])

                    self._arr_data.character_data_processors.append(
                        CharacterDataProcessorEntry(character_data_ids=new_character_data_ids)
                    )
                    old_to_new_processor[processor_index] = len(self._arr_data.character_data_processors) - 1
                    cloned_processor_count += 1

                new_a0.append(old_to_new_processor[processor_index])

            self._arr_data.h_entries.append(
                HEntry(
                    a0=new_a0,
                    a1=list(source_entry.a1),
                    a2=list(source_entry.a2),
                )
            )
        except ValueError as exc:
            messagebox.showerror("Clone Error", str(exc))
            return

        new_index = len(self._arr_data.h_entries) - 1
        self._idchar_populate_list()
        self._idchar_select_index(new_index)
        self.idchar_status_text.set(
            f"Cloned idChar {source_index} -> {new_index}; changed {len(replacements)} iconId, cloned {cloned_processor_count} processors, cloned {cloned_character_data_count} characterData entries."
        )

    # --- idChar callbacks ---

    def _idchar_browse(self) -> None:
        path = filedialog.askopenfilename(
            title="Open arr_data_game",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if path:
            self._idchar_file_path = path
            self.idchar_path_label.configure(text=path, fg=TEXT)

    def _idchar_load(self) -> None:
        if not self._idchar_file_path:
            self.idchar_status_text.set("No file selected. Click Browse first.")
            return
        try:
            self._arr_data = load_arr_data_game(self._idchar_file_path)
            self._idchar_selected_index = None
            self._idchar_populate_list()
            self._idchar_clear_entry_details()
            self.idchar_status_text.set(
                f"Loaded {len(self._arr_data.h_entries)} entries from "
                f"{os.path.basename(self._idchar_file_path)}"
            )
        except Exception as e:
            self._arr_data = None
            self._idchar_clear_entry_details()
            messagebox.showerror("Load Error", str(e))

    def _idchar_populate_list(self) -> None:
        self.idchar_listbox.delete(0, tk.END)
        if not self._arr_data:
            self.idchar_count_label.configure(text="0 entries")
            return

        search = self.idchar_search_var.get().strip()
        indices = list(range(len(self._arr_data.h_entries)))
        if search:
            indices = [i for i in indices if search in str(i)]
        self._idchar_filtered_indices = indices

        for i in indices:
            e = self._arr_data.h_entries[i]
            self.idchar_listbox.insert(
                tk.END,
                f"[{i:4d}]  a0={len(e.a0):3d}  a1={len(e.a1):3d}  a2={len(e.a2):3d}",
            )
        self.idchar_count_label.configure(
            text=f"{len(self._arr_data.h_entries)} entries"
        )

    def _idchar_filter_list(self) -> None:
        self._idchar_populate_list()

    def _idchar_on_select(self, _event=None) -> None:
        sel = self.idchar_listbox.curselection()
        if not sel or not self._arr_data:
            return
        list_idx = sel[0]
        if list_idx >= len(self._idchar_filtered_indices):
            return
        self._idchar_selected_index = self._idchar_filtered_indices[list_idx]
        entry = self._arr_data.h_entries[self._idchar_selected_index]

        self.idchar_index_label.configure(
            text=f"Index: {self._idchar_selected_index}", fg=TEXT,
        )
        for widget, values in (
            (self.idchar_a0_text, entry.a0),
            (self.idchar_a1_text, entry.a1),
            (self.idchar_a2_text, entry.a2),
        ):
            widget.delete("1.0", tk.END)
            widget.insert("1.0", ",".join(str(v) for v in values))
        self._idchar_populate_processor_slots(entry)
        self._idchar_refresh_a0_icon_tool(entry)
        self._idchar_clear_processor_inspector()

    def _idchar_apply_changes(self) -> None:
        if self._idchar_selected_index is None or not self._arr_data:
            return

        def _parse(widget):
            text = widget.get("1.0", tk.END).strip()
            if not text:
                return []
            return [int(v.strip()) for v in text.split(",") if v.strip()]

        try:
            entry = self._arr_data.h_entries[self._idchar_selected_index]
            entry.a0 = _parse(self.idchar_a0_text)
            entry.a1 = _parse(self.idchar_a1_text)
            entry.a2 = _parse(self.idchar_a2_text)
            self._idchar_populate_list()
            self._idchar_populate_processor_slots(entry)
            self._idchar_refresh_a0_icon_tool(entry)
            self._idchar_clear_processor_inspector()
            self.idchar_status_text.set(
                f"Updated entry {self._idchar_selected_index}"
            )
        except ValueError as exc:
            messagebox.showerror(
                "Invalid Values",
                f"Values must be comma-separated integers.\n{exc}",
            )

    def _idchar_add_empty(self) -> None:
        if not self._arr_data:
            self.idchar_status_text.set("Load a file first.")
            return
        self._arr_data.h_entries.append(HEntry(a0=[0], a1=[0], a2=[0]))
        new_idx = len(self._arr_data.h_entries) - 1
        self._idchar_populate_list()
        self._idchar_clear_processor_inspector()
        self.idchar_status_text.set(f"Added empty entry at index {new_idx}")

    def _idchar_clone(self) -> None:
        if self._idchar_selected_index is None or not self._arr_data:
            self.idchar_status_text.set("Select an entry to clone.")
            return
        src = self._arr_data.h_entries[self._idchar_selected_index]
        clone = HEntry(a0=list(src.a0), a1=list(src.a1), a2=list(src.a2))
        self._arr_data.h_entries.append(clone)
        new_idx = len(self._arr_data.h_entries) - 1
        self._idchar_populate_list()
        self._idchar_clear_processor_inspector()
        self.idchar_status_text.set(
            f"Cloned entry {self._idchar_selected_index} → {new_idx}"
        )

    def _idchar_set_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        if text:
            widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _idchar_clear_entry_details(self) -> None:
        self.idchar_index_label.configure(text="Select an entry", fg=MUTED)
        for widget in (self.idchar_a0_text, self.idchar_a1_text, self.idchar_a2_text):
            widget.delete("1.0", tk.END)
        self._idchar_processor_slot_refs = []
        self.idchar_processor_slot_listbox.delete(0, tk.END)
        self.idchar_icon_search_var.set("")
        self._idchar_set_text(self.idchar_icon_search_results_text, "")
        self._idchar_clear_a0_icon_tool()
        self._idchar_clear_processor_inspector()

    def _idchar_populate_processor_slots(self, entry: HEntry) -> None:
        self.idchar_processor_slot_listbox.delete(0, tk.END)
        self._idchar_processor_slot_refs = []
        for table_name, values in (("a0", entry.a0), ("a1", entry.a1), ("a2", entry.a2)):
            for slot_index, processor_index in enumerate(values):
                self._idchar_processor_slot_refs.append((table_name, slot_index, processor_index))
                self.idchar_processor_slot_listbox.insert(
                    tk.END,
                    f"{table_name}[{slot_index:02d}] -> {processor_index}",
                )

    def _idchar_clear_processor_inspector(self) -> None:
        self._idchar_active_processor_index = None
        self._idchar_visible_character_data_ids = []
        self.idchar_processor_var.set("")
        self.idchar_processor_summary_label.configure(
            text="Select a processor slot or enter a processor index.",
            fg=MUTED,
        )
        self.idchar_character_data_listbox.delete(0, tk.END)
        self._idchar_set_text(self.idchar_processor_ids_text, "")
        self._idchar_set_text(self.idchar_character_data_detail_text, "")

    def _idchar_on_processor_slot_select(self, _event=None) -> None:
        if not self._arr_data:
            return
        selection = self.idchar_processor_slot_listbox.curselection()
        if not selection:
            return
        list_index = selection[0]
        if list_index >= len(self._idchar_processor_slot_refs):
            return
        table_name, slot_index, processor_index = self._idchar_processor_slot_refs[list_index]
        self._idchar_show_processor(processor_index, source=f"{table_name}[{slot_index}]")

    def _idchar_open_processor(self) -> None:
        if not self._arr_data:
            self.idchar_status_text.set("Load a file first.")
            return
        raw_value = self.idchar_processor_var.get().strip()
        if not raw_value:
            self.idchar_status_text.set("Enter a processor index.")
            return
        try:
            processor_index = int(raw_value)
        except ValueError:
            self.idchar_status_text.set("Processor index must be an integer.")
            return
        self._idchar_show_processor(processor_index, source="manual")

    def _idchar_show_processor(self, processor_index: int, source: str = "manual") -> None:
        if not self._arr_data:
            return
        if processor_index < 0 or processor_index >= len(self._arr_data.character_data_processors):
            self._idchar_clear_processor_inspector()
            self.idchar_processor_summary_label.configure(
                text=f"Processor {processor_index} is out of range.",
                fg="#fca5a5",
            )
            return

        self._idchar_active_processor_index = processor_index
        self.idchar_processor_var.set(str(processor_index))
        processor = self._arr_data.character_data_processors[processor_index]
        self._idchar_visible_character_data_ids = list(processor.character_data_ids)
        self.idchar_processor_summary_label.configure(
            text=(
                f"Processor {processor_index} from {source}"
                f"  |  characterDataIds: {len(processor.character_data_ids)}"
            ),
            fg=TEXT,
        )
        self._idchar_set_text(
            self.idchar_processor_ids_text,
            ", ".join(str(value) for value in processor.character_data_ids),
        )

        self.idchar_character_data_listbox.delete(0, tk.END)
        for char_data_id in processor.character_data_ids:
            if 0 <= char_data_id < len(self._arr_data.character_data):
                char_data = self._arr_data.character_data[char_data_id]
                icon_ids = [frame.icon_id for frame in char_data.animation_frames if frame.icon_id != 0]
                preview = ",".join(str(icon_id) for icon_id in icon_ids[:6])
                if len(icon_ids) > 6:
                    preview += ",..."
                self.idchar_character_data_listbox.insert(
                    tk.END,
                    f"{char_data_id:4d}  part={char_data.part_type}  icons={preview}",
                )
            else:
                self.idchar_character_data_listbox.insert(
                    tk.END,
                    f"{char_data_id:4d}  <out of range>",
                )

        if processor.character_data_ids:
            self.idchar_character_data_listbox.selection_clear(0, tk.END)
            self.idchar_character_data_listbox.selection_set(0)
            self.idchar_character_data_listbox.activate(0)
            self._idchar_show_character_data(processor.character_data_ids[0])
        else:
            self._idchar_set_text(self.idchar_character_data_detail_text, "Processor has no characterDataIds.")

    def _idchar_on_character_data_select(self, _event=None) -> None:
        selection = self.idchar_character_data_listbox.curselection()
        if not selection:
            return
        list_index = selection[0]
        if list_index >= len(self._idchar_visible_character_data_ids):
            return
        self._idchar_show_character_data(self._idchar_visible_character_data_ids[list_index])

    def _idchar_show_character_data(self, char_data_id: int) -> None:
        if not self._arr_data:
            return
        if char_data_id < 0 or char_data_id >= len(self._arr_data.character_data):
            self._idchar_set_text(
                self.idchar_character_data_detail_text,
                f"CharacterData {char_data_id} is out of range.",
            )
            return

        char_data = self._arr_data.character_data[char_data_id]
        lines = [
            f"CharacterData {char_data_id}",
            f"partType={char_data.part_type}",
            "",
        ]
        non_zero_frames = 0
        for frame_index, frame in enumerate(char_data.animation_frames):
            if frame.icon_id == 0:
                continue
            non_zero_frames += 1
            lines.append(
                f"frame[{frame_index}] iconId={frame.icon_id} "
                f"rot={frame.rotation_frame} hueFlag={frame.hue_flag} "
                f"offset=({frame.offset_x},{frame.offset_y})"
            )
        if non_zero_frames == 0:
            lines.append("No non-zero animation frames.")
        self._idchar_set_text(self.idchar_character_data_detail_text, "\n".join(lines))

    def _idchar_find_icon(self) -> None:
        if not self._arr_data:
            self.idchar_status_text.set("Load a file first.")
            return
        raw_value = self.idchar_icon_search_var.get().strip()
        if not raw_value:
            self.idchar_status_text.set("Enter an iconId to search.")
            return
        try:
            icon_id = int(raw_value)
        except ValueError:
            self.idchar_status_text.set("iconId must be an integer.")
            return

        lines: list[str] = []
        for char_data_id, char_data in enumerate(self._arr_data.character_data):
            frame_indexes = [
                frame_index
                for frame_index, frame in enumerate(char_data.animation_frames)
                if frame.icon_id == icon_id
            ]
            if not frame_indexes:
                continue
            processor_indexes = [
                processor_index
                for processor_index, processor in enumerate(self._arr_data.character_data_processors)
                if char_data_id in processor.character_data_ids
            ]
            processor_preview = ", ".join(str(value) for value in processor_indexes[:12])
            if len(processor_indexes) > 12:
                processor_preview += ", ..."
            lines.append(
                f"charData {char_data_id} part={char_data.part_type} "
                f"frames={frame_indexes} processors=[{processor_preview}]"
            )

        if lines:
            self._idchar_set_text(self.idchar_icon_search_results_text, "\n".join(lines))
            self.idchar_status_text.set(f"Found iconId {icon_id} in {len(lines)} characterData entries.")
        else:
            self._idchar_set_text(self.idchar_icon_search_results_text, f"iconId {icon_id} was not found.")
            self.idchar_status_text.set(f"iconId {icon_id} was not found.")

    def _idchar_save(self) -> None:
        if not self._arr_data:
            self.idchar_status_text.set("No data to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save arr_data_game",
            initialfile=os.path.basename(self._arr_data.original_path),
            initialdir=os.path.dirname(self._arr_data.original_path),
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            size = save_arr_data_game(
                self._arr_data, path,
                backup=self.idchar_backup_var.get(),
            )
            self.idchar_status_text.set(
                f"Saved {size:,} bytes → {os.path.basename(path)}"
            )
        except Exception as exc:
            messagebox.showerror("Save Error", str(exc))


def main() -> None:
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = WeaponSpriteAdapterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
