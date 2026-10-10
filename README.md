<div align="center">
<p><strong>简体中文</strong> | <a href="./README_EN.md">English</a></p>
<table>
<tr>
<td>
<pre>
   ██████╗ ██████╗ ██████╗ ███████╗██╗  ██╗████████╗ ██████╗ ██████╗  
  ██╔════╝██╔═══██╗██╔══██╗██╔════╝╚██╗██╔╝╚══██╔══╝██╔═══██╗██╔══██╗ 
  ██║     ██║   ██║██║  ██║█████╗   ╚███╔╝    ██║   ██║   ██║██████╔╝ 
  ██║     ██║   ██║██║  ██║██╔══╝   ██╔██╗    ██║   ██║   ██║██╔═══╝  
  ╚██████╗╚██████╔╝██████╔╝███████╗██╔╝ ██╗   ██║   ╚██████╔╝██║      
   ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝      
</pre>
</td>
</tr>
</table>

<strong>CodexTOP：本地 Codex 多账号额度用量监控与管理工具</strong>

基于 Rich 的可点击交互终端界面。
</div>

<div align="center">
  <img src="./assets/demo-1.png" alt="CodexTOP focus mode" width="820">
  <p>专注模式</p>
  <img src="./assets/demo-2.png" alt="CodexTOP board mode" width="820">
  <p>看板模式</p>
  <img src="./assets/demo-3.png" alt="CodexTOP merged mode" width="820">
  <p>合并模式</p>
  <img src="./assets/demo-4.png" alt="CodexTOP usage mode" width="820">
  <p>用量模式</p>
  <img src="./assets/demo-summary.png" alt="CodexTOP usage mode" width="410">
  <p>数据统计</p>
  <img src="./assets/demo-auth.png" alt="CodexTOP usage mode" width="820">
  <p>账号管理</p>
  <img src="./assets/demo-check.png" alt="CodexTOP single check" width="820">
  <p>命令行单次查询</p>
</div>

## 功能概览

| 状态 | 功能 | 说明 |
|---|---|---|
| ✅已支持 | 彩色可交互终端界面 | 支持看板、专注、合并和用量模式；额度与 Token 曲线分开展示 |
| ✅已支持 | 命令行用量查询 | `CHECK_CODEX_QUOTA` 输出账号额度和每日缓存的联网使用总量 |
| ✅已支持 | 后台采样服务 | 用量与额度重置次数独立查询、缓存，并写入本地 JSONL |
| ✅已支持 | 多账号切换 | `CODEXAUTH` 管理 `auth-openai-*` 账号并切换 provider |
| ✅已支持 | 版本更新 | 仅根据 GitHub Releases 检测新版本，并自动快进同步 |
| ✅已支持 | Token 用量统计 | 用量模式增量读取可信目录根线程日志；支持四类曲线、堆叠柱状和精细柱状 |
| ✅已支持 | 周期统计总结 | 每日首次打开显示昨日总结；侧栏可查看日、周、月额度与 Token 统计 |
| ✅已支持 | 新账号注册 | 支持新账号接入和初始化流程 |
| ⏩规划中 | 额度重置通知 | 额度恢复后通过系统通知提醒 |

## 安装

### 方式 A：让 Codex 自动安装（推荐）

把下面的安装说明链接直接发给 Codex，它会自动选择用户级安装目录、检查依赖、备份已有安装、完成安装与验证，并在命令行输出后续使用指引：

- GitHub 页面：<https://github.com/JamesZhutheThird/codextop/blob/main/AI_AGENT_INSTALL.md>
- Raw Markdown：<https://raw.githubusercontent.com/JamesZhutheThird/codextop/main/AI_AGENT_INSTALL.md>

可以直接发送：

```text
请按照这个说明自动安装 CodexTOP：
https://github.com/JamesZhutheThird/codextop/blob/main/AI_AGENT_INSTALL.md
```

在已能正常使用 Codex CLI 的 Linux/WSL + zsh 环境中，不需要手工选择安装目录、编辑 shell 配置或准备账号文件；已有的 `~/.codex/auth.json` 会在首次运行时自动导入。安装流程不会无备份覆盖已有源码目录或 `.zshrc`。

### 方式 B：手动安装

克隆仓库并在仓库根目录执行：

```bash
git clone https://github.com/JamesZhutheThird/codextop.git
cd codextop
python3 -m pip install --user 'rich>=13.0.0'
./scripts/install_zshrc.sh
source ~/.zshrc
rehash
```

安装后可直接使用这些命令：

```bash
CODEXTOP                # 打开 CodexTOP 主界面
CHECK_CODEX_QUOTA       # 单次查看账号额度信息
CODEXAUTH               # 查看当前账号信息/切换账号
CODEXTOP_UPDATE         # 检测 GitHub 更新并自动同步
```

## 配置

### 语言

首次启动时会先读取 `CODEXTOP_LANG`，未设置时依次参考系统的 `LC_ALL`、`LC_MESSAGES`、
`LANGUAGE` 和 `LANG`。目前内置 `en` 与 `zh_CN`；系统语言不受支持或无法识别时使用英文。
可以在首次运行前显式指定：

```bash
export CODEXTOP_LANG=zh_CN  # 简体中文
export CODEXTOP_LANG=en     # English
```

主界面按 `F9` 进入设置后，可通过“界面语言”在简体中文和 English 之间切换。选择会保存到
`codextop_state.json`，优先于环境自动检测，并立即生效，不会重新加载历史数据。WSL 如果使用
`C.UTF-8`，其中不包含可识别的语言代码，因此首次启动会回退到英文；在设置中选择一次
简体中文即可持久生效。

历史与 Token 派生索引缓存在 `~/.codex/codextop/cache/`；月度 JSONL 和 Codex 会话日志仍是
权威数据源。缓存与源文件不一致、版本不兼容或内容损坏时会被自动丢弃并重建。

翻译文件位于 `src/core/locales/`，每种语言对应一个 JSON 文件。新增语言时复制
`en.json`，保留相同的 key 并翻译 value；文件名就是 `CODEXTOP_LANG` 使用的语言代码。

### 运行路径

默认使用 Codex 目录：

```bash
~/.codex
```

也可以改成其他目录：

```bash
export CODEXTOP_CODEX_DIR=/path/to/.codex
```

账号文件放这里：

```bash
~/.codex/codextop/auth_list/auth-openai-1.json
~/.codex/codextop/auth_list/auth-openai-2.json
~/.codex/codextop/auth_list/auth-openai-3.json
```

文件名格式是 `auth-{关键词}.json`，例如 `auth-openai-1.json` 的关键词是 `openai-1`。

安装或首次运行时，如果 auth list 为空且 `~/.codex/auth.json` 已存在，会自动复制为 `auth-openai-1.json`。

运行时数据会自动写到：

```bash
~/.codex/codextop/auth_list/backup/
~/.codex/codextop/log/
~/.codex/codextop/log/quota_snapshots_YYYY-MM.jsonl
~/.codex/codextop/settings/current_provider.json
~/.codex/codextop/settings/auth_registry.json
~/.codex/codextop/settings/token_usage_daily.json
```

同步一次配置：

```bash
CODEXAUTH sync
```

## 使用

打开 CodexTOP 主界面：

```bash
CODEXTOP
```

### 账号切换功能

查看当前 provider 和账号列表：

```bash
CODEXAUTH list
```

通过 Codex device auth 新增 provider：

```bash
CODEXAUTH --new 4                 # 新增并切换到 openai-4
CODEXAUTH --new name_of_provider  # 使用自定义 provider 名称
CODEXAUTH --new my-api --api      # 交互导入 API 源并切换
```

`--new` 不带 `--api` 时，会启动并维护一个 `codex login --device-auth` 子进程，登录网址、设备码和进度会
原样输出到当前终端。系统提示与 Codex 登录信息之间会保留空行；等待登录时可按 `Ctrl-C` 取消。
按照屏幕提示在网页版完成登录后，CodexTOP 会检查 `codex login status`、
账号信息和额度 status；全部通过后才会保存 `auth-{provider}.json`、切换到新 provider、
更新 auth registry，并通知后台采样器立即刷新数据。数字 `4` 会规范化为 `openai-4`。

手动刷新已有 provider（不指定时刷新当前 provider）：

```bash
CODEXAUTH --refresh
CODEXAUTH --refresh openai-2
```

刷新同样在临时目录中执行 device auth。只有新 auth 通过登录状态和在线额度校验，且
token 邮箱、在线返回邮箱与原 provider 邮箱一致时，才会备份并替换原 auth；任一条件
无法明确验证时，原 provider 和当前 `auth.json` 都不会被覆盖。

删除已注册 provider（不指定时以当前 provider 为目标）：

```bash
CODEXAUTH --delete            # 删除当前 provider
CODEXAUTH --delete openai-2
```

删除前必须按提示再次输入完整且完全一致的 provider ID，例如 `openai-2`。确认成功后会
删除该 provider 的 auth 文件及其凭据备份；若删除当前 provider，会改用另一个已注册
provider，没有剩余 provider 时则移除当前 `auth.json`。已有额度历史记录不会被删除。

重命名已有账号或 API 源：

```bash
CODEXAUTH --rename openai-2 work
CODEXAUTH --rename my-api proxy
```

重命名会更新账号文件、备份目录和当前 provider 标记；已有额度历史记录保留原名称。

添加 API 源时，`CODEXAUTH --new my-api --api` 会打开终端交互界面，选择手动输入 API Key
（隐藏输入），或在 PATH 中填写环境变量名（如 `OPENAI_API_KEY`，需先在当前终端 export）。
输入后检查变量是否存在且非空，并显示脱敏密钥（如 `sk-a******z`）；每次切换到该源时
都会重新检查环境变量。此方式仅在 `config.toml` 的 provider 配置中写入 `env_key = "变量名"`，
由 Codex 在运行时读取密钥，不生成 `auth.json`；切换时会移除当前 `auth.json`，
原 ChatGPT 凭证保存在账号列表中，切回时恢复。交互界面无外框，与普通 `--new` 风格一致。Base URL 留空默认
`https://api.openai.com/v1`，第三方源填写其兼容 Responses API 的地址。
导入后自动切换，后续使用 `CODEXAUTH switch my-api`。凭证以 `0600` 权限保存，API Key
不写入列表或 JSON 摘要。导入检查本地配置与密钥格式，不发送在线验证请求；API 源不提供
ChatGPT 订阅额度查询，也不使用 `--refresh` 设备登录流程。

切换 provider：

```bash
CODEXAUTH switch 1
CODEXAUTH switch 2
CODEXAUTH switch 3
CODEXAUTH switch openai-1
CODEXAUTH switch my-api
CODEXAUTH 5                       # API account number
```

参数形式为 `CODEXAUTH [--new/--refresh/--delete/--rename/...] provider_name [...] -f`，兼容原有 `switch` 和数字目标。

```bash
CODEXAUTH work -f
CODEXAUTH --new work -f
CODEXAUTH --new my-api --api -f
CODEXAUTH --refresh work -f
CODEXAUTH --rename work home -f
CODEXAUTH --delete home -f
CODEXAUTH --kill                  # 关闭当前用户的全部 Codex 进程
```

`--kill` 和 `-f` 共用关闭流程：若 CLI 支持，先执行 `codex app-server daemon stop`，再对剩余独立进程发送 `SIGTERM` 并等待退出；不会使用 `SIGKILL`。进程未能退出时操作报错，账号操作不会继续。`--delete -f` 仍需要完整 ID 确认。

关闭前会记录前台 Codex 所在的终端，成功退出后关闭这些终端的鼠标与焦点事件上报，避免 tmux pane 返回 shell 后出现 `35;23;47M` 等字符。后台进程以及已运行其他前台应用的终端会跳过清理；已关闭或无法访问的终端不影响账号操作。

目标与当前账号相同且不带 `-f` 时，不检查或结束进程，ChatGPT 账号直接将当前 `auth.json` 同步到账号槽，并通知 CODEXTOP 采样器刷新凭证；API 源仍按已保存的配置重新应用凭证。带 `-f` 时先走上述关闭流程。

输入数字时会先找 `auth-1.json`，再找 `auth-openai-1.json`。
如果目标不存在，错误信息会给出对应的 `CODEXAUTH --new ...` 添加命令。

可选的 `--transfer [-d DURATION | -s SESSION_ID]` 用于迁移历史会话的内部 provider 标记。启用但不指定范围时默认 `1h`；不带这个参数时不会修改会话。

```bash
CODEXAUTH work --transfer -f          # 切换内部 provider id 时迁移最近 1h 的原 provider 会话
CODEXAUTH work --transfer -d 12h -f
CODEXAUTH work --transfer -d 7d -f
CODEXAUTH work --transfer -d all -f
CODEXAUTH work --transfer -s SESSION_UUID -f  # 迁移指定会话，可用于修复标记
CODEXAUTH --transfer -s SESSION_UUID -f       # 单独将指定会话迁移到当前 provider
CODEXAUTH --transfer -d 1h -f                 # 单独将最近 1h 的会话迁移到当前 provider
```

批量范围按 SQLite 的最近活动时间筛选；未被索引的旧会话使用 rollout 修改时间。配合 provider 操作时，只有内部 `model_provider` 改变才批量迁移，并仅处理原 provider 的会话；单独使用则将范围内的会话迁移到当前 provider。完整 UUID 不受时间范围限制。范围包含归档会话。

迁移同步修改 rollout 的 `session_meta.model_provider` 和 SQLite 索引，保留消息内容与会话活动时间。原标记备份保存在 `~/.codex/codextop/session_provider_backups/`，完成后输出路径；`--json` 输出包含 `session_provider_update`。迁移要求 Codex 已退出，可配合 `-f` 使用上述关闭流程。

API 账号接在已有数字账号之后编号，可用 `CODEXAUTH list` 查看，并通过 `CODEXAUTH 5` 切换。API 账号的配置与凭证仅保存在 `auth_registry.json` 的 `meta` 中，不再使用独立的 API 账号文件，切换时将当前 API 配置写到 `config.toml` 末尾的 `[model_providers.codexauth-third-party-api]`，并清除 CODEXAUTH 管理的其他 API provider 配置；切回 ChatGPT 时清除这些 API 配置，账号数据仍保留。

如果有命令名以 `codex` 开头的 Codex 主程序正在运行，默认会拒绝新增、重命名、刷新、删除或切换到其他 provider；切换支持上述 `-f` 和同账号同步行为。

### 命令行查询

查询额度信息：

```bash
CHECK_CODEX_QUOTA             # 输出当前账号额度信息
CHECK_CODEX_QUOTA --all-auth  # 输出所有账号额度信息
```

### 更新

检测并同步 CodexTOP 更新：

```bash
CODEXTOP_UPDATE               # 有更新时自动从 GitHub 快进同步
CODEXTOP_UPDATE --check-only  # 只检测，不更新
```

每天第一次打开 `CODEXTOP` 主界面时，会在后台读取一次 GitHub latest release。只有 Release 版本号高于当前版本时，侧栏最下方才会在作者和当前版本号后空一行显示黄色按钮：

```text
@JamesZhutheThird
v3.2.0

F11 / 新版本可用
```

点击该按钮或按 `F11` 会打开更新日志悬浮窗，内容直接来自对应 GitHub Release；可用方向键上下滚动，按 `Backspace` 关闭，按 `Enter` 确认更新。确认后 CodexTOP 会退出全屏界面并执行更新脚本，直接展示更新过程中的命令行输出。更新完成后需要手动重新打开 `CODEXTOP`。

默认更新源是当前仓库的 `origin`，分支默认使用当前分支。也可以通过环境变量覆盖：

```bash
export CODEXTOP_UPDATE_REMOTE=origin
export CODEXTOP_UPDATE_BRANCH=main
```

自动更新只接受 GitHub remote，并且只执行 fast-forward；如果本地有未提交改动或远端不是快进更新，会拒绝自动同步。

### 样式文件

内置配色定义位于 `src/ui/styles/`，每个方案一个文件，文件名就是方案 key，例如：

```text
src/ui/styles/classic.json
src/ui/styles/icefire.json
src/ui/styles/bright.json
```

内置方案的显示名称来自语言翻译文件。也可以通过 `CODEXTOP_COLOR_SCHEME_DIR` 指向其他
样式目录；自定义方案可使用可选的 `label` 字段。`percent` 可以写满 `0` 到 `100`，也可以
只写关键点；未写出的百分比会按相邻关键点线性插值。

```json
{
  "label": "自定义经典",
  "percent": {
    "0": "#e07b7b",
    "50": "#e0e07b",
    "100": "#7be07b"
  }
}
```

### 后台采样服务

后台采样服务会在安装后自动启动，也可以手动启动或停止：

```bash
./scripts/start_codextop_backend.sh
./scripts/stop_codextop_backend.sh
```

用量接口按右侧栏“更新间隔”查询；额度重置次数由独立后台任务刷新，默认每 60 秒查询
一次，并按账号持久化缓存。重置次数查询失败不会丢弃同一时刻成功获取的用量数据，也
不会在下一次用量采样时立即重试。5h/7d 用量遇到 429 时，会在配置的兜底时间内
沿用上次成功结果；额度重置次数则永久沿用最后一次成功结果，只有成功查询明确返回 0
时才显示为没有可用次数。

这两个默认值可在项目的 `pyproject.toml` 中调整，修改后重启后台采样器和 CodexTOP 生效：

```toml
[tool.codextop]
reset_credits_interval_seconds = 60
http_429_fallback_seconds = 600
week_start = "monday"
```

周统计默认以周一为第一天。`week_start` 可改为 `tuesday` 至 `sunday` 中的任意英文星期名。

账号“使用总量”通过账号统计接口查询，并按账号持久化缓存 24 小时；查询失败也会等待
下一周期，避免刷新风暴。CodexTOP 首次启动时会读取 `~/.codex/config.toml` 中的可信项目，并同步到
`~/.codex/codextop/settings/token_usage_directories.json`。可手动将目录条目的 `disable`
改为 `true`，使其不参与总量统计；“用量范围”支持仅统计当前可信目录或全部未禁用目录。

用量模式会聚合相应目录下根线程的本地 `token_count` 事件。程序先比较日志的修改时间和
大小，只有变化时才读取追加内容，约每 200 毫秒检查活跃日志；没有新事件时当前速率记为
0。输入、缓存输入、输出和总量速率经过轻量平滑后绘制为四条固定配色曲线；图例实时显示
当前速率，并在括号内显示对应累计总量。“用量布局”可选择合并或拆分；拆分时根据绘图区
自适应使用 1×4、2×2 或 4×1，每个子图独立缩放；高度足够时显示完整六档纵轴，空间不足时
只标 0 和最高值，纵向子图之间不留空行。只有合并柱状和精细柱状模式将前三项累计堆叠；
精细柱状使用 2×4 子字符填充。所有非柱状模式均将四条曲线分别独立绘制。

合并图纵轴按数据范围自动选用 `0/.5/1/1.5/2/2.5`、`0/1/2/3/4/5` 或
`0/2/4/6/8/10` 六档标签；绘图区每列覆盖时间严格大于 15 分钟时使用 `tok/h`，否则使用
`tok/min`。其余展示模式不扫描日志，只保留原有额度曲线和坐标轴。旧版 JSONL 中已有的
Token 字段可继续兼容，无需迁移。

## License

本项目使用 `CC BY-NC-SA 4.0` 协议。
