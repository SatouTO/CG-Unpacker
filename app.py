"""CG Unpacker 图形界面

重点是：高清（DPI 感知）、可以一张一张挑着解、拖进来只打开不自动解。
"""
import ctypes
import json
import os
import queue
import sys
import tempfile
import threading
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from PIL import Image, ImageTk

import unpacker  # noqa: F401  (导入即注册所有格式)
from unpacker import ExtractContext, ExtractOptions, extract
from unpacker.discovery import scan_archives
from unpacker.extract import read_entry
from unpacker.registry import archive_formats, open_archive_ex
from scan_dialog import ScanDialog

try:                                    # 拖拽支持，缺这个包也不影响其他功能
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _TK_BASE = TkinterDnD.Tk
    HAS_DND = True
except Exception:                       # noqa: BLE001
    _TK_BASE = tk.Tk
    HAS_DND = False

APP_NAME = "CG Unpacker"
APP_VER = "1.5"

# ---------------------------------------------------------------- 配色
BG = "#f2f4f8"
CARD = "#ffffff"
BORDER = "#e2e6ee"
TEXT = "#1b2130"
MUTED = "#7c8698"
ACCENT = "#3d6df2"
ACCENT_DARK = "#2b52c4"
ACCENT_SOFT = "#e8eeff"
ROW_ALT = "#fafbfd"


def enable_dpi_awareness():
    """不开这个，Windows 会把整个窗口当位图拉伸，怎么调都模糊"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # 每显示器 v2
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


enable_dpi_awareness()


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def _hex_head(path: str, n: int = 32) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(n)
    except Exception as e:
        return f"(读取失败: {e})"
    return " ".join(f"{b:02X}" for b in data)


def rounded_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """画个圆角矩形（canvas 没有原生圆角）"""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """扁平圆角按钮，比 ttk 默认按钮顺眼一点"""

    def __init__(self, master, text, command=None, kind="secondary",
                 padx=18, height=None, font=None, bg=None):
        self.sf = getattr(master, "sf", 1.0)
        self.font = font or ("Microsoft YaHei UI", 10)
        h = height or int(34 * self.sf)
        self.padx = int(padx * self.sf)
        self.kind = kind
        self.command = command
        self._text = text
        self._enabled = True
        try:
            self._bg = bg or master.cget("bg")
        except Exception:
            self._bg = bg or BG

        colors = {
            "primary": (ACCENT, "white", ACCENT_DARK, ACCENT_DARK),
            "secondary": (CARD, TEXT, "#eef2f9", BORDER),
            "ghost": (self._bg, MUTED, "#e7ebf3", ""),
        }
        self.fill, self.fg, self.hover_fill, self.outline = colors[kind]

        f = tkfont.Font(font=self.font)
        w = f.measure(text) + self.padx * 2
        super().__init__(master, width=w, height=h, bg=self._bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bw, self._bh = w, h
        self._draw()

    def _draw(self, fill=None):
        self.delete("all")
        fill = fill or self.fill
        rounded_rect(self, 1, 1, self._bw - 1, self._bh - 1, (self._bh - 2) / 2,
                     fill=fill, outline=self.outline or fill,
                     width=1 if self.outline else 0)
        self.create_text(self._bw / 2, self._bh / 2, text=self._text,
                         fill=self.fg if self._enabled else "#b9c0cd",
                         font=self.font)
        self.bind("<Enter>", lambda e: self._enabled and self._draw(self.hover_fill))
        self.bind("<Leave>", lambda e: self._draw())
        self.bind("<Button-1>", self._click)

    def _click(self, _e):
        if self._enabled and self.command:
            self.command()

    def set_text(self, text: str):
        if text == self._text:
            return
        self._text = text
        f = tkfont.Font(font=self.font)
        self._bw = f.measure(text) + self.padx * 2
        self.configure(width=self._bw)
        self._draw()

    def set_enabled(self, ok: bool):
        self._enabled = ok
        self.configure(cursor="hand2" if ok else "arrow")
        self._draw()


class App(_TK_BASE):
    _update_cancel = None
    _update_dialog = None
    _scan_dialog = None

    def __init__(self):
        super().__init__()
        self.sf = self._dpi_scale()
        self.title(f"{APP_NAME} v{APP_VER}")
        self.geometry(f"{int(1200 * self.sf)}x{int(800 * self.sf)}")
        self.minsize(int(1000 * self.sf), int(640 * self.sf))
        self.configure(bg=BG)

        self.archive = None
        self.ctx = None
        self.entries = []
        self.rows = []
        self._photo = None
        self._preview_token = 0
        self._preview_archive = None  # 正在被预览线程读取的包；回到 UI 线程后再关闭
        self._pending_preview = None
        self._closing = False
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self._scan_results = []
        self._scan_root = ""
        self.config_data = self._load_config()
        self.out_dir = os.path.abspath(self.config_data.get("out_dir") or os.path.join(app_dir(), "output"))
        self._sash_done = False

        if os.environ.get("GU_DEBUG"):
            try:
                info = (f"sf={self.sf} scaling={self.tk.call('tk', 'scaling')} "
                        f"fpixels={self.winfo_fpixels('1i')} geom={self.geometry()}")
                with open(os.path.join(app_dir(), "_dpi_debug.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(info)
            except Exception:
                pass

        self._build_style()
        self._build_menu()
        self._build_ui()
        self.after(80, self._poll)
        self.after_idle(self.lift)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _dpi_scale(self) -> float:
        """tk scaling = 每磅多少像素，96dpi 下是 1.3333"""
        try:
            sc = float(self.tk.call("tk", "scaling"))
            if sc > 0:
                return max(1.0, sc * 72.0 / 96.0)
        except Exception:
            pass
        try:
            return max(1.0, self.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0

    # ------------------------------------------------------------ 样式
    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        f = ("Microsoft YaHei UI", 10)
        style.configure(".", font=f, background=BG, foreground=TEXT)
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, borderwidth=0, relief="flat",
                        rowheight=int(30 * self.sf), font=f)
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview.Heading", background=CARD, foreground=MUTED,
                        relief="flat", borderwidth=0,
                        font=("Microsoft YaHei UI", 9, "bold"),
                        padding=(8, int(6 * self.sf)))
        style.map("Treeview.Heading", background=[("active", "#f7f9fc")])
        style.map("Treeview",
                  background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", TEXT)])
        style.configure("Vertical.TScrollbar", background="#cfd5e0", troughcolor=CARD,
                        bordercolor=CARD, arrowcolor=MUTED, relief="flat", width=int(10 * self.sf))
        style.configure("Horizontal.TProgressbar", background=ACCENT,
                        troughcolor=BG, bordercolor=BG,
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=int(6 * self.sf))
        style.configure("TCheckbutton", background=BG, foreground=TEXT, font=f)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Path.TCheckbutton", background=CARD)
        style.map("Path.TCheckbutton", background=[("active", CARD)])
        style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=(6, 4))

    def _build_menu(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="打开文件", accelerator="Ctrl+O", command=self.choose_file)
        fm.add_command(label="扫描文件夹…", accelerator="Ctrl+B", command=self.choose_folder)
        fm.add_separator()
        fm.add_command(label="打开默认目录", command=self.open_out_dir)
        fm.add_separator()
        fm.add_command(label="退出", command=self._on_close)
        m.add_cascade(label="文件", menu=fm)

        tm = tk.Menu(m, tearoff=0)
        tm.add_command(label="支持的格式…", command=self.show_formats)
        m.add_cascade(label="工具", menu=tm)

        hm = tk.Menu(m, tearoff=0)
        hm.add_command(label="检查更新", command=self.check_for_updates)
        hm.add_command(label="GitHub 发布页", command=self._open_releases)
        hm.add_separator()
        hm.add_command(label="关于", command=self.show_about)
        m.add_cascade(label="帮助", menu=hm)
        self.config(menu=m)
        self.bind_all("<Control-o>", lambda e: self.choose_file())
        self.bind_all("<Control-b>", lambda e: self.choose_folder())

    # ------------------------------------------------------------ 界面
    def _build_ui(self):
        sf = self.sf

        # 顶部标题
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=int(18 * sf), pady=(int(12 * sf), 0))
        tk.Label(head, text=APP_NAME, bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        tk.Label(head, text=f"v{APP_VER}", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0), pady=(3 * sf, 0))
        self.btn_update = RoundButton(head, "检查更新", self.check_for_updates, "secondary")
        self.btn_update.pack(side="left", padx=int(10 * sf))
        tk.Label(head, text="拖入资源包可预览，拖入游戏文件夹可扫描", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="right")

        # 工具条卡片
        bar = tk.Frame(self, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        bar.pack(fill="x", padx=int(18 * sf), pady=(int(10 * sf), int(8 * sf)))
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=int(12 * sf), pady=int(10 * sf))

        self.btn_open = RoundButton(inner, "打开文件", self.choose_file, "primary")
        self.btn_open.pack(side="left")
        self.btn_batch = RoundButton(inner, "扫描文件夹", self.choose_folder, "secondary")
        self.btn_batch.pack(side="left", padx=(int(8 * sf), 0))
        self.btn_results = RoundButton(inner, "扫描结果", self._show_scan_results, "secondary")
        self.btn_results.pack(side="left", padx=(int(8 * sf), 0))
        self.btn_results.set_enabled(False)

        tk.Frame(inner, bg=BORDER, width=1, height=int(24 * sf)).pack(
            side="left", padx=int(14 * sf), pady=int(4 * sf))

        self.btn_sel = RoundButton(inner, "解包选中", self.extract_selected, "secondary")
        self.btn_sel.pack(side="left")
        self.btn_all = RoundButton(inner, "全部解包", self.extract_all, "secondary")
        self.btn_all.pack(side="left", padx=(int(8 * sf), 0))
        self.btn_cancel = RoundButton(inner, "取消", self.cancel, "ghost")
        self.btn_cancel.pack(side="left", padx=(int(8 * sf), 0))
        self.btn_cancel.set_enabled(False)

        # 路径单独占一行，窄窗口里也能选中、滚动和复制完整目录。
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x")
        dest = tk.Frame(bar, bg=CARD)
        dest.pack(fill="x", padx=int(12 * sf), pady=int(8 * sf))
        self.path_label = tk.Label(dest, bg=CARD, fg=TEXT, font=("Microsoft YaHei UI", 10, "bold"))
        self.path_label.pack(side="left", padx=(0, int(8 * sf)))
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(dest, textvariable=self.path_var, state="readonly",
                                    width=1, font=("Microsoft YaHei UI", 10), foreground=TEXT)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.btn_dir = RoundButton(dest, "更改目录…", self.choose_out_dir, "secondary")
        self.btn_dir.pack(side="left", padx=int(8 * sf))
        self.var_ask = tk.BooleanVar(value=self.config_data.get("ask_dir", True))
        self.ask_check = ttk.Checkbutton(dest, text="解包时问保存位置", variable=self.var_ask,
                                         style="Path.TCheckbutton", command=self._refresh_save_path)
        self.ask_check.pack(side="left")
        self._refresh_save_path()

        # 过滤行
        flt = tk.Frame(self, bg=BG)
        flt.pack(fill="x", padx=int(18 * sf), pady=(0, int(6 * sf)))
        tk.Label(flt, text="搜索", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.var_filter = tk.StringVar()
        self.var_filter.trace_add("write", lambda *a: self.refresh_tree())
        self.ent_filter = ttk.Entry(flt, textvariable=self.var_filter, width=22)
        self.ent_filter.pack(side="left", padx=(6 * sf, int(14 * sf)))

        self.var_only_img = tk.BooleanVar(value=self.config_data.get("only_images", True))
        ttk.Checkbutton(flt, text="只要图片", variable=self.var_only_img).pack(side="left")
        self.var_png = tk.BooleanVar(value=self.config_data.get("convert_png", True))
        ttk.Checkbutton(flt, text="转成 PNG", variable=self.var_png).pack(side="left", padx=(10 * sf, 0))

        self.lbl_status = tk.Label(flt, text="尚未打开文件", bg=BG, fg=MUTED,
                                   font=("Microsoft YaHei UI", 9))
        self.lbl_status.pack(side="right")

        # 底部（先 pack，保证有位置）
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=int(18 * sf), pady=(0, int(10 * sf)))
        self.progress = ttk.Progressbar(bottom, style="Horizontal.TProgressbar", mode="determinate")
        self.progress.pack(fill="x")
        logbox = tk.Frame(bottom, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        logbox.pack(fill="x", pady=(int(8 * sf), 0))
        self.log = tk.Text(logbox, height=4, font=("Consolas", 9), wrap="none",
                           bg=CARD, fg=MUTED, relief="flat", bd=0,
                           padx=int(8 * sf), pady=int(4 * sf))
        self.log.pack(fill="x")
        self.log.configure(state="disabled")

        # 主区域
        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=int(18 * sf), pady=(0, int(8 * sf)))
        self.pane = pane

        left = tk.Frame(pane, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        pane.add(left, weight=3)
        cols = ("size", "type")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="名称", command=lambda: self._sort_by("name"))
        self.tree.heading("size", text="大小", command=lambda: self._sort_by("size"))
        self.tree.heading("type", text="类型", command=lambda: self._sort_by("type"))
        self.tree.column("#0", width=int(380 * sf), anchor="w")
        self.tree.column("size", width=int(86 * sf), anchor="e", stretch=False)
        self.tree.column("type", width=int(58 * sf), anchor="center", stretch=False)
        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        vs.pack(side="right", fill="y", pady=1)
        self.tree.tag_configure("odd", background=ROW_ALT)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._context_menu)

        right = tk.Frame(pane, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        pane.add(right, weight=2)
        self.canvas = tk.Canvas(right, bg="#151a21", highlightthickness=0,
                                width=int(360 * sf))
        self.canvas.pack(fill="both", expand=True, padx=1, pady=(1, 0))
        self.canvas.bind("<Configure>", lambda e: self._redraw_preview())
        self.lbl_info = tk.Label(right, text="在左边选一张看看", bg=CARD, fg=MUTED,
                                 font=("Microsoft YaHei UI", 9), anchor="center",
                                 pady=int(7 * sf))
        self.lbl_info.pack(fill="x")
        self._preview_img = None

        pane.bind("<Configure>", self._on_pane_configure)
        self._init_dnd()

    def _on_pane_configure(self, event):
        if self._sash_done or event.width < 400:
            return
        self._sash_done = True
        try:
            self.pane.sashpos(0, int(event.width * 0.62))
        except tk.TclError:
            pass

    # ------------------------------------------------------------ 拖拽
    def _init_dnd(self):
        if not HAS_DND:
            self.log_line("（没装 tkinterdnd2，拖拽不可用，用「打开文件」也一样）")
            return
        ok = 0
        for w in (self, self.tree, self.canvas, self.log):
            try:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)
                ok += 1
            except Exception:
                pass
        if ok:
            self.log_line("可拖入游戏文件夹扫描资源包，或拖入单个包预览。")
        else:
            self.log_line("拖拽注册失败，用「打开文件」也一样能用")

    def on_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except tk.TclError:
            paths = [event.data.strip().strip("{}")]
        if paths:
            self.after(10, lambda: self._handle_dropped(paths))
            return "copy"
        return "refuse_drop"

    def _handle_dropped(self, paths):
        if self._closing or self._update_dialog is not None or self._scan_dialog is not None:
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "请等当前任务完成，或先点「取消」。")
            return
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        self.log_line("拖进来：" + "、".join(os.path.basename(p) for p in paths))
        if any(os.path.isdir(p) for p in paths):
            self._start_scan(paths)
        elif len(paths) == 1:
            self.open_file(paths[0])          # 只打开，不自动解
        else:
            base = self._ask_target(batch=True)
            if not base:
                return
            self._start_batch(paths, base)

    # ------------------------------------------------------------ 配置
    def _load_config(self) -> dict:
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        data = {
            "out_dir": self.out_dir,
            "only_images": self.var_only_img.get(),
            "convert_png": self.var_png.get(),
            "ask_dir": self.var_ask.get(),
        }
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------ 日志 / 状态
    def log_line(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, text: str):
        self.lbl_status.configure(text=text)

    # ------------------------------------------------------------ 打开文件
    def choose_file(self):
        if self.busy or self._closing or self._update_dialog is not None or self._scan_dialog is not None:
            return
        path = filedialog.askopenfilename(
            title="打开文件",
            filetypes=[("资源包", "*.afa *.ald *.xp3 *.zip *.ypf *.npa *.pfs *.pf6 *.pf7 *.pf8 *.g00 *.pak *.dat *.arc *.bin *.exe"),
                       ("所有文件", "*.*")])
        if path:
            self.open_file(path)

    def open_file(self, path: str, *, parent=None) -> bool:
        if self._closing:
            return False
        if self.busy:
            messagebox.showinfo(APP_NAME, "正在解包，等这一轮跑完再换包吧")
            return False
        try:
            archive, err = open_archive_ex(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"打开失败：{e}", parent=parent or self)
            return False
        if archive is None:
            head = _hex_head(path)
            messagebox.showerror(
                APP_NAME,
                f"识别不了这个格式\n\n{err}\n\n文件头（前 32 字节）：\n{head}", parent=parent or self)
            return False
        if self.archive and self.archive is not self._preview_archive:
            self.archive.close()
        self.archive = archive
        self._refresh_save_path()
        self.ctx = ExtractContext(archive)
        self.entries = archive.entries
        self.set_status(f"{os.path.basename(path)} · {archive.format} · {len(self.entries)} 个文件")
        self.log_line(f"打开 {os.path.basename(path)}  [{archive.format}]  {len(self.entries)} 个文件")
        self.refresh_tree()
        self._update_sel_button()
        first = self.tree.get_children()
        if first:
            self.tree.selection_set(first[0])
            self.tree.focus(first[0])
        return True

    def refresh_tree(self):
        self._clear_preview()
        self.tree.delete(*self.tree.get_children())
        self.rows = []
        kw = self.var_filter.get().strip().lower()
        for i, e in enumerate(self.entries):
            if kw and kw not in e.name.lower():
                continue
            self.rows.append(e)
            self.tree.insert("", "end",
                             text=e.name.replace("\uff0f", "/"),
                             values=(human_size(e.display_size), e.ext or "-"),
                             tags=("odd",) if len(self.rows) % 2 == 0 else ())
        self._update_sel_button()

    def _sort_by(self, key: str):
        if key == "name":
            self.entries.sort(key=lambda e: e.name.lower())
        elif key == "size":
            self.entries.sort(key=lambda e: e.display_size, reverse=True)
        else:
            self.entries.sort(key=lambda e: e.ext)
        self.refresh_tree()

    # ------------------------------------------------------------ 选择 / 预览
    def _selected_entries(self):
        out = []
        for item in self.tree.selection():
            idx = self.tree.index(item)
            if idx < len(self.rows):
                out.append(self.rows[idx])
        return out

    def _update_sel_button(self):
        n = len(self.tree.selection()) if self.archive else 0
        self.btn_sel.set_text(f"解包选中 ({n})" if n else "解包选中")
        self.btn_sel.set_enabled(bool(self.archive))

    def on_select(self, _event=None):
        if self._closing:
            return
        self._update_sel_button()
        sel = self.tree.selection()
        self._clear_preview()
        if not sel or not self.archive:
            return
        item = sel[0]
        idx = self.tree.index(item)
        if idx >= len(self.rows):
            return
        entry = self.rows[idx]
        self._pending_preview = entry
        self.lbl_info.configure(text=f"解码中 {entry.name} …")
        self._start_pending_preview()

    def _clear_preview(self):
        self._preview_token += 1
        self._pending_preview = None
        self._preview_img = self._photo = None
        self.canvas.delete("all")
        self.lbl_info.configure(text="在左边选一张看看")

    def _start_pending_preview(self):
        if self._preview_archive is not None or self._pending_preview is None:
            return
        archive, ctx = self.archive, self.ctx
        token, entry = self._preview_token, self._pending_preview
        self._preview_archive = archive
        self._pending_preview = None

        def work():
            try:
                _, img, desc = read_entry(archive, entry, ctx)
            except Exception as e:
                result = ("preview", token, entry, None, "", f"{type(e).__name__}: {e}")
            else:
                result = ("preview", token, entry, img, desc, None)
            self.queue.put(result)

        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, entry, img, desc):
        if img is None:
            self._preview_img = None
            self.canvas.delete("all")
            w = max(self.canvas.winfo_width(), 50)
            h = max(self.canvas.winfo_height(), 50)
            self.canvas.create_text(w // 2, h // 2, text="不是图片，或者解码器还不支持",
                                    fill="#6d7789", font=("Microsoft YaHei UI", 11))
            self.lbl_info.configure(text=f"{entry.name}   {human_size(entry.display_size)}")
            return
        self._preview_img = img
        self._redraw_preview()
        self.lbl_info.configure(
            text=f"{img.width}×{img.height}   {img.mode}   {desc}   {human_size(entry.display_size)}")

    def _redraw_preview(self):
        img = self._preview_img
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), 50)
        h = max(self.canvas.winfo_height(), 50)
        if img is None:
            self.canvas.create_text(
                w // 2, h // 2,
                text="拖入资源包预览图片\n或拖入游戏文件夹扫描",
                fill="#5c6678", justify="center",
                font=("Microsoft YaHei UI", int(11 * self.sf)))
            return
        scale = min(w / img.width, h / img.height, 1.0)
        nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        thumb = img.resize((nw, nh), Image.LANCZOS) if scale < 1 else img
        if thumb.mode == "RGBA":
            bg = Image.new("RGB", thumb.size, (21, 26, 33))
            bg.paste(thumb, mask=thumb.split()[3])
            thumb = bg
        self._photo = ImageTk.PhotoImage(thumb)
        self.canvas.create_image(w // 2, h // 2, image=self._photo)

    # ------------------------------------------------------------ 解包
    def _suggested_target(self, batch=False) -> str:
        if self.archive and not batch and not self.var_ask.get():
            name = os.path.splitext(os.path.basename(self.archive.path))[0]
            return os.path.join(self.out_dir, name)
        return self.out_dir

    def _display_save_path(self, label, path):
        self.path_label.configure(text=label)
        self.path_var.set(path)
        self.path_entry.xview_moveto(0)

    def _refresh_save_path(self):
        label = "默认保存路径" if self.var_ask.get() else "保存路径"
        self._display_save_path(label, self._suggested_target())

    def _ask_target(self, *, batch=False, parent=None) -> str | None:
        if self._closing or self._update_dialog is not None:
            return None
        if not self.var_ask.get():
            return self._suggested_target(batch)
        init = self.out_dir if os.path.isdir(self.out_dir) else app_dir()
        d = filedialog.askdirectory(title="解出来的图片保存到哪里", initialdir=init, parent=parent or self)
        if not d:
            return None
        self.out_dir = os.path.abspath(d)
        self._save_config()
        return self.out_dir

    def choose_out_dir(self):
        d = filedialog.askdirectory(title="选择默认输出目录", initialdir=self.out_dir or app_dir())
        if d:
            self.out_dir = os.path.abspath(d)
            self._refresh_save_path()
            self._save_config()
            self.log_line(f"默认目录改成：{d}")

    def open_out_dir(self):
        if os.path.isdir(self.out_dir):
            os.startfile(self.out_dir)  # noqa: S606
        else:
            messagebox.showinfo(APP_NAME, "还没有输出目录，先解一个包")

    def extract_all(self):
        if not self.archive:
            messagebox.showinfo(APP_NAME, "先打开一个资源包")
            return
        if self.busy:
            return
        target = self._ask_target()
        if not target:
            return
        self._start_extract(list(self.entries), "全部文件", target)

    def extract_selected(self):
        if not self.archive:
            return
        sel = self._selected_entries()
        if not sel:
            messagebox.showinfo(APP_NAME, "先在列表里选要解的图，按住 Ctrl / Shift 能多选")
            return
        if self.busy:
            return
        target = self._ask_target()
        if not target:
            return
        self._start_extract(sel, f"{len(sel)} 个文件", target)

    def _start_extract(self, entries, label, out_dir):
        if self.busy or self._closing or self._update_dialog is not None:
            return
        self._display_save_path("保存路径", out_dir)
        opts = ExtractOptions(
            out_dir=out_dir,
            base_dir=self.out_dir,
            only_images=self.var_only_img.get(),
            convert_png=self.var_png.get(),
            should_stop=self.stop_event.is_set,
        )
        self.stop_event.clear()
        self._set_busy(True)
        self.progress.configure(maximum=max(1, len(entries)), value=0)
        self.log_line(f"开始解包 {label} → {out_dir}")

        def work():
            try:
                stats = extract(self.archive, entries, opts,
                                progress=lambda d, t, n: self.queue.put(("progress", d, t, n)))
                self.queue.put(("done", stats, out_dir))
            except Exception:
                self.queue.put(("error", traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()

    def choose_folder(self):
        if self.busy or self._closing or self._update_dialog is not None or self._scan_dialog is not None:
            return
        folder = filedialog.askdirectory(title="选择要扫描的游戏文件夹", parent=self)
        if folder:
            self._start_scan([folder])

    def _start_scan(self, paths):
        roots = [os.path.abspath(p if os.path.isdir(p) else os.path.dirname(p)) for p in paths]
        try:
            self._scan_root = os.path.commonpath(roots)
        except ValueError:  # 同时拖入不同盘符下的文件夹时显示完整路径
            self._scan_root = ""
        self._scan_results = []
        self.btn_results.set_text("扫描结果")
        self.btn_results.set_enabled(False)
        self.stop_event.clear()
        self._set_busy(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.set_status("正在扫描文件夹…")
        self.log_line("扫描目录及其子文件夹中的资源包…")

        def work():
            try:
                result = scan_archives(paths, should_stop=self.stop_event.is_set,
                                       progress=lambda n, found, path: self.queue.put(
                                           ("scan_progress", n, found, path)))
            except Exception:
                self.queue.put(("scan_error", traceback.format_exc()))
            else:
                self.queue.put(("scan_done", result))

        threading.Thread(target=work, daemon=True).start()

    def _finish_scan(self, msg):
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._set_busy(False)
        if self._closing:
            return
        if msg[0] == "scan_error":
            self.set_status("扫描失败，详情见日志")
            self.log_line(msg[1])
            messagebox.showerror(APP_NAME, "扫描失败，详情见窗口下方日志。", parent=self)
            return
        result = msg[1]
        self._scan_results = result.archives
        count = len(result.archives)
        cancelled = result.cancelled or self.stop_event.is_set()
        status = "扫描已取消" if cancelled else "扫描完成"
        summary = f"{status} · 已检查 {result.scanned} 个文件，识别到 {count} 个资源包"
        if result.unreadable:
            summary += f" · {result.unreadable} 项无法读取"
        self.set_status(summary)
        self.log_line(summary)
        self.btn_results.set_text(f"扫描结果 ({count})")
        self.btn_results.set_enabled(bool(count))
        if not cancelled:
            if count:
                self._show_scan_results()
            else:
                messagebox.showinfo(APP_NAME, summary, parent=self)

    def _show_scan_results(self):
        if self.busy or self._closing or self._update_dialog is not None or not self._scan_results:
            return
        if self._scan_dialog is None:
            self._scan_dialog = ScanDialog(self, self._scan_results, self._scan_root)
        self._scan_dialog.lift()

    @staticmethod
    def _batch_output_names(files):
        from unpacker.extract import safe_parts

        stems = {path: safe_parts(os.path.splitext(os.path.basename(path))[0])[-1]
                 for path in files}
        reserved = {stem.casefold() for stem in stems.values()}
        next_suffix = {}
        names = {}
        # 按源路径分配编号，保留本身就叫 “data (2)” 的包名。
        for path in sorted(stems, key=lambda p: (os.path.abspath(p).casefold(), os.fspath(p))):
            stem = stems[path]
            key = stem.casefold()
            suffix = next_suffix.get(key, 1)
            name = stem
            if suffix > 1:
                name = f"{stem} ({suffix})"
                while name.casefold() in reserved:
                    suffix += 1
                    name = f"{stem} ({suffix})"
                reserved.add(name.casefold())
            next_suffix[key] = suffix + 1
            names[path] = name
        return names

    def _start_batch(self, files, base_dir, *, all_scan_paths=None):
        if self.busy or self._closing or self._update_dialog is not None:
            return
        names = self._batch_output_names(all_scan_paths if all_scan_paths is not None else files)
        self._display_save_path("批量保存路径", base_dir)
        self.stop_event.clear()
        self._set_busy(True)
        self.progress.configure(maximum=max(1, len(files)), value=0)
        self.log_line(f"批量解包 {len(files)} 个包 → {base_dir}")
        only_img = self.var_only_img.get()
        png = self.var_png.get()
        stop = self.stop_event.is_set

        def work():
            ok = failed = converted = raw = 0
            for i, f in enumerate(files, 1):
                if stop():
                    break
                self.queue.put(("progress", i - 1, len(files), os.path.basename(f)))
                try:
                    arc, err = open_archive_ex(f, should_stop=stop)
                    if arc is None:
                        if stop():
                            break
                        raise ValueError(err)
                    opts = ExtractOptions(
                        out_dir=os.path.join(base_dir, names[f]),
                        base_dir=base_dir,
                        only_images=only_img,
                        convert_png=png,
                        should_stop=stop,
                    )
                    with arc:
                        stats = extract(arc, arc.entries, opts)
                    converted += stats.converted
                    raw += stats.raw
                    processed = stats.converted + stats.raw + stats.skipped + stats.failed
                    if processed < len(arc.entries):
                        self.queue.put(("log", f"已中止 {f} → {names[f]}　{stats.summary()}"))
                        break
                    if stats.failed:
                        failed += 1
                        mark = "×"
                    else:
                        ok += 1
                        mark = "√"
                    self.queue.put(("log", f"{mark} {f} → {names[f]}　{stats.summary()}"))
                except Exception as e:
                    failed += 1
                    self.queue.put(("log", f"× {f}: {e}"))
            self.queue.put(("batch_done", ok, failed, len(files), converted, raw, base_dir))

        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        if self.busy:
            self.stop_event.set()
            self.log_line("正在取消，处理完当前文件后停止…")

    def _set_busy(self, busy: bool):
        self.busy = busy
        self.btn_cancel.set_enabled(busy)
        self.btn_dir.set_enabled(not busy)
        self.ask_check.state(["disabled"] if busy else ["!disabled"])

    def _on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self._extract_item(item)

    def _context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item not in self.tree.selection():
            self.tree.selection_set(item)
        n = len(self.tree.selection())
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="解包这一张", command=lambda: self._extract_item(item))
        m.add_command(label=f"解包选中的 {n} 张", command=self.extract_selected)
        m.add_separator()
        m.add_command(label="复制文件名", command=self._copy_name)
        m.tk_popup(event.x_root, event.y_root)

    def _extract_item(self, item):
        if self.busy or not self.archive or not self.tree.exists(item):
            return
        entry = self.rows[self.tree.index(item)]
        target = self._ask_target()
        if not target:
            return
        self._start_extract([entry], "这一张", target)

    def _copy_name(self):
        sel = self._selected_entries()
        if sel:
            self.clipboard_clear()
            self.clipboard_append("\n".join(e.name for e in sel))

    # ------------------------------------------------------------ 消息泵
    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind.startswith("update_"):
                    self._handle_update(msg)
                elif kind == "scan_progress":
                    _, scanned, found, path = msg
                    self.set_status(f"正在扫描 · 已检查 {scanned} 个文件，找到 {found} 个包  {os.path.basename(path)}")
                elif kind in ("scan_done", "scan_error"):
                    self._finish_scan(msg)
                elif kind == "progress":
                    _, done, total, name = msg
                    self.progress.configure(maximum=max(1, total), value=done)
                    self.set_status(f"处理中 {done}/{total}  {name}")
                elif kind == "preview":
                    _, token, entry, img, desc, error = msg
                    if self._preview_archive is not self.archive:
                        self._preview_archive.close()
                    self._preview_archive = None
                    if token == self._preview_token:
                        if error:
                            self.lbl_info.configure(text=f"预览失败：{error}")
                        else:
                            self._show_preview(entry, img, desc)
                    self._start_pending_preview()
                elif kind == "log":
                    self.log_line(msg[1])
                elif kind == "done":
                    _, stats, out_dir = msg
                    self.progress.configure(value=self.progress["maximum"])
                    self.log_line(f"完成：{stats.summary()}")
                    for err in stats.errors[:3]:
                        self.log_line(f"  ! {err}")
                    self.set_status(f"完成：{stats.summary()}")
                    self._set_busy(False)
                    if not self._closing and messagebox.askyesno(APP_NAME, f"解包完成\n\n{stats.summary()}\n\n保存路径\n{out_dir}\n\n打开目录看看？"):
                        os.startfile(out_dir)  # noqa: S606
                elif kind == "batch_done":
                    _, ok, failed, total, converted, raw, base_dir = msg
                    title = "批量解包已取消" if ok + failed < total else "批量解包结束"
                    summary = f"{total} 个包，成功 {ok} 个，失败 {failed} 个"
                    if ok + failed < total:
                        summary += f"，未完成 {total - ok - failed} 个"
                    exports = f"转换 {converted} 张图片，原样导出 {raw} 个文件"
                    self.progress.configure(value=ok + failed)
                    self.log_line(f"{title}：{summary}。{exports}")
                    self.set_status(f"{title}：{summary}")
                    self._set_busy(False)
                    if not self._closing:
                        messagebox.showinfo(APP_NAME, f"{title}\n{summary}\n{exports}\n\n批量保存路径\n{base_dir}")
                elif kind == "error":
                    self._set_busy(False)
                    self.log_line("出错了：\n" + msg[1])
                    if not self._closing:
                        messagebox.showerror(APP_NAME, "解包过程出错了，详情看下面日志")
        except queue.Empty:
            pass
        if (self._closing and not self.busy and self._preview_archive is None
                and self._update_cancel is None):
            if self.archive:
                self.archive.close()
            self.destroy()
            return
        self.after(80, self._poll)

    # ------------------------------------------------------------ 版本更新
    def _open_releases(self):
        from updater import RELEASES_URL
        history = RELEASES_URL.removesuffix("/latest")
        try:
            if sys.platform == "win32":
                os.startfile(history)
            else:
                import webbrowser
                webbrowser.open(history)
        except OSError:
            messagebox.showinfo("GitHub 发布页", history, parent=self)

    def check_for_updates(self):
        if self._closing or self._update_cancel is not None:
            return
        if self.busy:
            messagebox.showinfo("检查更新", "请等当前解包任务完成后再检查更新。", parent=self)
            return
        self._update_cancel = threading.Event()
        self.btn_update.set_text("正在检查…")
        self.btn_update.set_enabled(False)

        def work():
            try:
                from updater import check_update
                release = check_update(APP_VER)
                self.queue.put(("update_checked", release))
            except Exception as exc:
                self.queue.put(("update_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _finish_update(self):
        if self._update_dialog is not None:
            self._update_dialog.destroy()
            self._update_dialog = None
        self._update_cancel = None
        self.btn_update.set_text("检查更新")
        self.btn_update.set_enabled(True)

    def _report_update_error(self, error):
        self._finish_update()
        if messagebox.askyesno("更新失败", f"{error}\n\n打开 GitHub 发布页？", parent=self):
            self._open_releases()

    def _handle_update(self, msg):
        cancel = self._update_cancel
        if cancel is None:
            return
        kind, value = msg
        if kind in ("update_progress", "update_mib", "update_preparing"):
            if not cancel.is_set():
                if kind == "update_mib":
                    if str(self._update_progress.cget("mode")) != "indeterminate":
                        self._update_progress.configure(mode="indeterminate")
                        self._update_progress.start()
                    label = f"正在下载更新，已接收 {value:.1f} MiB"
                elif kind == "update_preparing":
                    self._update_progress.stop()
                    self._update_progress.configure(mode="determinate", value=100)
                    label = "正在准备安装…"
                else:
                    self._update_progress.configure(value=value)
                    label = f"正在下载更新  {value}%"
                self._update_label.configure(text=label)
            return
        if self._closing or cancel.is_set() or self.busy:
            if kind == "update_ready":
                from update_install import discard_update
                discard_update(value)
            if self.busy and not self._closing:
                self.log_line("请在当前解包完成后再次点击「检查更新」。")
            self._finish_update()
            return
        if kind == "update_failed":
            self._report_update_error(value)
            return
        if kind == "update_ready":
            from update_install import discard_update, launch_update
            try:
                launch_update(value)
            except Exception as exc:
                discard_update(value)
                self._report_update_error(exc)
            else:
                self._finish_update()
                self._on_close()
            return
        if value is None:
            self._finish_update()
            messagebox.showinfo("检查更新", f"当前版本 v{APP_VER}，暂无可安装的新版本。", parent=self)
            return
        release = value
        notes = (release.notes or "").strip()
        if len(notes) > 700:
            notes = notes[:700].rstrip() + "…"
        prompt = f"当前版本 v{APP_VER}　→　新版本 v{release.version}"
        if notes:
            prompt += f"\n\n{notes}"
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            self._finish_update()
            if messagebox.askyesno("发现新版本", prompt + "\n\n打开 GitHub 发布页下载？", parent=self):
                self._open_releases()
            return
        accepted = messagebox.askyesno("发现新版本", prompt + "\n\n立即下载并重启安装？", parent=self)
        if not accepted or self._closing or cancel.is_set():
            self._finish_update()
            return
        self._download_update(release)

    def _download_update(self, release):
        from updater import download_update
        from update_install import prepare_update
        cancel = self._update_cancel
        dialog = self._update_dialog = tk.Toplevel(self)
        dialog.title(f"更新到 v{release.version}")
        dialog.transient(self)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        self._update_label = ttk.Label(body, text="正在下载更新…")
        self._update_label.pack(anchor="w")
        self._update_progress = ttk.Progressbar(body, mode="determinate", maximum=100,
                                                length=int(265 * self.sf))
        self._update_progress.pack(fill="x", pady=10)

        def cancel_download():
            cancel.set()
            self._update_label.configure(text="正在取消…")
            button.state(["disabled"])
        button = ttk.Button(body, text="取消更新", command=cancel_download)
        button.pack(anchor="e")
        dialog.protocol("WM_DELETE_WINDOW", cancel_download)
        dialog.grab_set()

        def work():
            last_report = None

            def progress(done, total):
                nonlocal last_report
                if cancel.is_set():
                    raise InterruptedError("更新已取消")
                if total:
                    report = ("update_progress", min(100, 100 * done // total))
                else:
                    report = ("update_mib", round(done / (1 << 20), 1))
                if report != last_report:
                    last_report = report
                    self.queue.put(report)

            prepared = None
            try:
                with tempfile.TemporaryDirectory(prefix="CG-Unpacker-download-") as directory:
                    archive = Path(directory, "update.zip")
                    download_update(release, archive, progress)
                    if cancel.is_set():
                        raise InterruptedError("更新已取消")
                    self.queue.put(("update_preparing", None))
                    prepared = prepare_update(archive, app_dir(), os.path.basename(sys.executable))
                self.queue.put(("update_ready", prepared))
            except Exception as exc:
                if prepared is not None:
                    from update_install import discard_update
                    discard_update(prepared)
                self.queue.put(("update_failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 杂项
    def show_formats(self):
        lines = ["可打开的文件格式：", ""]
        for name, desc in archive_formats():
            lines.append(f"  {name:<6} {desc}")
        lines += ["", "支持的图片格式：", "",
                  "  QNT    AliceSoft 位图（含 alpha）",
                  "  DCF    AliceSoft 差分图（自动合成底图）",
                  "  AJP    AliceSoft JPEG（含 alpha）",
                  "  PNG/JPG/BMP/WebP/GIF  直接转换"]
        messagebox.showinfo("支持的格式", "\n".join(lines))

    def show_about(self):
        messagebox.showinfo("关于", f"{APP_NAME} v{APP_VER}\n\n"
                                    "游戏资源包图片提取工具\n"
                                    "插件式架构，格式可以一直加\n\n"
                                    "引擎：unpacker（Python + Pillow）")

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        if self._update_cancel is not None:
            self._update_cancel.set()
        self.stop_event.set()
        self._clear_preview()
        self._save_config()
        self.withdraw()  # 由消息泵等当前文件写完、预览读完，再关闭归档和 Tk。


def main():
    app = App()
    if sys.argv[1:]:
        app.after(120, lambda: app._handle_dropped(sys.argv[1:]))

    shot = os.environ.get("GU_SELFTEST")
    if shot:
        def _capture():
            try:
                from PIL import ImageGrab
                app.lift()
                app.attributes("-topmost", True)
                app.update()
                ImageGrab.grab().save(shot)
            finally:
                app.destroy()
        app.after(4000, _capture)

    app.mainloop()


if __name__ == "__main__":
    main()
