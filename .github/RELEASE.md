# 发版说明 / Release Guide

本文件记录了本项目的发版流程，以及一个**必须避开的编码陷阱**。

---

## ⚠️ 关键陷阱：中文 Release Notes 会变成 `?`

### 现象

用 PowerShell 的 `Invoke-RestMethod` 提交含中文的 release body 时，
GitHub 上显示的内容会变成 `????????`（所有中文与 emoji 丢失）。

### 根因

PowerShell 在把 JSON body 交给 `Invoke-RestMethod` 时，**按本地代码页
（Windows 上通常是 GBK/cp1252）编码字符串**，而不是 UTF-8。中文在
**请求发出之前**就已经被替换成 `?`，所以这不是 GitHub 的问题 ——
服务端存下来的内容本身就是坏的。

判定方法：

```powershell
$rel = Invoke-RestMethod "https://api.github.com/repos/<owner>/<repo>/releases/tags/v1.0.0" -Headers $h
$rel.body -match '[\u4e00-\u9fa5]'   # False 表示已损坏
```

> 注：`git push` 走的是另一条链路，UTF-8 处理正确 ——
> 所以 README 等文件不会有这个问题，只有 API 提交的 body 会。

### 正确做法

构建 JSON 后，**显式转成 UTF-8 字节数组**再发送：

```powershell
$txt  = [System.IO.File]::ReadAllText($notesPath, [System.Text.Encoding]::UTF8)
$json = @{ tag_name = "v1.0.0"; name = "..."; body = $txt } | ConvertTo-Json -Depth 5
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

Invoke-RestMethod $apiUrl -Method Post -Headers $h `
  -Body $bytes -ContentType "application/json; charset=utf-8"
```

**两个要点缺一不可**：
1. `[System.Text.Encoding]::UTF8.GetBytes(...)` —— 保证字节是 UTF-8
2. `charset=utf-8` —— 告诉服务端按 UTF-8 解析

---

## 发版流程 / Release steps

### 1. 准备 release notes

在 `.github/release-notes/` 下新建 `v<version>.md`，内容即 Release 正文。
文件必须存为 **UTF-8（无 BOM）**。

### 2. 更新版本号

* `plugin.json` 的 `version` 字段（必须符合 semver，例如 `1.0.1`）
* 若最低 QwenPaw 版本要求变化，同步修改 `qwenpaw_version.min`

### 3. 运行测试

```bash
python -m unittest discover -s tests -t .
```

测试必须全绿再发版。

### 4. 打包

ZIP 内**必须**保持如下结构（顶层是插件目录，不是散文件）：

```
code-review-copilot/
├── plugin.json
├── plugin.py
├── reviewer.py
├── rules.py
└── README.md
```

按 `plugin.json` 的 `pack_exclude` 排除 `tests/`、`__pycache__/`、`*.pyc`：

```powershell
# 只复制运行时文件，不要整个目录打包
Compress-Archive -Path "$staging\code-review-copilot" `
                 -DestinationPath "code-review-copilot-<version>.zip" -Force
```

### 5. 创建 Release 并上传附件

按上文「正确做法」提交 body（UTF-8 字节），然后上传 ZIP：

```powershell
$up = @{ Authorization = "Bearer $tok"; "User-Agent" = "dsh"
         "Content-Type"  = "application/zip" }
Invoke-RestMethod "https://uploads.github.com/repos/<owner>/<repo>/releases/$relId/assets?name=<file>.zip" `
  -Method Post -Headers $up -Body ([System.IO.File]::ReadAllBytes($zipPath))
```

### 6. 验证（不要跳过）

```powershell
# a. 服务端 body 是正确中文
$rel.body -match '[\u4e00-\u9fa5]'

# b. 附件可下载且结构正确
#    解压后应有 code-review-copilot/plugin.json
```

安装命令格式：

```
qwenpaw plugin install https://github.com/<owner>/<repo>/releases/download/v<version>/code-review-copilot-<version>.zip
```

---

## 检查清单 / Checklist

发版前逐项确认：

- [ ] `plugin.json` 的 `version` 已更新为 semver
- [ ] `plugin.json` 的 `author` 正确（`hxx0611`）
- [ ] `qwenpaw_version.min` 与实际使用的 API 相符
- [ ] 全部单元测试通过
- [ ] ZIP 顶层是 `code-review-copilot/` 目录（**不是**散落的文件）
- [ ] ZIP 内**不含** `tests/` 与 `__pycache__/`
- [ ] Release body 用 UTF-8 字节提交，且服务端校验过中文
- [ ] 附件已上传且可下载
- [ ] 从公网下载解压后，引擎可正常导入运行
