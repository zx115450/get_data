"""ACM 出数据 —— 桌面 GUI（tkinter）。

三块输入：标程(std) / 题面 / 输入描述 / 输出描述；题面、范围与输出可「美化」。
第四 Tab：数据方案（range）可视化。

用法：
    python gui.py
"""
import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import scrolledtext, filedialog, messagebox

import ttkbootstrap as ttk
from ttkbootstrap import ScrolledText
from dotenv import load_dotenv

load_dotenv()

_SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
_SERVER_PORT = int(os.getenv("SERVER_PORT") or "8000")
BASE = f"http://{_SERVER_HOST}:{_SERVER_PORT}"
PROBLEM_TYPES = [
    "自动", "array", "tree", "graph", "string", "number_theory", "geometry",
    "multi_test", "dp", "matrix", "range_query", "weighted_tree", "weighted_graph",
    "interactive",
]
LANGS = ["python", "cpp"]
BUILTIN_CHECKER_OPTIONS = ["无", "lcmp", "wcmp", "rcmp4", "rcmp6", "rcmp9", "yesno"]

# 使用 ttkbootstrap 主题，可选：darkly / flatly / litera / minty / pulse / superhero 等
_DEFAULT_THEME = os.getenv("GUI_THEME", "flatly")


def _pids_listening_on_port(port: int) -> set[int]:
    """查出正在监听指定端口的进程 PID 集合。"""
    pids: set[int] = set()
    if os.name == "nt":
        try:
            out = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
        except Exception:
            return pids
        for line in out.splitlines():
            if "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            # TCP 127.0.0.1:8000  0.0.0.0:0  LISTENING  12345
            local = parts[1]
            if not (local.endswith(f":{port}") or local.endswith(f"]:{port}")):
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
        return pids

    # macOS / Linux: lsof
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            text=True, errors="ignore",
        )
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    except Exception:
        pass
    return pids


def _kill_pids(pids: set[int]) -> list[int]:
    """强制结束给定 PID，返回实际尝试杀掉的列表。"""
    killed: list[int] = []
    for pid in sorted(pids):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
            else:
                import signal
                os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except Exception:
            continue
    return killed


class ToolTip:
    """简单悬浮提示，使用固定深色样式，跨主题稳定。"""

    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._after_id = None
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)
        widget.bind("<Destroy>", self._on_leave)

    def _on_enter(self, _event=None):
        self._after_id = self.widget.after(self.delay, self._show)

    def _on_leave(self, _event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        self._hide()

    def _show(self):
        x, y, _, _ = self.widget.bbox("insert") or (0, 0, 0, 0)
        x += self.widget.winfo_rootx() + 20
        y += self.widget.winfo_rooty() + 24
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip, text=self.text, justify="left",
            background="#2b2b2b", foreground="#f0f0f0",
            relief="solid", borderwidth=1,
            font=("Segoe UI", 9), padx=6, pady=4,
        ).pack()

    def _hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# 与后端 runner 阶段文案对齐，用于顶部阶段条
STAGES = [
    ("std", "准备标程"),
    ("agent", "Agent 写 gen/validator"),
    ("check", "校验产物"),
    ("batch", "批量生成 in/out"),
    ("pack", "打包 zip"),
]


class App:
    def __init__(self, root):
        self.root = root
        root.title("ACM 出数据")
        root.geometry("1300x900")
        root.minsize(1000, 700)

        # 公共变量
        self.lang = tk.StringVar(value="cpp")
        self.ptype = tk.StringVar(value="自动")
        self.special_judge_var = tk.BooleanVar(value=False)
        self.builtin_checker = tk.StringVar(value="无")
        self.count_var = tk.StringVar(value="")
        self.plan_status = tk.StringVar(value="尚未生成方案 — 提交时将从「写 range」开始")
        self.rag_source = tk.StringVar(value="全部")
        self.rag_ptype = tk.StringVar(value="全部")
        self.rag_show_disabled = tk.BooleanVar(value=True)
        self.server_pid_var = tk.StringVar(value="未启动")
        self.status = tk.StringVar(value="待提交 — 可先在「数据方案」生成方案，或直接提交（含写 range）")

        self.nav_buttons = {}
        self.nav_tabs = {}
        self.stage_vars = {}
        self._current_tab = None
        self.job_id = None
        self._seen_progress = 0
        self._current_stage = None
        self.server_proc: subprocess.Popen | None = None

        self._build_header()
        self._build_main_area()
        self._build_bottom_panel()
        self._bind_shortcuts()

        self._show_tab("std")

    # ---- UI 构建 ----
    def _build_header(self):
        """顶部工具栏：品牌、服务器控制、主要操作。"""
        header = ttk.Frame(self.root, padding=8)
        header.pack(fill="x", padx=10, pady=(8, 2))

        # 品牌
        brand = ttk.Frame(header)
        brand.pack(side="left", fill="y")
        ttk.Label(brand, text="ACM 出数据", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(brand, text="智能测试数据生成器", font=("Segoe UI", 9), bootstyle="secondary").pack(anchor="w")

        # 服务器控制
        srv_box = ttk.Labelframe(header, text="后端服务器", padding=6)
        srv_box.pack(side="left", padx=(24, 12))
        self._srv_dot = ttk.Label(srv_box, text="●", font=("Segoe UI", 12), foreground="#adb5bd")
        self._srv_dot.pack(side="left", padx=(0, 6))
        self.btn_start_srv = ttk.Button(
            srv_box, text="启动", bootstyle="success", command=self.on_start_server,
        )
        self.btn_start_srv.pack(side="left", padx=2)
        ToolTip(self.btn_start_srv, "启动本地 FastAPI 后端（自动清理端口占用）")
        self.btn_kill_srv = ttk.Button(
            srv_box, text="停止", bootstyle="danger", command=self.on_kill_server,
        )
        self.btn_kill_srv.pack(side="left", padx=2)
        ToolTip(self.btn_kill_srv, "停止占用服务器端口的进程")
        ttk.Label(srv_box, textvariable=self.server_pid_var, bootstyle="secondary").pack(side="left", padx=8)

        # 选项
        opts = ttk.Frame(header)
        opts.pack(side="right", padx=(12, 4))
        ttk.Checkbutton(opts, text="Special Judge", variable=self.special_judge_var).pack(side="left", padx=4)
        ttk.Label(opts, text="Checker:").pack(side="left", padx=(8, 0))
        ttk.Combobox(
            opts, textvariable=self.builtin_checker,
            values=BUILTIN_CHECKER_OPTIONS, width=8, state="readonly",
        ).pack(side="left", padx=2)

        # 主要操作
        act_box = ttk.Frame(header)
        act_box.pack(side="right", fill="y")
        self.btn_beautify_all = ttk.Button(
            act_box, text="一键美化", bootstyle="outline-info", command=self.on_beautify_all,
        )
        self.btn_beautify_all.pack(side="left", padx=3)
        ToolTip(self.btn_beautify_all, "一键美化题面、输入描述、输出描述，直接保留")
        self.btn_submit = ttk.Button(
            act_box, text="提交生成", bootstyle="primary", command=self.on_submit,
        )
        self.btn_submit.pack(side="left", padx=3)
        ToolTip(self.btn_submit, "Ctrl+Enter 快捷提交")
        ttk.Separator(act_box, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn_download = ttk.Button(
            act_box, text="下载 zip", state="disabled", bootstyle="success",
            command=self.on_download,
        )
        self.btn_download.pack(side="left", padx=3)
        self.btn_download_sources = ttk.Button(
            act_box, text="下载源码", state="disabled", bootstyle="outline-info",
            command=self.on_download_sources,
        )
        self.btn_download_sources.pack(side="left", padx=3)
        self.btn_download_checker = ttk.Button(
            act_box, text="下载 checker", state="disabled", bootstyle="outline-info",
            command=self.on_download_checker,
        )
        self.btn_download_checker.pack(side="left", padx=3)

    def _build_main_area(self):
        """主工作区：可上下拖拽的 PanedWindow，上方为侧边栏+内容，下方为日志。"""
        self._vpaned = ttk.Panedwindow(self.root, orient="vertical")
        self._vpaned.pack(fill="both", expand=True, padx=10, pady=4)

        workspace = ttk.Frame(self._vpaned, padding=4)
        self._vpaned.add(workspace, weight=3)

        self.sidebar = ttk.Frame(workspace, width=180, padding=10)
        self.sidebar.pack(side="left", fill="y", padx=(0, 8))
        self.sidebar.pack_propagate(False)

        content = ttk.Frame(workspace, padding=4)
        content.pack(side="left", fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        self._build_sidebar(self.sidebar)
        self._build_content(content)

    def _build_sidebar(self, parent):
        """左侧导航栏。"""
        ttk.Label(parent, text="工作区", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 12))

        nav_items = [
            ("std", "1. 标程"),
            ("stmt", "2. 题面"),
            ("range", "3. 输入描述"),
            ("output", "4. 输出描述"),
            ("plan", "5. 数据方案"),
            ("rag", "6. RAG 语料"),
        ]
        for key, text in nav_items:
            btn = ttk.Button(
                parent, text=text,
                command=lambda k=key: self._show_tab(k),
                bootstyle="outline-primary",
            )
            btn.pack(fill="x", pady=3)
            self.nav_buttons[key] = btn

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=14)
        tips = ttk.Frame(parent)
        tips.pack(fill="x")
        ttk.Label(tips, text="快捷键", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(
            tips, text="Ctrl+1~6 切换标签\nCtrl+Enter 提交",
            bootstyle="secondary", font=("Segoe UI", 8),
        ).pack(anchor="w", pady=4)

    def _build_content(self, parent):
        """右侧各内容面板。"""
        def _build_tab(tab_name: str, tab_text: str) -> ttk.Frame:
            """创建一个内容面板，左侧导航按钮已在 _build_sidebar 中创建。"""
            frame = ttk.Frame(parent)
            self.nav_tabs[tab_name] = frame
            return frame

        # 1) 标程
        tab_std = _build_tab("std", "1. 标程")
        cfg = ttk.Labelframe(tab_std, text="标程配置", padding=8)
        cfg.pack(fill="x", padx=4, pady=(4, 8))
        ttk.Label(cfg, text="语言：").pack(side="left")
        ttk.Combobox(
            cfg, textvariable=self.lang, values=LANGS, width=8, state="readonly",
        ).pack(side="left", padx=4)
        ttk.Label(cfg, text="题型：").pack(side="left", padx=(16, 0))
        ttk.Combobox(
            cfg, textvariable=self.ptype, values=PROBLEM_TYPES, width=12, state="readonly",
        ).pack(side="left", padx=4)
        ttk.Label(
            cfg, text="（自动=生成方案时由大模型判题型）", bootstyle="secondary",
        ).pack(side="left")

        code_frm = ttk.Labelframe(tab_std, text="标准程序代码", padding=4)
        code_frm.pack(fill="both", expand=True, padx=4, pady=4)
        self.std_code = ScrolledText(
            code_frm, height=16, font=("Consolas", 10), wrap="none", autohide=True,
        )
        self.std_code.pack(fill="both", expand=True, padx=2, pady=2)

        # 2) 题面
        tab_stmt = _build_tab("stmt", "2. 题面")
        tip2 = ttk.Frame(tab_stmt)
        tip2.pack(fill="x", padx=4, pady=(4, 8))
        ttk.Label(
            tip2, text="支持 Markdown / HTML / LaTeX；可美化排版",
            bootstyle="secondary",
        ).pack(side="left")
        ttk.Button(
            tip2, text="美化", bootstyle="outline-info",
            command=lambda: self.open_rewrite("beautify", "statement", self.statement),
        ).pack(side="right", padx=2)
        self.statement = ScrolledText(
            tab_stmt, height=16, font=("Consolas", 10), wrap="word", autohide=True,
        )
        self.statement.pack(fill="both", expand=True, padx=4, pady=4)

        # 3) 输入描述
        tab_range = _build_tab("range", "3. 输入描述")
        tip3 = ttk.Frame(tab_range)
        tip3.pack(fill="x", padx=4, pady=(4, 8))
        ttk.Label(
            tip3, text="支持 Markdown / HTML / LaTeX；可美化排版",
            bootstyle="secondary",
        ).pack(side="left")
        ttk.Button(
            tip3, text="美化", bootstyle="outline-info",
            command=lambda: self.open_rewrite("beautify", "range", self.range_desc),
        ).pack(side="right", padx=2)
        self.range_desc = ScrolledText(
            tab_range, height=16, font=("Consolas", 10), wrap="word", autohide=True,
        )
        self.range_desc.pack(fill="both", expand=True, padx=4, pady=4)

        # 4) 输出描述
        tab_output = _build_tab("output", "4. 输出描述")
        tip_output = ttk.Frame(tab_output)
        tip_output.pack(fill="x", padx=4, pady=(4, 8))
        ttk.Label(
            tip_output, text="支持 Markdown / HTML / LaTeX；可美化排版",
            bootstyle="secondary",
        ).pack(side="left")
        ttk.Button(
            tip_output, text="美化", bootstyle="outline-info",
            command=lambda: self.open_rewrite("beautify", "output", self.output_desc),
        ).pack(side="right", padx=2)
        self.output_desc = ScrolledText(
            tab_output, height=16, font=("Consolas", 10), wrap="word", autohide=True,
        )
        self.output_desc.pack(fill="both", expand=True, padx=4, pady=4)

        # 5) 数据方案 range
        tab_plan = _build_tab("plan", "5. 数据方案")
        tip4 = ttk.Frame(tab_plan)
        tip4.pack(fill="x", padx=4, pady=(4, 8))
        ttk.Label(
            tip4, text="先点「生成方案」让大模型只写 range；提交全流程时若已有方案则跳过写 range",
            bootstyle="secondary",
        ).pack(side="left")
        self.btn_propose = ttk.Button(
            tip4, text="用大模型生成方案", bootstyle="success", command=self.on_propose_range,
        )
        self.btn_propose.pack(side="right", padx=2)
        ttk.Button(
            tip4, text="清空方案", bootstyle="outline-secondary", command=self.clear_range_plan,
        ).pack(side="right", padx=2)

        summary = ttk.Labelframe(tab_plan, text="方案概览", padding=6)
        summary.pack(fill="x", padx=4, pady=4)
        ttk.Label(summary, text="测例组数 count：", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.count_entry = ttk.Entry(summary, textvariable=self.count_var, width=8)
        self.count_entry.pack(side="left", padx=6)
        ttk.Label(summary, textvariable=self.plan_status, bootstyle="info").pack(side="left", padx=12)

        panes = ttk.Panedwindow(tab_plan, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=4, pady=4)

        cons_frm = ttk.Labelframe(panes, text="变量约束 constraints", padding=4)
        panes.add(cons_frm, weight=1)
        cols = ("name", "lo", "hi")
        self.cons_tree = ttk.Treeview(
            cons_frm, columns=cols, show="headings", height=6, bootstyle="info",
        )
        self.cons_tree.heading("name", text="变量")
        self.cons_tree.heading("lo", text="最小值")
        self.cons_tree.heading("hi", text="最大值")
        self.cons_tree.column("name", width=120)
        self.cons_tree.column("lo", width=140)
        self.cons_tree.column("hi", width=140)
        self.cons_tree.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        cons_scroll = ttk.Scrollbar(cons_frm, orient="vertical", command=self.cons_tree.yview)
        cons_scroll.pack(side="right", fill="y")
        self.cons_tree.configure(yscrollcommand=cons_scroll.set)

        edge_frm = ttk.Labelframe(panes, text="边界类型 edge_cases", padding=4)
        panes.add(edge_frm, weight=1)
        self.edge_list = ttk.Treeview(edge_frm, height=5, show="tree", bootstyle="info")
        self.edge_list.pack(fill="both", expand=True, padx=2, pady=2)

        self.range_data = None

        # 6) RAG 语料运营
        tab_rag = _build_tab("rag", "6. RAG 语料")
        tip_rag = ttk.Frame(tab_rag)
        tip_rag.pack(fill="x", padx=4, pady=(4, 8))
        ttk.Label(
            tip_rag,
            text="查看 / 禁用 / 删除范例；可合并 data/ 下别人贡献的 *.json",
            bootstyle="secondary",
        ).pack(side="left")
        ttk.Button(
            tip_rag, text="刷新列表", bootstyle="outline-primary", command=self.on_rag_refresh,
        ).pack(side="right", padx=2)

        filt = ttk.Frame(tab_rag)
        filt.pack(fill="x", padx=4, pady=2)
        ttk.Label(filt, text="来源：").pack(side="left")
        ttk.Combobox(
            filt, textvariable=self.rag_source,
            values=["全部", "job", "template"], width=8, state="readonly",
        ).pack(side="left", padx=4)
        ttk.Label(filt, text="题型：").pack(side="left", padx=(8, 0))
        ttk.Combobox(
            filt, textvariable=self.rag_ptype,
            values=["全部", "array", "tree", "graph", "string", "number_theory", "geometry",
                    "multi_test", "dp", "matrix", "range_query", "weighted_tree", "weighted_graph",
                    "interactive"],
            width=12, state="readonly",
        ).pack(side="left", padx=4)
        ttk.Checkbutton(filt, text="显示已禁用", variable=self.rag_show_disabled).pack(side="left", padx=8)

        rag_body = ttk.Frame(tab_rag)
        rag_body.pack(fill="both", expand=True, padx=4, pady=4)

        left = ttk.Frame(rag_body)
        left.pack(side="left", fill="both", expand=True)
        cols_rag = ("key", "source", "type", "rate", "disabled", "time")
        self.rag_tree = ttk.Treeview(
            left, columns=cols_rag, show="headings", height=10, bootstyle="primary",
        )
        self.rag_tree.heading("key", text="key")
        self.rag_tree.heading("source", text="来源")
        self.rag_tree.heading("type", text="题型")
        self.rag_tree.heading("rate", text="成功率")
        self.rag_tree.heading("disabled", text="禁用")
        self.rag_tree.heading("time", text="入库时间")
        self.rag_tree.column("key", width=110)
        self.rag_tree.column("source", width=70)
        self.rag_tree.column("type", width=90)
        self.rag_tree.column("rate", width=70)
        self.rag_tree.column("disabled", width=50)
        self.rag_tree.column("time", width=140)
        self.rag_tree.pack(side="left", fill="both", expand=True)
        rag_scroll = ttk.Scrollbar(left, orient="vertical", command=self.rag_tree.yview)
        rag_scroll.pack(side="right", fill="y")
        self.rag_tree.configure(yscrollcommand=rag_scroll.set)
        self.rag_tree.bind("<<TreeviewSelect>>", lambda _e: self.on_rag_select())

        right = ttk.Frame(rag_body, width=300)
        right.pack(side="right", fill="both", padx=(8, 0))
        ttk.Label(right, text="摘要 / 内容预览", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.rag_preview = ScrolledText(
            right, height=12, font=("Consolas", 9), wrap="word", autohide=True,
        )
        self.rag_preview.pack(fill="both", expand=True, pady=4)
        self.rag_preview.configure(state="disabled")

        rag_ops = ttk.Frame(tab_rag)
        rag_ops.pack(fill="x", padx=4, pady=4)
        ttk.Button(
            rag_ops, text="查看详情", bootstyle="outline-info", command=self.on_rag_view,
        ).pack(side="left", padx=2)
        ttk.Button(
            rag_ops, text="禁用/启用", bootstyle="outline-warning", command=self.on_rag_toggle_disable,
        ).pack(side="left", padx=2)
        ttk.Button(
            rag_ops, text="删除", bootstyle="outline-danger", command=self.on_rag_delete,
        ).pack(side="left", padx=2)
        ttk.Separator(rag_ops, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(
            rag_ops, text="合并 data/*.json…", bootstyle="outline-secondary", command=self.on_rag_merge,
        ).pack(side="left", padx=2)
        ttk.Button(
            rag_ops, text="从文件导入…", bootstyle="outline-secondary", command=self.on_rag_import_file,
        ).pack(side="left", padx=2)
        self.rag_status = tk.StringVar(value="请先启动服务器，再点「刷新列表」")
        ttk.Label(rag_ops, textvariable=self.rag_status, bootstyle="secondary").pack(side="left", padx=12)

    def _build_bottom_panel(self):
        """底部状态栏、阶段条、日志面板。"""
        bottom = ttk.Frame(self._vpaned, padding=4)
        self._vpaned.add(bottom, weight=1)

        status_bar = ttk.Frame(bottom)
        status_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(status_bar, text="状态：", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Label(status_bar, textvariable=self.status, bootstyle="info").pack(side="left", fill="x", expand=True)
        ttk.Button(
            status_bar, text="清空日志", bootstyle="outline-secondary",
            command=self._clear_log,
        ).pack(side="right", padx=2)

        stage_frm = ttk.Labelframe(bottom, text="流程阶段", padding=6)
        stage_frm.pack(fill="x", pady=4)
        row = ttk.Frame(stage_frm)
        row.pack(fill="x")
        for i, (key, label) in enumerate(STAGES):
            if i:
                ttk.Label(row, text="→", bootstyle="secondary").pack(side="left", padx=6)
            var = tk.StringVar(value=f"○ {label}")
            self.stage_vars[key] = var
            ttk.Label(row, textvariable=var, font=("Consolas", 10)).pack(side="left")

        log_frm = ttk.Labelframe(bottom, text="过程日志", padding=4)
        log_frm.pack(fill="both", expand=True)
        self.progress = ScrolledText(
            log_frm, height=10, font=("Consolas", 9), wrap="word", autohide=True,
        )
        self.progress.pack(fill="both", expand=True, padx=2, pady=2)
        self.progress.tag_configure("phase", foreground="#0a5")
        self.progress.tag_configure("ok", foreground="#060")
        self.progress.tag_configure("err", foreground="#c00")
        self.progress.tag_configure("tool", foreground="#06c")
        self.progress.tag_configure("dim", foreground="#888")

    def _bind_shortcuts(self):
        tab_map = {
            "1": "std",
            "2": "stmt",
            "3": "range",
            "4": "output",
            "5": "plan",
            "6": "rag",
        }
        for digit, name in tab_map.items():
            self.root.bind(f"<Control-Key-{digit}>", lambda _e, n=name: self._show_tab(n))
        self.root.bind("<Control-Return>", lambda _e: self.on_submit())

    def _show_tab(self, tab_name: str):
        """切换左侧导航按钮对应的内容面板。"""
        if self._current_tab == tab_name:
            return
        for name, btn in self.nav_buttons.items():
            btn.config(bootstyle="primary" if name == tab_name else "outline-primary")
        for name, frame in self.nav_tabs.items():
            if name == tab_name:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_forget()
        self._current_tab = tab_name

    def _clear_log(self):
        self.progress.delete("1.0", "end")
        self._seen_progress = 0

    def _update_server_indicator(self, running: bool):
        color = "#28a745" if running else "#adb5bd"
        self._srv_dot.config(foreground=color)

    def _call_text_rewrite(self, text: str, kind: str, mode: str, extra_hint: str = "") -> str:
        """调用 /text/simplify 或 /text/beautify，返回改写后的文本。"""
        endpoint = "/text/simplify" if mode == "simplify" else "/text/beautify"
        body = {"text": text, "kind": kind, "extra_hint": extra_hint or ""}
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{BASE}{endpoint}", data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            r = urllib.request.urlopen(req, timeout=120)
            return json.loads(r.read()).get("result") or ""
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw else {}
                detail = payload.get("detail") or raw
            except Exception:
                detail = ""
            if isinstance(detail, list):
                detail = "; ".join(str(x) for x in detail)
            msg = f"HTTP Error {e.code}: {detail or e.reason or 'Bad Request'}"
            raise RuntimeError(msg) from e

    def open_rewrite(self, mode: str, kind: str, widget):
        """mode: simplify|beautify；弹出审阅窗：保留 / 加提示词重生成 / 不保留。"""
        src = widget.get("1.0", "end").strip()
        if not src:
            messagebox.showwarning("提示", "内容为空")
            return
        title = "简化题面/范围" if mode == "simplify" else "美化题面/范围"
        if kind == "range":
            title = title.replace("题面/范围", "输入描述")
        elif kind == "output":
            title = title.replace("题面/范围", "输出描述")
        else:
            title = title.replace("题面/范围", "题面")

        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("1000x780")
        win.minsize(900, 650)
        win.transient(self.root)

        hint_var = tk.StringVar(value="")
        status_var = tk.StringVar(value="正在调用大模型…")

        top = ttk.Frame(win, padding=6)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, textvariable=status_var, bootstyle="info").pack(side="left")

        paned = ttk.Panedwindow(win, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        f1 = ttk.Labelframe(paned, text="原文")
        f2 = ttk.Labelframe(paned, text="改写结果（简化=纯文本无符号；美化=Markdown/LaTeX）")
        paned.add(f1, weight=1)
        paned.add(f2, weight=2)
        src_box = ScrolledText(f1, height=8, font=("Consolas", 9), wrap="word", autohide=True)
        src_box.pack(fill="both", expand=True, padx=4, pady=4)
        src_box.insert("1.0", src)
        src_box.configure(state="disabled")
        out_box = ScrolledText(f2, height=14, font=("Consolas", 10), wrap="word", autohide=True)
        out_box.pack(fill="both", expand=True, padx=4, pady=4)

        hint_frm = ttk.Labelframe(win, text="附加提示词（重新生成时生效）")
        hint_frm.pack(fill="x", padx=8, pady=4)
        hint_entry = ttk.Entry(hint_frm, textvariable=hint_var)
        hint_entry.pack(fill="x", padx=6, pady=6)

        btns = ttk.Frame(win, padding=6)
        btns.pack(fill="x", padx=8, pady=8)

        state = {"busy": False, "base_text": src}

        def set_result(text: str):
            out_box.configure(state="normal")
            out_box.delete("1.0", "end")
            out_box.insert("1.0", text)
            out_box.configure(state="normal")

        def call_api(extra: str, base: str):
            return self._call_text_rewrite(base, kind, mode, extra)

        def run_gen(extra="", use_current_as_base=False):
            if state["busy"]:
                return
            state["busy"] = True
            status_var.set("正在调用大模型…")
            for b in (btn_keep, btn_regen, btn_discard):
                b.config(state="disabled")

            def work():
                try:
                    base = out_box.get("1.0", "end").strip() if use_current_as_base else state["base_text"]
                    if not base:
                        base = state["base_text"]
                    result = call_api(extra, base)
                    self.root_after(lambda r=result: set_result(r))
                    self.root_after(lambda: status_var.set("完成 — 可「保留」写入编辑区，「重新生成」或「不保留」"))
                except Exception as e:
                    err = str(e) or repr(e)
                    self.root_after(lambda m=err: status_var.set(f"失败: {m}"))
                    self.root_after(lambda m=err: messagebox.showerror(title, m, parent=win))
                finally:
                    state["busy"] = False
                    self.root_after(lambda: [b.config(state="normal") for b in (btn_keep, btn_regen, btn_discard)])

            threading.Thread(target=work, daemon=True).start()

        def on_keep():
            text = out_box.get("1.0", "end").strip()
            if not text:
                messagebox.showwarning("提示", "结果为空", parent=win)
                return
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            self.status.set(("已保留简化结果" if mode == "simplify" else "已保留美化结果")
                            + " — 提交生成将使用当前编辑区文本")
            win.destroy()

        def on_regen():
            # 基于原文 + 附加提示词重新生成（不沿用上一次草稿）
            run_gen(extra=hint_var.get(), use_current_as_base=False)

        def on_discard():
            win.destroy()

        btn_keep = ttk.Button(btns, text="保留", bootstyle="success", command=on_keep)
        btn_keep.pack(side="left", padx=4)
        btn_regen = ttk.Button(btns, text="添加提示词后重新生成", bootstyle="primary", command=on_regen)
        btn_regen.pack(side="left", padx=4)
        btn_discard = ttk.Button(btns, text="不保留", bootstyle="outline-secondary", command=on_discard)
        btn_discard.pack(side="right", padx=4)

        run_gen(extra="", use_current_as_base=False)

    def on_beautify_all(self):
        """一键美化题面、输入描述、输出描述，直接保留，不弹确认对话框。"""
        fields = [
            ("statement", self.statement, "题面"),
            ("range", self.range_desc, "输入描述"),
            ("output", self.output_desc, "输出描述"),
        ]
        texts = [
            (kind, widget, label, widget.get("1.0", "end").strip())
            for kind, widget, label in fields
        ]
        if not any(t for _, _, _, t in texts):
            messagebox.showwarning("提示", "题面、输入描述、输出描述均为空")
            return

        self.status.set("正在一键美化题面、输入描述、输出描述…")
        self.btn_beautify_all.config(state="disabled")
        threading.Thread(target=self._beautify_all_worker, args=(texts,), daemon=True).start()

    def _beautify_all_worker(self, texts):
        """在后台线程串行调用 /text/beautify，完成后在主线程写回编辑区。"""
        results = {}
        errors = []
        for kind, widget, label, text in texts:
            if not text:
                continue
            try:
                result = self._call_text_rewrite(text, kind, "beautify")
                results[kind] = (widget, result)
            except Exception as e:
                errors.append(f"{label}: {e}")

        def apply():
            for widget, result in results.values():
                widget.delete("1.0", "end")
                widget.insert("1.0", result)
            self.btn_beautify_all.config(state="normal")
            if errors:
                self.status.set("一键美化部分失败: " + "; ".join(errors))
                messagebox.showerror("一键美化失败", "\n".join(errors))
            else:
                self.status.set("已一键美化并保留 — 提交生成将使用当前编辑区文本")

        self.root_after(apply)

    # ---- 数据方案（range）----
    def clear_range_plan(self):
        self.range_data = None
        self.count_var.set("")
        for i in self.cons_tree.get_children():
            self.cons_tree.delete(i)
        for i in self.edge_list.get_children():
            self.edge_list.delete(i)
        self.plan_status.set("尚未生成方案 — 提交时将从「写 range」开始")

    def apply_range_plan(self, data: dict, problem_type: str = ""):
        """把 range dict 填到可视化控件；若带 problem_type 则同步题型下拉框。"""
        ptype = (problem_type or data.get("problem_type") or "").strip()
        self.range_data = {
            "count": int(data.get("count") or 15),
            "constraints": dict(data.get("constraints") or {}),
            "edge_cases": list(data.get("edge_cases") or []),
        }
        if ptype:
            self.range_data["problem_type"] = ptype
            if ptype in PROBLEM_TYPES:
                self.ptype.set(ptype)
        self.count_var.set(str(self.range_data["count"]))
        for i in self.cons_tree.get_children():
            self.cons_tree.delete(i)
        for name, bounds in self.range_data["constraints"].items():
            lo, hi = bounds[0], bounds[1] if isinstance(bounds, (list, tuple)) and len(bounds) >= 2 else ("?", "?")
            self.cons_tree.insert("", "end", values=(name, lo, hi))
        for i in self.edge_list.get_children():
            self.edge_list.delete(i)
        for e in self.range_data["edge_cases"]:
            self.edge_list.insert("", "end", text=f"  •  {e}")
        type_s = f" · 题型 {ptype}" if ptype else ""
        self.plan_status.set(
            f"已就绪：{self.range_data['count']} 组 · "
            f"{len(self.range_data['constraints'])} 个变量 · "
            f"{len(self.range_data['edge_cases'])} 种边界{type_s} — 提交将跳过写 range"
        )

    def collect_range_from_ui(self):
        """从可视化控件读回 dict；若无有效方案返回 None。"""
        if self.range_data is None and not self.cons_tree.get_children():
            return None
        try:
            count = int(self.count_var.get().strip() or "0")
        except ValueError:
            count = 0
        cons = {}
        for iid in self.cons_tree.get_children():
            name, lo, hi = self.cons_tree.item(iid, "values")
            try:
                cons[str(name)] = [int(lo), int(hi)]
            except (TypeError, ValueError):
                continue
        edges = []
        for item in self.edge_list.get_children():
            t = self.edge_list.item(item, "text").strip().lstrip("•").strip()
            if t:
                edges.append(t)
        if count <= 0 or not cons:
            return None
        out = {"count": count, "constraints": cons, "edge_cases": edges}
        ptype = self.ptype.get()
        if ptype and ptype != "自动":
            out["problem_type"] = ptype
        elif isinstance(self.range_data, dict) and self.range_data.get("problem_type"):
            out["problem_type"] = self.range_data["problem_type"]
        return out

    def on_propose_range(self):
        stmt = self.statement.get("1.0", "end").strip()
        rng = self.range_desc.get("1.0", "end").strip()
        if not stmt and not rng:
            messagebox.showwarning("提示", "请先填写题面或输入描述")
            return
        ptype = self.ptype.get()
        if ptype == "自动":
            ptype = ""
        body = {
            "problem_statement": stmt,
            "data_range_desc": rng,
            "problem_type": ptype,
            "std_code": self.std_code.get("1.0", "end").strip(),
            "lang": self.lang.get(),
        }
        self.btn_propose.config(state="disabled")
        self.status.set("正在用大模型生成数据方案（仅 range）…")
        threading.Thread(target=self._propose_worker, args=(body,), daemon=True).start()

    def _propose_worker(self, body):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                f"{BASE}/range/propose", data=data,
                headers={"Content-Type": "application/json"},
            )
            # 同步等 LLM，超时放宽
            r = urllib.request.urlopen(req, timeout=180)
            payload = json.loads(r.read())
            rj = payload.get("range_json") or {}
            ptype = payload.get("problem_type") or rj.get("problem_type") or ""
            self.root_after(lambda: self.apply_range_plan(rj, ptype))
            msg = "数据方案已生成 — 可在「5. 数据方案」查看，再点提交生成"
            if ptype:
                msg = f"数据方案已生成（题型={ptype}）— 可在「5. 数据方案」查看，再点提交生成"
            self.root_after(lambda m=msg: self.status.set(m))
        except Exception as e:
            self.root_after(lambda: messagebox.showerror("生成方案失败", str(e)))
            self.root_after(lambda: self.status.set(f"生成方案失败: {e}"))
        finally:
            self.root_after(lambda: self.btn_propose.config(state="normal"))

    # ---- 提交 ----
    def on_submit(self):
        std = self.std_code.get("1.0", "end").strip()
        stmt = self.statement.get("1.0", "end").strip()
        rng = self.range_desc.get("1.0", "end").strip()
        out = self.output_desc.get("1.0", "end").strip()
        if not std:
            messagebox.showwarning("提示", "请填写「1. 标程」")
            return
        if not stmt:
            messagebox.showwarning("提示", "请填写「2. 题面」")
            return
        if not rng:
            messagebox.showwarning("提示", "请填写「3. 输入描述」")
            return

        ptype = self.ptype.get()
        if ptype == "自动":
            ptype = ""
        bc = self.builtin_checker.get()
        if bc == "无":
            bc = ""
        body = {
            "std_code": std,
            "lang": self.lang.get(),
            "problem_type": ptype,
            "problem_statement": stmt,
            "data_range_desc": rng,
            "output_desc": out,
            "special_judge": self.special_judge_var.get(),
            "builtin_checker": bc,
        }
        plan = self.collect_range_from_ui()
        if plan:
            body["range_json"] = plan
        self.btn_submit.config(state="disabled")
        self.btn_download.config(state="disabled")
        self.btn_download_sources.config(state="disabled")
        self.btn_download_checker.config(state="disabled")
        self.progress.delete("1.0", "end")
        self._seen_progress = 0
        self._reset_stages()
        if plan:
            self.status.set(f"提交中（已带方案 count={plan['count']}，将跳过写 range）…")
            self._append_log(f"使用 GUI 数据方案: count={plan['count']} edges={plan['edge_cases']}")
        else:
            self.status.set("提交中（无方案，将从写 range 开始）…")
            self._append_log("未提供数据方案 — Agent 将先 write_range")
        threading.Thread(target=self._submit, args=(body,), daemon=True).start()

    def _submit(self, body):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                f"{BASE}/jobs", data=data,
                headers={"Content-Type": "application/json"},
            )
            r = urllib.request.urlopen(req, timeout=10)
            self.job_id = json.loads(r.read())["job_id"]
            self.root_after(lambda: self.status.set(f"已提交  job={self.job_id}  运行中…"))
            self.root_after(self._poll, 800)
        except Exception as e:
            self.root_after(lambda: self.status.set(f"提交失败: {e}"))
            self.root_after(lambda: self.btn_submit.config(state="normal"))

    # ---- 轮询 ----
    def _poll(self):
        if not self.job_id:
            return

        def work():
            try:
                st = json.loads(urllib.request.urlopen(
                    f"{BASE}/jobs/{self.job_id}", timeout=10).read())
                self.root_after(lambda: self._render(st))
            except Exception as e:
                self.root_after(lambda: self.status.set(f"查询失败: {e}"))
                self.root_after(lambda: self.btn_submit.config(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    def _format_elapsed(self, elapsed_ms) -> str:
        try:
            ms = int(elapsed_ms or 0)
        except (TypeError, ValueError):
            return "-"
        total_s = max(0, ms) / 1000.0
        if total_s < 60:
            return f"{total_s:.1f}s"
        m = int(total_s // 60)
        s = total_s - m * 60
        if m < 60:
            return f"{m}m{s:04.1f}s"
        h = m // 60
        m = m % 60
        return f"{h}h{m}m{s:04.1f}s"

    def _format_tokens(self, usage: dict | None) -> str:
        if not usage:
            return "tokens=-"
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or 0)
        calls = int(usage.get("chat_calls") or 0)
        embed = int(usage.get("embedding_tokens") or 0)
        text = f"tokens={total} (in {prompt} / out {completion}, calls={calls})"
        if embed:
            text += f" embed={embed}"
        return text

    def _stats_suffix(self, st: dict) -> str:
        elapsed = self._format_elapsed(st.get("elapsed_ms"))
        tokens = self._format_tokens(st.get("token_usage") or {})
        return f"耗时 {elapsed} · {tokens}"

    def _render(self, st):
        lines = st.get("progress") or []
        # 增量追加新日志
        for msg in lines[self._seen_progress:]:
            self._append_log(msg)
            self._update_stage_from_msg(msg)
        self._seen_progress = len(lines)

        err = st.get("error") or ""
        status = st["status"]
        stats = self._stats_suffix(st)
        if status == "done":
            self.status.set(f"完成 — {stats} — 可下载测例 zip / 源码包")
            self._set_stage("pack", "done")
            self.btn_download.config(state="normal")
            if st.get("has_sources_zip"):
                self.btn_download_sources.config(state="normal")
            if st.get("has_checker_zip"):
                self.btn_download_checker.config(state="normal")
            self.btn_submit.config(state="normal")
        elif status == "error":
            self.status.set(f"失败: {err[:120]} — {stats}")
            if self._current_stage:
                self._set_stage(self._current_stage, "error")
            self.btn_submit.config(state="normal")
        else:
            self.status.set(
                f"运行中  job={self.job_id}  已收到 {len(lines)} 条进度 — {stats}"
            )
            self.root_after(self._poll, 1200)

    def _append_log(self, msg: str):
        tag = "dim"
        display = msg
        if msg.startswith("【阶段"):
            tag = "phase"
        elif msg.startswith("【统计】"):
            tag = "ok"
        elif "ERROR" in msg or msg.startswith("ERROR"):
            tag = "err"
        elif msg.startswith("[step"):
            tag = "tool"
            display = self._format_tool_line(msg)
        elif "完成" in msg or "OK" in msg or "统计" in msg:
            tag = "ok"
        self.progress.insert("end", display + "\n", tag)
        self.progress.see("end")

    @staticmethod
    def _format_tool_line(msg: str) -> str:
        """把冗长的 [step N] name {args} -> preview 压成一行可读摘要。"""
        m = re.match(r"\[step (\d+)\] (\w+)\s+(\{.*?\})\s*->\s*(.*)$", msg)
        if not m:
            return msg if len(msg) <= 200 else msg[:200] + "…"
        step, name, args_s, preview = m.group(1), m.group(2), m.group(3), m.group(4)
        hint = ""
        try:
            args = json.loads(args_s.replace("'", '"'))
        except Exception:
            args = {}
        if name == "write_range":
            hint = "写 range.json"
        elif name == "write_gen":
            hint = "写+编译 gen.cpp"
        elif name == "write_validate":
            hint = "写+编译 validator.cpp"
        elif name == "write_checker":
            hint = "写+编译 checker.cpp"
        elif name == "use_builtin_checker":
            hint = f"安装内置 checker={args.get('name', '?')}"
        elif name == "run_self_check":
            hint = "强化自检"
        elif name == "run_gen":
            hint = f"生成 type={args.get('type', '?')} seed={args.get('seed', '?')}"
        elif name == "run_validate":
            hint = "校验输入"
        elif name == "run_std":
            hint = "跑标程"
        elif name == "finish":
            hint = args.get("summary", "finish")
        else:
            hint = name
        pv = preview if len(preview) <= 80 else preview[:80] + "…"
        return f"  step {step}  {name:<14}  {hint}  → {pv}"

    def _reset_stages(self):
        self._current_stage = None
        for key, label in STAGES:
            self.stage_vars[key].set(f"○ {label}")

    def _set_stage(self, key: str, state: str):
        label = dict(STAGES)[key]
        if state == "run":
            self.stage_vars[key].set(f"● {label}")
            self._current_stage = key
        elif state == "done":
            self.stage_vars[key].set(f"✓ {label}")
        elif state == "error":
            self.stage_vars[key].set(f"✗ {label}")

    def _update_stage_from_msg(self, msg: str):
        # 优先吃后端 【阶段 N/5】 打点
        m = re.search(r"【阶段\s*(\d+)/5】", msg)
        if m:
            idx = int(m.group(1)) - 1
            keys = [k for k, _ in STAGES]
            if 0 <= idx < len(keys):
                for k in keys[:idx]:
                    cur = self.stage_vars[k].get()
                    if not cur.startswith("✓") and not cur.startswith("✗"):
                        self._set_stage(k, "done")
                self._set_stage(keys[idx], "run")
            return
        if "打包完成" in msg:
            self._set_stage("pack", "done")
        elif "生成统计" in msg:
            self._set_stage("batch", "done")
        elif "产物 OK" in msg:
            self._set_stage("check", "done")
        elif "Agent 结束" in msg:
            self._set_stage("agent", "done")
        elif "标程就绪" in msg:
            self._set_stage("std", "done")

    # ---- 下载 ----
    def on_download(self):
        if not self.job_id:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".zip", filetypes=[("zip", "*.zip")],
            initialfile="data.zip",
        )
        if not path:
            return

        def work():
            try:
                content = urllib.request.urlopen(
                    f"{BASE}/jobs/{self.job_id}/download", timeout=30).read()
                with open(path, "wb") as f:
                    f.write(content)
                self.root_after(lambda: self.status.set(f"已保存: {path}"))
            except Exception as e:
                self.root_after(lambda: messagebox.showerror("下载失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ---- 服务器控制 ----
    def on_start_server(self):
        """启动后端 FastAPI 服务器。"""
        # 端口已被占用时先清掉，避免旧进程继续跑旧代码
        occupied = _pids_listening_on_port(_SERVER_PORT)
        if occupied:
            killed = _kill_pids(occupied)
            self._append_log(
                f"启动前清理端口 {_SERVER_PORT}，已结束 PID: {killed or list(occupied)}"
            )
            self.server_proc = None

        if self.server_proc is not None and self.server_proc.poll() is None:
            messagebox.showwarning("提示", "服务器已在运行")
            return

        try:
            # 使用当前 Python 解释器启动 uvicorn
            # --no-access-log：关闭 GET /jobs 轮询刷屏；--log-level warning：少打 INFO
            cmd = [
                sys.executable, "-m", "uvicorn", "server.app:app",
                "--host", _SERVER_HOST, "--port", str(_SERVER_PORT),
                "--no-access-log",
                "--log-level", "warning",
            ]
            self.server_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            pid = self.server_proc.pid
            self.server_pid_var.set(f"端口 {_SERVER_PORT} | PID={pid}")
            self.btn_start_srv.config(state="disabled")
            self._update_server_indicator(True)
            self._append_log(f"服务器已启动: port={_SERVER_PORT} PID={pid}")

            # 后台线程读取服务器输出到日志
            threading.Thread(
                target=self._server_log_reader,
                args=(self.server_proc,),
                daemon=True,
            ).start()
        except Exception as e:
            messagebox.showerror("启动服务器失败", str(e))
            self.server_proc = None
            self._update_server_indicator(False)
            self.btn_start_srv.config(state="normal")

    def _server_log_reader(self, proc: subprocess.Popen):
        """读取服务器 stdout/stderr 并追加到日志区。"""
        try:
            for line in proc.stdout:
                self.root_after(lambda l=line.strip(): self._append_log(f"[server] {l}"))
        except Exception:
            pass
        finally:
            self.root_after(lambda: self._update_server_indicator(False))
            self.root_after(lambda: self.server_pid_var.set("未启动"))
            self.root_after(lambda: self.btn_start_srv.config(state="normal"))

    def on_kill_server(self):
        """按 SERVER_PORT 强制结束占用该端口的进程（不依赖本次是否由 GUI 启动）。"""
        pids = set(_pids_listening_on_port(_SERVER_PORT))
        # 顺带结束 GUI 记住的子进程（若仍在）
        if self.server_proc is not None and self.server_proc.poll() is None:
            try:
                pids.add(self.server_proc.pid)
            except Exception:
                pass

        if not pids:
            self.server_proc = None
            self.server_pid_var.set("未启动")
            self._update_server_indicator(False)
            self.btn_start_srv.config(state="normal")
            self._append_log(f"端口 {_SERVER_PORT} 上没有监听进程")
            messagebox.showinfo("提示", f"端口 {_SERVER_PORT} 上没有服务器在运行")
            return

        try:
            killed = _kill_pids(pids)
            # 再扫一次，确认清空
            left = _pids_listening_on_port(_SERVER_PORT)
            if left:
                _kill_pids(left)
                left = _pids_listening_on_port(_SERVER_PORT)
            if left:
                messagebox.showwarning(
                    "未完全清理",
                    f"仍有进程占用端口 {_SERVER_PORT}: {sorted(left)}\n请手动 taskkill /F /PID …",
                )
                self._append_log(f"杀端口未净: 残留 PID {sorted(left)}")
            else:
                self._append_log(
                    f"已按端口 {_SERVER_PORT} 结束进程: {sorted(killed) or sorted(pids)}"
                )
        except Exception as e:
            messagebox.showerror("杀死服务器失败", str(e))
        finally:
            self.server_proc = None
            self.server_pid_var.set("未启动")
            self._update_server_indicator(False)
            self.btn_start_srv.config(state="normal")

    def on_download_sources(self):
        if not self.job_id:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".zip", filetypes=[("zip", "*.zip")],
            initialfile="sources.zip",
        )
        if not path:
            return

        def work():
            try:
                content = urllib.request.urlopen(
                    f"{BASE}/jobs/{self.job_id}/download_sources", timeout=30).read()
                with open(path, "wb") as f:
                    f.write(content)
                self.root_after(lambda: self.status.set(f"已保存源码包: {path}"))
            except Exception as e:
                self.root_after(lambda: messagebox.showerror("下载源码失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def on_download_checker(self):
        if not self.job_id:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".zip", filetypes=[("zip", "*.zip")],
            initialfile="checker.zip",
        )
        if not path:
            return

        def work():
            try:
                content = urllib.request.urlopen(
                    f"{BASE}/jobs/{self.job_id}/download_checker", timeout=30).read()
                with open(path, "wb") as f:
                    f.write(content)
                self.root_after(lambda: self.status.set(f"已保存 checker: {path}"))
            except Exception as e:
                self.root_after(lambda: messagebox.showerror("下载 checker 失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ---- RAG 语料运营 ----
    def _rag_selected_key(self) -> str:
        sel = self.rag_tree.selection()
        if not sel:
            return ""
        return self.rag_tree.item(sel[0], "values")[0]

    def _rag_get_json(self, path: str, timeout: int = 30):
        r = urllib.request.urlopen(f"{BASE}{path}", timeout=timeout)
        return json.loads(r.read())

    def _rag_post_json(self, path: str, body: dict, timeout: int = 120):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{BASE}{path}", data=data,
            headers={"Content-Type": "application/json"},
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read())

    def _rag_delete(self, path: str, timeout: int = 30):
        req = urllib.request.Request(f"{BASE}{path}", method="DELETE")
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read())

    def on_rag_refresh(self):
        def work():
            try:
                src = self.rag_source.get()
                ptype = self.rag_ptype.get()
                qs = []
                if src and src != "全部":
                    qs.append(f"source={src}")
                if ptype and ptype != "全部":
                    qs.append(f"problem_type={ptype}")
                qs.append(f"include_disabled={'true' if self.rag_show_disabled.get() else 'false'}")
                q = "?" + "&".join(qs)
                data = self._rag_get_json(f"/rag/corpus{q}")
                items = data.get("items") or []

                def fill():
                    self.rag_tree.delete(*self.rag_tree.get_children())
                    for it in items:
                        rate = it.get("valid_rate")
                        rate_s = f"{rate:.3f}" if isinstance(rate, (int, float)) else "-"
                        self.rag_tree.insert("", "end", values=(
                            it.get("key", ""),
                            it.get("source", ""),
                            it.get("problem_type") or "-",
                            rate_s,
                            "是" if it.get("disabled") else "否",
                            (it.get("created_at") or "-")[:19],
                        ))
                    model = data.get("model") or "?"
                    self.rag_status.set(f"共 {data.get('total', len(items))} 条 | model={model}")

                self.root_after(fill)
            except Exception as e:
                self.root_after(lambda: self.rag_status.set(f"刷新失败: {e}"))
                self.root_after(lambda: messagebox.showerror("RAG 刷新失败", str(e)))

        self.rag_status.set("加载中…")
        threading.Thread(target=work, daemon=True).start()

    def on_rag_select(self):
        key = self._rag_selected_key()
        if not key:
            return

        def work():
            try:
                it = self._rag_get_json(f"/rag/corpus/{urllib.parse.quote(key)}")
                preview = (it.get("text") or it.get("text_preview") or "")[:1500]
                content_head = (it.get("content") or "")[:2000]
                text = (
                    f"key: {it.get('key')}\n"
                    f"source: {it.get('source')} | type: {it.get('problem_type') or '-'}\n"
                    f"valid_rate: {it.get('valid_rate')} | disabled: {it.get('disabled')}\n"
                    f"created_at: {it.get('created_at') or '-'}\n\n"
                    f"--- text ---\n{preview}\n\n"
                    f"--- content (截断) ---\n{content_head}"
                )

                def show():
                    self.rag_preview.configure(state="normal")
                    self.rag_preview.delete("1.0", "end")
                    self.rag_preview.insert("1.0", text)
                    self.rag_preview.configure(state="disabled")

                self.root_after(show)
            except Exception as e:
                self.root_after(lambda: self.rag_status.set(f"预览失败: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def on_rag_view(self):
        key = self._rag_selected_key()
        if not key:
            messagebox.showinfo("提示", "请先选中一条语料")
            return
        self.on_rag_select()

        def work():
            try:
                it = self._rag_get_json(f"/rag/corpus/{urllib.parse.quote(key)}")
                content = it.get("content") or ""

                def popup():
                    win = tk.Toplevel(self.root)
                    win.title(f"语料详情 — {key}")
                    win.geometry("720x520")
                    box = ScrolledText(win, font=("Consolas", 9), wrap="word", autohide=True)
                    box.pack(fill="both", expand=True, padx=8, pady=8)
                    box.insert("1.0", content)
                    box.configure(state="disabled")

                self.root_after(popup)
            except Exception as e:
                self.root_after(lambda: messagebox.showerror("查看失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def on_rag_toggle_disable(self):
        key = self._rag_selected_key()
        if not key:
            messagebox.showinfo("提示", "请先选中一条语料")
            return
        sel = self.rag_tree.selection()[0]
        cur_disabled = self.rag_tree.item(sel, "values")[4] == "是"
        new_disabled = not cur_disabled
        action = "禁用" if new_disabled else "启用"
        if not messagebox.askyesno("确认", f"确定{action}「{key}」？\n禁用后召回时会跳过。"):
            return

        def work():
            try:
                self._rag_post_json(
                    f"/rag/corpus/{urllib.parse.quote(key)}/disable",
                    {"disabled": new_disabled},
                )
                self.root_after(lambda: self.rag_status.set(f"已{action}: {key}"))
                self.root_after(self.on_rag_refresh)
            except Exception as e:
                self.root_after(lambda: messagebox.showerror(f"{action}失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def on_rag_delete(self):
        key = self._rag_selected_key()
        if not key:
            messagebox.showinfo("提示", "请先选中一条语料")
            return
        if not messagebox.askyesno("确认删除", f"永久删除语料「{key}」？此操作不可撤销。"):
            return

        def work():
            try:
                self._rag_delete(f"/rag/corpus/{urllib.parse.quote(key)}")
                self.root_after(lambda: self.rag_status.set(f"已删除: {key}"))
                self.root_after(self.on_rag_refresh)
            except Exception as e:
                self.root_after(lambda: messagebox.showerror("删除失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def on_rag_merge(self):
        """列出 data/*.json 供选择合并。"""
        def work():
            try:
                data = self._rag_get_json("/rag/data-files")
                files = data.get("files") or []
                if not files:
                    self.root_after(lambda: messagebox.showinfo("提示", "data/ 下没有可合并的 *.json"))
                    return

                def choose():
                    win = tk.Toplevel(self.root)
                    win.title("合并 data/*.json")
                    win.geometry("480x320")
                    ttk.Label(win, text="选择要合并进当前语料库的文件（跳过同 key / 高相似）：").pack(
                        anchor="w", padx=8, pady=6
                    )
                    lb = tk.Listbox(win, font=("Consolas", 10))
                    lb.pack(fill="both", expand=True, padx=8, pady=4)
                    for f in files:
                        mark = " [种子]" if f.get("is_seed") else ""
                        lb.insert("end", f"{f['name']}  ({f['size']} bytes){mark}")

                    def do_merge():
                        idxs = lb.curselection()
                        if not idxs:
                            messagebox.showinfo("提示", "请选一个文件")
                            return
                        name = files[idxs[0]]["name"]
                        if files[idxs[0]].get("is_seed"):
                            if not messagebox.askyesno(
                                "确认",
                                "这是当前种子文件 few_shots_rag_corpus.json，合并通常无新条目。仍继续？",
                            ):
                                return
                        win.destroy()

                        def run():
                            try:
                                self.rag_status.set(f"合并中: {name} …")
                                st = self._rag_post_json(
                                    "/rag/corpus/merge",
                                    {"path": name, "skip_disabled": True},
                                    timeout=300,
                                )
                                msg = (
                                    f"合并完成：新增 {st.get('added', 0)}，"
                                    f"跳过同key {st.get('skipped_key', 0)}，"
                                    f"跳过重复 {st.get('skipped_dup', 0)}，"
                                    f"跳过禁用 {st.get('skipped_disabled', 0)}，"
                                    f"无效 {st.get('skipped_invalid', 0)}"
                                )
                                self.root_after(lambda: self.rag_status.set(msg))
                                self.root_after(lambda: messagebox.showinfo("合并结果", msg))
                                self.root_after(self.on_rag_refresh)
                            except Exception as e:
                                self.root_after(lambda: messagebox.showerror("合并失败", str(e)))

                        threading.Thread(target=run, daemon=True).start()

                    ttk.Button(win, text="合并选中文件", command=do_merge).pack(pady=8)

                self.root_after(choose)
            except Exception as e:
                self.root_after(lambda: messagebox.showerror("列出文件失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def on_rag_import_file(self):
        """从任意本地 JSON 文件导入（读入后 POST corpus 对象）。"""
        path = filedialog.askopenfilename(
            title="选择语料 JSON",
            filetypes=[("JSON", "*.json"), ("全部", "*.*")],
        )
        if not path:
            return

        def work():
            try:
                raw = Path(path).read_text(encoding="utf-8")
                corpus = json.loads(raw)
                self.rag_status.set(f"导入中: {path} …")
                st = self._rag_post_json(
                    "/rag/corpus/merge",
                    {"corpus": corpus, "skip_disabled": True},
                    timeout=300,
                )
                msg = (
                    f"导入完成：新增 {st.get('added', 0)}，"
                    f"跳过同key {st.get('skipped_key', 0)}，"
                    f"跳过重复 {st.get('skipped_dup', 0)}"
                )
                self.root_after(lambda: self.rag_status.set(msg))
                self.root_after(lambda: messagebox.showinfo("导入结果", msg))
                self.root_after(self.on_rag_refresh)
            except Exception as e:
                self.root_after(lambda: messagebox.showerror("导入失败", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def root_after(self, func, ms=0):
        self.root.after(ms, func)


def main():
    root = ttk.Window(title="ACM 出数据", themename=_DEFAULT_THEME, size=(980, 820), minsize=(760, 640))
    app = App(root)
    # 预填 A+B，方便直接测
    app.std_code.insert("1.0", """#include <bits/stdc++.h>
using namespace std;
int main() {
    long long a, b;
    if (!(cin >> a >> b)) return 0;
    cout << a + b << "\\n";
    return 0;
}
""")
    app.statement.insert(
        "1.0",
        "# A+B Problem\n\n"
        "给定整数 $a,b$，输出 $a+b$。\n\n"
        "## 输入格式\n"
        "一行两个整数 $a\\ b$，空格分隔。\n\n"
        "## 输出格式\n"
        "一行一个整数。\n\n"
        "约束：$|a|,|b| \\leq 10^9$。本题**没有**测试组数 $T$。\n",
    )
    app.range_desc.insert(
        "1.0",
        "a,b ∈ [-1e9, 1e9]，共生成 15 组。\n"
        "边界：a=0、b=0、双负、双最大、双最小、一正一负。",
    )
    root.mainloop()


if __name__ == "__main__":
    main()
