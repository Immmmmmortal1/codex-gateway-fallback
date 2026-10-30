#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_codex_gateway.py —— Codex 网关与 fallback 安装器（交互式）

只做「模型选择 + 报错自动 fallback」，不配置子 agent。
本文件是 codex-gateway-fallback skill 的安装器，可单独拷到新机器运行。

做四件事：
  1. 现场读取模型列表：优先问网关 /models，其次本地 catalog，最后 codex debug models
  2. 依次询问：主模型 / 一级 fallback / 二级 fallback
  3. 写入：顶层 model、$CODEX_HOME/fb.config.toml、fb2.config.toml
  4. 生成 codex-fb wrapper（exec 模式下按 主 → fb → fb2 自动重试）

安全约束：
  - 改前备份 config.toml / fb.profile / fb2.profile / wrapper，`--restore` 可回滚
  - 只改本脚本负责的键，其余内容（mcp_servers / projects / hooks ...）原样保留
  - 会清掉历史遗留的 [agents] / [agents.*] 段与 roles/ overlay（子 agent 已不支持）
  - 写前写后用 tomllib 校验，并实际调用 `codex debug prompt-input` 确认配置能被加载

用法：
  python3 setup_codex_gateway.py                 # 交互式
  python3 setup_codex_gateway.py --list-models    # 只看模型列表
  python3 setup_codex_gateway.py --dry-run        # 预演，不落盘
  python3 setup_codex_gateway.py --fix-legacy     # 顺手清掉本版本已废弃字段
  python3 setup_codex_gateway.py --restore        # 回滚最近一次安装
  # 非交互（CI / 复现）
  python3 setup_codex_gateway.py --yes \
      --main deepseek-flash --fb1 gpt-6.1-terra --fb2 glm-5.3 --effort low
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
CONFIG = CODEX_HOME / "config.toml"
ROLES_DIR = CODEX_HOME / "roles"  # 仅用于清理历史遗留，不再写入
LEGACY_ROLES_DIR = CODEX_HOME / "agents"
BIN_DIR = CODEX_HOME / "bin"
WRAPPER = BIN_DIR / "codex-fb"
# 安装器自身的稳定副本：curl|bash 时装在临时目录，退出即删，
# 之后想 --restore 就没路径可用了，所以落一份到 CODEX_HOME/bin。
INSTALLER_COPY = BIN_DIR / "setup_codex_gateway.py"


def installer_hint() -> str:
    """给用户的回滚命令路径：优先稳定副本，其次当前脚本（本地直跑场景）。"""
    if INSTALLER_COPY.exists():
        return INSTALLER_COPY.as_posix()
    try:
        return Path(__file__).resolve().as_posix()
    except NameError:
        return "setup_codex_gateway.py"

PROFILE_FB1 = "fb"
PROFILE_FB2 = "fb2"

REASONING_LEVELS = ["low", "medium", "high"]

# 本版本（0.165.x）strict-config 判定的废弃字段：值是无害的，但会被静默忽略
LEGACY_TOP_LEVEL = ("env_key",)
LEGACY_TABLES = ("defaults",)
LEGACY_PROJECT_KEYS = ("language", "prompt_prefix")
LEGACY_FEATURE_KEYS = ("rmcp_client",)

C_HEAD = "\033[1;36m"
C_OK = "\033[1;32m"
C_WARN = "\033[1;33m"
C_ERR = "\033[1;31m"
C_DIM = "\033[2m"
C_END = "\033[0m"


def colorize(enabled: bool):
    def c(text: str, color: str) -> str:
        return f"{color}{text}{C_END}" if enabled else text

    return c


def tty_available() -> bool:
    """当前进程能否直接拿到终端。curl | bash 时 stdin 是管道，但 /dev/tty 通常仍可用。"""
    try:
        with open("/dev/tty", "r"):
            return True
    except OSError:
        return False


def ask(prompt: str) -> str:
    """从终端读一行。

    不能用内置 input()：curl | bash 场景下 stdin 是脚本内容，
    input() 会读到脚本剩余文本，表现为静默取到垃圾值。
    """
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().rstrip("\n")
    except OSError:
        return input(prompt)


def ask_secret(prompt: str) -> str:
    """同 ask()，但不回显输入。"""
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            try:
                import termios

                fd = tty.fileno()
                old = termios.tcgetattr(fd)
                new = termios.tcgetattr(fd)
                new[3] &= ~termios.ECHO
                termios.tcsetattr(fd, termios.TCSADRAIN, new)
                try:
                    value = tty.readline().rstrip("\n")
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    tty.write("\n")
                    tty.flush()
                return value
            except (ImportError, OSError):
                return tty.readline().rstrip("\n")
    except OSError:
        return getpass.getpass(prompt)


USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
c = colorize(USE_COLOR)


def head(msg: str) -> None:
    print(f"\n{c('▸ ' + msg, C_HEAD)}")


def ok(msg: str) -> None:
    print(f"  {c('✓', C_OK)} {msg}")


def warn(msg: str) -> None:
    print(f"  {c('!', C_WARN)} {msg}")


def err(msg: str) -> None:
    print(f"  {c('✗', C_ERR)} {msg}")


def info(msg: str) -> None:
    print(f"    {c(msg, C_DIM)}")


# --------------------------------------------------------------------------- #
# codex 二进制定位 / 执行
# --------------------------------------------------------------------------- #
def find_codex() -> str | None:
    for cand in (
        shutil.which("codex"),
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
        str(CODEX_HOME / "bin" / "codex"),
    ):
        if not cand:
            continue
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def run(cmd: list[str], timeout: int = 120):
    env = dict(os.environ)
    env["CODEX_HOME"] = str(CODEX_HOME)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None


# --------------------------------------------------------------------------- #
# 模型目录
# --------------------------------------------------------------------------- #
def catalog_path() -> Path | None:
    """配置里声明的 model_catalog_json 优先，其次 ~/.memhub/model-catalog.json。"""
    if CONFIG.exists():
        m = re.search(
            r'^\s*model_catalog_json\s*=\s*"([^"]+)"',
            CONFIG.read_text(encoding="utf-8"),
            re.M,
        )
        if m:
            return Path(m.group(1)).expanduser()
    fallback = Path.home() / ".memhub" / "model-catalog.json"
    return fallback if fallback.is_file() else None


def read_catalog(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        return data
    if isinstance(data, list):
        return {"models": data}
    return None


def models_from_catalog(data: dict) -> list[dict]:
    out = []
    for m in data["models"]:
        if not m.get("slug"):
            continue
        if m.get("visibility", "list") != "list":
            continue
        out.append(
            {
                "slug": m["slug"],
                "display": m.get("display_name") or m["slug"],
                "ctx": m.get("context_window"),
                "effort": m.get("default_reasoning_level"),
            }
        )
    return out


def fetch_gateway_models(base_url: str, token: str, timeout: int = 30) -> list[dict]:
    """直接问网关要模型列表（换机器时这是唯一可靠来源：codex debug models 只返回内置模型）。"""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"访问 {url} 失败：{e}") from e
    if isinstance(data, dict):
        raw = data.get("data") or data.get("models") or []
    else:
        raw = data
    out = []
    for m in raw:
        slug = m.get("id") or m.get("slug")
        if not slug:
            continue
        out.append(
            {
                "slug": slug,
                "display": m.get("display_name") or m.get("name") or slug,
                "ctx": m.get("context_window") or m.get("context_length"),
                "effort": m.get("default_reasoning_level"),
            }
        )
    return out


def load_models(codex_bin: str | None, prov: dict | None = None) -> tuple[list[dict], str]:
    if prov and prov.get("token"):
        try:
            out = fetch_gateway_models(prov["base_url"], prov["token"])
            if out:
                return out, f"网关 {prov['base_url']}/models"
        except RuntimeError as e:
            warn(str(e))
    if codex_bin:
        r = run([codex_bin, "debug", "models"], timeout=90)
        if r and r.returncode == 0 and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                data = data if isinstance(data, dict) else {"models": data}
                data.setdefault("models", [])
                out = models_from_catalog(data)
                if out:
                    return out, "codex debug models"
            except json.JSONDecodeError:
                pass
    p = catalog_path()
    if p:
        data = read_catalog(p)
        if data:
            out = models_from_catalog(data)
            if out:
                return out, f"模型目录 {p}"
    return [], "无"


def current_main_model() -> str | None:
    if not CONFIG.exists():
        return None
    m = re.search(r'^\s*model\s*=\s*"([^"]+)"', CONFIG.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# 交互选择
# --------------------------------------------------------------------------- #
def prompt_choice(
    title: str, models: list[dict], default: str | None, exclude: set[str], hint: str = "",
) -> str:
    slugs = [m["slug"] for m in models]
    print(f"\n{c(title, C_HEAD)}")
    if hint:
        info(hint)
    for i, m in enumerate(models, 1):
        mark = c(" ← 当前", C_DIM) if m["slug"] == default else ""
        ex = c("  (已被前面的选择占用)", C_DIM) if m["slug"] in exclude else ""
        print(
            f"  {i:2d}. {m['slug']:<22} {m['display']:<22} "
            f"ctx={m['ctx'] or '-'} effort={m['effort'] or '-'}{mark}{ex}"
        )
    manual_idx = len(models) + 1
    print(f"  {manual_idx:2d}. 手动输入模型名")
    if default:
        info(f"直接回车 = 保持当前：{default}")

    while True:
        raw = ask(f"{c('选择', C_OK)} [1-{manual_idx}]: ").strip()
        if not raw:
            if default:
                return default
            warn("请输入序号")
            continue
        if not raw.isdigit():
            if raw in slugs or re.fullmatch(r"[A-Za-z0-9._:-]+", raw):
                if raw in exclude:
                    warn(f"{raw} 已被前面占用，换一个")
                    continue
                return raw
            warn("请输入序号或合法模型名")
            continue
        n = int(raw)
        if n == manual_idx:
            name = ask("  模型名: ").strip()
            if not name:
                warn("不能为空")
                continue
            if name in exclude:
                warn(f"{name} 已被前面占用，换一个")
                continue
            return name
        if 1 <= n <= len(models):
            slug = models[n - 1]["slug"]
            if slug in exclude:
                warn(f"{slug} 已被前面占用，换一个")
                continue
            return slug
        warn("序号超范围")


def prompt_effort(default: str = "medium") -> str:
    print(f"\n{c('主模型思考档位', C_HEAD)}")
    for i, lv in enumerate(REASONING_LEVELS, 1):
        mark = c(" ← 当前", C_DIM) if lv == default else ""
        print(f"  {i}. {lv}{mark}")
    while True:
        raw = ask(f"{c('选择', C_OK)} [1-{len(REASONING_LEVELS)}，回车={default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(REASONING_LEVELS):
            return REASONING_LEVELS[int(raw) - 1]
        if raw in REASONING_LEVELS:
            return raw
        warn(f"请输入 1-{len(REASONING_LEVELS)} 或 low/medium/high")


# --------------------------------------------------------------------------- #
# config.toml 文本级编辑（保留用户其余内容）
# --------------------------------------------------------------------------- #
TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def read_lines() -> list[str]:
    return CONFIG.read_text(encoding="utf-8").splitlines() if CONFIG.exists() else []


def first_table_index(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if TABLE_RE.match(line):
            return i
    return len(lines)


MANAGED_HEADER = "# 主模型（由 setup_codex_gateway.py 管理）"
# 兼容历史脚本名：旧版本写过这两种注释，替换时要一并清掉，避免重复堆积
MANAGED_HEADERS = (MANAGED_HEADER, "# 主模型（由 install-codex-config.py 管理）")


def strip_managed_top_level(lines: list[str]) -> list[str]:
    end = first_table_index(lines)
    out: list[str] = []
    for i, line in enumerate(lines):
        if i < end:
            if line.strip() in MANAGED_HEADERS:
                continue
            key = line.split("=", 1)[0].strip()
            if key in ("model", "model_reasoning_effort", "model_provider", "profile"):
                continue
        out.append(line)
    return out


def strip_generated_banner(lines: list[str]) -> list[str]:
    """删掉上一版脚本留下的“由 install-codex-config.py 生成”注释块。"""
    idx = [
        i
        for i, l in enumerate(lines)
        if "由 install-codex-config.py 生成" in l or "由 setup_codex_gateway.py 生成" in l
    ]
    if not idx:
        return lines
    drop: set[int] = set()
    for i in idx:
        lo = i
        while lo > 0 and lines[lo - 1].lstrip().startswith("#"):
            lo -= 1
        hi = i
        while hi + 1 < len(lines) and lines[hi + 1].lstrip().startswith("#"):
            hi += 1
        drop.update(range(lo, hi + 1))
    return [l for i, l in enumerate(lines) if i not in drop]


def strip_agents_tables(lines: list[str]) -> list[str]:
    out: list[str] = []
    skipping = False
    for line in lines:
        m = TABLE_RE.match(line)
        if m:
            name = m.group(1).strip()
            skipping = name == "agents" or name.startswith("agents.")
        if not skipping:
            out.append(line)
    return out


def strip_legacy(lines: list[str]) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    out: list[str] = []
    section: str | None = None
    skip_table = False
    for line in lines:
        m = TABLE_RE.match(line)
        if m:
            section = m.group(1).strip()
            skip_table = section in LEGACY_TABLES
            if skip_table:
                removed.append(f"[{section}]（整段废弃）")
                continue
        if skip_table:
            continue
        if section is None:
            key = line.split("=", 1)[0].strip()
            if key in LEGACY_TOP_LEVEL:
                removed.append(f"顶层 {key}")
                continue
        elif section.startswith("projects."):
            key = line.split("=", 1)[0].strip()
            if key in LEGACY_PROJECT_KEYS:
                removed.append(f"[{section}] {key}")
                continue
        elif section == "features":
            key = line.split("=", 1)[0].strip()
            if key in LEGACY_FEATURE_KEYS:
                removed.append(f"[features] {key}")
                continue
        out.append(line)
    return out, removed


def detect_legacy(lines: list[str]) -> list[str]:
    _, removed = strip_legacy(lines)
    return removed


def render_profile(name: str, model: str, effort: str, provider: str, note: str) -> str:
    return f"""# {note}
# 由 setup_codex_gateway.py 生成；用法：codex -p {name} ...
model = "{model}"
model_reasoning_effort = "{effort}"
model_provider = "{provider}"
"""


# --------------------------------------------------------------------------- #
# wrapper
# --------------------------------------------------------------------------- #
def render_wrapper(codex_bin: str, fb1_model: str, fb2_model: str) -> str:
    return f"""#!/bin/bash
# codex-fb —— 带 fallback 的 Codex 启动器（由 setup_codex_gateway.py 生成）
#
# 仅对 `exec` 子命令做自动 fallback：主模型 → {PROFILE_FB1}（{fb1_model}）→ {PROFILE_FB2}（{fb2_model}）
# 其它子命令（TUI、resume、review ...）原样透传给 codex，不做重试。
#
# 只在「可判定的 API/网络失败」时重试；Ctrl-C（130）与成功（0）一律不重试。
# 注意：fallback 会重新执行整条 prompt，已被主模型改过的文件不会自动回滚。

set -uo pipefail
CODEX_BIN="${{CODEX_BIN:-{codex_bin}}}"
CODEX_HOME_DIR="${{CODEX_HOME:-{CODEX_HOME.as_posix()}}}"
export CODEX_HOME="$CODEX_HOME_DIR"
LOG_DIR="$CODEX_HOME_DIR/logs"
mkdir -p "$LOG_DIR"

if [ ! -x "$CODEX_BIN" ]; then
  echo "codex-fb: 找不到 codex 可执行文件：$CODEX_BIN" >&2
  echo "        可用 CODEX_BIN=/path/to/codex 覆盖" >&2
  exit 127
fi

if [ "${{1:-}}" != "exec" ]; then
  exec "$CODEX_BIN" "$@"
fi

is_retryable() {{
  grep -qE 'ERROR: (unexpected status (4|5)[0-9]{{2}}|Connection failed|stream|request_build_error)' "$1" 2>/dev/null && return 0
  grep -qE 'ERROR: Reconnecting\\.\\.\\.' "$1" 2>/dev/null && return 0
  return 1
}}

label_for() {{
  case "$1" in
    "")   echo "主模型" ;;
    "{PROFILE_FB1}") echo "一级 fallback" ;;
    "{PROFILE_FB2}") echo "二级 fallback" ;;
    *)    echo "$1" ;;
  esac
}}

LAST_LOG=""
run_attempt() {{
  local profile="$1"; shift
  local log
  log="$LOG_DIR/codex-fb-$(date +%Y%m%d-%H%M%S)-${{profile:-main}}.log"

  local -a args=("$CODEX_BIN")
  if [ -n "$profile" ]; then args+=(-p "$profile"); fi
  # 关掉无限重连，否则网络失败时进程永不退出，fallback 永远不会触发
  args+=(-c features.unbounded_connection_retries=false)
  args+=("$@")

  if [ -t 0 ]; then
    "${{args[@]}}" 2> >(tee "$log" >&2)
    local rc=$?
    sleep 0.2
  else
    "${{args[@]}}" >/dev/null 2>"$log"
    local rc=$?
    cat "$log" >&2
  fi
  LAST_LOG="$log"
  return $rc
}}

for profile in "" "{PROFILE_FB1}" "{PROFILE_FB2}"; do
  run_attempt "$profile" "$@"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    exit 0
  fi
  if [ "$rc" -eq 130 ]; then
    exit 130
  fi
  if ! is_retryable "$LAST_LOG"; then
    echo "codex-fb: $(label_for "$profile") 失败且不是可重试的 API/网络错误 (exit=${{rc}})，停止 fallback" >&2
    exit "$rc"
  fi
  echo "codex-fb: $(label_for "$profile") 失败 (exit=${{rc}})，准备切到下一个模型…" >&2
  sleep 1
done

echo "codex-fb: 主模型与两级 fallback 全部失败" >&2
exit 1
"""


# --------------------------------------------------------------------------- #
# 备份 / 回滚
# --------------------------------------------------------------------------- #
def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def backup(tag: str) -> Path:
    root = CODEX_HOME / "backups" / f"codex-config-{tag}-{stamp()}"
    root.mkdir(parents=True, exist_ok=False)
    if CONFIG.exists():
        shutil.copy2(CONFIG, root / "config.toml")
    for name in (f"{PROFILE_FB1}.config.toml", f"{PROFILE_FB2}.config.toml"):
        p = CODEX_HOME / name
        if p.exists():
            shutil.copy2(p, root / name)
    if ROLES_DIR.is_dir():
        shutil.copytree(ROLES_DIR, root / "roles")
    if WRAPPER.exists():
        shutil.copy2(WRAPPER, root / "codex-fb")
    (root / "MANIFEST.txt").write_text(
        f"tag={tag}\ncreated={time.ctime()}\n", encoding="utf-8"
    )
    return root


def all_backups() -> list[Path]:
    root = CODEX_HOME / "backups"
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and ".restored-" not in p.name]
    return sorted(dirs, key=lambda p: p.name)


def latest_backup() -> Path | None:
    dirs = all_backups()
    return dirs[-1] if dirs else None


def do_restore() -> int:
    src = latest_backup()
    if not src:
        err("没有可回滚的备份")
        return 1
    head(f"回滚到 {src}")
    remaining_before = len(all_backups())

    if (src / "config.toml").exists():
        shutil.copy2(src / "config.toml", CONFIG)
        ok("config.toml 已还原")

    for name in (f"{PROFILE_FB1}.config.toml", f"{PROFILE_FB2}.config.toml"):
        dst = CODEX_HOME / name
        if (src / name).exists():
            shutil.copy2(src / name, dst)
            ok(f"{name} 已还原")
        elif dst.exists():
            dst.unlink()
            ok(f"{name} 已删除（备份里没有）")

    if ROLES_DIR.exists():
        shutil.rmtree(ROLES_DIR)
        ok("roles/ 已删除（当前版本不再使用子 agent）")

    if (src / "codex-fb").exists():
        shutil.copy2(src / "codex-fb", WRAPPER)
        WRAPPER.chmod(0o755)
        ok("codex-fb 已还原")
    elif WRAPPER.exists():
        WRAPPER.unlink()
        ok("codex-fb 已删除（备份里没有）")

    archived = src.with_name(f"{src.name}.restored-{stamp()}")
    src.rename(archived)
    ok(f"该备份已归档：{archived.name}")
    left = remaining_before - 1
    if left > 0:
        info(f"还能继续往前回退 {left} 步；再执行一次 --restore 即可")
    else:
        info("没有更早的备份了")
    return 0


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate_toml(path: Path) -> tuple[bool, str]:
    try:
        import tomllib
    except ModuleNotFoundError:
        return True, "跳过（python 无 tomllib）"
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
        return True, "语法 OK"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def verify_with_codex(codex_bin: str) -> bool:
    r = run([codex_bin, "debug", "prompt-input", "配置校验探针"], timeout=180)
    if r is None:
        warn("codex debug prompt-input 超时，跳过运行时校验")
        return True
    if r.returncode != 0:
        err("codex 无法加载新配置：")
        first = (r.stderr or "").strip().splitlines()
        info(first[0] if first else "未知错误")
        return False

    out = r.stdout or ""
    if not out.strip():
        warn("codex debug prompt-input 没有输出，跳过内容校验")
        return True
    if '"name": "functions"' in out or "exec_command" in out:
        ok("配置可被 Codex 加载，工具面正常")
    else:
        ok("配置可被 Codex 加载")
    return True


def probe_effort(codex_bin: str, model: str, desired: str, timeout: int = 180) -> tuple[str | None, str]:
    """以网关实测为准探测可用档位（catalog 里的 supported_reasoning_levels 不可信）。

    返回 (可用档位 or None, 说明)。
    """
    order = [desired] + [x for x in ("low", "high", "medium", "max") if x != desired]
    last_err = ""
    for cand in order:
        r = run(
            [
                codex_bin, "exec", "--skip-git-repo-check", "-s", "read-only",
                "-m", model,
                "-c", f'model_reasoning_effort="{cand}"',
                "-c", "features.unbounded_connection_retries=false",
                "只回复 OK，不要调用任何工具。",
            ],
            timeout=timeout,
        )
        if r is None:
            last_err = "超时"
            continue
        if r.returncode == 0:
            note = "" if cand == desired else f"（{desired} 被网关拒绝，降到 {cand}）"
            return cand, note
        err_text = r.stderr or ""
        for line in err_text.splitlines():
            if "ERROR:" in line:
                last_err = line.strip()[:160]
        if "不存在或已下线" in err_text or "model not found" in err_text.lower():
            return None, f"模型不可用：{last_err}"
    return None, f"所有档位都失败：{last_err}"


def verify_profiles(codex_bin: str) -> bool:
    good = True
    for prof in (PROFILE_FB1, PROFILE_FB2):
        r = run([codex_bin, "-p", prof, "debug", "prompt-input", "probe"], timeout=180)
        if r is not None and r.returncode == 0:
            ok(f"profile {prof} 可加载")
        else:
            err(f"profile {prof} 加载失败")
            if r and r.stderr:
                info(r.stderr.strip().splitlines()[0])
            good = False
    return good


# --------------------------------------------------------------------------- #
# provider（换机器时的关键：全新 Codex 里没有网关 provider 段）
# --------------------------------------------------------------------------- #
GATEWAY_DEFAULT = "https://memhub.vvicat.dev/api/llm/v1"
PROVIDER_DEFAULT = "codex-lb"


def parse_provider() -> dict | None:
    """从现有 config.toml 读出 provider 名 / base_url / token。"""
    if not CONFIG.exists():
        return None
    text = CONFIG.read_text(encoding="utf-8")
    m = re.search(r'^\s*model_provider\s*=\s*"([^"]+)"', text, re.M)
    name = m.group(1) if m else None
    if not name:
        m = re.search(r"^\s*\[model_providers\.([^\]]+)\]", text, re.M)
        name = m.group(1) if m else None
    if not name:
        return None
    block = re.search(
        rf"^\s*\[model_providers\.{re.escape(name)}\]\s*$(.*?)(?=^\s*\[|\Z)",
        text,
        re.M | re.S,
    )
    if not block:
        return None
    body = block.group(1)
    base = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', body, re.M)
    tok = re.search(r'^\s*experimental_bearer_token\s*=\s*"([^"]+)"', body, re.M)
    if not tok:
        tok = re.search(r'^\s*env_key\s*=\s*"([^"]+)"', body, re.M)
    return {
        "name": name,
        "base_url": base.group(1) if base else GATEWAY_DEFAULT,
        "token": tok.group(1) if tok else None,
        "exists": True,
    }


def memhub_credentials() -> dict:
    """读 ~/.memhub/credentials.json（base_url 是站点根，不是 /api）。"""
    p = Path.home() / ".memhub" / "credentials.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def skill_env_values(skill_slug: str = "codex-gateway-fallback") -> dict:
    """从 MemHub Skill Detail 的 Environment 配置里取网关地址与 token。

    只在本地环境变量缺失时调用；取不到就返回空字典，由调用方决定是否询问用户。
    不打印任何 secret 值。
    """
    cred = memhub_credentials()
    api_key = os.environ.get("MEMHUB_API_KEY") or cred.get("api_key")
    site = os.environ.get("MEMHUB_BASE_URL") or cred.get("base_url") or "https://memhub.vvicat.dev"
    if not api_key:
        return {}
    url = site.rstrip("/") + f"/api/skills/enabled/{skill_slug}/env"
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}

    entries = data.get("skillenv") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        return {}

    wanted = ("CODEX_GATEWAY_TOKEN", "CODEX_LB_API_KEY", "token", "api_key")
    base_keys = ("CODEX_GATEWAY_BASE_URL", "base_url", "gateway_url")

    def pick(cfg: dict) -> dict:
        if not isinstance(cfg, dict):
            return {}
        found = {}
        for k in base_keys:
            if isinstance(cfg.get(k), str) and cfg[k].strip():
                found["base_url"] = cfg[k].strip()
                break
        for k in wanted:
            if isinstance(cfg.get(k), str) and cfg[k].strip():
                found["token"] = cfg[k].strip()
                break
        return found

    def flatten(cfg) -> dict:
        """config 可能是对象，也可能是按项目分组的数组。"""
        if isinstance(cfg, dict):
            direct = pick(cfg)
            if direct.get("token"):
                return direct
            for v in cfg.values():
                if isinstance(v, dict):
                    got = pick(v)
                    if got.get("token"):
                        return got
            return direct
        if isinstance(cfg, list):
            for item in cfg:
                if isinstance(item, dict):
                    got = pick(item)
                    if got.get("token"):
                        return got
        return {}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        got = flatten(entry.get("config"))
        if got.get("token"):
            return got
    return {}


def bootstrap_provider(args) -> dict | None:
    """全新机器：问网关地址与 token，并校验能否拉到模型列表。"""
    head("这台机器上还没有配置模型网关")
    info("需要一个 OpenAI 兼容网关的地址和 token（MemHub 的 token 在 _knowledge/.secrets/api-keys.md）")
    interactive = sys.stdin.isatty() or tty_available()
    base = args.base_url or os.environ.get("CODEX_BASE_URL")
    if not base:
        if interactive:
            raw = ask(f"  网关地址 [{GATEWAY_DEFAULT}]: ").strip()
            base = raw or GATEWAY_DEFAULT
        else:
            base = GATEWAY_DEFAULT
            info(f"非交互环境，使用默认网关 {base}")
    token = args.token or os.environ.get("CODEX_LB_API_KEY") or os.environ.get("MEMHUB_API_KEY")
    if not token:
        env_cfg = skill_env_values()
        if env_cfg.get("token"):
            token = env_cfg["token"]
            info("已从 MemHub Skill Environment 读取 token")
            if env_cfg.get("base_url"):
                base = base or env_cfg["base_url"]
    if not token:
        if not interactive:
            err("非交互环境必须提供 --token 或 CODEX_LB_API_KEY")
            return None
        try:
            token = ask_secret("  网关 token（输入不回显）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
    if not token:
        err("没有 token，无法继续")
        return None
    info(f"校验 {base} …")
    try:
        models = fetch_gateway_models(base, token)
    except RuntimeError as e:
        err(str(e))
        return None
    if not models:
        err("网关没有返回任何模型")
        return None
    ok(f"连通，网关有 {len(models)} 个模型")
    return {"name": PROVIDER_DEFAULT, "base_url": base, "token": token, "exists": False}


def render_provider(name: str, base_url: str, token: str) -> str:
    return f"""
[model_providers.{name}]
name = "MemHub LLM Gateway"
base_url = "{base_url}"
wire_api = "responses"
supports_websockets = false
requires_openai_auth = false
experimental_bearer_token = "{token}"
"""


def pick_fallbacks(models: list[dict], main_model: str, count: int = 2) -> list[str]:
    """零交互模式下自动挑 fallback：优先官方 gpt-* 模型，且与主模型不同。"""
    slugs = [m["slug"] for m in models if m["slug"] != main_model]
    official = [s for s in slugs if s.startswith("gpt-")]
    others = [s for s in slugs if not s.startswith("gpt-")]
    return (official + others)[:count]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Codex 配置安装器：主模型 + 两级 fallback（exec 下自动切换）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--list-models", action="store_true", help="只列出可用模型")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容")
    ap.add_argument("--fix-legacy", action="store_true", help="清理本版本已废弃字段")
    ap.add_argument("--no-probe", action="store_true", help="跳到档位探测，直接用声明的 effort（可能被网关拒绝）")
    ap.add_argument("--restore", action="store_true", help="回滚最近一次安装")
    ap.add_argument("--yes", action="store_true", help="非交互，必须配合 --main / --fb1 / --fb2")
    ap.add_argument("--auto", action="store_true", help="零交互：用现有主模型，自动挑两级 fallback")
    ap.add_argument("--main", help="主模型")
    ap.add_argument("--fb1", help="一级 fallback 模型")
    ap.add_argument("--fb2", help="二级 fallback 模型")
    ap.add_argument(
        "--effort", choices=REASONING_LEVELS,
        help="主模型思考档位（默认沿用现有配置，没有则 low）",
    )
    ap.add_argument("--base-url", help="模型网关地址（换机器时用，默认 MemHub 网关）")
    ap.add_argument("--token", help="网关 token（也可用 CODEX_LB_API_KEY 环境变量；不写进日志）")
    args = ap.parse_args()

    if args.restore:
        return do_restore()

    head("环境")
    info(f"CODEX_HOME = {CODEX_HOME}")
    if not CODEX_HOME.is_dir():
        warn(f"{CODEX_HOME} 不存在，将新建（这台机器可能是首次配置）")
        CODEX_HOME.mkdir(parents=True, exist_ok=True)
    codex_bin = find_codex()
    if codex_bin:
        ok(f"codex = {codex_bin}")
    else:
        warn("没找到 codex 可执行文件，将只写配置，跳过运行时校验")

    prov = parse_provider()
    if prov:
        ok(f"已有 provider：{prov['name']} → {prov['base_url']}")
    else:
        prov = bootstrap_provider(args)
        if not prov:
            err("没有可用的模型网关配置，无法继续")
            return 1

    models, source = load_models(codex_bin, prov)
    if not models:
        err("读不到模型列表（网关 / 模型目录 / codex debug models 都失败），无法继续")
        return 1
    ok(f"读到 {len(models)} 个模型，来源：{source}")

    lines = read_lines()
    cur_main = current_main_model()
    provider = prov["name"]
    cur_effort = "medium"
    if CONFIG.exists():
        text = CONFIG.read_text(encoding="utf-8")
        m = re.search(r'^\s*model_reasoning_effort\s*=\s*"([^"]+)"', text, re.M)
        if m and m.group(1) in REASONING_LEVELS:
            cur_effort = m.group(1)
    info(f"当前主模型 = {cur_main or '（未设置）'}；provider = {provider}；effort = {cur_effort}")

    if args.list_models:
        for i, m in enumerate(models, 1):
            print(f"  {i:2d}. {m['slug']:<22} {m['display']:<22} ctx={m['ctx'] or '-'}")
        return 0

    legacy = detect_legacy(lines)
    if legacy:
        head("配置体检")
        for item in legacy:
            warn(f"本版本已不识别，当前被静默忽略：{item}")
        if args.fix_legacy:
            ok("已按 --fix-legacy 清理")
        else:
            info("加 --fix-legacy 可在本次安装时一并清掉（会先备份）")

    # 选择
    if args.auto:
        main_model = args.main or cur_main
        if not main_model:
            main_model = pick_fallbacks(models, "", 1)[0] if models else None
            if not main_model:
                err("读不到模型列表，无法自动选择")
                return 1
        auto_fb = pick_fallbacks(models, main_model, 2)
        if len(auto_fb) < 2:
            err(f"网关模型不足，无法为 {main_model} 选出两级 fallback")
            return 1
        fb1 = args.fb1 or auto_fb[0]
        fb2 = args.fb2 or auto_fb[1]
        main_effort = args.effort or cur_effort
        info(f"自动模式：主模型 {main_model} @{main_effort}，一级 {fb1}，二级 {fb2}")
    elif args.yes:
        need = {"--main": args.main, "--fb1": args.fb1, "--fb2": args.fb2}
        missing = [k for k, v in need.items() if not v]
        if missing:
            err(f"--yes 模式缺少参数：{', '.join(missing)}")
            return 2
        main_model, fb1, fb2 = args.main, args.fb1, args.fb2
        main_effort = args.effort or cur_effort
    else:
        print(f"\n{c('共 3 个模型要选；每个菜单都可以直接敲模型名，回车保持默认。', C_DIM)}")
        main_model = prompt_choice(
            "① 主模型（日常对话 / 干活都用它）", models, cur_main, set(),
        )
        main_effort = prompt_effort(cur_effort)
        fb1 = prompt_choice(
            "② 一级 fallback（主模型报错时接管）", models, None, {main_model},
            hint="建议选与主模型不同厂商，避免同一家一起挂",
        )
        fb2 = prompt_choice(
            f"③ 二级 fallback（{fb1} 也失败时兜底）", models, None, {main_model, fb1},
            hint="再换一家厂商，兜底能力更强",
        )

    if len({main_model, fb1, fb2}) < 3:
        err("主模型与两级 fallback 不能重复")
        return 2

    # 档位探测：以网关实测为准（catalog 的 supported_reasoning_levels 与实际不一致）
    if codex_bin and not args.no_probe:
        head("档位探测（以网关实测为准，catalog 声明不可靠）")
        plan = [
            ("主模型", main_model, main_effort),
            (f"{PROFILE_FB1}", fb1, main_effort),
            (f"{PROFILE_FB2}", fb2, main_effort),
        ]
        resolved: dict[str, str] = {}
        failed = False
        for label, model, want in plan:
            key = f"{label}:{model}"
            if key in resolved:
                got = resolved[key]
                info(f"{label} = {model} @{got}（同前）")
                continue
            got, note = probe_effort(codex_bin, model, want)
            if got is None:
                err(f"{label} = {model} 探测失败：{note}")
                failed = True
                continue
            resolved[key] = got
            (ok if got == want else warn)(f"{label} = {model} @{got}{note}")
        if failed:
            err("有模型不可用或档位全部被拒，先不写配置。换模型后重跑。")
            return 4
        main_effort = resolved[f"主模型:{main_model}"]
        fb1_effort = resolved[f"{PROFILE_FB1}:{fb1}"]
        fb2_effort = resolved[f"{PROFILE_FB2}:{fb2}"]
    else:
        fb1_effort = fb2_effort = main_effort

    fb1_file = render_profile(PROFILE_FB1, fb1, fb1_effort, provider, f"{PROFILE_FB1}：一级 fallback")
    fb2_file = render_profile(PROFILE_FB2, fb2, fb2_effort, provider, f"{PROFILE_FB2}：二级 fallback")
    wrapper = render_wrapper(
        codex_bin or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex", fb1, fb2
    )

    head("即将写入")
    info(f"顶层 model           = {main_model}（effort={main_effort}, provider={provider}）")
    info(f"{PROFILE_FB1}.config.toml      = {fb1} @{fb1_effort}")
    info(f"{PROFILE_FB2}.config.toml     = {fb2} @{fb2_effort}")
    info(f"provider              = {provider} → {prov['base_url']}")
    info(f"wrapper               = {WRAPPER}")
    info(f"config                = {CONFIG}")

    if args.dry_run:
        head("dry-run：config.toml 顶层")
        print(f'model = "{main_model}"')
        print(f'model_reasoning_effort = "{main_effort}"')
        print(f'model_provider = "{provider}"')
        if not prov.get("exists"):
            head("dry-run：新增 provider 段")
            print(render_provider(prov["name"], prov["base_url"], "***"))
        head("dry-run：fb.config.toml")
        print(fb1_file)
        head("dry-run：fb2.config.toml")
        print(fb2_file)
        return 0

    bk = backup("pre-install")
    ok(f"已备份到 {bk}")

    # 1) config.toml（顺带清掉历史遗留的 [agents] 段）
    new_lines = strip_managed_top_level(lines)
    new_lines = strip_agents_tables(new_lines)
    new_lines = strip_generated_banner(new_lines)
    if args.fix_legacy:
        new_lines, _ = strip_legacy(new_lines)
    prefix = [
        MANAGED_HEADER,
        f'model = "{main_model}"',
        f'model_reasoning_effort = "{main_effort}"',
        f'model_provider = "{provider}"',
        "",
    ]
    body_text = "\n".join(prefix) + "\n".join(new_lines).rstrip() + "\n"
    if not prov.get("exists"):
        body_text += render_provider(prov["name"], prov["base_url"], prov["token"])
        created_provider = True
    else:
        created_provider = False
    CONFIG.write_text(body_text, encoding="utf-8")
    CONFIG.chmod(0o600)
    ok("config.toml 已写入" + ("（含新写入的网关 provider 段）" if created_provider else ""))

    # 2) profiles
    for name, content in (
        (f"{PROFILE_FB1}.config.toml", fb1_file),
        (f"{PROFILE_FB2}.config.toml", fb2_file),
    ):
        p = CODEX_HOME / name
        p.write_text(content, encoding="utf-8")
        p.chmod(0o600)
        ok(f"{name} 已写入")

    # 3) 清掉历史遗留的角色 overlay（子 agent 已不支持）
    for d in (ROLES_DIR, LEGACY_ROLES_DIR):
        if d.is_dir():
            shutil.rmtree(d)
            ok(f"{d.as_posix()} 已删除（子 agent 不再使用）")

    # 4) wrapper
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    WRAPPER.write_text(wrapper, encoding="utf-8")
    WRAPPER.chmod(0o755)
    ok(f"{WRAPPER} 已写入")

    # 安装器落一份稳定副本，保证事后 --restore 有可用路径
    try:
        me = Path(__file__).resolve()
        if me != INSTALLER_COPY.resolve():
            shutil.copy2(me, INSTALLER_COPY)
            INSTALLER_COPY.chmod(0o644)
            ok(f"{INSTALLER_COPY} 已写入（回滚用）")
    except (OSError, NameError, RuntimeError):
        pass

    # 5) 校验
    head("校验")
    all_ok = True
    for path in [
        CONFIG,
        CODEX_HOME / f"{PROFILE_FB1}.config.toml",
        CODEX_HOME / f"{PROFILE_FB2}.config.toml",
    ]:
        good, msg = validate_toml(path)
        (ok if good else err)(f"{path.name}: {msg}")
        all_ok &= good

    r = subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True, text=True)
    if r.returncode == 0:
        ok("codex-fb 语法 OK")
    else:
        err(f"codex-fb 语法错误：{r.stderr.strip()}")
        all_ok = False

    if codex_bin:
        all_ok &= verify_with_codex(codex_bin)
        all_ok &= verify_profiles(codex_bin)

    head("完成")
    if all_ok:
        ok("全部校验通过")
    else:
        warn("有校验未通过，见上面输出；可 `--restore` 回滚")
    print(f"""
{c('怎么用', C_HEAD)}

  {c('App（图形界面）', C_OK)}
    主模型已改好，直接用。切 fallback 在 App 内是手动的：输入 /model 选模型。
    App 没有读 profile 的入口，自动 fallback 只在命令行生效。

  {c('命令行：自动 fallback', C_OK)}
    {WRAPPER} exec "把 XXX 功能实现一下"
      主模型失败 → {PROFILE_FB1}({fb1}) → {PROFILE_FB2}({fb2})，只对可判定的 API/网络失败重试
      日志落在 {CODEX_HOME.as_posix()}/logs/codex-fb-*.log

  {c('命令行：手动切某一级', C_OK)}
    codex -p {PROFILE_FB1} exec "..."        # 用 {fb1}
    codex -p {PROFILE_FB2} exec "..."        # 用 {fb2}

  {c('回滚', C_OK)}
    python3 {installer_hint()} --restore
""")
    return 0 if all_ok else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消，未做任何修改。")
        sys.exit(130)
    except EOFError:
        print("\n输入中断（EOF），已取消，未做任何修改。")
        sys.exit(130)
