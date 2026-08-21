# CodexTOP AI Agent 自动安装指南

仓库：<https://github.com/JamesZhutheThird/codextop>

本文件是给 Codex 等本地 AI 编程助手使用的执行说明。用户提供本文件链接并要求安装时，请实际完成安装、验证和命令行指引输出，不要只复述步骤或要求用户手动配置。

## 1. 默认约定

- 适用环境：Linux 或 WSL，使用 `zsh`，且已经安装并能正常使用 Codex CLI。
- 默认安装目录：`${CODEXTOP_INSTALL_DIR:-$HOME/.local/share/codextop}`。
- Codex 数据目录：`${CODEXTOP_CODEX_DIR:-${CODEX_HOME:-$HOME/.codex}}`。
- 不要把源码安装到 `~/.codex/codextop`；该目录用于 CodexTOP 的运行时数据。
- 不询问安装目录或依赖安装方式；环境变量未指定时直接使用上述默认值。
- 不使用 `sudo`，不读取或输出 `auth.json`、API key、access token、refresh token 的内容。
- 现有源码目录、shell 配置或命令包装器在覆盖前必须备份。不要删除自动创建的备份。

只有缺少 Git、zsh、Python 3.10+，或外部执行层阻止克隆/安装依赖时才停止；此时报告具体阻塞原因，不要声称安装成功。

## 2. 检查运行环境

先执行以下检查：

```bash
command -v git
command -v bash
command -v zsh
command -v python3
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
```

CodexTOP 的 Python 依赖只有 `rich>=13.0.0`。如果下列检查失败，自动安装到当前用户的 Python 环境；不要使用 `sudo`：

```bash
python3 -c 'from importlib.metadata import version; raise SystemExit(0 if int(version("rich").split(".", 1)[0]) >= 13 else 1)'
```

```bash
if python3 -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)'; then
  python3 -m pip install --disable-pip-version-check 'rich>=13.0.0'
else
  python3 -m pip install --user --disable-pip-version-check 'rich>=13.0.0'
fi
```

安装后重新执行版本检查。若当前 Python 禁止用户级安装或缺少 `pip`，先尝试该环境明确支持的用户级安装方式；仍无法完成时报告原始错误，不要修改系统 Python，也不要用 `sudo` 绕过限制。

## 3. 暂存并验证源码

必须先克隆到临时目录并验证，再替换现有安装：

```bash
REPO_URL="https://github.com/JamesZhutheThird/codextop.git"
INSTALL_DIR="${CODEXTOP_INSTALL_DIR:-$HOME/.local/share/codextop}"
INSTALL_PARENT="$(dirname -- "$INSTALL_DIR")"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codextop-install.XXXXXX")"
STAGED_REPO="$STAGE_DIR/repository"

git clone --depth 1 "$REPO_URL" "$STAGED_REPO"
test -d "$STAGED_REPO/.git"
test -f "$STAGED_REPO/README.md"
test -f "$STAGED_REPO/pyproject.toml"
test -x "$STAGED_REPO/scripts/install_zshrc.sh"
bash -n "$STAGED_REPO/scripts/install_zshrc.sh"
python3 -c 'import tomllib, pathlib, sys; data = tomllib.loads((pathlib.Path(sys.argv[1]) / "pyproject.toml").read_text()); assert data["project"]["name"] == "codextop"' "$STAGED_REPO"
```

任一验证失败都应停止，不得替换现有安装。

## 4. 备份并安装

先停止已有后台采样器。优先调用旧安装的停止脚本；如果旧安装位置未知，可以调用暂存仓库中的停止脚本。停止失败时保留错误信息并确认没有误杀其他进程。

然后执行安全替换：

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
ZSHRC_PATH="${ZSHRC:-$HOME/.zshrc}"
BACKUP_DIR=""
ZSHRC_BACKUP=""
COMMAND_BACKUP_DIR=""

if [[ -n "${CODEXTOP_BIN_DIR:-}" ]]; then
  BIN_DIR="$CODEXTOP_BIN_DIR"
elif [[ -d "$HOME/.npm-global/bin" ]]; then
  BIN_DIR="$HOME/.npm-global/bin"
else
  BIN_DIR="$HOME/.local/bin"
fi

mkdir -p "$INSTALL_PARENT"

if [[ -f "$ZSHRC_PATH" ]]; then
  ZSHRC_BACKUP="$ZSHRC_PATH.codextop.backup.$STAMP"
  cp -p "$ZSHRC_PATH" "$ZSHRC_BACKUP"
fi

for COMMAND_NAME in CODEXTOP CHECK_CODEX_QUOTA CODEXAUTH SWITCH_CODEX_PROVIDER CODEXTOP_UPDATE; do
  if [[ -e "$BIN_DIR/$COMMAND_NAME" ]]; then
    if [[ -z "$COMMAND_BACKUP_DIR" ]]; then
      COMMAND_BACKUP_DIR="$INSTALL_PARENT/codextop-command-backups/$STAMP"
      mkdir -p "$COMMAND_BACKUP_DIR"
      chmod 700 "$COMMAND_BACKUP_DIR"
    fi
    cp -a "$BIN_DIR/$COMMAND_NAME" "$COMMAND_BACKUP_DIR/"
  fi
done

if [[ -x "$INSTALL_DIR/scripts/stop_codextop_backend.sh" ]]; then
  "$INSTALL_DIR/scripts/stop_codextop_backend.sh" || true
else
  "$STAGED_REPO/scripts/stop_codextop_backend.sh" || true
fi

if [[ -e "$INSTALL_DIR" ]]; then
  BACKUP_DIR="$INSTALL_DIR.backup.$STAMP"
  mv "$INSTALL_DIR" "$BACKUP_DIR"
fi

mv "$STAGED_REPO" "$INSTALL_DIR"
rmdir "$STAGE_DIR"
"$INSTALL_DIR/scripts/install_zshrc.sh"
```

`install_zshrc.sh` 会完成以下工作：

- 在 `~/.zshrc` 中写入带有 CodexTOP 标记的可重复更新区块；
- 安装 `CODEXTOP`、`CHECK_CODEX_QUOTA`、`CODEXAUTH`、`CODEXTOP_UPDATE` 等命令包装器；
- 启动后台采样器；
- 如果 auth list 为空且已有 `~/.codex/auth.json`，首次运行时自动导入为 `auth-openai-1.json`。

如果安装命令失败，不要删除新目录或备份。把失败的新目录移动为带时间戳的 `.failed` 目录，恢复原安装目录、`.zshrc` 和已有命令包装器备份，并报告失败命令及原始错误。

## 5. 验证安装

安装后必须验证真实命令，而不是只检查文件存在：

```bash
zsh -ic 'command -v CODEXTOP && command -v CHECK_CODEX_QUOTA && command -v CODEXAUTH && command -v CODEXTOP_UPDATE'
zsh -ic 'CODEXTOP --help >/dev/null'
zsh -ic 'CODEXAUTH sync'
```

再检查后台采样器 PID 文件是否存在，并确认其中 PID 对应的命令包含 `codex_quota_sampler.py`：

```bash
CODEX_DIR="${CODEXTOP_CODEX_DIR:-${CODEX_HOME:-$HOME/.codex}}"
PID_FILE="$CODEX_DIR/codextop/log/sampler.pid"
for _ in {1..20}; do
  [[ -s "$PID_FILE" ]] && break
  sleep 0.25
done
test -s "$PID_FILE"
SAMPLER_PID="$(tr -d '[:space:]' < "$PID_FILE")"
[[ "$SAMPLER_PID" =~ ^[0-9]+$ ]]
kill -0 "$SAMPLER_PID"
if [[ -r "/proc/$SAMPLER_PID/cmdline" ]]; then
  SAMPLER_COMMAND="$(tr '\0' ' ' < "/proc/$SAMPLER_PID/cmdline")"
else
  SAMPLER_COMMAND="$(ps -p "$SAMPLER_PID" -o command=)"
fi
[[ "$SAMPLER_COMMAND" == *codex_quota_sampler.py* ]]
git -C "$INSTALL_DIR" rev-parse HEAD
python3 -c 'import sys; from importlib.metadata import version; print(sys.version.split()[0], version("rich"))'
```

不得通过打印认证文件内容来验证安装。如果用户尚未执行过 `codex login`，程序安装仍可判定成功，但应明确说明额度查询需要先登录 Codex。

## 6. 在命令行输出使用指引

验证通过后，必须在命令行执行一段清晰的安装报告，而不是只在聊天回复中说明：

```bash
printf '%s\n' \
  '' \
  'CodexTOP 安装并验证完成。' \
  "安装目录：$INSTALL_DIR" \
  "源码备份：${BACKUP_DIR:-无（此前没有安装）}" \
  "Shell 配置备份：${ZSHRC_BACKUP:-无（此前没有配置文件）}" \
  "命令包装器备份：${COMMAND_BACKUP_DIR:-无（此前没有同名包装器）}" \
  '' \
  '打开新的 zsh 终端即可直接使用；当前终端可执行：' \
  "  source $ZSHRC_PATH" \
  '  rehash' \
  '' \
  '常用命令：' \
  '  CODEXTOP                 打开主界面' \
  '  CHECK_CODEX_QUOTA        单次查询当前账号额度' \
  '  CHECK_CODEX_QUOTA --all-auth  查询全部账号' \
  '  CODEXAUTH list           查看账号列表' \
  '  CODEXAUTH --new 1        通过 device auth 添加账号' \
  '  CODEXTOP_UPDATE --check-only  只检查更新' \
  '  CODEXTOP_UPDATE          检查并安装快进更新'
```

如果未检测到 `${CODEXTOP_CODEX_DIR:-${CODEX_HOME:-$HOME/.codex}}/auth.json`，紧接着输出：

```text
尚未检测到 Codex 登录信息：可运行 CODEXAUTH --new 1，并按屏幕提示在网页版完成登录。
```

## 7. 最终回复

最后向用户简要报告：

- 实际安装目录与 Git commit；
- 是否创建源码或 `.zshrc` 备份及其路径；
- Python 与 `rich` 版本；
- 四个命令的验证结果；
- 后台采样器状态；
- 是否需要先执行 `codex login`；
- 当前终端重新加载命令：`source ~/.zshrc && rehash`。

只有全部必要验证通过后才能说“安装完成”。
