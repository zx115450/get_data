# ACM 出数据工具 —— 使用说明

把一道算法题的**标程、题面、数据范围**填进去，程序会自动生成测试数据（`1.in` / `1.out` …），并打包成 zip 下载。

适合：出题人、助教、需要批量造数据的同学。  
不需要会写生成器代码，界面点几下即可。

---

## 一、你需要提前准备什么

在开始之前，请确认电脑上有这三样：

| 需要什么 | 干什么用 | 怎么检查 |
|---------|---------|---------|
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

3. 窗口打开后，先点顶部的 **「启动服务器」**  
   - 状态变成类似「运行中 PID: xxxx」就说明后端已就绪  
   - 不用了可以点 **「杀死服务器」**

> 也可以不用 GUI 按钮，另开一个终端执行 `python server/app.py`，效果一样。

---

## 五、出一套数据（完整流程）

按界面上的 Tab 从左到右填写。

### 1. 标程 (std)

- **语言**：选 `cpp` 或 `python`（和你的标程一致）
- **题型**：不会选就留「自动」
- 把**能正确通过样例的标程代码**完整粘贴进去

标程非常重要：后面生成的输入会丢给标程跑，才能得到 `.out` 答案。

### 2. 题面

粘贴题目描述（可以含 Markdown / LaTeX）。  
可用「简化」「美化」按钮让文字更干净（可选）。

### 3. 输入描述 / 数据范围

写清楚变量范围，例如：

```text
第一行一个整数 T (1 <= T <= 10)
每组数据：第一行 n (1 <= n <= 1e5)，第二行 n 个整数 a_i (1 <= a_i <= 1e9)
保证所有测试点 sum n <= 1e5
```

越清楚，生成的数据越靠谱。

### 3.5. 输出描述（可选，但推荐填）

写输出格式、多解规则等，例如：

```text
输出一行一个整数，表示答案。
若有多解，输出任意一组即可。
```

如果题目答案不唯一、需要 **Special Judge**，这里务必写清楚判定规则。

### 4. 数据方案（可选）

- 可点「生成方案」让 AI 先规划 `range.json`
- 也可以跳过，直接提交，由后端一并生成

### 5. 提交生成

1. 若题目需要特殊评测，勾选 **Special Judge**
2. 点 **「提交生成」**
3. 下方「过程日志」会滚动显示进度（准备标程 → Agent 写代码 → 批量生成 → 打包）
4. 成功后点 **「下载 zip」** 保存测试数据  
5. 若勾选了 Special Judge，还可点 **「下载 checker」**

耐心等待：一次完整生成通常要几分钟，取决于模型和题目复杂度。

---

## 六、下载到的 zip 里有什么

一般包含：

- `1.in` / `1.out`、`2.in` / `2.out` … 测试数据
- 以及生成过程中用到的 `gen.cpp`、`validator.cpp`、`range.json` 等（以实际打包结果为准）

把 `.in` / `.out` 拷到评测机或题库平台即可使用。

---

## 七、常见问题

### 1. 点「启动服务器」失败 / 提交报连不上

- 确认已点过 **启动服务器**，状态不是「未启动」
- 确认 `.env` 里 `SERVER_PORT=8000` 没有被别的程序占用  
- 关掉后再点一次「启动服务器」

### 2. 提示找不到 g++ / 编译失败

- 终端执行 `g++ --version`，确认已安装并加入 PATH  
- 改完 PATH 后要**重新打开** GUI / 终端

### 3. 提示 API Key 无效 / 401 / 余额不足

- 检查 `.env` 是否保存、Key 是否复制完整（不要多空格、少字符）
- 检查 `LLM_BASE_URL` 是否和平台一致
- 到对应平台看账户余额与权限

### 4. 生成到一半报 ERROR

- 看「过程日志」里的红色/ERROR 信息  
- 常见原因：题面与标程输入格式不一致、范围描述太含糊、标程本身有 bug  
- 改完题面/标程/范围后重新「提交生成」

### 5. Special Judge 什么时候勾？

- 答案不唯一（任意合法解都行）
- 浮点误差比较
- 需要按自定义规则判对错  

勾选后请尽量填写「3.5. 输出描述」，AI 写 checker 时会参考。

### 6. 每次都要重新装依赖吗？

不用。装过一次后，每次只需：

```powershell
cd 项目目录
.\.venv\Scripts\activate
python gui.py
```

再点「启动服务器」即可。

---

## 八、给别人发这个项目时注意

请打包/分享这些：

- `agent/`、`server/`、`pipeline/`、`utils/`
- `gui.py`、`requirements.txt`、`.env.example`、`.gitignore`、`README.md`

**不要分享：**

- `.env`（含你的密钥）
- `.venv/`（对方自己装）
- `jobs/`、`.cache/`（本地运行垃圾）

对方按本文「二、安装」和「三、配置」操作即可。

---

## 九、一句话流程（速查）

```text
装 Python + g++
  → 创建 .venv 并 pip install -r requirements.txt
  → 复制 .env.example 为 .env，填入 API Key
  → python gui.py
  → 点「启动服务器」
  → 填标程 / 题面 / 范围（可选输出描述）
  → 点「提交生成」
  → 完成后「下载 zip」
```

遇到卡住的步骤，把界面里的报错原文发给会用的同学，通常都能很快定位。
