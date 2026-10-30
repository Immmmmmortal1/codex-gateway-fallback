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

# curl | bash 时 stdin 是管道，交互式提问会读到脚本剩余文本而不是用户输入。
# 把 stdin 重新绑到终端；没有终端（CI/管道）就保持原样，由 python 侧判为非交互。
if [ -r /dev/tty ]; then
  exec < /dev/tty
fi

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
if [ -f "$(dirname "$0")/$SCRIPT_NAME" ]; then
  cp "$(dirname "$0")/$SCRIPT_NAME" "$TARGET"
else
  command -v curl >/dev/null 2>&1 || die "找不到 curl，无法下载安装器"
  curl -fsSL --max-time 60 "$REPO_RAW/$SCRIPT_NAME" -o "$TARGET" \
    || die "下载 $REPO_RAW/$SCRIPT_NAME 失败（检查网络或仓库是否 public）"
fi

[ -s "$TARGET" ] || die "安装器为空"

python3 - "$TARGET" <<'PY' || exit 1
import sys

path = sys.argv[1]
src = open(path, encoding="utf-8").read()
if "setup_codex_gateway.py" not in src or "def main(" not in src:
    sys.exit("install.sh: 下载到的内容不像安装器，已中止（可能是 404 页面或被篡改）")
PY

exec python3 "$TARGET" "$@"
