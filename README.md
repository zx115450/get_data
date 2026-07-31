# ACM 出数据工具 —— 使用说明

把一道算法题的**标程、题面、输入描述、输出描述**填进去，程序会自动生成测试数据（`1.in` / `1.out` …），并打包成 zip 下载。

适合：出题人、助教、需要批量造数据的同学。  
不需要会写生成器代码，界面点几下即可。

---

## 一、你需要提前准备什么

在开始之前，请确认电脑上有这三样：

| 需要什么 | 干什么用 | 怎么检查 |
| --- | --- | --- |
| **Python 3.10 或更高** | 运行本软件 | 打开「命令提示符」或 PowerShell，输入 `python --version` |
| **g++ 编译器** | 编译标程和数据生成器 | 输入 `g++ --version`，能显示版本号即可 |
| **大模型 API Key** | 让 AI 帮你写生成器 | 需要自己去 DeepSeek / 通义 / OpenAI 等平台申请 |

### Windows 上没有 g++ 怎么办？

任选一种安装即可：

1. 安装 [MinGW-w64](https://www.mingw-w64.org/) 或 [MSYS2](https://www.msys2.org/)，并把 `g++.exe` 所在目录加入系统 PATH
2. 或安装 Visual Studio，并勾选「使用 C++ 的桌面开发」（本项目默认调用的是 `g++` 命令）

装好后**重新打开**终端，再执行一次 `g++ --version` 确认成功。

---

## 二、安装（只需做一次）

### 方式 A：一键脚本（推荐）

1. 把本项目解压到任意目录
2. **Windows**：双击 `setup.bat`，或在项目目录执行：

```powershell
cd F:\py_project\get_data
python setup.py
```

按提示选择大模型供应商、填写 API Key；是否启用 Embedding（不用可跳过）。  
脚本会自动生成 `.env`、创建 `.venv` 并安装依赖。

3. 用 **PyCharm** 打开项目文件夹 → 解释器选 `.venv` → 运行 `gui.py`。

### 方式 B：手动安装

1. 把本项目解压到任意目录，例如：`F:\py_project\get_data`
2. 打开 PowerShell / 命令提示符，进入该目录：

```powershell
cd F:\py_project\get_data
```

3. 创建虚拟环境（推荐，避免弄乱系统 Python）：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

成功后，命令行前面会出现 `(.venv)`。

4. 安装依赖：

```powershell
pip install -r requirements.txt
```

看到成功安装、没有红色报错即可。

---

## 三、配置 API Key（最重要）

1. 在项目根目录找到文件 `.env.example`
2. **复制一份**，改名为 `.env`（注意：前面有个点，没有 `.txt` 后缀）

```powershell
copy .env.example .env
```

3. 用记事本或 VS Code 打开 `.env`，填入你的 Key。

### 最常见配置示例（推荐国内）

**聊天用 DeepSeek，向量（RAG）用通义千问：**

```env
# 聊天模型
LLM_API_KEY=你的DeepSeek密钥
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# Embedding（和聊天分开时再填）
EMBEDDING_API_KEY=你的通义密钥
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_EMBEDDING_MODEL=text-embedding-v3

# 服务器（一般不用改）
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
```

**如果聊天和 Embedding 用同一家服务**，只需填 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_EMBEDDING_MODEL`，`EMBEDDING_*` 可以留空。

> 安全提醒：`.env` 里有密钥，**不要发给别人，也不要上传到 GitHub**。发给别人时只发 `.env.example`。

---

## 四、启动软件

以后每次使用，按下面做：

1. 打开终端，进入项目目录，激活虚拟环境：

```powershell
cd F:\py_project\get_data
.\.venv\Scripts\activate
```

2. 启动界面：

```powershell
python gui.py
```

3. 窗口打开后，先点顶部的 **「启动」**  
   - 状态显示端口与 PID 即表示后端已就绪  
   - 不用了可以点 **「停止」**

> 也可以不用 GUI 按钮，另开一个终端执行 `python -m uvicorn server.app:app --host 127.0.0.1 --port 8000`，效果类似。

---

## 五、出一套数据（完整流程）

按左侧导航从「1. 标程」到「5. 数据方案」填写，填完点顶部 **「提交生成」**。

### 1. 标程

- **语言**：选 `cpp` 或 `python`（和你的标程一致）
- **题型**：不会选就留「自动」。点「生成方案」时会**单独调用一次大模型**判定题型，写入 `range.json.problem_type` 并回填下拉框；完整任务若无方案也会在 Range 阶段同样判定。可选类型包括：
  - 基础：`array` / `tree` / `graph` / `string` / `number_theory` / `multi_test`
  - 扩展：`geometry` / `dp` / `matrix` / `range_query` / `weighted_tree` / `weighted_graph` / `interactive`
- 把**能正确通过样例的标程代码**完整粘贴进去

标程非常重要：后面生成的输入会丢给标程跑，才能得到 `.out` 答案。

树 / 图 / 几何 / 带权结构题会自动使用 **ACM-generator**（`generator.h`，基于 testlib），方便生成链、菊花、二分图、DAG、凸包等；Agent 仍须遵守本项目的 `--seed` / `--type` / `--index` / `--count` 契约。树/图正确写法是 `t.gen(); cout << t`（或 `for (auto &e : t.edges())`）；`get_edges()` / `t.shuffle()` 会被 `write_gen` 静态拒绝。

### 2. 题面

粘贴题目描述（可以含 Markdown / HTML / LaTeX）。

排版可选：

- 各 Tab 上的 **「美化」**：只美化当前字段，弹窗预览后可「保留」或「不保留」
- 顶部工具栏 **「一键美化」**：同时美化题面、输入描述、输出描述，**直接写回编辑区，无需确认**

### 3. 输入描述

写输入格式与变量范围，例如：

```text
第一行一个整数 T (1 <= T <= 10)
每组数据：第一行 n (1 <= n <= 1e5)，第二行 n 个整数 a_i (1 <= a_i <= 1e9)
保证所有测试点 sum n <= 1e5
```

写得越清楚，生成的数据越靠谱。同样可用「美化」或顶部「一键美化」。

### 4. 输出描述（可选，但推荐填）

写输出格式、多解规则等，例如：

```text
输出一行一个整数，表示答案。
若有多解，输出任意一组即可。
```

如果题目答案不唯一、需要 **Special Judge**，这里务必写清楚判定规则。

### 5. 数据方案（可选）

「数据方案」对应后端的 `range.json`（测例组数、变量上下界、边界类型名等）。

- 可点 **「用大模型生成方案」**，先预览再提交；提交时会带上该方案并**跳过写 range**
- 也可以跳过本页，直接提交：后端会先跑一轮 Range Agent 写出 `range.json`，再写 `gen` / `validator`

**何时算「已有方案」？**

- GUI：测例组数 `count > 0`，且至少有一个变量约束（constraints 非空）
- 后端：提交体里带了合法的 `range_json`（含非空 `constraints`），并通过结构校验

### 6. 提交生成

1. 若题目需要特殊评测，勾选 **Special Judge**
2. 可选 **内置 Checker**：`lcmp`（按行比 token）/ `wcmp`（按词）/ `rcmp4|6|9`（浮点）/ `yesno`
   - 未开 Special Judge 时选中，也会额外打包一份标准比较器，方便上传 OJ
   - 开了 Special Judge 时，会优先让 AI 用该内置比较器，而不是手写 checker
3. 点 **「提交生成」**（快捷键 `Ctrl+Enter`）
4. 下方「过程日志」会滚动显示进度，大致为：

```text
准备标程
  →（可选）同题复用历史产物
  →（若无方案）Range Agent 写 range.json
  → Plan 写 gen_plan.md
  → Coder 写 gen / validator
  → 自检 → 批量生成 → 打包
```

5. 成功后：
   - **下载 zip**：仅测例 `1.in` / `1.out` …
   - **下载源码**：`gen.cpp` / `validator.cpp` / `range.json` 等，方便二次修改
   - **下载 checker**（若有）：special judge 或内置比较器

耐心等待：一次完整生成通常要几分钟，取决于模型和题目复杂度。树 / 图若用了 `generator.h`，单次编译可能稍慢（约十余秒），属正常现象。

任务运行中与结束后，状态栏会显示**耗时**与 **token 用量**（prompt / completion / total，以及调用次数）；过程日志末尾也会有一行 `【统计】`。

### 同题复用（自动）

提交时，后端会尝试从 `jobs/` 里**最近 3 个带 `failure_context.json` 的历史任务**中找「同题」：

- 判定依据：题面原文 SHA256 + 标程原文 SHA256 + 语言（`cpp` / `python`）三者完全一致
- 命中后会复制父任务已有产物（例如 `range.json`），少做重复劳动
- 因此：即使 GUI 没填「数据方案」，日志里也可能出现「同题校验通过…复制 range.json」，并跳过写 range

注意：

- 只比对原文指纹，不看语义；题面或标程改一个字就不算同题
- `failure_context.json` 一般在任务**失败或取消**时写入；纯成功结束的 job 通常不会成为候选父任务

---

## 六、下载到的文件里有什么

### 测例 zip（「下载 zip」）

扁平结构，只含：

- `1.in` / `1.out`、`2.in` / `2.out` …

适合直接丢给评测机。

### 源码包（「下载源码」）

通常包含：

- `range.json`、`gen.cpp`、`validator.cpp`
- 若有：`checker.cpp`、标程源码
- `testlib.h` / `generator.h`（从公共 `sandbox/` 打入，运行时 job 目录不再落盘这两份头文件）

方便你本地改生成器再造数据。

### checker zip（「下载 checker」）

Special Judge 或内置比较器产物（`checker.cpp` / 可执行文件 / `testlib.h`）。

---

## 七、常见问题

### 1. 点「启动」失败 / 提交报连不上

- 确认已点过顶部 **启动**，状态不是「未启动」
- 确认 `.env` 里 `SERVER_PORT=8000` 没有被别的程序占用
- 关掉后再点一次「启动」（会先清理占用该端口的进程）

### 2. 提示找不到 g++ / 编译失败

- 终端执行 `g++ --version`，确认已安装并加入 PATH
- 改完 PATH 后要**重新打开** GUI / 终端

### 3. 提示 API Key 无效 / 401 / 余额不足

- 检查 `.env` 是否保存、Key 是否复制完整（不要多空格、少字符）
- 检查 `LLM_BASE_URL` 是否和平台一致
- 到对应平台看账户余额与权限

### 4. 生成到一半报 ERROR

- 看「过程日志」里的红色 / ERROR 信息
- 常见原因：题面与标程输入格式不一致、范围描述太含糊、标程本身有 bug
- 改完题面 / 标程 / 输入描述后重新「提交生成」
- 若上次失败留下了 `failure_context.json`，再次提交同题会自动复用部分产物并续跑

### 5. 日志写「未提供数据方案」，却又跳过了写 range？

多半是触发了**同题复用**，从旧 job 拷来了 `range.json`。  
若要强制重新规划 range：改一丁点题面或标程（使指纹变化），或先清空相关历史 `jobs/` 目录后再提交。

### 6. Special Judge / 内置 Checker 什么时候用？

- **Special Judge**：答案不唯一、或要按自定义规则判对错时勾选，并尽量填写「输出描述」
- **内置 Checker**：答案唯一时优先选 `lcmp` / `wcmp` / `rcmp*` / `yesno`，不必让 AI 手写比较器
- 浮点题用 `rcmp4`（1e-4）/ `rcmp6` / `rcmp9`；Yes/No 题用 `yesno`

### 7. 每次都要重新装依赖吗？

不用。装过一次后，每次只需：

```powershell
cd 项目目录
.\.venv\Scripts\activate
python gui.py
```

再点「启动」即可。

---

## 八、给别人发这个项目时注意

请打包 / 分享这些：

- `agent/`、`server/`、`pipeline/`、`utils/`、`sandbox/`（含 `testlib.h`、`generator.h`、内置 checker 源码）、`data/`
- `gui.py`、`requirements.txt`、`.env.example`、`.gitignore`、`README.md`

其中 **`data/few_shots_rag_corpus.json`** 是 RAG few-shot 语料（含历史优质范例与向量）。  
别人拿到后首次运行会自动加载，有助于提高生成准确性。你本地成功出题后，语料会同步写回这个文件，记得一并提交 / 打包。

**不要分享：**

- `.env`（含你的密钥）
- `.venv/`（对方自己装）
- `jobs/`、`.cache/`（本地运行副本；种子在 `data/` 里已够用）

对方按本文「二、安装」和「三、配置」操作即可。

> 提示：若对方 `.env` 里的 `LLM_EMBEDDING_MODEL` 与语料生成时不一致，程序会自动按新模型重算向量，不影响使用（首次可能多花一点 embedding 费用）。

---

## 九、一句话流程（速查）

```text
装 Python + g++
  → python setup.py（或双击 setup.bat）填 API、装依赖
  → 运行 gui.py，点「启动」
  → 填标程 / 题面 / 输入描述（可选输出描述、数据方案）
  → 可选「一键美化」
  →「提交生成」→「下载 zip」
```

遇到卡住的步骤，把界面里的报错原文发给会用的同学，通常都能很快定位。
