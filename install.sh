#!/bin/bash
# install.sh —— Codex 网关与 fallback 一键安装入口
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh | bash
#
# 带参数（会透传给 setup_codex_gateway.py）：
#   curl -fsSL .../install.sh | bash -s -- --main deepseek-flash --fb1 gpt-6.1-terra --fb2 glm-5.3
#
# 说明：本脚本不含任何密钥；网关 token 从本机已有配置或环境变量读取，读不到才提示。

set -uo pipefail

REPO_RAW="${CODEX_GF_RAW:-https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main}"
SCRIPT_NAME="setup_codex_gateway.py"

# 注意：这里绝不能 `exec < /dev/tty`。
# bash 正在从 stdin（管道）逐行读取本脚本，一旦把 fd0 换成终端，
# 后续脚本行就会从键盘读，表现为「命令没反应，一直等输入」。
# 交互式提问由 python 侧显式打开 /dev/tty 解决。

die() {
  echo "install.sh: $*" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || die "找不到 python3，请先安装 Python 3.9+"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codex-gf.XXXXXX")" || die "无法创建临时目录"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT INT TERM

TARGET="$TMP_DIR/$SCRIPT_NAME"

# 优先从本地同目录读取（适合 git clone 后直接跑），否则从 GitHub 下载
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
if [ -f "$LOCAL_DIR/$SCRIPT_NAME" ]; then
  cp "$LOCAL_DIR/$SCRIPT_NAME" "$TARGET"
  echo "install.sh: 使用本地安装器 $LOCAL_DIR/$SCRIPT_NAME"
else
  command -v curl >/dev/null 2>&1 || die "找不到 curl，无法下载安装器"
  # 先探一次，失败就明确报错，避免 curl 静默重试让用户以为命令没反应
  echo "install.sh: 正在下载 $SCRIPT_NAME …"
  ok=0
  for attempt in 1 2 3 4; do
    if curl -fSL --connect-timeout 8 --max-time 60 \
         --retry 2 --retry-delay 1 --retry-connrefused \
         "$REPO_RAW/$SCRIPT_NAME" -o "$TARGET" 2>/dev/null; then ok=1; break; fi
    [ "$attempt" -lt 4 ] && echo "install.sh: 第 $attempt 次下载失败，重试…"
  done
  [ "$ok" = 1 ] || {
    echo "" >&2
    echo "install.sh: 下载失败。" >&2
    if ! curl -s -o /dev/null --connect-timeout 8 --max-time 12 https://raw.githubusercontent.com; then
      echo "  看起来 raw.githubusercontent.com 连不上（国内网络常被墙）。" >&2
      echo "  如果你本机有代理，改成这样重跑：" >&2
      echo "    export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897" >&2
      echo "    curl -fsSL $REPO_RAW/install.sh | bash -s -- --auto" >&2
    else
      echo "  能连到 GitHub，但取不到文件。确认仓库是 public、分支是 main。" >&2
    fi
    exit 1
  }
fi

[ -s "$TARGET" ] || die "安装器为空"

python3 - "$TARGET" <<'PY' || exit 1
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()
if "setup_codex_gateway.py" not in src or "def main(" not in src:
    sys.exit("install.sh: 下载到的内容不像安装器，已中止（可能是 404 页面或被篡改）")
PY

# 不用 exec：exec 会把 bash 进程替换掉，EXIT trap 不再执行，临时目录就泄漏了。
python3 "$TARGET" "$@"
exit $?
