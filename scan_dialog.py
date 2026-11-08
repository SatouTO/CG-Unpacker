"""文件夹扫描结果：先选资源包，再进入原有的预览或批量导出流程。"""
import os
import tkinter as tk
from tkinter import ttk


class ScanDialog(tk.Toplevel):
    def __init__(self, app, archives, root):
        super().__init__(app)
        self.app = app
        self.archives = archives
        self.title("扫描结果")
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.close)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"识别到 {len(archives)} 个资源包",
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text="双击资源包可预览内容。按住 Ctrl 或 Shift 可多选。").pack(anchor="w", pady=(3, 8))

        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, show="headings", selectmode="extended", height=12,
                                 columns=("path", "format", "count"))
        for key, title, width in (("path", "资源包路径", 370), ("format", "格式", 75), ("count", "条目数", 75)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=int(width * app.sf), stretch=(key == "path"),
                             anchor="w" if key == "path" else "center")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        for i, archive in enumerate(archives):
            path = os.path.relpath(archive.path, root) if root else archive.path
            self.tree.insert("", "end", iid=str(i), values=(path, archive.format, archive.entry_count))

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(10, 0))
        self.open_button = ttk.Button(actions, text="打开预览", command=self.open_selected)
        self.open_button.pack(side="left")
        self.extract_button = ttk.Button(actions, text="解包选中", command=self.extract_selected)
        self.extract_button.pack(side="left", padx=6)
        ttk.Button(actions, text="解包全部", command=self.extract_all).pack(side="left")
        ttk.Button(actions, text="关闭", command=self.close).pack(side="right")

        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Return>", lambda _event: self.open_selected())
        self.bind("<Escape>", lambda _event: self.close())
        self.tree.selection_set("0")
        self.tree.focus("0")
        self.tree.focus_set()
        self.grab_set()

    def _selection_changed(self, _event):
        count = len(self.tree.selection())
        self.open_button.state(["!disabled"] if count == 1 else ["disabled"])
        self.extract_button.state(["!disabled"] if count else ["disabled"])
        self.extract_button.configure(text=f"解包选中 ({count})" if count else "解包选中")

    def _double_click(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.open_selected()
        return "break"

    def open_selected(self):
        selected = self.tree.selection()
        if len(selected) == 1 and self.app.open_file(self.archives[int(selected[0])].path, parent=self):
            self.close()

    def extract_selected(self):
        self._extract([self.archives[int(i)].path for i in self.tree.selection()])

    def extract_all(self):
        self._extract([archive.path for archive in self.archives])

    def _extract(self, paths):
        if paths:
            target = self.app._ask_target(batch=True, parent=self)
            if target:
                self.close()
                self.app._start_batch(paths, target, all_scan_paths=[a.path for a in self.archives])

    def close(self):
        self.app._scan_dialog = None
        self.destroy()
