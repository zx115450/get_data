#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键初始化：交互填写大模型配置 → 生成 .env → 创建 venv → 安装依赖。

用法（在项目根目录）：
    python setup.py

Windows 也可双击 setup.bat。

完成后用 PyCharm 打开本项目，解释器选 .venv，运行 gui.py 即可。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
REQ_PATH = ROOT / "requirements.txt"
VENV_DIR = ROOT / ".venv"

# 预设：显示名 -> (base_url, 默认模型)
CHAT_PRESETS: dict[str, tuple[str, str]] = {
    "1": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "2": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
    "3": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "4": ("", "gpt-4o-mini"),  # OpenAI 官方，base 可留空
    "5": ("", ""),  # 自定义
}

CHAT_LABELS = {
    "1": "DeepSeek（推荐国内）",
    "2": "通义千问 / 阿里云 DashScope",
    "3": "智谱 GLM",
    "4": "OpenAI 官方",
    "5": "自定义（自己填 Base URL 和模型名）",
}

EMBED_PRESETS: dict[str, tuple[str, str]] = {
    "1": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "text-embedding-v3"),
    "2": ("https://open.bigmodel.cn/api/paas/v4", "embedding-3"),
    "3": ("", "text-embedding-3-small"),
    "4": ("", ""),
}

EMBED_LABELS = {
    "1": "通义千问 embedding（推荐国内，配合 DeepSeek 聊天）",
    "2": "智谱 embedding-3",
    "3": "OpenAI text-embedding-3-small",
    "4": "自定义",
}


def _print(msg: str = "") -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("gbk", errors="replace").decode("gbk"))


def _input(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raw = ""
    return raw if raw else default


def _yes_no(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = _input(f"{prompt} ({d})", "y" if default else "n").lower()
        if ans in ("y", "yes", "是"):
            return True
        if ans in ("n", "no", "否"):
            return False
        if not ans:
            return default
        _print("请输入 y 或 n")


def _choose(title: str, labels: dict[str, str]) -> str:
    _print(f"\n{title}")
    for k, name in labels.items():
        _print(f"  {k}. {name}")
    keys = set(labels)
    while True:
        choice = _input("请输入序号", "1")
        if choice in keys:
            return choice
        _print(f"无效序号，请输入 {'/'.join(sorted(keys))}")


def _escape_env(value: str) -> str:
    """简单转义：含空格或 # 时加引号。"""
    if not value:
        return ""
    if any(c in value for c in ' \t#"\'\\'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def collect_chat_config() -> tuple[str, str, str]:
    choice = _choose("选择【聊天大模型】供应商（写 gen/validator 用）", CHAT_LABELS)
    base, model = CHAT_PRESETS[choice]
    if choice == "5":
        base = _input("LLM Base URL（OpenAI 兼容，如 https://api.xxx.com/v1）")
        model = _input("模型名称", "gpt-4o-mini")
    else:
        model = _input("模型名称（回车用默认）", model)
        if choice == "4" and not base:
            custom_base = _input("Base URL（OpenAI 官方可直接回车留空）", "")
            base = custom_base

    api_key = ""
    while not api_key:
        api_key = _input("聊天模型 API Key")
        if not api_key:
            _print("API Key 不能为空")
    return api_key, base, model


def collect_embed_config() -> tuple[str, str, str] | None:
    if not _yes_no("是否启用 Embedding / RAG 语义召回？", default=False):
        _print("已跳过 Embedding：将按题型关键词选用固定模板。")
        return None

    choice = _choose("选择【Embedding】供应商", EMBED_LABELS)
    base, model = EMBED_PRESETS[choice]
    if choice == "4":
        base = _input("Embedding Base URL")
        model = _input("Embedding 模型名", "text-embedding-v3")
    else:
        model = _input("Embedding 模型名（回车用默认）", model)
        if choice == "3":
            custom_base = _input("Base URL（OpenAI 官方可回车留空）", "")
            base = custom_base

    api_key = ""
    while not api_key:
        api_key = _input("Embedding API Key")
        if not api_key:
            _print("API Key 不能为空")
    return api_key, base, model


def write_env(
    chat_key: str,
    chat_base: str,
    chat_model: str,
    embed: tuple[str, str, str] | None,
) -> None:
    if ENV_PATH.exists():
        if not _yes_no(f"已存在 {ENV_PATH.name}，是否覆盖？", default=False):
            _print("保留原有 .env，不覆盖。")
            return

    lines = [
        "# 由 setup.py 自动生成，勿提交到 Git",
        "",
        "# ---- 聊天模型 ----",
        f"LLM_API_KEY={_escape_env(chat_key)}",
        f"LLM_BASE_URL={_escape_env(chat_base)}",
        f"LLM_MODEL={_escape_env(chat_model)}",
        "",
        "# ---- Embedding / RAG（留空则自动降级为关键词模板）----",
    ]
    if embed:
        ek, eb, em = embed
        lines.extend([
            f"EMBEDDING_API_KEY={_escape_env(ek)}",
            f"EMBEDDING_BASE_URL={_escape_env(eb)}",
            f"LLM_EMBEDDING_MODEL={_escape_env(em)}",
        ])
    else:
        lines.extend([
            "EMBEDDING_API_KEY=",
            "EMBEDDING_BASE_URL=",
            "LLM_EMBEDDING_MODEL=",
        ])

    lines.extend([
        "",
        "# ---- 服务器 ----",
        "SERVER_HOST=127.0.0.1",
        "SERVER_PORT=8000",
        "",
    ])
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    _print(f"已写入 {ENV_PATH}")


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _venv_pip() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "pip.exe"
    return VENV_DIR / "bin" / "pip"


def ensure_venv_and_deps() -> None:
    py = sys.executable
    ver = sys.version_info
    if ver < (3, 10):
        _print(f"需要 Python 3.10+，当前为 {ver.major}.{ver.minor}.{ver.micro}")
        sys.exit(1)

    if not VENV_DIR.exists():
        _print(f"\n创建虚拟环境: {VENV_DIR}")
        subprocess.check_call([py, "-m", "venv", str(VENV_DIR)])
    else:
        _print(f"\n虚拟环境已存在: {VENV_DIR}")

    vpy = _venv_python()
    if not vpy.exists():
        _print(f"找不到虚拟环境解释器: {vpy}")
        sys.exit(1)

    _print("升级 pip …")
    subprocess.check_call([str(vpy), "-m", "pip", "install", "--upgrade", "pip"])

    if not REQ_PATH.exists():
        _print(f"缺少 {REQ_PATH.name}")
        sys.exit(1)

    _print(f"安装依赖: {REQ_PATH.name} …")
    subprocess.check_call([str(vpy), "-m", "pip", "install", "-r", str(REQ_PATH)])
    _print("依赖安装完成。")


def print_pycharm_tips() -> None:
    vpy = _venv_python()
    _print("\n" + "=" * 56)
    _print("初始化完成。用 PyCharm 运行：")
    _print("  1. File → Open → 选择本项目文件夹")
    _print("  2. Settings → Project → Python Interpreter")
    _print(f"     选择已有解释器: {vpy}")
    _print("  3. 右键 gui.py → Run 'gui'")
    _print("  4. 窗口里点「启动服务器」，再填题出数据")
    _print("")
    _print("命令行也可以：")
    if os.name == "nt":
        _print("  .\\.venv\\Scripts\\activate")
        _print("  python gui.py")
    else:
        _print("  source .venv/bin/activate")
        _print("  python gui.py")
    _print("=" * 56)
    _print("提醒：出 C++ 数据需要本机已安装 g++，并在 PATH 中。")


def main() -> None:
    os.chdir(ROOT)
    _print("ACM 出数据工具 — 环境初始化")
    _print(f"项目目录: {ROOT}")

    chat_key, chat_base, chat_model = collect_chat_config()
    embed = collect_embed_config()
    write_env(chat_key, chat_base, chat_model, embed)

    if _yes_no("是否现在创建虚拟环境并安装依赖？", default=True):
        try:
            ensure_venv_and_deps()
        except subprocess.CalledProcessError as e:
            _print(f"安装失败 (exit={e.returncode})，请检查网络或手动: pip install -r requirements.txt")
            sys.exit(e.returncode)
    else:
        _print("已跳过依赖安装。可稍后手动: python -m venv .venv && pip install -r requirements.txt")

    print_pycharm_tips()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _print("\n已取消。")
        sys.exit(130)
