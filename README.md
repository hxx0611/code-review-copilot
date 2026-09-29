# Code Review Copilot

只读代码评审插件 for [QwenPaw](https://github.com/agentscope-ai/QwenPaw)。
A read-only code review plugin: static rules for common defects **plus** a
context bundle so the agent can perform an AI semantic review.

---

## 它能做什么 / What it does

| 工具 / Tool | 作用 |
| --- | --- |
| `review_git_diff` | 评审**未提交**的改动（工作区 / 暂存区） |
| `review_rev_range` | 评审**已提交**的区间，如 `main..HEAD` |
| `review_context` | 提取变更内容，交给 **AI 做语义评审** |
| `/review` | 斜杠命令，等价于 `review_rev_range` |

### 静态规则 / Static rules

| 严重度 | 检查项 |
| --- | --- |
| 🔴 Critical | AWS key、`sk-` API key、私钥块 |
| 🟠 Major | 硬编码凭证、提交了 `.env`、`eval`/`exec`、`pickle`/`yaml.load`、`shell=True`、SQL 字符串拼接、`innerHTML`、空的 `catch`、被吞掉的异常 |
| 🟡 Minor | `md5`/`sha1`、过于宽泛的 `except Exception`、调试输出残留 |
| 🔵 Info | `TODO`/`FIXME` 标记、超大文件变更 |

### 为什么需要 AI 语义评审

静态规则只能匹配**已知模式**。逻辑错误、边界条件、资源泄漏、竞态、命名不清
这类问题必须理解语义才能发现。

推荐工作流：

```
1. review_git_diff        → 快速抓出确定性问题
2. review_context         → 拿到变更内容
3. （Agent 自己分析）      → 语义级评审，按 file:line 报告
```

`review_context` 的返回值里已经写好了给模型的指令，Agent 会据此输出结构化结论。

---

## 安装 / Install

```bash
# 从本地目录安装
qwenpaw plugin install /path/to/code-review-copilot

# 或从 ZIP 安装
zip -r code-review-copilot-1.0.0.zip code-review-copilot/
qwenpaw plugin install https://example.com/code-review-copilot-1.0.0.zip
```

安装后在 **Agent 设置 → 工具** 中启用 `review_git_diff` 等工具（默认不启用）。

---

## 使用 / Usage

### 对话中直接提问

> 帮我评审一下当前的改动
>
> review 一下 `main..HEAD`，重点看有没有安全问题
>
> 看看我的改动有没有逻辑漏洞（AI 语义评审）

### 斜杠命令

```
/review
/review main..HEAD
/review HEAD~3..HEAD
```

### 配置项

| 工具 | 配置 | 说明 |
| --- | --- | --- |
| `review_git_diff` | `repo_dir` | 仓库路径，留空用 Agent 工作区 |
| | `ignore` | 忽略路径的正则，逗号分隔，如 `^vendor/,^third_party/` |
| `review_rev_range` | `rev_range` | 默认区间，如 `HEAD~1..HEAD` |
| `review_context` | `max_files` | 纳入 AI 评审的最大文件数（默认 30） |

---

## 安全设计 / Security

本插件**只读**，且有明确边界：

* **只允许只读 git 子命令**：`diff` / `log` / `show` / `status` /
  `rev-parse` / `ls-files` / `rev-list`。其它一律拒绝（有单元测试覆盖）。
* **不通过 shell 执行**：始终以参数数组调用 `subprocess`，不做字符串拼接，
  因此不存在命令注入。
* **不写仓库**：不修改任何文件、不创建提交、不改索引。
* **工具类型如实声明**：声明为 `tool_type="shell"`，因为确实调用了 git。
  这样治理层（governance）能正常审计，不会被当作 `file`/`internal` 绕过检查。
* **不联网**：全部逻辑在本地完成，无外部 API 调用。

---

## 设计说明 / Design notes

* **零依赖**：只用 Python 标准库，不需要任何 API key。
* **引擎与宿主解耦**：`reviewer.py` / `rules.py` 是纯逻辑，可脱离 QwenPaw
  独立测试（见 `tests/`）。
* **语言跟随配置**：报告语言跟随 Agent 的 `language` 设置；文本始终按
  UTF-8 生成，仅在直接打印到旧编码终端（GBK/cp1252）时才降级，
  避免 Windows 下崩溃或乱码。
* **保守判定**：宁可少报也不误报。例如参数化查询
  `execute("...%s", (x,))` 是**正确写法，绝不报警**（有测试锁死）。

---

## 开发 / Development

```bash
# 运行全部测试（73 个，无需安装 QwenPaw）
python -m unittest discover -s tests -t .
```

测试覆盖：
* diff 解析（行号、多文件、二进制/锁文件跳过、边界情况）
* 规则命中与**误报防护**
* git 安全白名单（写操作必须被拒绝）
* 插件注册（工具数量、`tool_type`、异步签名、命令注册）
* 真实临时仓库的端到端评审

### 扩展规则

编辑 `rules.py` 的 `RULES` 元组：

```python
Rule(
    id="my.custom-rule",
    severity="major",
    title="Short title",
    pattern=r"regex-against-stripped-added-line",
    message="Why this is a problem.",
    suggestion="How to fix it.",
    include_paths=r"\.py$",      # 可选：只检查匹配的路径
    exclude_paths=r"^tests/",    # 可选：跳过匹配的路径
),
```

路径类规则（如"提交了 .env"）放在 `PATH_RULES`。

---

## 局限 / Limitations

* 规则是**模式匹配**，无法理解语义 —— 复杂逻辑问题需靠 AI 评审或人工。
* 一次最多扫描 `max_files` 个文件（默认 50），超大改动会被截断。
* 需要本地安装 `git` 并在 `PATH` 中。
* 工作区默认路径取自 Agent 配置；非 git 仓库会返回明确错误而非崩溃。

---

## 许可 / License

与 QwenPaw 项目保持一致。
