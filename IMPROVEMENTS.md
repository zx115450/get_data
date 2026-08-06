# ACM 出数据工具 — 完善建议文档

> 基于对项目架构与实际痛点的分析。真正值得优先做的，是贴着出题成功率与运维成本的小改动；通用工程清单（Docker / Web UI / SQLite 等）按需再开。
>
> **优先级**：P0 = 建议优先吃 · P1 = 有条件再做 · P2 = 现阶段可搁置
>
> **进度（本地）**：P0 七项已落地（`config/`、LLM 重试/预算、`clean-jobs`、`agent_trace.jsonl`、`tests/`）。另：`agent/tools` 已按 write/run/checker/self_check/schema 拆包；`python main.py analyze-traces` 可汇总工具失败 / 题型 token / nudge。P1/P2 仍按原文按需。

---

## 一、高可取（建议优先吃）— P0 ✅ 已完成

这些项改动相对可控，直接降低「任务白跑 / 烧 token / 磁盘涨爆 / 改核心逻辑无回归网」的风险。

### 1.1 核心纯逻辑单元测试 · 约 3-5 天 ✅

不依赖真 LLM，优先锁住一改就影响出题成功率的函数。引入 `pytest`，放在 `tests/`。

| 模块 | 建议测试内容 |
|------|------------|
| `agent/llm.py` | `parse_tool_arguments` 截断 JSON 恢复、`compact_messages` 压缩、`TokenUsage` 累加、`_truncate` 边界 |
| `pipeline/gen_data.py` | `normalize_range_json`、`validate_range_json` |
| `server/runners/resume.py` | SHA256 指纹匹配、同题复用判定、失败上下文恢复 |
| `utils/markup.py` | `to_plain_for_llm` 的数学公式清洗 |
| `agent/tools.py` | 纯逻辑参数校验与返回值格式（不含真实编译） |
| `agent/prompts.py` | 按题型/阶段拼装是否正确 |
| `pipeline/pack.py` | zip 文件列表与内容完整性 |
| `server/job_store.py` | Job 创建/状态转换/并发锁 |

做法：先写上表前四行的参数化测试，再逐步补其余模块。

### 1.2 Mock LLM 的 Agent 循环测试 · 约 2-3 天 ✅

用预定 `tool_calls` 的 mock LLM 跑 `agent/core.py`，专门验证：

- 阶段切换（range → gen → check）
- nudge（模型只回文字不调工具）
- 写代码门禁 / `write_check_discipline`

比先铺满 FastAPI `TestClient` 更能锁住自研 Agent 逻辑。

### 1.3 LLM 429 / 5xx 指数退避重试 · 约 1 天 ✅

`agent/llm.py` 的 `chat()` 已对 413 特判，但 429（rate limit）与 5xx 直接抛出，容易整单打挂。

建议对瞬时失败做指数退避（如最多 3 次，`wait` 2–30s）。可用 `tenacity` 或手写 retry loop。收益直接，应先于「整套项目级异常类树」。

### 1.4 启动时配置校验 · 约 1 天 ✅

现状：各处 `os.getenv()`，`LLM_MODEL` 等配错要到第一次 API 调用才暴露。

建议用 `pydantic-settings`（或手写）在启动时一次性校验 `LLM_*` / 端口 / 超时等。不必和「多环境 `.env.test`」绑死。

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    llm_api_key: str
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 16384
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    llm_embedding_model: str = "text-embedding-3-small"
    server_host: str = "127.0.0.1"
    server_port: int = 8000

    model_config = {"env_file": ".env"}

settings = Settings()  # 启动即校验
```

### 1.5 Job 自动清理 · 约 1 天 ✅

`jobs/` 含 `data.zip` / `sources.zip`，会无限涨盘。

建议：

- 配置 `JOB_RETENTION_DAYS`（默认 30）或最多保留 N 个（LRU）
- CLI：`python main.py clean-jobs --keep-days 30`

比 SQLite 迁移更实在。

### 1.6 Agent 链路 JSONL 追踪 · 约 1-2 天

比「全面换成 logging 替代 print」更贴场景。每次 tool call 记一行 JSONL：

```jsonl
{"ts":"2026-08-06T12:00:01","job":"20260806_...","step":3,"tool":"write_gen","args_size":4523,"result":"OK","tokens_prompt":12400,"tokens_completion":5200}
```

便于分析：哪些工具易失败、哪类题 token 高、空转典型模式。进度仍可走现有 `job_store.add_progress()`，JSONL 与之并存即可。

### 1.7 Token 成本预算控制 · 约 1-2 天

已有用量统计，缺上限。建议：

- `.env`：`LLM_MAX_TOTAL_TOKENS`（单次任务上限）
- 每次调用前检查剩余预算，超限优雅终止
- GUI 可显示实时消耗（可选）

---

## 二、中等可取（有条件再做）— P1

| 条目 | 预估 | 何时值得做 |
|------|------|------------|
| ruff + 基础 CI | 0.5–2 天 | 已有一点单测之后；低成本护栏 |
| GUI 模块拆分（`gui.py` ~180KB） | 5–7 天 | 有人持续改 GUI 时；拆前需有回归网，勿与单测并列抢第一 |
| 健康检查增强（`GET /`：gcc / LLM / 磁盘 / 活跃 job） | 0.5 天 | 小改大用，随时可做 |
| Docker server-only | 2 天 | 要部署或协作统一环境时；本机出题收益有限 |
| `RangeJSON` / Job 等 TypedDict（局部） | 1–2 天 | 只标核心结构，勿全仓 mypy strict |
| API Key 认证 + 沙箱加固 | 1–2 天 | **仅当**暴露非本机或多人共用时 |
| 部分 seed 失败恢复（`out/<seed>.status.json`） | 2 天 | 大数据量 batch 失败频繁时 |
| FastAPI 集成测试（mock LLM） | 2–3 天 | Agent 循环测稳定后再补 HTTP 层 |
| 结构化日志渐进替换 print | 1–2 天 | 优先 JSONL 后，再按模块替换，避免大改通信路径 |

### GUI 拆分参考结构（需要时再动手）

```text
gui/
├── app.py
├── pages/          # std / statement / input_desc / output_desc / range / log
├── widgets/        # code_editor / markdown_view / status_bar
├── server_manager.py
├── api_client.py
└── session.py
```

### 健康检查示例

```json
{
  "status": "ok",
  "checks": {
    "gcc_available": true,
    "llm_configured": true,
    "embedding_configured": false,
    "active_jobs": 2,
    "disk_free_gb": 45.2
  }
}
```

---

## 三、现阶段可搁置 — P2

以下多为通用工程或长期愿景，本地单机出题场景 ROI 偏低，默认不做：

- SQLite 替代文件 JSON Job 存储
- Web UI 替代 tkinter、pyinstaller 打包、PyPI 发布
- 多模型投票 / Streaming / 语义同题缓存
- 多环境 `.env.test` / `.env.production`
- 英文 README、ISSUE 模板、bandit / Dependabot
- 整套细粒度异常类树（不如先做 429 重试）
- 并行编译缓存、统计面板大而全

---

## 优先级总览

| 优先级 | 条目 | 预估 |
|--------|------|------|
| **P0** | 核心纯逻辑单元测试 | 3–5 天 |
| **P0** | Mock LLM 的 Agent 循环测试 | 2–3 天 |
| **P0** | LLM 429 / 5xx 退避重试 | 1 天 |
| **P0** | 启动时配置校验 | 1 天 |
| **P0** | Job 自动清理 | 1 天 |
| **P0** | Agent 链路 JSONL | 1–2 天 |
| **P0** | Token 预算上限 | 1–2 天 |
| **P1** | ruff + CI（有测之后） | 0.5–2 天 |
| **P1** | 健康检查增强 | 0.5 天 |
| **P1** | GUI 拆分（有人改 GUI 时） | 5–7 天 |
| **P1** | Docker / API Key / 沙箱 / seed 恢复 / 局部类型 | 按需 |
| **P2** | SQLite / Web UI / 多模型 / 打包发布等 | 搁置 |

---

## 总结

项目在 Agent 模式、Prompt、阶段化流水线、GUI+CLI、同题复用与失败续跑上已经较成熟。短板不是「缺 LangChain 级框架」，而是：

1. **脆弱纯逻辑无自动化回归** — 先测 `llm` 解析/压缩、`range` 校验、resume、markup
2. **LLM 韧性不足** — 重试 + token 预算，减少白跑与烧爆
3. **运维与可观测** — Job 清理 + Agent JSONL，比先上 Docker / SQLite 更贴痛点

建议只按上文 **P0 七项**推进；P1 看是否远程部署、是否持续改 GUI；P2 默认不排期。
