"""ACM 出数据 —— 桌面 GUI（tkinter）。

三块输入：标程(std) / 题面 / 输入描述 / 输出描述；题面、范围与输出可「美化」。
第四 Tab：数据方案（range）可视化。

用法：
    python -m gui
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

from gui.api_client import BASE, call_text_rewrite, http_get, http_post, http_delete
from gui.constants import (
    BUILTIN_CHECKER_OPTIONS,
    EDGE_CASE_UI_LIMIT,
    LANGS,
    _SERVER_HOST,
    _SERVER_PORT,
    _builtin_checker_id_from_label,
    _builtin_checker_label_from_any,
    _edge_case_zh,
    _trim_edge_cases,
)
from gui.server_manager import _kill_pids, _pids_listening_on_port
from storage import problem_store
from gui.widgets import (
    STAGES,
    ToolTip,
    _BP_COMPACT,
    _BP_NARROW,
    _BP_NORMAL,
    _BP_WIDE,
    _DEFAULT_THEME,
    _MIN_H,
    _MIN_W,
    _fit_to_screen,
    _try_enable_dpi_awareness,
)


class App:
    def __init__(self, root):
        self.root = root
        root.title("ACM 出数据")
        # 根据屏幕分辨率自适应初始窗口
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        init_w, init_h, init_x, init_y = _fit_to_screen(
            min(1480, screen_w - 60),
            min(920, screen_h - 60),
            screen_w, screen_h,
            margin=40,
        )
        root.geometry(f"{init_w}x{init_h}+{init_x}+{init_y}")
        root.minsize(_MIN_W, _MIN_H)

        # 公共变量
        self.lang = tk.StringVar(value="cpp")
        # 题型只读展示：由 Range Agent / LLM 写入，界面不可选手动指定
        self.ptype = tk.StringVar(value="待模型判定")
        self.special_judge_var = tk.BooleanVar(value=False)
        self.builtin_checker = tk.StringVar(value="无")
        self.count_var = tk.StringVar(value="")  # 常规样例数（不含特殊样例）
        self.special_count_var = tk.StringVar(value="1")  # 每方案样例数（默认 1 组即可）
        self.time_limit_var = tk.StringVar(value="5000")  # time_limit_ms，默认 5s
        self.memory_limit_var = tk.StringVar(value="1024")  # memory_limit_mb，默认 1024MB
        self.special_desc_var = tk.StringVar(value="")
        self.auto_discover_special_var = tk.BooleanVar(value=False)  # 无提示时自动挖特殊方案
        self.special_schemes: list[dict] = []  # Range 阶段挖出的特殊方案（含 selected）
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
        self.current_problem_id = ""  # problems/<id>
        # 历史 Job 加载时的复用意图：提交时生效
        self.pending_resume_parent_id: str | None = None  # 显式复用该 job
        self.prefer_skip_resume: bool = False  # 历史加载时选了「仅加载题目」
        self.problem_title_var = tk.StringVar(value="（未保存题目）")
        self._seen_progress = 0
        self._current_stage = None
        self.server_proc: subprocess.Popen | None = None

        # 自适应状态
        self._sidebar_user_touched = False
        self._layout_mode = ""          # wide / normal / compact / narrow
        self._configure_after_id = None
        self._last_layout_wh = (0, 0)
        self._last_sash_h = 0
        self._sash_ratio = 0.72
        self._tip_labels: list[ttk.Label] = []
        self._problem_title_lbl = None
        self._rag_right = None
        self._plan_tip_label = None
        self._hist_tip_label = None
        self._act_sep = None
        self._srv_frame = None
        self._brand_frame = None
        self._act_frame = None

        self._build_header()
        self._build_main_area()
        self._build_bottom_panel()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # 默认多留工作区高度，避免「数据方案」被底部日志挤扁
        self.root.after(150, self._init_main_sash)
        self.root.after(180, self._force_adaptive_layout)
        self.root.bind("<Configure>", self._on_root_configure, add="+")

        self._show_tab("std")

    @staticmethod
    def _format_ptype_display(raw) -> str:
        """把 range/LLM 返回的题型格式化为界面只读文案。"""
        if isinstance(raw, list):
            text = ", ".join(str(x) for x in raw if x)
        else:
            text = str(raw or "").strip()
        if not text or text in ("自动", "待模型判定"):
            return "待模型判定"
        return text

    def _set_detected_ptype(self, raw) -> str:
        """更新题型只读展示；返回规范化后的显示字符串。"""
        text = self._format_ptype_display(raw)
        self.ptype.set(text)
        return text

    # ---- UI 构建 ----
    def _build_header(self):
        """顶部工具栏：品牌、主要操作、服务器控制、选项；窄屏自动换行/缩短文案。"""
        self._header_frame = header = ttk.Frame(self.root, padding=8)
        header.pack(fill="x", padx=10, pady=(8, 2))
        header.columnconfigure(0, weight=0)
        header.columnconfigure(1, weight=1)
        header.columnconfigure(2, weight=0)
        header.columnconfigure(3, weight=0)

        # 品牌：标题和副标题放在同一行，节省高度
        brand = ttk.Frame(header)
        self._brand_frame = brand
        brand.grid(row=0, column=0, sticky="w")
        self._brand_title = ttk.Label(brand, text="ACM 出数据", font=("Segoe UI", 14, "bold"))
        self._brand_title.pack(side="left")
        self._header_subtitle = ttk.Label(
            brand, text="智能测试数据生成器", font=("Segoe UI", 8),
            bootstyle="secondary",
        )
        self._header_subtitle.pack(side="left", padx=(4, 0))

        # 主要操作：保存 / 历史 / 美化 / 提交 / 下载菜单
        act_box = ttk.Frame(header)
        self._act_frame = act_box
        act_box.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.btn_save_problem = ttk.Button(
            act_box, text="保存题目", bootstyle="outline-secondary", command=self.on_save_problem,
        )
        self.btn_save_problem.pack(side="left", padx=2)
        ToolTip(self.btn_save_problem, "保存标程/题面/输入/输出到题库（problems/）；换题时会提示另存或覆盖")
        self.btn_save_as_new = ttk.Button(
            act_box, text="另存为新题", bootstyle="outline-secondary",
            command=self.on_save_as_new_problem,
        )
        self.btn_save_as_new.pack(side="left", padx=2)
        ToolTip(self.btn_save_as_new, "始终新建题库条目，不覆盖当前关联的旧题")
        self.btn_new_problem = ttk.Button(
            act_box, text="新建题目", bootstyle="outline-secondary",
            command=self.on_new_problem,
        )
        self.btn_new_problem.pack(side="left", padx=2)
        ToolTip(self.btn_new_problem, "清空工作区并断开题库 ID，避免粘贴新题覆盖旧题")
        self.btn_open_history = ttk.Button(
            act_box, text="历史题目", bootstyle="outline-secondary",
            command=lambda: self._show_tab("history"),
        )
        self.btn_open_history.pack(side="left", padx=2)
        ToolTip(self.btn_open_history, "从已出过的题或 Job 加载到工作区")
        self.btn_beautify_all = ttk.Button(
            act_box, text="一键美化", bootstyle="outline-info", command=self.on_beautify_all,
        )
        self.btn_beautify_all.pack(side="left", padx=2)
        ToolTip(self.btn_beautify_all, "一键美化题面、输入描述、输出描述，直接保留")
        self.btn_submit = ttk.Button(
            act_box, text="提交生成", bootstyle="primary", command=self.on_submit,
        )
        self.btn_submit.pack(side="left", padx=2)
        ToolTip(self.btn_submit, "Ctrl+Enter 快捷提交")
        self._act_sep = ttk.Separator(act_box, orient="vertical")
        self._act_sep.pack(side="left", fill="y", padx=6)
        self.btn_download_menu = ttk.Menubutton(
            act_box, text="下载 ▼", state="disabled", bootstyle="success",
        )
        self.btn_download_menu.pack(side="left", padx=2)
        self.download_menu = tk.Menu(self.btn_download_menu, tearoff=0)
        self.btn_download_menu.config(menu=self.download_menu)
        self.download_menu.add_command(
            label="下载 zip", command=self.on_download, state="disabled",
        )
        self.download_menu.add_command(
            label="下载源码", command=self.on_download_sources, state="disabled",
        )
        self.download_menu.add_command(
            label="下载 checker", command=self.on_download_checker, state="disabled",
        )
        ToolTip(self.btn_download_menu, "下载生成的测例 zip、源码包或 checker")

        # 服务器控制：紧凑 Frame，去掉 Labelframe 外框/标题以节省横向空间
        srv_box = ttk.Frame(header)
        self._srv_frame = srv_box
        srv_box.grid(row=0, column=2, sticky="w", padx=(10, 6))
        self._srv_dot = ttk.Label(srv_box, text="●", font=("Segoe UI", 12), foreground="#adb5bd")
        self._srv_dot.pack(side="left", padx=(0, 3))
        self.btn_start_srv = ttk.Button(
            srv_box, text="启动", bootstyle="success", command=self.on_start_server,
        )
        self.btn_start_srv.pack(side="left", padx=1)
        ToolTip(self.btn_start_srv, "启动本地 FastAPI 后端（自动清理端口占用）")
        self.btn_kill_srv = ttk.Button(
            srv_box, text="停止", bootstyle="danger", command=self.on_kill_server,
        )
        self.btn_kill_srv.pack(side="left", padx=1)
        ToolTip(self.btn_kill_srv, "停止占用服务器端口的进程")
        self._srv_pid_lbl = ttk.Label(srv_box, textvariable=self.server_pid_var, bootstyle="secondary")
        self._srv_pid_lbl.pack(side="left", padx=4)

        # 选项
        self._opts_frame = opts = ttk.Frame(header)
        opts.grid(row=0, column=3, sticky="e")
        self._sj_chk = ttk.Checkbutton(opts, text="Special Judge", variable=self.special_judge_var)
        self._sj_chk.pack(side="left", padx=4)
        self._checker_lbl = ttk.Label(opts, text="Checker:")
        self._checker_lbl.pack(side="left", padx=(8, 0))
        self._checker_cb = ttk.Combobox(
            opts, textvariable=self.builtin_checker,
            values=BUILTIN_CHECKER_OPTIONS, width=14, state="readonly",
        )
        self._checker_cb.pack(side="left", padx=2)

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
        """左侧导航栏：可折叠，折叠后只显示编号。"""
        self._sidebar_collapsed = False

        head = ttk.Frame(parent)
        head.pack(fill="x", pady=(0, 8))
        self._sidebar_title = ttk.Label(
            head, text="工作区", font=("Segoe UI", 12, "bold"),
        )
        self._sidebar_title.pack(side="left", anchor="w")
        self.btn_sidebar_toggle = ttk.Button(
            head, text="«", width=2, bootstyle="outline-secondary",
            command=self._toggle_sidebar,
        )
        self.btn_sidebar_toggle.pack(side="right")
        ToolTip(self.btn_sidebar_toggle, "折叠/展开侧栏（窄屏自动收起）")

        nav_items = [
            ("std", "1. 标程"),
            ("stmt", "2. 题面"),
            ("range", "3. 输入描述"),
            ("output", "4. 输出描述"),
            ("plan", "5. 数据方案"),
            ("history", "6. 历史题目"),
            ("rag", "7. RAG 语料"),
        ]
        self._nav_labels = {}
        for key, text in nav_items:
            num = text.split(".", 1)[0]
            self._nav_labels[key] = (num, text)
            btn = ttk.Button(
                parent, text=text,
                command=lambda k=key: self._show_tab(k),
                bootstyle="outline-primary",
            )
            btn.pack(fill="x", pady=3)
            self.nav_buttons[key] = btn

        # 附加区整体放进一个容器，折叠时只隐藏这一个容器，内部布局不变
        self._sidebar_extra = ttk.Frame(parent)
        self._sidebar_extra.pack(fill="x")
        ttk.Separator(self._sidebar_extra, orient="horizontal").pack(fill="x", pady=14)
        ttk.Label(self._sidebar_extra, text="当前题目", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self._problem_title_lbl = ttk.Label(
            self._sidebar_extra, textvariable=self.problem_title_var,
            bootstyle="secondary", font=("Segoe UI", 8), wraplength=150,
        )
        self._problem_title_lbl.pack(anchor="w", pady=(2, 8))
        ttk.Label(self._sidebar_extra, text="快捷键", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(
            self._sidebar_extra, text="Ctrl+1~7 切换标签\nCtrl+S 保存题目\nCtrl+Enter 提交",
            bootstyle="secondary", font=("Segoe UI", 8),
        ).pack(anchor="w", pady=4)

    def _toggle_sidebar(self):
        self._sidebar_user_touched = True
        self._sidebar_collapsed = not self._sidebar_collapsed
        self._apply_sidebar_state()

    def _apply_sidebar_state(self):
        collapsed = self._sidebar_collapsed
        for key, btn in self.nav_buttons.items():
            num, full = self._nav_labels[key]
            btn.config(text=num if collapsed else full)
        if collapsed:
            self._sidebar_extra.pack_forget()
        else:
            self._sidebar_extra.pack(fill="x")
        self._sidebar_title.config(text="" if collapsed else "工作区")
        self.btn_sidebar_toggle.config(text="»" if collapsed else "«")
        try:
            if collapsed:
                self.sidebar.config(width=46)
            else:
                wide = getattr(self, "_layout_mode", "") == "wide"
                self.sidebar.config(width=200 if wide else 180)
        except tk.TclError:
            pass
        if self._problem_title_lbl is not None:
            try:
                self._problem_title_lbl.config(wraplength=40 if collapsed else 170)
            except tk.TclError:
                pass

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
        ttk.Label(
            cfg, textvariable=self.ptype, bootstyle="info", width=28,
        ).pack(side="left", padx=4)
        ttk.Label(
            cfg, text="（由大模型根据题面/标程自动判定，可多选；界面不可改）",
            bootstyle="secondary",
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
        lbl2 = ttk.Label(
            tip2, text="支持 Markdown / HTML / LaTeX；可美化排版",
            bootstyle="secondary",
        )
        lbl2.pack(side="left")
        self._tip_labels.append(lbl2)
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
        lbl3 = ttk.Label(
            tip3, text="支持 Markdown / HTML / LaTeX；可美化排版",
            bootstyle="secondary",
        )
        lbl3.pack(side="left")
        self._tip_labels.append(lbl3)
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
        lbl_out = ttk.Label(
            tip_output, text="支持 Markdown / HTML / LaTeX；可美化排版",
            bootstyle="secondary",
        )
        lbl_out.pack(side="left")
        self._tip_labels.append(lbl_out)
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
        self._plan_tip_label = ttk.Label(
            tip4, text="可先点「生成方案」：常规样例数由 AI 自定（≥15）；提交时审核 range（合理复用/不合理重写）",
            bootstyle="secondary",
        )
        self._plan_tip_label.pack(side="left")
        self._tip_labels.append(self._plan_tip_label)
        self.btn_propose = ttk.Button(
            tip4, text="用大模型生成方案", bootstyle="success", command=self.on_propose_range,
        )
        self.btn_propose.pack(side="right", padx=2)
        self.btn_clear_plan = ttk.Button(
            tip4, text="清空方案", bootstyle="outline-secondary", command=self.clear_range_plan,
        )
        self.btn_clear_plan.pack(side="right", padx=2)

        summary = ttk.Labelframe(tab_plan, text="方案概览", padding=6)
        summary.pack(fill="x", padx=4, pady=4)
        # 三行：样例数 / 时空限制 / 状态文案（状态行独占一行，避免被挤窄）
        summary.columnconfigure(4, weight=1)
        ttk.Label(summary, text="常规样例数：", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 4),
        )
        self.count_entry = ttk.Entry(summary, textvariable=self.count_var, width=8)
        self.count_entry.grid(row=0, column=1, sticky="w", padx=(0, 12))
        ttk.Label(summary, text="每方案样例数：", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=2, sticky="w", padx=(0, 4),
        )
        self.special_count_entry = ttk.Entry(summary, textvariable=self.special_count_var, width=8)
        self.special_count_entry.grid(row=0, column=3, sticky="w", padx=(0, 12))
        self.total_count_label = ttk.Label(summary, text="总样例数：—", bootstyle="secondary")
        self.total_count_label.grid(row=0, column=4, sticky="w")
        ttk.Label(summary, text="时限(ms)：", font=("Segoe UI", 10, "bold")).grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=(8, 0),
        )
        ttk.Entry(summary, textvariable=self.time_limit_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(0, 12), pady=(8, 0),
        )
        ttk.Label(summary, text="内存(MB)：", font=("Segoe UI", 10, "bold")).grid(
            row=1, column=2, sticky="w", padx=(0, 4), pady=(8, 0),
        )
        ttk.Entry(summary, textvariable=self.memory_limit_var, width=8).grid(
            row=1, column=3, sticky="w", padx=(0, 12), pady=(8, 0),
        )
        ttk.Label(summary, textvariable=self.plan_status, bootstyle="info").grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(8, 0),
        )

        special_frm = ttk.Labelframe(
            tab_plan,
            text="特殊情况提示（非空则挖 1 条；点「模式」可手改 mutate/build）",
            padding=4,
        )
        special_frm.pack(fill="x", padx=4, pady=(0, 2))
        # 用多行文本：单行 Entry 在中文 IME 未上屏时点按钮易读到空串，导致不挖方案
        self.special_desc_text = ScrolledText(
            special_frm, height=2, font=("Segoe UI", 10), wrap="word", autohide=True,
        )
        self.special_desc_text.pack(fill="x", expand=False, padx=2, pady=2)
        ttk.Checkbutton(
            special_frm,
            text="自动挖掘特殊方案（未填提示时由大模型产出 1 条）",
            variable=self.auto_discover_special_var,
            bootstyle="round-toggle",
        ).pack(anchor="w", padx=2, pady=(2, 0))

        # 下方可拖拽：上=特殊方案（优先高度），下=约束/边界
        self._plan_vpaned = ttk.Panedwindow(tab_plan, orient="vertical")
        self._plan_vpaned.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        schemes_frm = ttk.Labelframe(
            self._plan_vpaned,
            text="特殊方案（点「模式」切换；双击/右键编辑）",
            padding=6,
        )
        self._plan_vpaned.add(schemes_frm, weight=3)

        scheme_btns = ttk.Frame(schemes_frm)
        scheme_btns.pack(fill="x", pady=(0, 6))
        for text, style, cmd in (
            ("编辑", "info-outline", self._edit_selected_scheme),
            ("新增", "success-outline", self._add_scheme),
            ("复制", "secondary-outline", self._copy_selected_scheme),
            ("删除", "danger-outline", self._delete_selected_scheme),
        ):
            ttk.Button(
                scheme_btns, text=text, bootstyle=style, width=8, command=cmd,
            ).pack(side="left", padx=(0, 8))
        ttk.Label(
            scheme_btns,
            text="选中一行后操作；双击也可编辑",
            bootstyle="secondary",
        ).pack(side="right")

        scheme_body = ttk.Frame(schemes_frm)
        scheme_body.pack(fill="both", expand=True)
        self.scheme_tree = ttk.Treeview(
            scheme_body,
            columns=("sel", "title", "mode", "why", "samples"),
            show="headings",
            height=8,
            bootstyle="secondary",
        )
        self.scheme_tree.heading("sel", text="选")
        self.scheme_tree.heading("title", text="方案")
        self.scheme_tree.heading("mode", text="模式")
        self.scheme_tree.heading("why", text="理由")
        self.scheme_tree.heading("samples", text="样例数")
        self.scheme_tree.column("sel", width=40, anchor="center", stretch=False)
        self.scheme_tree.column("title", width=140, stretch=True)
        self.scheme_tree.column("mode", width=70, anchor="center", stretch=False)
        self.scheme_tree.column("why", width=280, stretch=True)
        self.scheme_tree.column("samples", width=60, anchor="center", stretch=False)
        scheme_scroll = ttk.Scrollbar(
            scheme_body, orient="vertical", command=self.scheme_tree.yview,
        )
        scheme_scroll.pack(side="right", fill="y")
        self.scheme_tree.pack(side="left", fill="both", expand=True, padx=(0, 2), pady=0)
        self.scheme_tree.configure(yscrollcommand=scheme_scroll.set)
        self.scheme_tree.bind("<Button-1>", self._on_scheme_tree_click)
        self.scheme_tree.bind("<Double-Button-1>", self._on_scheme_tree_dblclick)
        self.scheme_tree.bind("<Delete>", lambda _e: self._delete_selected_scheme())
        self.scheme_tree.bind("<Button-3>", self._on_scheme_tree_context_menu)

        # 绑定变更：每方案样例数或常规样例数变化时刷新总样例数显示
        self.count_var.trace_add("write", lambda *_: self._update_total_count_label())
        self.special_count_var.trace_add("write", lambda *_: self._on_per_scheme_samples_change())

        lower = ttk.Frame(self._plan_vpaned)
        self._plan_vpaned.add(lower, weight=2)
        panes = ttk.Panedwindow(lower, orient="horizontal")
        panes.pack(fill="both", expand=True)

        cons_frm = ttk.Labelframe(panes, text="变量约束 constraints", padding=4)
        panes.add(cons_frm, weight=1)
        cols = ("name", "lo", "hi")
        self.cons_tree = ttk.Treeview(
            cons_frm, columns=cols, show="headings", height=4, bootstyle="info",
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
        edge_ops = ttk.Frame(edge_frm)
        edge_ops.pack(fill="x", padx=2, pady=(0, 2))
        ttk.Button(
            edge_ops, text="删除选中", bootstyle="danger-outline", width=10,
            command=self._delete_selected_edge,
        ).pack(side="left")
        ttk.Label(
            edge_ops,
            text="中文说明 · 建议约 5 个 · Delete 可删",
            bootstyle="secondary",
        ).pack(side="left", padx=8)
        self.edge_list = ttk.Treeview(
            edge_frm,
            columns=("zh", "id"),
            show="headings",
            height=5,
            bootstyle="info",
        )
        self.edge_list.heading("zh", text="说明")
        self.edge_list.heading("id", text="标识")
        self.edge_list.column("zh", width=180, stretch=True)
        self.edge_list.column("id", width=140, stretch=True)
        self.edge_list.pack(fill="both", expand=True, padx=2, pady=2)
        self.edge_list.bind("<Delete>", lambda _e: self._delete_selected_edge())
        self.edge_list.bind("<BackSpace>", lambda _e: self._delete_selected_edge())

        self._plan_sash_inited = False

        def _init_plan_sash(_event=None):
            if self._plan_sash_inited:
                return
            try:
                h = int(self._plan_vpaned.winfo_height())
                if h >= 180:
                    # 默认把约 60% 高度留给特殊方案表（仅首次）
                    self._plan_vpaned.sashpos(0, max(140, int(h * 0.60)))
                    self._plan_sash_inited = True
            except tk.TclError:
                pass

        self._plan_vpaned.bind("<Map>", lambda e: self.root.after(50, _init_plan_sash), add="+")
        self.root.after(200, _init_plan_sash)

        self.range_data = None

        # 6) 历史题目
        tab_hist = _build_tab("history", "6. 历史题目")
        hist_tip = ttk.Frame(tab_hist)
        hist_tip.pack(fill="x", padx=4, pady=(4, 8))
        self._hist_tip_label = ttk.Label(
            hist_tip,
            text="选择已保存的题目或历史 Job，加载后会替换当前工作区的标程/题面/输入/输出",
            bootstyle="secondary",
        )
        self._hist_tip_label.pack(side="left")
        self._tip_labels.append(self._hist_tip_label)
        ttk.Button(
            hist_tip, text="刷新列表", bootstyle="outline-info", command=self.on_history_refresh,
        ).pack(side="right", padx=2)
        hist_body = ttk.Frame(tab_hist)
        hist_body.pack(fill="both", expand=True, padx=4, pady=4)
        cols_h = ("kind", "title", "lang", "type", "updated", "id")
        self.history_tree = ttk.Treeview(
            hist_body, columns=cols_h, show="headings", height=14, bootstyle="primary",
        )
        self.history_tree.heading("kind", text="来源")
        self.history_tree.heading("title", text="标题")
        self.history_tree.heading("lang", text="语言")
        self.history_tree.heading("type", text="题型")
        self.history_tree.heading("updated", text="更新时间")
        self.history_tree.heading("id", text="ID")
        self.history_tree.column("kind", width=70, anchor="center")
        self.history_tree.column("title", width=220)
        self.history_tree.column("lang", width=60, anchor="center")
        self.history_tree.column("type", width=100)
        self.history_tree.column("updated", width=150)
        self.history_tree.column("id", width=180)
        self.history_tree.pack(side="left", fill="both", expand=True)
        hist_scroll = ttk.Scrollbar(hist_body, orient="vertical", command=self.history_tree.yview)
        hist_scroll.pack(side="right", fill="y")
        self.history_tree.configure(yscrollcommand=hist_scroll.set)
        self.history_tree.bind("<Double-1>", lambda _e: self.on_history_load())
        self._history_rows = {}
        hist_ops = ttk.Frame(tab_hist)
        hist_ops.pack(fill="x", padx=4, pady=4)
        ttk.Button(
            hist_ops, text="加载到工作区", bootstyle="success", command=self.on_history_load,
        ).pack(side="left", padx=2)
        ttk.Button(
            hist_ops, text="保存当前为题目", bootstyle="outline-secondary", command=self.on_save_problem,
        ).pack(side="left", padx=2)
        ttk.Button(
            hist_ops, text="另存为新题", bootstyle="outline-secondary",
            command=self.on_save_as_new_problem,
        ).pack(side="left", padx=2)
        ttk.Button(
            hist_ops, text="新建题目", bootstyle="outline-secondary", command=self.on_new_problem,
        ).pack(side="left", padx=2)
        ttk.Button(
            hist_ops, text="删除题库条目", bootstyle="outline-danger", command=self.on_history_delete,
        ).pack(side="left", padx=2)
        self.history_status = tk.StringVar(value="点「刷新列表」查看已出题目与 Job")
        ttk.Label(hist_ops, textvariable=self.history_status, bootstyle="secondary").pack(
            side="left", padx=12
        )

        # 7) RAG 语料运营
        tab_rag = _build_tab("rag", "7. RAG 语料")
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
        self._rag_right = right
        right.pack(side="right", fill="both", padx=(8, 0))
        right.pack_propagate(False)
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

    def _init_main_sash(self):
        """首次布局：默认收起日志，工作区占满。"""
        try:
            h = int(self._vpaned.winfo_height())
            if h >= 300:
                if getattr(self, "_log_collapsed", True):
                    self._vpaned.sashpos(0, max(200, h - self._collapsed_bottom_h()))
                else:
                    self._vpaned.sashpos(0, int(h * getattr(self, "_log_expanded_ratio", 0.72)))
                self._last_sash_h = h
        except tk.TclError:
            pass

    def _collapsed_bottom_h(self) -> int:
        """日志收起时底部（状态+阶段）预估高度。"""
        try:
            bottom = self._bottom_frame
            bottom.update_idletasks()
            req = int(bottom.winfo_reqheight())
            return max(110, min(220, req + 8))
        except (tk.TclError, AttributeError):
            return 140

    def _build_bottom_panel(self):
        """底部状态栏、阶段条、可收缩日志面板（默认收起）。"""
        bottom = ttk.Frame(self._vpaned, padding=4)
        self._bottom_frame = bottom
        self._vpaned.add(bottom, weight=1)

        self._log_collapsed = True
        self._log_expanded_ratio = 0.72
        self._log_user_touched = False

        status_bar = ttk.Frame(bottom)
        status_bar.pack(fill="x", pady=(0, 4))
        ttk.Label(status_bar, text="状态：", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Label(status_bar, textvariable=self.status, bootstyle="info").pack(
            side="left", fill="x", expand=True,
        )
        self.btn_log_toggle = ttk.Button(
            status_bar, text="展开日志", bootstyle="outline-secondary",
            command=self._toggle_log_panel, width=10,
        )
        self.btn_log_toggle.pack(side="right", padx=2)
        ToolTip(self.btn_log_toggle, "展开/收起过程日志（提交生成时自动展开）")
        ttk.Button(
            status_bar, text="清空日志", bootstyle="outline-secondary",
            command=self._clear_log,
        ).pack(side="right", padx=2)

        stage_frm = ttk.Labelframe(bottom, text="流程阶段", padding=6)
        stage_frm.pack(fill="x", pady=4)
        self._stage_grid = stage_grid = ttk.Frame(stage_frm)
        stage_grid.pack(fill="x")
        # 阶段标签按需重排：宽屏单行、中屏两行、窄屏逐行
        self._stage_arrows = []
        self._stage_labels = []
        for key, label in STAGES:
            var = tk.StringVar(value=f"○ {label}")
            self.stage_vars[key] = var
            self._stage_labels.append((key, var, label))
        self._stage_cols = 0  # 强制首次重排
        self._relayout_stages(cols=5)  # 默认单行（5 个阶段）

        self._log_frm = log_frm = ttk.Labelframe(bottom, text="过程日志", padding=4)
        # 默认不 pack，保持收起
        self.progress = ScrolledText(
            log_frm, height=10, font=("Consolas", 9), wrap="word", autohide=True,
        )
        self.progress.pack(fill="both", expand=True, padx=2, pady=2)
        self.progress.tag_configure("phase", foreground="#0a5")
        self.progress.tag_configure("ok", foreground="#060")
        self.progress.tag_configure("err", foreground="#c00")
        self.progress.tag_configure("tool", foreground="#06c")
        self.progress.tag_configure("dim", foreground="#888")

    def _toggle_log_panel(self):
        self._log_user_touched = True
        self.set_log_collapsed(not self._log_collapsed)

    def set_log_collapsed(self, collapsed: bool, *, remember_ratio: bool = True):
        """收起/展开过程日志；同步主 sash。"""
        collapsed = bool(collapsed)
        was = getattr(self, "_log_collapsed", None)

        # 收起前记下当前展开比例
        if collapsed and remember_ratio and was is False:
            try:
                h = int(self._vpaned.winfo_height())
                pos = int(self._vpaned.sashpos(0))
                if h >= 300 and 80 < pos < h - 60:
                    self._log_expanded_ratio = pos / h
                    self._sash_ratio = self._log_expanded_ratio
            except tk.TclError:
                pass

        self._log_collapsed = collapsed
        try:
            if collapsed:
                self._log_frm.pack_forget()
                self.btn_log_toggle.config(text="展开日志")
            else:
                if not self._log_frm.winfo_ismapped():
                    self._log_frm.pack(fill="both", expand=True)
                self.btn_log_toggle.config(text="收起日志")
        except (tk.TclError, AttributeError):
            pass

        if was != collapsed:
            self.root.after(30, self._apply_log_sash)

    def _apply_log_sash(self):
        """按日志展开/收起调整主 sash。"""
        try:
            h = int(self._vpaned.winfo_height())
            if h < 300:
                return
            if self._log_collapsed:
                self._vpaned.sashpos(0, max(200, h - self._collapsed_bottom_h()))
            else:
                ratio = getattr(self, "_log_expanded_ratio", None) or getattr(self, "_sash_ratio", 0.72)
                ratio = min(0.88, max(0.45, float(ratio)))
                self._vpaned.sashpos(0, max(220, int(h * ratio)))
            self._last_sash_h = h
        except tk.TclError:
            pass

    def _relayout_stages(self, cols: int):
        """按每行 cols 个阶段重排流程阶段条；cols 变化时才真正重建。"""
        cols = max(1, int(cols))
        if cols == self._stage_cols:
            return
        self._stage_cols = cols
        # 清掉旧的箭头/标签控件
        for child in self._stage_grid.winfo_children():
            child.destroy()
        self._stage_arrows.clear()
        for i, (key, var, label) in enumerate(self._stage_labels):
            row, col_pos = divmod(i, cols)
            col = col_pos * 2
            if col_pos:
                arr = ttk.Label(self._stage_grid, text="→", bootstyle="secondary")
                arr.grid(row=row, column=col - 1, padx=4, sticky="w")
                self._stage_arrows.append(arr)
            ttk.Label(
                self._stage_grid, textvariable=var, font=("Consolas", 10),
            ).grid(row=row, column=col, sticky="w")

    def _on_root_configure(self, event):
        """窗口尺寸变化：防抖后走统一自适应。"""
        if event.widget is not self.root:
            return
        if self._configure_after_id is not None:
            try:
                self.root.after_cancel(self._configure_after_id)
            except tk.TclError:
                pass
        self._configure_after_id = self.root.after(80, self._force_adaptive_layout)

    def _force_adaptive_layout(self):
        self._configure_after_id = None
        try:
            w = int(self.root.winfo_width())
            h = int(self.root.winfo_height())
        except tk.TclError:
            return
        if w < 100 or h < 100:
            return
        # 尺寸几乎不变则跳过，避免 Configure 风暴
        lw, lh = self._last_layout_wh
        if abs(w - lw) < 8 and abs(h - lh) < 8 and self._layout_mode:
            return
        self._last_layout_wh = (w, h)
        self._apply_adaptive_layout(w, h)

    def _layout_mode_for(self, w: int) -> str:
        if w >= _BP_WIDE:
            return "wide"
        if w >= _BP_NORMAL:
            return "normal"
        if w >= _BP_COMPACT:
            return "compact"
        return "narrow"

    def _apply_adaptive_layout(self, w: int, h: int):
        """按窗口宽高统一调整：顶栏、侧栏、阶段条、表格列、sash、提示换行。"""
        mode = self._layout_mode_for(w)
        prev = self._layout_mode
        self._layout_mode = mode

        # 1) 侧栏：窄屏自动收起（用户手动点过则不再自动）
        if not self._sidebar_user_touched:
            want_collapse = mode in ("compact", "narrow")
            if want_collapse != self._sidebar_collapsed:
                self._sidebar_collapsed = want_collapse
                self._apply_sidebar_state()
        # 侧栏展开宽度随模式微调
        if not self._sidebar_collapsed:
            try:
                self.sidebar.config(width=200 if mode == "wide" else 180)
            except tk.TclError:
                pass
        if self._problem_title_lbl is not None:
            try:
                self._problem_title_lbl.config(
                    wraplength=170 if not self._sidebar_collapsed else 40,
                )
            except tk.TclError:
                pass

        # 2) 顶栏：文案缩短 + 换行
        self._adapt_header(mode)

        # 3) 阶段条列数
        if w >= _BP_NORMAL:
            self._relayout_stages(5)
        elif w >= _BP_NARROW:
            self._relayout_stages(3)
        else:
            self._relayout_stages(2)

        # 4) 提示文案 wraplength
        content_w = max(200, w - (56 if self._sidebar_collapsed else 210) - 48)
        for lbl in self._tip_labels:
            try:
                lbl.config(wraplength=max(160, content_w - 180))
            except tk.TclError:
                pass

        # 5) 方案/历史按钮文案
        try:
            if mode == "narrow":
                self.btn_propose.config(text="生成方案")
                self.btn_clear_plan.config(text="清空")
            else:
                self.btn_propose.config(text="用大模型生成方案")
                self.btn_clear_plan.config(text="清空方案")
        except (tk.TclError, AttributeError):
            pass

        # 6) 表格列宽 + RAG 预览宽度
        self._adapt_tree_columns(content_w, mode)

        # 7) 主 sash：日志收起时贴底；展开时保持比例
        try:
            pane_h = int(self._vpaned.winfo_height())
            last = self._last_sash_h
            if pane_h >= 300:
                if getattr(self, "_log_collapsed", True):
                    if abs(pane_h - last) > 40 or prev != mode or last == 0:
                        self._vpaned.sashpos(0, max(200, pane_h - self._collapsed_bottom_h()))
                        self._last_sash_h = pane_h
                else:
                    if last > 0:
                        try:
                            pos = int(self._vpaned.sashpos(0))
                            if 80 < pos < pane_h - 80 and last >= 300:
                                self._sash_ratio = pos / last
                                self._log_expanded_ratio = self._sash_ratio
                        except tk.TclError:
                            pass
                    elif mode in ("compact", "narrow"):
                        self._sash_ratio = 0.78
                    else:
                        self._sash_ratio = 0.72
                    if abs(pane_h - last) > 40 or prev != mode or last == 0:
                        ratio = getattr(self, "_log_expanded_ratio", None) or self._sash_ratio
                        self._vpaned.sashpos(0, max(220, int(pane_h * ratio)))
                        self._last_sash_h = pane_h
        except tk.TclError:
            pass

    def _adapt_header(self, mode: str):
        """顶栏：副标题显隐、按钮短文案、opts/srv 换行。"""
        header = self._header_frame
        opts = self._opts_frame
        srv = self._srv_frame
        act = self._act_frame

        # 副标题 / 品牌字号
        try:
            if mode == "wide":
                self._header_subtitle.pack(side="left", padx=(4, 0))
                self._brand_title.config(font=("Segoe UI", 14, "bold"))
            else:
                self._header_subtitle.pack_forget()
                self._brand_title.config(
                    font=("Segoe UI", 12, "bold") if mode == "narrow" else ("Segoe UI", 14, "bold"),
                )
        except tk.TclError:
            pass

        # 按钮文案
        compact = mode in ("compact", "narrow")
        try:
            self.btn_save_problem.config(text="保存" if compact else "保存题目")
            self.btn_save_as_new.config(text="另存" if compact else "另存为新题")
            self.btn_new_problem.config(text="新建" if compact else "新建题目")
            self.btn_open_history.config(text="历史" if compact else "历史题目")
            self.btn_beautify_all.config(text="美化" if compact else "一键美化")
            self.btn_submit.config(text="提交" if compact else "提交生成")
            self.btn_download_menu.config(text="下载" if mode == "narrow" else "下载 ▼")
            self._sj_chk.config(text="SPJ" if compact else "Special Judge")
            self._checker_lbl.config(text="Ck:" if mode == "narrow" else "Checker:")
            self._checker_cb.config(width=10 if mode == "narrow" else 14)
        except (tk.TclError, AttributeError):
            pass

        # 布局行：wide/normal 单行；compact opts 第二行；narrow srv+opts 第二行
        try:
            self._brand_frame.grid(row=0, column=0, sticky="w")
            if mode == "narrow":
                act.grid(row=0, column=1, columnspan=3, sticky="w", padx=(8, 0))
                srv.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
                opts.grid(row=1, column=2, columnspan=2, sticky="e", pady=(6, 0))
                try:
                    self._srv_pid_lbl.pack_forget()
                except tk.TclError:
                    pass
            elif mode == "compact":
                act.grid(row=0, column=1, sticky="w", padx=(12, 0))
                srv.grid(row=0, column=2, sticky="w", padx=(10, 6))
                opts.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 0))
                try:
                    if not self._srv_pid_lbl.winfo_ismapped():
                        self._srv_pid_lbl.pack(side="left", padx=4)
                except tk.TclError:
                    pass
            else:
                act.grid(row=0, column=1, sticky="w", padx=(12, 0))
                srv.grid(row=0, column=2, sticky="w", padx=(10, 6))
                opts.grid(row=0, column=3, sticky="e")
                try:
                    if not self._srv_pid_lbl.winfo_ismapped():
                        self._srv_pid_lbl.pack(side="left", padx=4)
                except tk.TclError:
                    pass
        except (tk.TclError, AttributeError):
            pass
        # 避免未使用告警（header 供后续扩展）
        _ = header

    def _adapt_tree_columns(self, content_w: int, mode: str):
        """按内容区宽度缩放主要 Treeview 列。"""
        cw = max(320, content_w)

        def _cols(tree, specs: dict[str, tuple[int, bool]]):
            """specs: col -> (prefer_width, stretch)"""
            total_fixed = sum(w for w, stretch in specs.values() if not stretch)
            stretch_keys = [k for k, (_, stretch) in specs.items() if stretch]
            remain = max(80, cw - total_fixed - 24)
            stretch_sum = sum(specs[k][0] for k in stretch_keys) or 1
            for key, (pref, stretch) in specs.items():
                if stretch:
                    width = max(48, int(remain * pref / stretch_sum))
                else:
                    width = pref if mode != "narrow" else max(36, int(pref * 0.85))
                try:
                    tree.column(key, width=width)
                except tk.TclError:
                    pass

        if hasattr(self, "scheme_tree"):
            _cols(self.scheme_tree, {
                "sel": (40, False),
                "title": (140, True),
                "mode": (70, False),
                "why": (280, True),
                "samples": (60, False),
            })
        if hasattr(self, "history_tree"):
            _cols(self.history_tree, {
                "kind": (60, False),
                "title": (220, True),
                "lang": (56, False),
                "type": (90, False),
                "updated": (140, False),
                "id": (160, True),
            })
        if hasattr(self, "rag_tree"):
            _cols(self.rag_tree, {
                "key": (110, True),
                "source": (64, False),
                "type": (90, False),
                "rate": (64, False),
                "disabled": (48, False),
                "time": (130, True),
            })
        if hasattr(self, "cons_tree"):
            col_w = max(80, min(160, cw // 6))
            for c in ("name", "lo", "hi"):
                try:
                    self.cons_tree.column(c, width=col_w)
                except tk.TclError:
                    pass

        # RAG 右侧预览：窄屏收窄，极窄隐藏
        right = self._rag_right
        if right is not None:
            try:
                if mode == "narrow":
                    right.pack_forget()
                else:
                    if not right.winfo_ismapped():
                        right.pack(side="right", fill="both", padx=(8, 0))
                    right.config(width=340 if mode == "wide" else (280 if mode == "normal" else 220))
            except tk.TclError:
                pass

    def place_dialog(
        self,
        win: tk.Toplevel,
        prefer_w: int,
        prefer_h: int,
        *,
        min_w: int | None = None,
        min_h: int | None = None,
        grab: bool = False,
    ):
        """按屏幕裁剪弹窗尺寸并相对主窗口居中。"""
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        # 相对主窗口再略收一点，避免弹窗比主界面还大
        try:
            parent_w = max(400, int(self.root.winfo_width()))
            parent_h = max(300, int(self.root.winfo_height()))
            prefer_w = min(prefer_w, parent_w - 40)
            prefer_h = min(prefer_h, parent_h - 40)
        except tk.TclError:
            pass
        mw = min_w if min_w is not None else max(320, int(prefer_w * 0.7))
        mh = min_h if min_h is not None else max(240, int(prefer_h * 0.7))
        w, h, _, _ = _fit_to_screen(
            prefer_w, prefer_h, screen_w, screen_h, margin=32, min_w=mw, min_h=mh,
        )
        try:
            px = self.root.winfo_rootx()
            py = self.root.winfo_rooty()
            pw = self.root.winfo_width()
            ph = self.root.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 2)
        except tk.TclError:
            x = max(0, (screen_w - w) // 2)
            y = max(0, (screen_h - h) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.minsize(mw, mh)
        win.transient(self.root)
        if grab:
            try:
                win.grab_set()
            except tk.TclError:
                pass

    def _bind_shortcuts(self):
        tab_map = {
            "1": "std",
            "2": "stmt",
            "3": "range",
            "4": "output",
            "5": "plan",
            "6": "history",
            "7": "rag",
        }
        for digit, name in tab_map.items():
            self.root.bind(f"<Control-Key-{digit}>", lambda _e, n=name: self._show_tab(n))
        self.root.bind("<Control-Return>", lambda _e: self.on_submit())
        self.root.bind("<Control-s>", lambda _e: self.on_save_problem())
        self.root.bind("<Control-S>", lambda _e: self.on_save_problem())

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
        if tab_name == "history":
            self.on_history_refresh()

    def _clear_log(self):
        self.progress.delete("1.0", "end")
        self._seen_progress = 0

    def _update_server_indicator(self, running: bool):
        color = "#28a745" if running else "#adb5bd"
        self._srv_dot.config(foreground=color)

    def _call_text_rewrite(self, text: str, kind: str, mode: str, extra_hint: str = "") -> str:
        """调用 /text/simplify 或 /text/beautify，返回改写后的文本。"""
        return call_text_rewrite(text, kind, mode, extra_hint)

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
        self.place_dialog(win, 1000, 780, min_w=640, min_h=480)

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
    def _selected_special_count(self) -> int:
        """选中方案 × 每方案样例数。"""
        try:
            per = int(self.special_count_var.get().strip() or "1")
        except ValueError:
            per = 5
        per = max(1, per)
        n = 0
        for s in self.special_schemes:
            if s.get("selected", True):
                try:
                    n += max(1, int(s.get("samples_per_scheme") or per))
                except (TypeError, ValueError):
                    n += per
        return n

    def _update_total_count_label(self):
        """根据常规样例数与选中特殊方案刷新总样例数标签。"""
        try:
            regular = int(self.count_var.get().strip() or "0")
        except ValueError:
            regular = 0
        special = self._selected_special_count()
        self.total_count_label.config(
            text=f"特殊 {special} · 总样例数：{regular + special}"
        )

    def _on_per_scheme_samples_change(self):
        """每方案样例数变更时，同步到各方案并刷新列表/总数。"""
        try:
            per = max(1, int(self.special_count_var.get().strip() or "1"))
        except ValueError:
            per = 5
        for s in self.special_schemes:
            s["samples_per_scheme"] = per
        self._refresh_scheme_tree()
        self._update_total_count_label()

    def _scheme_mode_label(self, scheme: dict) -> str:
        mode = str(scheme.get("construct_mode") or "build").strip().lower()
        if mode == "mute":
            mode = "mutate"
        return "mutate" if mode == "mutate" else "build"

    def _sanitize_scheme_id(self, raw: str, fallback: str = "scheme") -> str:
        sid = re.sub(r"[^a-zA-Z0-9_]", "_", (raw or "").strip()).lower().strip("_")
        return sid or fallback

    def _unique_scheme_id(self, base: str, exclude_index: int | None = None) -> str:
        base = self._sanitize_scheme_id(base, "scheme")
        existing = {
            str(s.get("id") or "")
            for i, s in enumerate(self.special_schemes)
            if exclude_index is None or i != exclude_index
        }
        if base not in existing:
            return base
        n = 2
        while f"{base}_{n}" in existing:
            n += 1
        return f"{base}_{n}"

    def _scheme_index_from_iid(self, iid: str) -> int | None:
        try:
            idx = int(iid)
        except (TypeError, ValueError):
            return None
        if 0 <= idx < len(self.special_schemes):
            return idx
        return None

    def _selected_scheme_index(self) -> int | None:
        sel = self.scheme_tree.selection() if hasattr(self, "scheme_tree") else ()
        if not sel:
            return None
        return self._scheme_index_from_iid(sel[0])

    def _refresh_scheme_tree(self):
        if not hasattr(self, "scheme_tree"):
            return
        for i in self.scheme_tree.get_children():
            self.scheme_tree.delete(i)
        try:
            per = max(1, int(self.special_count_var.get().strip() or "1"))
        except ValueError:
            per = 5
        for i, s in enumerate(self.special_schemes):
            if "construct_mode" not in s:
                s["construct_mode"] = "build"
            sel = "☑" if s.get("selected", True) else "☐"
            try:
                samples = int(s.get("samples_per_scheme") or per)
            except (TypeError, ValueError):
                samples = per
            self.scheme_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    sel,
                    s.get("title") or s.get("id"),
                    self._scheme_mode_label(s),
                    (s.get("why") or "")[:80],
                    samples,
                ),
            )

    def _on_scheme_tree_click(self, event):
        """单击：选中行；点「选」列切换勾选；点「模式」列切换 mutate/build。"""
        row = self.scheme_tree.identify_row(event.y)
        if not row:
            return
        idx = self._scheme_index_from_iid(row)
        if idx is None:
            return
        col = self.scheme_tree.identify_column(event.x)
        # columns: #1=sel #2=title #3=mode #4=why #5=samples
        s = self.special_schemes[idx]
        changed = False
        if col == "#1":
            s["selected"] = not bool(s.get("selected", True))
            changed = True
        elif col == "#3":
            cur = self._scheme_mode_label(s)
            s["construct_mode"] = "build" if cur == "mutate" else "mutate"
            changed = True
        if changed:
            self._refresh_scheme_tree()
            self._update_total_count_label()
        try:
            self.scheme_tree.selection_set(str(idx))
        except tk.TclError:
            pass

    def _on_scheme_tree_dblclick(self, event):
        """双击打开方案编辑对话框。"""
        row = self.scheme_tree.identify_row(event.y)
        if not row:
            return
        idx = self._scheme_index_from_iid(row)
        if idx is None:
            return
        self._open_scheme_editor(idx)

    def _on_scheme_tree_context_menu(self, event):
        """右键菜单：编辑 / 新增 / 复制 / 删除。"""
        row = self.scheme_tree.identify_row(event.y)
        if row:
            try:
                self.scheme_tree.selection_set(row)
                self.scheme_tree.focus(row)
            except tk.TclError:
                pass
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="编辑", command=self._edit_selected_scheme)
        menu.add_command(label="新增", command=self._add_scheme)
        menu.add_command(label="复制", command=self._copy_selected_scheme)
        menu.add_separator()
        menu.add_command(label="删除", command=self._delete_selected_scheme)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _edit_selected_scheme(self):
        idx = self._selected_scheme_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选中一条特殊方案（或双击该行）")
            return
        self._open_scheme_editor(idx)

    def _add_scheme(self):
        try:
            per = max(1, int(self.special_count_var.get().strip() or "1"))
        except ValueError:
            per = 5
        sid = self._unique_scheme_id("custom_scheme")
        scheme = {
            "id": sid,
            "title": "自定义特殊方案",
            "why": "用户手动添加",
            "must_hold": ["满足自定义特殊条件"],
            "construct_hint": "",
            "construct_mode": "build",
            "source": "user_hint",
            "priority": len(self.special_schemes) + 1,
            "selected": True,
            "samples_per_scheme": per,
        }
        self.special_schemes.append(scheme)
        if self.range_data is None:
            self.range_data = {
                "count": 0,
                "constraints": {},
                "edge_cases": [],
                "special_schemes": self.special_schemes,
            }
        else:
            self.range_data["special_schemes"] = self.special_schemes
        self._refresh_scheme_tree()
        self._update_total_count_label()
        new_idx = len(self.special_schemes) - 1
        try:
            self.scheme_tree.selection_set(str(new_idx))
            self.scheme_tree.see(str(new_idx))
        except tk.TclError:
            pass
        self._open_scheme_editor(new_idx)

    def _copy_selected_scheme(self):
        idx = self._selected_scheme_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选中要复制的方案")
            return
        src = dict(self.special_schemes[idx])
        src["id"] = self._unique_scheme_id(str(src.get("id") or "scheme") + "_copy")
        src["title"] = (str(src.get("title") or src["id"]) + "（副本）")[:80]
        src["must_hold"] = list(src.get("must_hold") or [])
        src["selected"] = True
        self.special_schemes.append(src)
        if isinstance(self.range_data, dict):
            self.range_data["special_schemes"] = self.special_schemes
        self._refresh_scheme_tree()
        self._update_total_count_label()
        new_idx = len(self.special_schemes) - 1
        try:
            self.scheme_tree.selection_set(str(new_idx))
        except tk.TclError:
            pass

    def _delete_selected_scheme(self):
        idx = self._selected_scheme_index()
        if idx is None:
            messagebox.showinfo("提示", "请先选中要删除的方案")
            return
        s = self.special_schemes[idx]
        title = s.get("title") or s.get("id") or f"#{idx}"
        if not messagebox.askyesno("确认删除", f"删除特殊方案「{title}」？"):
            return
        self.special_schemes.pop(idx)
        if isinstance(self.range_data, dict):
            self.range_data["special_schemes"] = self.special_schemes
        self._refresh_scheme_tree()
        self._update_total_count_label()

    def _open_scheme_editor(self, index: int):
        """弹窗编辑 special_schemes[index]；保存后写回列表。"""
        if not (0 <= index < len(self.special_schemes)):
            return
        scheme = self.special_schemes[index]
        win = tk.Toplevel(self.root)
        win.title(f"编辑特殊方案 — {scheme.get('id') or index}")
        self.place_dialog(win, 640, 560, min_w=480, min_h=420, grab=True)

        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)

        id_var = tk.StringVar(value=str(scheme.get("id") or ""))
        title_var = tk.StringVar(value=str(scheme.get("title") or ""))
        mode_var = tk.StringVar(value=self._scheme_mode_label(scheme))
        samples_var = tk.StringVar(value=str(scheme.get("samples_per_scheme") or 1))
        selected_var = tk.BooleanVar(value=bool(scheme.get("selected", True)))
        source_var = tk.StringVar(value=str(scheme.get("source") or "user_hint"))

        def _row(label: str, row: int, widget):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="ne", padx=(0, 8), pady=4)
            widget.grid(row=row, column=1, sticky="ew", pady=4)

        frm.columnconfigure(1, weight=1)
        _row("id", 0, ttk.Entry(frm, textvariable=id_var))
        _row("标题", 1, ttk.Entry(frm, textvariable=title_var))
        mode_cb = ttk.Combobox(
            frm, textvariable=mode_var, values=("mutate", "build"), state="readonly", width=12,
        )
        _row("模式", 2, mode_cb)
        _row("样例数", 3, ttk.Entry(frm, textvariable=samples_var, width=10))
        _row("来源", 4, ttk.Entry(frm, textvariable=source_var))
        ttk.Checkbutton(frm, text="启用（selected）", variable=selected_var).grid(
            row=5, column=1, sticky="w", pady=4,
        )

        ttk.Label(frm, text="理由 why").grid(row=6, column=0, sticky="ne", padx=(0, 8), pady=4)
        why_box = ScrolledText(frm, height=3, font=("Segoe UI", 9), wrap="word", autohide=True)
        why_box.grid(row=6, column=1, sticky="nsew", pady=4)
        why_box.insert("1.0", str(scheme.get("why") or ""))

        ttk.Label(frm, text="must_hold\n(每行一条)").grid(
            row=7, column=0, sticky="ne", padx=(0, 8), pady=4,
        )
        must_box = ScrolledText(frm, height=5, font=("Consolas", 9), wrap="word", autohide=True)
        must_box.grid(row=7, column=1, sticky="nsew", pady=4)
        must_lines = scheme.get("must_hold") or []
        if isinstance(must_lines, str):
            must_lines = [must_lines]
        must_box.insert("1.0", "\n".join(str(x) for x in must_lines if str(x).strip()))

        ttk.Label(frm, text="构造提示\nconstruct_hint").grid(
            row=8, column=0, sticky="ne", padx=(0, 8), pady=4,
        )
        hint_box = ScrolledText(frm, height=5, font=("Consolas", 9), wrap="word", autohide=True)
        hint_box.grid(row=8, column=1, sticky="nsew", pady=4)
        hint_box.insert("1.0", str(scheme.get("construct_hint") or ""))

        frm.rowconfigure(6, weight=1)
        frm.rowconfigure(7, weight=2)
        frm.rowconfigure(8, weight=2)

        tip = ttk.Label(
            frm,
            text="must_hold / construct_hint / 模式会直接影响 SpecialCoder 与 property_check",
            bootstyle="secondary",
        )
        tip.grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))

        btns = ttk.Frame(win, padding=8)
        btns.pack(fill="x")

        def on_save():
            new_id = self._unique_scheme_id(id_var.get(), exclude_index=index)
            title = title_var.get().strip() or new_id
            mode = mode_var.get().strip().lower()
            if mode not in ("mutate", "build"):
                mode = "build"
            try:
                samples = max(1, int(samples_var.get().strip() or "1"))
            except ValueError:
                messagebox.showwarning("提示", "样例数必须是正整数", parent=win)
                return
            must_hold = [
                ln.strip()
                for ln in must_box.get("1.0", "end").splitlines()
                if ln.strip()
            ]
            if not must_hold:
                must_hold = [title]
            why = why_box.get("1.0", "end").strip()
            hint = hint_box.get("1.0", "end").strip()
            scheme.update({
                "id": new_id,
                "title": title,
                "why": why,
                "must_hold": must_hold,
                "construct_hint": hint,
                "construct_mode": mode,
                "source": source_var.get().strip() or "user_hint",
                "selected": bool(selected_var.get()),
                "samples_per_scheme": samples,
            })
            if isinstance(self.range_data, dict):
                self.range_data["special_schemes"] = self.special_schemes
            self._refresh_scheme_tree()
            self._update_total_count_label()
            try:
                self.scheme_tree.selection_set(str(index))
            except tk.TclError:
                pass
            self.plan_status.set("已更新特殊方案 — 提交时将使用编辑后的 schemes")
            win.destroy()

        ttk.Button(btns, text="保存", bootstyle="success", command=on_save).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", bootstyle="secondary", command=win.destroy).pack(side="right", padx=4)

        win.bind("<Escape>", lambda *_: win.destroy())
        win.focus_set()

    def _flush_focus(self):
        """强制结束 IME 组合态，避免点按钮时输入框内容尚未提交。"""
        try:
            self.root.focus_set()
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def _get_special_desc(self) -> str:
        """读取特殊情况提示（优先多行文本控件）。"""
        self._flush_focus()
        if hasattr(self, "special_desc_text"):
            try:
                return self.special_desc_text.get("1.0", "end").strip()
            except tk.TclError:
                pass
        return (self.special_desc_var.get() or "").strip()

    def _set_special_desc(self, text: str) -> None:
        text = text or ""
        self.special_desc_var.set(text)
        if hasattr(self, "special_desc_text"):
            try:
                self.special_desc_text.delete("1.0", "end")
                if text:
                    self.special_desc_text.insert("1.0", text)
            except tk.TclError:
                pass

    def _fallback_user_schemes(self, hint: str, per: int = 5) -> list[dict]:
        """服务端未返回方案时，本地补 1 条（mode 由启发式选，可再手改）。"""
        from server.special_discover import fallback_user_scheme

        return fallback_user_scheme(hint, max(1, int(per or 1)))

    def clear_range_plan(self):
        self.range_data = None
        self.special_schemes = []
        self.count_var.set("")
        self.special_count_var.set("1")
        self.time_limit_var.set("5000")
        self.memory_limit_var.set("1024")
        self._set_special_desc("")
        self.auto_discover_special_var.set(False)
        self._set_detected_ptype("")
        for i in self.cons_tree.get_children():
            self.cons_tree.delete(i)
        self._clear_edge_list_ui()
        self._refresh_scheme_tree()
        self.plan_status.set("尚未生成方案 — 提交时将从「写 range」开始")
        self._update_total_count_label()

    def _clear_edge_list_ui(self):
        for i in self.edge_list.get_children():
            self.edge_list.delete(i)

    def _refresh_edge_list_ui(self, edges: list | None = None):
        """把 edge_cases 刷到界面（中文说明 + 英文 id）。"""
        self._clear_edge_list_ui()
        src = edges
        if src is None and isinstance(self.range_data, dict):
            src = self.range_data.get("edge_cases") or []
        for e in src or []:
            if not isinstance(e, str) or not e.strip():
                continue
            eid = e.strip()
            # Treeview iid 不能有空格等；边界名一般是标识符
            iid = eid.replace(" ", "_")
            try:
                self.edge_list.insert(
                    "", "end", iid=iid, values=(_edge_case_zh(eid), eid),
                )
            except tk.TclError:
                # iid 冲突时跳过重复
                continue

    def _collect_edges_from_ui(self) -> list[str]:
        """从界面读回英文 edge id 列表。"""
        out: list[str] = []
        for item in self.edge_list.get_children():
            vals = self.edge_list.item(item, "values") or ()
            eid = ""
            if len(vals) >= 2 and str(vals[1]).strip():
                eid = str(vals[1]).strip()
            elif item:
                eid = str(item).strip()
            if eid and eid not in out:
                out.append(eid)
        return out

    def _delete_selected_edge(self):
        """删除选中的边界类型（可多选）。"""
        sel = list(self.edge_list.selection() or ())
        if not sel:
            messagebox.showinfo("提示", "请先选中要删除的边界类型")
            return
        for item in sel:
            try:
                self.edge_list.delete(item)
            except tk.TclError:
                pass
        edges = self._collect_edges_from_ui()
        if isinstance(self.range_data, dict):
            self.range_data["edge_cases"] = edges
        n = len(edges)
        self.plan_status.set(f"已更新边界类型：当前 {n} 种（提交将使用界面列表）")

    @staticmethod
    def _parse_positive_int(raw: str):
        s = (raw or "").strip()
        if not s:
            return None
        try:
            v = int(s)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def apply_range_plan(self, data: dict, problem_type: str | list[str] = "", schemes: list | None = None):
        """把 range dict 填到可视化控件；若带 problem_type 则同步题型下拉框。

        count 在 range.json 中代表总样例数；UI 的 count_entry 代表常规样例数。
        """
        try:
            from pipeline.gen_data import normalize_range_json
            data = normalize_range_json(dict(data or {}))
        except Exception:
            data = dict(data or {})
        raw_pt = problem_type or data.get("problem_type") or ""
        if isinstance(raw_pt, list):
            ptype = ", ".join(str(x) for x in raw_pt if x)
        else:
            ptype = str(raw_pt).strip()
        total_count = int(data.get("count") or 15)
        special_count = int(data.get("special_samples_count") or 0)
        special_desc = (data.get("special_samples_desc") or "").strip()
        prev_desc = self._get_special_desc()
        # 服务端未带回提示时，保留用户已填内容（避免生成后输入框被清空）
        if not special_desc or special_desc == "（自动挖掘特殊方案）":
            special_desc_for_box = prev_desc
            if special_desc != "（自动挖掘特殊方案）":
                special_desc = prev_desc or special_desc
        else:
            special_desc_for_box = special_desc

        schemes_in = schemes if schemes is not None else data.get("special_schemes")
        self.special_schemes = [dict(s) for s in (schemes_in or []) if isinstance(s, dict)]
        # 以界面「每方案样例数」为准；勿被旧题库 schemes 里的 5 反向抬高
        try:
            from server.special_discover import SAMPLES_PER_SCHEME_CAP
            per = max(1, min(int(self.special_count_var.get().strip() or "1"), SAMPLES_PER_SCHEME_CAP))
        except ValueError:
            per = 1

        if self.special_schemes:
            self.special_count_var.set(str(per))
            for s in self.special_schemes:
                s["samples_per_scheme"] = per
                mode = str(s.get("construct_mode") or "build").strip().lower()
                s["construct_mode"] = "mutate" if mode == "mutate" else "build"
            special_count = self._selected_special_count()
            regular_count = max(0, total_count - special_count)
        else:
            if not self.special_count_var.get().strip():
                self.special_count_var.set(str(per))
            # 服务端只出了常规 range、未挖方案：有用户提示则本地补 1 条
            if special_desc_for_box:
                self.special_schemes = self._fallback_user_schemes(special_desc_for_box, per)
                special_count = self._selected_special_count()
                # total_count 此时通常不含特殊，整段视为常规
                regular_count = max(1, total_count)
            else:
                regular_count = max(0, total_count - special_count)

        time_limit = self._parse_positive_int(str(data.get("time_limit_ms") or "")) or 5000
        memory_limit = self._parse_positive_int(str(data.get("memory_limit_mb") or "")) or 1024
        special_constraints = [
            str(x).strip() for x in (data.get("special_constraints") or []) if str(x).strip()
        ]
        raw_edges = list(data.get("edge_cases") or [])
        edges = _trim_edge_cases(raw_edges, EDGE_CASE_UI_LIMIT)
        self.range_data = {
            "count": regular_count + special_count,
            "constraints": dict(data.get("constraints") or {}),
            "edge_cases": edges,
            "special_samples_count": special_count,
            "special_samples_desc": special_desc_for_box or special_desc,
            "special_schemes": self.special_schemes,
            "auto_discover_special": bool(data.get("auto_discover_special")),
            "special_constraints": special_constraints,
            "time_limit_ms": time_limit,
            "memory_limit_mb": memory_limit,
        }
        if "auto_discover_special" in data:
            self.auto_discover_special_var.set(bool(data.get("auto_discover_special")))
        if ptype:
            self.range_data["problem_type"] = ptype
            self._set_detected_ptype(ptype)
        else:
            self._set_detected_ptype("")
        self.count_var.set(str(regular_count))
        self.time_limit_var.set(str(time_limit))
        self.memory_limit_var.set(str(memory_limit))
        self._set_special_desc(special_desc_for_box)
        for i in self.cons_tree.get_children():
            self.cons_tree.delete(i)
        for name, bounds in self.range_data["constraints"].items():
            lo, hi = bounds[0], bounds[1] if isinstance(bounds, (list, tuple)) and len(bounds) >= 2 else ("?", "?")
            self.cons_tree.insert("", "end", values=(name, lo, hi))
        self._refresh_edge_list_ui(self.range_data["edge_cases"])
        self._refresh_scheme_tree()
        self._update_total_count_label()
        type_s = f" · 题型 {ptype}" if ptype else ""
        total = self.range_data["count"]
        special = self.range_data.get("special_samples_count", 0)
        regular = total - special
        n_sel = sum(1 for s in self.special_schemes if s.get("selected", True))
        special_s = (
            f" · 常规 {regular} + 特殊方案 {n_sel}× = {special} → 总 {total}"
            if special or self.special_schemes
            else ""
        )
        limit_s = f" · 时限 {time_limit}ms · 内存 {memory_limit}MB"
        trim_note = ""
        if len(raw_edges) > len(edges):
            trim_note = f"（已从 {len(raw_edges)} 精简为 {len(edges)}，可再删）"
        self.plan_status.set(
            f"已就绪：总 {total} 组{special_s}{limit_s} · "
            f"{len(self.range_data['constraints'])} 个变量 · "
            f"{len(self.range_data['edge_cases'])} 种边界{trim_note}{type_s} — 提交时将审核该方案（合理复用/不合理重写）"
        )

    def collect_range_from_ui(self):
        """从可视化控件读回 dict；若无有效方案返回 None。

        UI 中 count_entry 是常规样例数，返回的 range.json 中 count 是总样例数。
        """
        if self.range_data is None and not self.cons_tree.get_children():
            return None
        try:
            regular_count = int(self.count_var.get().strip() or "0")
        except ValueError:
            regular_count = 0
        try:
            per = max(1, int(self.special_count_var.get().strip() or "1"))
        except ValueError:
            per = 5
        special_desc = self._get_special_desc()
        for s in self.special_schemes:
            s["samples_per_scheme"] = per
            mode = str(s.get("construct_mode") or "build").strip().lower()
            s["construct_mode"] = "mutate" if mode == "mutate" else "build"
        special_count = self._selected_special_count()
        cons = {}
        for iid in self.cons_tree.get_children():
            name, lo, hi = self.cons_tree.item(iid, "values")
            try:
                cons[str(name)] = [int(lo), int(hi)]
            except (TypeError, ValueError):
                continue
        edges = self._collect_edges_from_ui()
        total_count = regular_count + special_count
        if total_count <= 0 or not cons:
            return None
        out = {"count": total_count, "constraints": cons, "edge_cases": edges}
        time_limit = self._parse_positive_int(self.time_limit_var.get())
        memory_limit = self._parse_positive_int(self.memory_limit_var.get())
        if time_limit is None and isinstance(self.range_data, dict):
            time_limit = self._parse_positive_int(str(self.range_data.get("time_limit_ms") or ""))
        if memory_limit is None and isinstance(self.range_data, dict):
            memory_limit = self._parse_positive_int(str(self.range_data.get("memory_limit_mb") or ""))
        out["time_limit_ms"] = time_limit if time_limit is not None else 5000
        out["memory_limit_mb"] = memory_limit if memory_limit is not None else 1024
        if isinstance(self.range_data, dict):
            sc = [
                str(x).strip()
                for x in (self.range_data.get("special_constraints") or [])
                if str(x).strip()
            ]
            if sc:
                out["special_constraints"] = sc
        if special_desc or self.special_schemes:
            # 有方案但输入框为空时保留自动挖掘标记，避免丢 special 启用状态
            if special_desc:
                out["special_samples_desc"] = special_desc
            elif self.auto_discover_special_var.get() or self.special_schemes:
                out["special_samples_desc"] = "（自动挖掘特殊方案）"
            out["special_samples_count"] = special_count
            out["special_schemes"] = [dict(s) for s in self.special_schemes]
        if self.auto_discover_special_var.get():
            out["auto_discover_special"] = True
        # 题型只来自 LLM 已写入的 range_data，不用界面下拉覆盖
        if isinstance(self.range_data, dict) and self.range_data.get("problem_type"):
            out["problem_type"] = self.range_data["problem_type"]
        # 无多测 T 时剔除 edge_T1 等；与服务端 normalize 对齐
        try:
            from pipeline.gen_data import normalize_range_json
            out = normalize_range_json(dict(out))
        except Exception:
            pass
        return out

    def on_propose_range(self):
        stmt = self.statement.get("1.0", "end").strip()
        rng = self.range_desc.get("1.0", "end").strip()
        if not stmt and not rng:
            messagebox.showwarning("提示", "请先填写题面或输入描述")
            return
        try:
            special_count = int(self.special_count_var.get().strip() or "1")
        except ValueError:
            special_count = 1
        special_desc = self._get_special_desc()
        auto_disc = bool(self.auto_discover_special_var.get())
        body = {
            "problem_statement": stmt,
            "data_range_desc": rng,
            # 题型由 Range Agent 写入 range.json.problem_type（不单独判型）
            "problem_type": "",
            "std_code": self.std_code.get("1.0", "end").strip(),
            "lang": self.lang.get(),
            "special_samples_count": special_count,
            "special_samples_desc": special_desc,
            "auto_discover_special": auto_disc,
        }
        self.btn_propose.config(state="disabled")
        if special_desc:
            phase = "range + 按提示挖掘特殊方案"
        elif auto_disc:
            phase = "range + 自动挖掘特殊方案"
        else:
            phase = "仅 range（未填特殊提示且未开自动挖掘）"
        self.status.set(f"正在用大模型生成数据方案（{phase}）…")
        threading.Thread(target=self._propose_worker, args=(body,), daemon=True).start()

    def _propose_worker(self, body):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                f"{BASE}/range/propose", data=data,
                headers={"Content-Type": "application/json"},
            )
            # 同步等 LLM（含特殊方案挖掘），超时放宽
            r = urllib.request.urlopen(req, timeout=300)
            payload = json.loads(r.read())
            rj = payload.get("range_json") or {}
            ptype = payload.get("problem_type") or rj.get("problem_type") or ""
            schemes = payload.get("special_schemes") or rj.get("special_schemes") or []
            if not isinstance(schemes, list):
                schemes = []
            hint = (body.get("special_samples_desc") or "").strip()
            n_sch = len(schemes)
            self.root_after(
                lambda rj=rj, ptype=ptype, schemes=schemes: self.apply_range_plan(rj, ptype, schemes)
            )
            msg = "数据方案已生成 — 可在「5. 数据方案」查看/勾选特殊方案，再点提交生成"
            if ptype:
                msg = f"数据方案已生成（题型={ptype}"
                if n_sch:
                    msg += f"，特殊方案 {n_sch} 条"
                elif hint:
                    msg += "，特殊方案由本地提示回退补全"
                msg += "）— 勾选后提交生成"
            self.root_after(lambda m=msg: self.status.set(m))
            if hint and n_sch == 0:
                self.root_after(
                    lambda: messagebox.showwarning(
                        "特殊方案",
                        "服务端未返回特殊方案列表，已根据你的提示自动补了 1 条。\n"
                        "可点「模式」切换 mutate/build，或再点「生成方案」。\n"
                        "若持续为空，请确认后端已重启到最新代码。",
                    )
                )
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

        plan = self.collect_range_from_ui()
        # 界面不传题型；有方案时带上 LLM 已写入的 problem_type，否则由 Range 重新判定
        if plan:
            ptype = plan.get("problem_type") or ""
        else:
            ptype = ""
        bc = _builtin_checker_id_from_label(self.builtin_checker.get())
        try:
            per_scheme = int(self.special_count_var.get().strip() or "1")
        except ValueError:
            per_scheme = 1
        special_desc = self._get_special_desc()
        auto_disc = bool(self.auto_discover_special_var.get())
        body = {
            "std_code": std,
            "lang": self.lang.get(),
            "problem_type": "",  # 始终由大模型判定，界面不可指定
            "problem_statement": stmt,
            "data_range_desc": rng,
            "output_desc": out,
            "special_judge": self.special_judge_var.get(),
            "builtin_checker": bc,
            "special_samples_count": per_scheme,
            "special_samples_desc": special_desc,
            "auto_discover_special": auto_disc,
        }
        if plan:
            # range_json 内可保留 LLM 已判题型，供 Range 审核；顶层 problem_type 仍为空
            body["range_json"] = plan
            body["special_samples_count"] = int(plan.get("special_samples_count") or per_scheme)
            if plan.get("auto_discover_special"):
                body["auto_discover_special"] = True
            if ptype:
                self._set_detected_ptype(ptype)
        # 历史 Job 加载时已选定的复用意图
        if self.pending_resume_parent_id:
            body["resume_context"] = {"parent_job_id": self.pending_resume_parent_id}
        if self.prefer_skip_resume:
            body["skip_resume"] = True

        # 已选定成功父任务：提交前直接挂下载，绝不进检查线程 / 开新 job
        prefer_id = str(self.pending_resume_parent_id or "").strip()
        if prefer_id and not body.get("skip_resume"):
            done = self._describe_done_job_local(prefer_id)
            if done:
                self.set_log_collapsed(False)
                if plan:
                    self._append_log(
                        f"使用 GUI 数据方案: count={plan['count']} edges={plan['edge_cases']}"
                    )
                self._append_log(
                    f"【直接复用】历史已选父任务 {prefer_id} 有 data.zip，跳过检查与生成"
                )
                self._bind_done_job_for_download(done)
                return

        self.btn_submit.config(state="disabled")
        self.btn_download_menu.config(state="disabled")
        self.download_menu.entryconfig(0, state="disabled")
        self.download_menu.entryconfig(1, state="disabled")
        self.download_menu.entryconfig(2, state="disabled")
        self.progress.delete("1.0", "end")
        self._seen_progress = 0
        self._reset_stages()
        self.set_log_collapsed(False)  # 开始生成时自动展开日志
        if plan:
            self.status.set(f"检查历史任务（已带方案 count={plan['count']}）…")
            self._append_log(f"使用 GUI 数据方案: count={plan['count']} edges={plan['edge_cases']}")
        else:
            self.status.set("检查历史任务（无方案）…")
            self._append_log("未提供数据方案 — Agent 将先 write_range")
        threading.Thread(target=self._check_resume_and_submit, args=(body,), daemon=True).start()

    def _submit(self, body):
        # 最终闸门：若指定父任务已有 data.zip，绝不再开跑，直接挂下载
        try:
            ctx = body.get("resume_context") if isinstance(body, dict) else None
            parent_id = ""
            if isinstance(ctx, dict):
                parent_id = str(ctx.get("parent_job_id") or ctx.get("parent_id") or "").strip()
            if not parent_id:
                parent_id = str(getattr(self, "pending_resume_parent_id", "") or "").strip()
            if parent_id and not body.get("skip_resume"):
                done = self._describe_done_job_local(parent_id)
                if done:
                    self._append_log(
                        f"【拦截】父任务 {parent_id} 已有 data.zip，改为直接下载，取消重新生成"
                    )
                    self.root_after(lambda d=done: self._bind_done_job_for_download(d))
                    self.root_after(lambda: self.btn_submit.config(state="normal"))
                    return
        except Exception as e:
            self._append_log(f"提交前完成任务检查异常（继续提交）: {e}")

        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                f"{BASE}/jobs", data=data,
                headers={"Content-Type": "application/json"},
            )
            r = urllib.request.urlopen(req, timeout=10)
            self.job_id = json.loads(r.read())["job_id"]
            # 提交成功后清空历史复用意图，避免影响下一次提交
            self.pending_resume_parent_id = None
            self.prefer_skip_resume = False
            try:
                # 后台线程：禁止弹窗；换题时自动另存，避免覆盖旧题
                msg = self._persist_workspace(
                    to_problem=True,
                    to_job=True,
                    confirm_fork=False,
                    auto_fork_on_diverge=True,
                )
                self.root_after(lambda m=msg: self._append_log(f"提交后落盘: {m}"))
            except Exception as e:
                self.root_after(lambda err=e: self._append_log(f"提交后落盘失败: {err}"))
            self.root_after(lambda: self.status.set(f"已提交  job={self.job_id}  运行中…"))
            self.root_after(self._poll, 800)
        except Exception as e:
            self.root_after(lambda: self.status.set(f"提交失败: {e}"))
            self.root_after(lambda: self.btn_submit.config(state="normal"))

    def _check_resume_and_submit(self, body):
        """提交前检查同题历史：已成功 → 直接下载；失败 → 续跑；否则新跑。

        优先本地扫描 jobs/（不依赖后端是否重启），再辅以 /jobs/check_resume。
        """
        if body.get("skip_resume"):
            self.root_after(lambda: self._append_log("沿用历史加载时的选择：跳过复用，从零生成"))
            self.root_after(lambda: self._submit(body))
            return

        prefer = ""
        if body.get("resume_context"):
            prefer = str(
                body["resume_context"].get("parent_job_id")
                or body["resume_context"].get("parent_id")
                or ""
            ).strip()

        lookback = 10
        stmt = body.get("problem_statement", "") or ""
        std = body.get("std_code", "") or ""
        lang = body.get("lang", "cpp") or "cpp"

        # 1) 本地扫描成功任务（GUI 与 jobs/ 同机，避免后端旧代码漏检）
        local_candidates = self._find_done_jobs_local(
            stmt, std, lang, lookback=lookback, prefer=prefer,
        )
        if local_candidates:
            self.root_after(
                lambda n=len(local_candidates): self._append_log(
                    f"同题检查(本地)：找到 {n} 个已成功任务（最近 {lookback} 个成功 job）"
                )
            )
            result = {
                "can_resume": True,
                "kind": "done",
                "candidates": local_candidates,
                **local_candidates[0],
            }
            self.root_after(lambda r=result, b=body: self._show_done_reuse_dialog(r, b))
            return

        # 2) 后端检查（失败续跑 / 或本地未扫到时的兜底）
        try:
            check_body = {
                "std_code": std,
                "lang": lang,
                "problem_statement": stmt,
                "lookback": lookback,
                "prefer_parent_id": prefer,
            }
            data = json.dumps(check_body).encode()
            req = urllib.request.Request(
                f"{BASE}/jobs/check_resume", data=data,
                headers={"Content-Type": "application/json"},
            )
            r = urllib.request.urlopen(req, timeout=15)
            result = json.loads(r.read())
        except Exception as e:
            self.root_after(
                lambda err=e: self._append_log(
                    f"同题检查(后端)失败: {err}；本地亦未找到成功任务，按新任务提交"
                )
            )
            self.root_after(lambda: self._submit(body))
            return

        if not result.get("can_resume"):
            hint = result.get("hint") or f"最近 {lookback} 个成功任务中无同题"
            self.root_after(lambda h=hint: self._append_log(f"同题检查：未找到可复用任务（{h}），按新任务提交"))
            self.root_after(lambda: self._submit(body))
            return

        kind = result.get("kind") or "failed"
        arts = result.get("artifacts") or []
        cands = result.get("candidates") or []
        if kind != "done" and (
            result.get("has_data_zip") or "data.zip" in arts or cands
        ):
            kind = "done"
            result = dict(result)
            result["kind"] = "done"
            result["has_data_zip"] = True
            if cands and not result.get("parent_job_id"):
                result.update(cands[0])

        if kind == "done":
            n = len(result.get("candidates") or [result])
            self.root_after(
                lambda n=n: self._append_log(f"同题检查(后端)：找到 {n} 个已成功任务")
            )
            self.root_after(lambda r=result, b=body: self._show_done_reuse_dialog(r, b))
            return

        if prefer:
            self.root_after(
                lambda: self._append_log(f"沿用历史加载时的复用选择，父任务 {prefer}")
            )
            self.root_after(lambda: self._submit(body))
            return

        self.root_after(lambda r=result, b=body: self._show_resume_dialog(r, b))

    def _describe_done_job_local(self, job_id: str) -> dict | None:
        """本地判断 job 是否已成功可下载。"""
        try:
            from runners.resume import describe_done_job
            return describe_done_job(job_id)
        except Exception:
            # 兜底：只看 data.zip
            try:
                d = problem_store.JOBS_DIR / str(job_id)
                if not (d / "data.zip").is_file():
                    return None
                return {
                    "parent_job_id": str(job_id),
                    "kind": "done",
                    "has_data_zip": True,
                    "has_sources_zip": (d / "sources.zip").is_file(),
                    "has_checker_zip": (d / "checker.zip").is_file(),
                    "artifacts": ["data.zip"],
                }
            except Exception:
                return None

    def _find_done_jobs_local(
        self,
        statement: str,
        std_code: str,
        lang: str,
        *,
        lookback: int = 10,
        prefer: str = "",
    ) -> list[dict]:
        """在本地 jobs/ 扫描同题已成功任务。"""
        if prefer:
            info = self._describe_done_job_local(prefer)
            if info:
                return [info]
        try:
            from runners.resume import find_matching_done_jobs, text_hash
            return find_matching_done_jobs(
                text_hash(statement or ""),
                text_hash(std_code or ""),
                lang,
                lookback=lookback,
            )
        except Exception as e:
            try:
                self._append_log(f"本地扫描成功任务失败: {e}")
            except Exception:
                pass
            return []

    def _format_zip_size(self, n: int) -> str:
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            return "?"
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        return f"{n / (1024 * 1024):.1f}MB"

    def _enable_downloads(self, *, has_data=True, has_sources=True, has_checker=False):
        self.btn_download_menu.config(state="normal")
        self.download_menu.entryconfig(0, state="normal" if has_data else "disabled")
        self.download_menu.entryconfig(1, state="normal" if has_sources else "disabled")
        self.download_menu.entryconfig(2, state="normal" if has_checker else "disabled")

    def _bind_done_job_for_download(self, result: dict):
        """把已成功任务挂到当前会话，直接开放下载，不跑流水线。"""
        parent_id = str(result.get("parent_job_id") or "")
        if not parent_id:
            return
        self.job_id = parent_id
        self.pending_resume_parent_id = None
        self.prefer_skip_resume = False
        self._reset_stages()
        for key, _ in STAGES:
            self._set_stage(key, "done")
        has_data = bool(result.get("has_data_zip", True))
        has_sources = bool(result.get("has_sources_zip", True))
        has_checker = bool(result.get("has_checker_zip", False))
        self._enable_downloads(
            has_data=has_data, has_sources=has_sources, has_checker=has_checker,
        )
        self.btn_submit.config(state="normal")
        self.status.set(f"已复用完成任务 {parent_id} — 可直接下载，未重新生成")
        self._append_log(
            f"【直接复用】同题已成功任务 {parent_id}："
            f"data.zip={'有' if has_data else '无'} · "
            f"sources={'有' if has_sources else '无'} · "
            f"checker={'有' if has_checker else '无'}；跳过全部生成阶段"
        )
        try:
            self._persist_workspace(
                to_problem=True,
                to_job=False,
                confirm_fork=False,
                auto_fork_on_diverge=True,
            )
        except Exception:
            pass

    def _show_done_reuse_dialog(self, result, body):
        """同题已成功：多条时列出供选择，选中后直接下载，或重新生成。"""
        candidates = list(result.get("candidates") or [])
        if not candidates and result.get("parent_job_id"):
            candidates = [result]
        if not candidates:
            self._submit(body)
            return

        # 去重（同 id 只留一条）
        seen: set[str] = set()
        uniq: list[dict] = []
        for c in candidates:
            jid = str(c.get("parent_job_id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            uniq.append(c)
        candidates = uniq
        selected: dict[str, dict | None] = {"item": candidates[0]}

        dialog = tk.Toplevel(self.root)
        dialog.title(f"发现 {len(candidates)} 个同题已完成任务")
        dialog.resizable(True, True)

        tip = (
            f"找到 {len(candidates)} 个相同题目的成功结果，请选择要复用的那一个：\n"
            "「复用」= 直接下载该包，不重新生成；「重新生成」= 忽略全部，从零出数。"
        )
        wrap = max(360, min(600, self.root.winfo_width() - 80))
        tk.Label(dialog, text=tip, justify=tk.LEFT, wraplength=wrap).pack(
            padx=16, pady=(14, 6), anchor="w",
        )

        # 表头
        hdr = tk.Frame(dialog)
        hdr.pack(fill="x", padx=16, pady=(4, 0))
        for col, w in (("选择", 6), ("任务 ID", 22), ("data.zip", 10), ("包含", 18), ("级别", 10)):
            tk.Label(hdr, text=col, width=w, anchor="w", font=("Segoe UI", 9, "bold")).pack(
                side="left", padx=2,
            )

        choice_var = tk.StringVar(value=str(candidates[0].get("parent_job_id") or ""))
        rows = tk.Frame(dialog)
        rows.pack(fill="both", expand=True, padx=12, pady=4)

        canvas = tk.Canvas(rows, highlightthickness=0, height=min(280, 36 * len(candidates) + 8))
        scroll = tk.Scrollbar(rows, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas)
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def pick(jid: str, item: dict):
            choice_var.set(jid)
            selected["item"] = item

        for c in candidates:
            jid = str(c.get("parent_job_id") or "?")
            size = self._format_zip_size(c.get("data_zip_bytes") or 0)
            packs = []
            if c.get("has_data_zip", True):
                packs.append("data")
            if c.get("has_sources_zip"):
                packs.append("sources")
            if c.get("has_checker_zip"):
                packs.append("checker")
            level = c.get("resume_level") or "-"
            row = tk.Frame(inner)
            row.pack(fill="x", pady=2)
            tk.Radiobutton(
                row, variable=choice_var, value=jid,
                command=lambda j=jid, it=c: pick(j, it),
            ).pack(side="left", padx=(4, 8))
            tk.Label(row, text=jid, width=22, anchor="w", font=("Consolas", 10)).pack(
                side="left", padx=2,
            )
            tk.Label(row, text=size, width=10, anchor="w").pack(side="left", padx=2)
            tk.Label(row, text=",".join(packs) or "-", width=18, anchor="w").pack(
                side="left", padx=2,
            )
            tk.Label(row, text=level, width=10, anchor="w").pack(side="left", padx=2)
            # 点击整行也可选中
            for w in row.winfo_children():
                if not isinstance(w, tk.Radiobutton):
                    w.bind("<Button-1>", lambda _e, j=jid, it=c: pick(j, it))

        def on_choice(choice):
            # 按当前 radio 取值再对齐一次
            jid = choice_var.get()
            item = next((c for c in candidates if str(c.get("parent_job_id")) == jid), None)
            item = item or selected.get("item") or candidates[0]
            parent_id = item.get("parent_job_id", "")
            if choice == "reuse":
                self._append_log(f"用户选择直接复用已完成任务 {parent_id}（不跑流水线）")
                self._bind_done_job_for_download(item)
                dialog.destroy()
            elif choice == "regenerate":
                self._append_log(
                    f"用户选择重新生成（忽略 {len(candidates)} 个已完成任务）"
                )
                body["skip_resume"] = True
                body.pop("resume_context", None)
                self.pending_resume_parent_id = None
                dialog.destroy()
                self._submit(body)
            else:
                self.btn_submit.config(state="normal")
                self.status.set("已取消提交")
                self._append_log("用户取消提交")
                dialog.destroy()

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=(10, 14))
        tk.Button(
            btn_frame, text="复用所选（直接下载）", width=18,
            command=lambda: on_choice("reuse"),
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="重新生成", width=10,
            command=lambda: on_choice("regenerate"),
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="取消", width=10,
            command=lambda: on_choice("cancel"),
        ).pack(side=tk.LEFT, padx=5)

        dialog.protocol("WM_DELETE_WINDOW", lambda: on_choice("cancel"))
        dialog.update_idletasks()
        req_w = max(560, dialog.winfo_reqwidth() + 24)
        req_h = max(280, dialog.winfo_reqheight() + 12)
        self.place_dialog(dialog, req_w, req_h, min_w=480, min_h=240, grab=True)

    def _show_resume_dialog(self, result, body):
        """弹出失败续跑选择对话框：复用 / 重新生成 / 取消。"""
        parent_id = result.get("parent_job_id", "")
        stage = result.get("stage", "unknown")
        error = result.get("error_summary", "")
        artifacts = result.get("artifacts", []) or []
        artifact_text = ", ".join(artifacts[:8]) or "无"
        if len(artifacts) > 8:
            artifact_text += f" 等共 {len(artifacts)} 项"
        _, tier_note = self._classify_history_artifacts(list(artifacts))

        msg = (
            f"发现同题历史失败任务：{parent_id}\n"
            f"失败阶段：{stage}\n"
            f"错误摘要：{error[:120] or '（无）'}\n"
            f"可复用产物：{artifact_text}\n"
            f"复用预期：{tier_note}\n\n"
            f"选择「复用」将继承产物继续生成；\n"
            f"选择「重新生成」将跳过复用，从零开始。"
        )

        def on_choice(choice):
            if choice == "reuse":
                # 只要父任务磁盘上有 data.zip，一律直接下载，绝不重新 Plan/写 gen
                done = self._describe_done_job_local(parent_id)
                if done:
                    self._append_log(
                        f"用户选择复用已完成任务 {parent_id}（有 data.zip，直接下载，跳过生成）"
                    )
                    self._bind_done_job_for_download(done)
                    return
                self._append_log(f"用户选择复用历史任务 {parent_id}（续跑流水线）")
                body["resume_context"] = {"parent_job_id": parent_id}
                body.pop("skip_resume", None)
                self._append_log(f"提交带 resume_context={body.get('resume_context')}")
                self._submit(body)
            elif choice == "regenerate":
                self._append_log(f"用户选择跳过复用，重新生成（父任务 {parent_id}）")
                body["skip_resume"] = True
                body.pop("resume_context", None)
                self._submit(body)
            else:  # cancel
                self.btn_submit.config(state="normal")
                self.status.set("已取消提交")
                self._append_log("用户取消提交")

        dialog = tk.Toplevel(self.root)
        dialog.title("发现可复用历史任务")
        dialog.resizable(True, True)
        wrap = max(320, min(520, self.root.winfo_width() - 120))
        lbl = tk.Label(dialog, text=msg, justify=tk.LEFT, wraplength=wrap)
        lbl.pack(padx=20, pady=(15, 10))
        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=(0, 15))
        tk.Button(
            btn_frame, text="复用", width=10,
            command=lambda: [dialog.destroy(), on_choice("reuse")]
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="重新生成", width=10,
            command=lambda: [dialog.destroy(), on_choice("regenerate")]
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="取消", width=10,
            command=lambda: [dialog.destroy(), on_choice("cancel")]
        ).pack(side=tk.LEFT, padx=5)
        dialog.update_idletasks()
        req_w = max(420, dialog.winfo_reqwidth() + 20)
        req_h = max(200, dialog.winfo_reqheight() + 10)
        self.place_dialog(dialog, req_w, req_h, min_w=360, min_h=180, grab=True)

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
        size_text = (st.get("artifact_sizes_text") or "").strip()
        if not size_text:
            sizes = st.get("artifact_sizes") or {}
            parts = []
            if sizes.get("data_zip") is not None:
                parts.append(f"data.zip={self._format_bytes(sizes['data_zip'])}")
            if sizes.get("out_total") is not None:
                parts.append(f"测例={self._format_bytes(sizes['out_total'])}")
            size_text = " · ".join(parts)
        base = f"耗时 {elapsed} · {tokens}"
        return f"{base} · {size_text}" if size_text else base

    @staticmethod
    def _format_bytes(n) -> str:
        try:
            b = float(n or 0)
        except (TypeError, ValueError):
            return "-"
        if b < 0:
            b = 0
        units = ("B", "KB", "MB", "GB")
        i = 0
        while b >= 1024 and i < len(units) - 1:
            b /= 1024
            i += 1
        if i == 0:
            return f"{int(b)}{units[i]}"
        return f"{b:.1f}{units[i]}"

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
            self.btn_download_menu.config(state="normal")
            self.download_menu.entryconfig(0, state="normal")
            self.download_menu.entryconfig(1, state="disabled")
            self.download_menu.entryconfig(2, state="disabled")
            if st.get("has_sources_zip"):
                self.download_menu.entryconfig(1, state="normal")
            if st.get("has_checker_zip"):
                self.download_menu.entryconfig(2, state="normal")
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
        elif msg.startswith("【统计】") or msg.startswith("【数据大小】"):
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
        # 失败类进度：保留更长摘要（完整正文已在 jobs/*/errors/）
        if "ERROR" in preview or "编译失败" in preview or "完整见" in preview or "FAIL " in preview:
            pv = preview if len(preview) <= 4000 else preview[:3600] + "…\n" + preview[-300:]
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
            # 必须在项目根目录启动，保证相对/绝对 jobs 路径一致（勿用 gui/ 作 cwd）
            _root = Path(__file__).resolve().parent.parent
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
                cwd=str(_root),
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
        return http_get(path, timeout=timeout)

    def _rag_post_json(self, path: str, body: dict, timeout: int = 120):
        return http_post(path, body, timeout=timeout)

    def _rag_delete(self, path: str, timeout: int = 30):
        return http_delete(path, timeout=timeout)

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
                    self.place_dialog(win, 720, 520, min_w=480, min_h=320)
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
                    self.place_dialog(win, 480, 320, min_w=360, min_h=240, grab=True)
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

    # ---- 题目工作区 / 历史 ----
    def _set_text_widget(self, widget, text: str):
        widget.delete("1.0", "end")
        if text:
            widget.insert("1.0", text)

    def _update_problem_title_label(self, title: str = ""):
        title = (title or "").strip()
        if self.current_problem_id:
            shown = title or self.current_problem_id
            self.problem_title_var.set(f"{shown}\n[{self.current_problem_id}]")
        elif title:
            self.problem_title_var.set(f"{title}\n（未写入题库）")
        else:
            self.problem_title_var.set("（未保存题目）")

    def collect_workspace(self) -> dict:
        ws = problem_store.empty_workspace()

        def _get(w):
            t = w.get("1.0", "end")
            return t[:-1] if t.endswith("\n") else t

        stmt = _get(self.statement)
        title = problem_store.extract_title(stmt, fallback=self.current_problem_id or "")
        ws.update({
            "id": self.current_problem_id or "",
            "title": title,
            "lang": self.lang.get() or "cpp",
            "problem_type": self._format_ptype_display(
                (isinstance(self.range_data, dict) and self.range_data.get("problem_type"))
                or self.ptype.get()
            ),
            "std_code": _get(self.std_code),
            "statement": stmt,
            "input_desc": _get(self.range_desc),
            "output_desc": _get(self.output_desc),
            "special_judge": bool(self.special_judge_var.get()),
            # 工作区存英文 id（与 API 一致）；空/无 存「无」便于旧逻辑兼容
            "builtin_checker": _builtin_checker_id_from_label(self.builtin_checker.get()) or "无",
            "last_job_id": self.job_id or "",
            "range_plan": self.collect_range_from_ui(),
        })
        return ws

    def apply_workspace(
        self, ws: dict, *, confirm: bool = False, load_range_plan: bool = True,
    ) -> bool:
        if not ws:
            return False
        if confirm:
            cur = self.collect_workspace()
            has_cur = any(
                (cur.get(k) or "").strip()
                for k in ("std_code", "statement", "input_desc", "output_desc")
            )
            dirty = has_cur and any(
                (cur.get(k) or "").strip() != (ws.get(k) or "").strip()
                for k in ("std_code", "statement", "input_desc", "output_desc")
            )
            if dirty and not messagebox.askyesno(
                "替换工作区",
                "加载将覆盖当前标程、题面、输入描述、输出描述（及数据方案，若有）。继续？",
            ):
                return False
        self._set_text_widget(self.std_code, ws.get("std_code") or "")
        self._set_text_widget(self.statement, ws.get("statement") or "")
        self._set_text_widget(self.range_desc, ws.get("input_desc") or "")
        self._set_text_widget(self.output_desc, ws.get("output_desc") or "")
        lang = (ws.get("lang") or "cpp").strip().lower()
        if lang in LANGS:
            self.lang.set(lang)
        raw_pt = ws.get("problem_type") or ""
        if isinstance(ws.get("range_plan"), dict) and ws["range_plan"].get("problem_type"):
            raw_pt = ws["range_plan"].get("problem_type") or raw_pt
        self._set_detected_ptype(raw_pt)
        self.special_judge_var.set(bool(ws.get("special_judge")))
        self.builtin_checker.set(_builtin_checker_label_from_any(ws.get("builtin_checker") or "无"))
        src = ws.get("source") or ""
        if src == "problem":
            self.current_problem_id = str(ws.get("id") or "")
        elif src == "job":
            jid = str(ws.get("last_job_id") or ws.get("id") or "")
            if jid:
                self.job_id = jid
            lib = str(ws.get("id") or "")
            if lib and (problem_store.PROBLEMS_DIR / lib).is_dir():
                self.current_problem_id = lib
        else:
            if ws.get("id"):
                self.current_problem_id = str(ws.get("id"))
            if ws.get("last_job_id"):
                self.job_id = str(ws.get("last_job_id"))
        plan = ws.get("range_plan")
        if load_range_plan and isinstance(plan, dict) and plan.get("constraints"):
            self.apply_range_plan(plan, problem_type=plan.get("problem_type") or raw_pt)
        elif src in ("problem", "job") or not load_range_plan:
            self.clear_range_plan()
        self._update_problem_title_label(ws.get("title") or "")
        self.status.set(
            f"已加载：{ws.get('title') or ws.get('id') or '工作区'}"
            + (f"（{src}）" if src else "")
        )
        return True

    def _persist_workspace(
        self,
        *,
        to_problem: bool = True,
        to_job: bool = True,
        force_new: bool = False,
        confirm_fork: bool = True,
        auto_fork_on_diverge: bool = False,
    ) -> str:
        """保存会话；可选写入题库与当前 job。返回简短说明。

        force_new：另存为新题（断开旧 ID）。
        confirm_fork：题面相对题库明显换题时弹窗（是=另存 / 否=覆盖 / 取消=中止）。
        auto_fork_on_diverge：不弹窗，换题时自动另存（关闭窗口用）。
        """
        ws = self.collect_workspace()
        notes = []
        if to_problem and any(
            (ws.get(k) or "").strip()
            for k in ("std_code", "statement", "input_desc", "output_desc")
        ):
            try:
                pid = None if force_new else (self.current_problem_id or None)
                do_force_new = bool(force_new)
                if pid and not do_force_new:
                    existing = problem_store.load_problem(pid)
                    if problem_store.should_fork_problem(existing, ws):
                        if auto_fork_on_diverge:
                            do_force_new = True
                            pid = None
                            notes.append("换题自动另存")
                        elif confirm_fork:
                            ans = messagebox.askyesnocancel(
                                "检测到换题",
                                "当前题面与已关联题库条目差异较大，继续保存会覆盖旧题。\n\n"
                                f"旧条目：{pid}\n"
                                f"旧标题：{(existing or {}).get('title') or '（无）'}\n"
                                f"新标题：{ws.get('title') or '（无）'}\n\n"
                                "是 = 另存为新题（推荐）\n"
                                "否 = 覆盖旧条目\n"
                                "取消 = 不保存到题库",
                            )
                            if ans is None:
                                notes.append("题库:已取消")
                                # 仍写会话 / job
                                pid = "__skip__"
                            elif ans:
                                do_force_new = True
                                pid = None
                                notes.append("换题另存")
                            else:
                                notes.append("换题覆盖")
                        else:
                            # 无确认且非自动另存：store 护栏强制新建
                            do_force_new = False
                if pid != "__skip__":
                    meta = problem_store.upsert_problem(
                        ws,
                        problem_id=None if do_force_new else pid,
                        force_new=do_force_new,
                        forbid_divergent_overwrite=not do_force_new and not confirm_fork,
                    )
                    self.current_problem_id = str(meta.get("id") or self.current_problem_id)
                    ws["id"] = self.current_problem_id
                    ws["title"] = meta.get("title") or ws.get("title") or ""
                    self._update_problem_title_label(ws["title"])
                    notes.append(f"题库:{self.current_problem_id}")
            except Exception as e:
                notes.append(f"题库失败:{e}")
        if to_job and self.job_id:
            try:
                problem_store.save_to_job(self.job_id, ws)
                notes.append(f"job:{self.job_id}")
            except Exception as e:
                notes.append(f"job失败:{e}")
        try:
            problem_store.save_session(ws)
            notes.append("会话")
        except Exception as e:
            notes.append(f"会话失败:{e}")
        return " · ".join(notes)

    def on_save_problem(self):
        msg = self._persist_workspace(to_problem=True, to_job=bool(self.job_id))
        self.status.set(f"已保存 — {msg}")
        self._append_log(f"保存题目: {msg}")
        if hasattr(self, "history_tree"):
            self.on_history_refresh()

    def on_save_as_new_problem(self):
        msg = self._persist_workspace(
            to_problem=True, to_job=bool(self.job_id), force_new=True, confirm_fork=False,
        )
        self.status.set(f"已另存 — {msg}")
        self._append_log(f"另存为新题: {msg}")
        if hasattr(self, "history_tree"):
            self.on_history_refresh()

    def on_new_problem(self):
        cur = self.collect_workspace()
        has_cur = any(
            (cur.get(k) or "").strip()
            for k in ("std_code", "statement", "input_desc", "output_desc")
        )
        if has_cur and not messagebox.askyesno(
            "新建题目",
            "将清空当前标程/题面/输入/输出，并断开题库关联。\n未保存的修改会丢失。继续？",
        ):
            return
        empty = problem_store.empty_workspace()
        self.current_problem_id = ""
        self.job_id = ""
        self.pending_resume_parent_id = None
        self.prefer_skip_resume = False
        self.apply_workspace(empty, confirm=False, load_range_plan=False)
        self.clear_range_plan()
        self._update_problem_title_label("")
        self.status.set("已新建空白题目（未关联题库）")
        self._append_log("新建题目：已清空工作区并断开题库 ID")

    def on_history_refresh(self):
        if not hasattr(self, "history_tree"):
            return
        for i in self.history_tree.get_children():
            self.history_tree.delete(i)
        self._history_rows = {}
        problems = problem_store.list_problems()
        jobs = problem_store.list_jobs_with_workspace()
        for item in problems:
            iid = self.history_tree.insert(
                "", "end",
                values=(
                    "题库",
                    item.get("title") or "",
                    item.get("lang") or "",
                    item.get("problem_type") or "",
                    item.get("updated_at") or "",
                    item.get("id") or "",
                ),
            )
            self._history_rows[iid] = item
        for item in jobs:
            iid = self.history_tree.insert(
                "", "end",
                values=(
                    "Job",
                    item.get("title") or "",
                    item.get("lang") or "",
                    item.get("problem_type") or "",
                    item.get("updated_at") or "",
                    item.get("id") or "",
                ),
            )
            self._history_rows[iid] = item
        self.history_status.set(f"题库 {len(problems)} · Job {len(jobs)}")

    def _selected_history_item(self) -> dict | None:
        sel = self.history_tree.selection()
        if not sel:
            return None
        return self._history_rows.get(sel[0])

    def _list_job_reusable_artifacts(self, job_id: str) -> list[str]:
        """列出历史 Job 目录里可复用的产物名（供弹窗展示）。"""
        job_dir = problem_store.JOBS_DIR / job_id
        if not job_dir.is_dir():
            return []
        names = []
        for name in (
            "range.json", "gen.cpp", "gen.py", "validator.cpp", "validate.py",
            "gen_special.cpp", "checker.cpp", "gen_plan.md", "checker_plan.md",
            "gen.cpp.good", "validator.cpp.good", "gen.py.good", "validate.py.good",
            "out.good",
        ):
            p = job_dir / name
            if p.is_file() or p.is_dir():
                names.append(name)
        return names

    def _classify_history_artifacts(self, artifacts: list[str]) -> tuple[str, str]:
        """返回 (级别标签, 对用户说明)。级别: full / gen_val / range_only / empty。"""
        names = set(artifacts)
        has_range = "range.json" in names
        has_gen = bool(names & {
            "gen.cpp", "gen.py", "gen.cpp.good", "gen.py.good",
        })
        has_val = bool(names & {
            "validator.cpp", "validate.py", "validator.cpp.good", "validate.py.good",
        })
        has_out = "out.good" in names
        if has_gen and has_val and has_out:
            return (
                "full",
                "父任务有 gen/validator 与测例快照：复用后可继承代码并可能跳过部分出数。",
            )
        if has_gen and has_val:
            return (
                "gen_val",
                "父任务有 gen/validator：复用后将继承代码并在此基础上修复/完善。",
            )
        if has_range:
            return (
                "range_only",
                "父任务仅有数据方案（range）：复用后会先审核 range（合理复用/不合理重写），再编写 gen/validator。",
            )
        return (
            "empty",
            "父任务几乎无可复用产物：选「复用」也基本等同从零生成（仍会回填题面/标程）。",
        )

    def _ask_history_job_reuse(self, item: dict, artifacts: list[str]) -> str | None:
        """历史 Job 加载前询问是否复用。返回 'reuse' / 'texts_only' / None(取消)。"""
        job_id = item.get("id") or ""
        title = item.get("title") or job_id
        artifact_text = ", ".join(artifacts[:10]) or "无（将只回填题面/标程）"
        if len(artifacts) > 10:
            artifact_text += f" 等共 {len(artifacts)} 项"
        _level, tier_note = self._classify_history_artifacts(artifacts)

        result: dict[str, str | None] = {"choice": None}
        dialog = tk.Toplevel(self.root)
        dialog.title("加载历史任务")
        dialog.resizable(True, True)
        msg = (
            f"历史任务：{title}\n"
            f"ID：{job_id}\n"
            f"可复用产物：{artifact_text}\n"
            f"复用预期：{tier_note}\n\n"
            "「复用」：加载题目内容 + 数据方案，提交时按上方预期继承产物。\n"
            "「仅加载题目」：只填入标程/题面/输入/输出，不复用产物，提交时从零开始。"
        )
        wrap = max(320, min(520, self.root.winfo_width() - 120))
        tk.Label(dialog, text=msg, justify=tk.LEFT, wraplength=wrap).pack(
            padx=20, pady=(15, 10)
        )
        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=(0, 15))

        def choose(choice: str):
            result["choice"] = choice
            dialog.destroy()

        tk.Button(
            btn_frame, text="复用", width=12, command=lambda: choose("reuse"),
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="仅加载题目", width=12, command=lambda: choose("texts_only"),
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="取消", width=10, command=lambda: choose(""),
        ).pack(side=tk.LEFT, padx=5)
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(""))
        dialog.update_idletasks()
        req_w = max(440, dialog.winfo_reqwidth() + 20)
        req_h = max(220, dialog.winfo_reqheight() + 10)
        self.place_dialog(dialog, req_w, req_h, min_w=360, min_h=180, grab=True)
        dialog.wait_window()
        choice = result["choice"]
        return choice if choice else None

    def on_history_load(self):
        item = self._selected_history_item()
        if not item:
            messagebox.showinfo("提示", "请先选择一条历史记录")
            return
        kind = item.get("kind")
        if kind == "problem":
            ws = problem_store.load_problem(item["id"])
            if not ws:
                messagebox.showerror("加载失败", "未找到可用的标程/题面内容")
                return
            if self.apply_workspace(ws, confirm=True):
                self.pending_resume_parent_id = None
                self.prefer_skip_resume = False
                self._show_tab("stmt")
                self._append_log(f"从题库加载: {item.get('id')}")
            return

        # Job：先问是否复用产物
        ws = problem_store.load_job_workspace(item["id"])
        if not ws:
            messagebox.showerror("加载失败", "未找到可用的标程/题面内容")
            return
        artifacts = self._list_job_reusable_artifacts(item["id"])
        choice = self._ask_history_job_reuse(item, artifacts)
        if choice is None:
            return
        reuse = choice == "reuse"
        if self.apply_workspace(ws, confirm=True, load_range_plan=reuse):
            if reuse:
                jid = str(item["id"])
                done = self._describe_done_job_local(jid)
                if done:
                    # 成功任务：加载即挂下载，无需再点提交
                    self._append_log(
                        f"从 Job 加载并直接复用完成包: {jid}（有 data.zip，无需再提交）"
                    )
                    self._bind_done_job_for_download(done)
                    self._show_tab("stmt")
                    return
                self.pending_resume_parent_id = jid
                self.prefer_skip_resume = False
                level, tier_note = self._classify_history_artifacts(artifacts)
                self._append_log(
                    f"从 Job 加载并选择复用: {item.get('id')} "
                    f"（级别={level}，产物 {len(artifacts)} 项）— {tier_note}"
                )
                self.status.set(f"已加载 Job {item.get('id')}（将复用 · {level}）")
            else:
                self.pending_resume_parent_id = None
                self.prefer_skip_resume = True
                self._append_log(
                    f"从 Job 仅加载题目: {item.get('id')}（提交时跳过复用）"
                )
                self.status.set(f"已加载 Job {item.get('id')}（仅题目，不复用）")
            self._show_tab("stmt")

    def on_history_delete(self):
        item = self._selected_history_item()
        if not item:
            messagebox.showinfo("提示", "请先选择一条记录")
            return
        if item.get("kind") != "problem":
            messagebox.showinfo("提示", "只能删除题库条目；Job 目录请手动清理 jobs/")
            return
        if not messagebox.askyesno(
            "确认删除",
            f"删除题库题目？\n{item.get('title')}\n{item.get('id')}",
        ):
            return
        if problem_store.delete_problem(item["id"]):
            if self.current_problem_id == item["id"]:
                self.current_problem_id = ""
                self._update_problem_title_label("")
            self.on_history_refresh()
            self.status.set(f"已删除题库: {item['id']}")
        else:
            messagebox.showerror("删除失败", "目录不存在或不可删")

    def on_close(self):
        try:
            # 关闭时若已换题，自动另存，避免静默覆盖旧题库
            msg = self._persist_workspace(
                to_problem=True,
                to_job=bool(self.job_id),
                confirm_fork=False,
                auto_fork_on_diverge=True,
            )
            print(f"[gui] close save: {msg}")
        except Exception as e:
            try:
                messagebox.showwarning("保存提醒", f"关闭前保存未完全成功：{e}\n仍将退出。")
            except Exception:
                pass
        self.root.destroy()

    def root_after(self, func, ms=0):
        self.root.after(ms, func)


def main():
    _try_enable_dpi_awareness()
    root = ttk.Window(
        title="ACM 出数据",
        themename=_DEFAULT_THEME,
        size=(_MIN_W + 220, _MIN_H + 260),
        minsize=(_MIN_W, _MIN_H),
    )
    app = App(root)
    saved = problem_store.load_session()
    if saved:
        app.apply_workspace(saved, confirm=False)
        app.status.set("已恢复上次关闭前的工作区")
    else:
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


