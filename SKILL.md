---
name: codex-gateway-fallback
description: 给 Codex（ChatGPT.app 内置 CLI 或独立 codex）接入自建 OpenAI 兼容网关模型，并配置「主模型 + 两级 fallback + 报错自动切换」；也可用于把这套配置迁移到新电脑、换模型、排查 fallback 不生效。Use when 用户提到 codex 换模型、codex 接自己的网关、codex 模型报错自动切换、codex fallback、codex 配置迁移到新电脑、codex-lb、memhub 网关。
metadata:
  version: 1.1.8
---

# Codex Gateway Fallback

把任意 OpenAI 兼容网关（默认 MemHub LLM Gateway）接进 Codex，并装上三级模型链：主模型跑挂了自动切一级 fallback，再挂切二级。

Codex 没有原生 fallback 链（`model_fallbacks` 之类的键会被 strict-config 拒绝），所以本 skill 用「profile 文件 + shell wrapper」实现：profile 负责声明备用模型，wrapper 只识别可重试错误并逐级重试。

## 先判断用户要哪一种

| 用户诉求 | 走哪条路 |
|---|---|
| 第一次在这台机器上配 | 完整安装（第 1 步起） |
| 想换模型 / 换档位 | 重跑同一脚本，幂等 |
| 配好了但报错没切换 | 跳到「排查 fallback 不生效」 |
| 换到新电脑 | 只拷 `scripts/setup_codex_gateway.py`，在新机器上跑 |

## 1. 准备凭据

按顺序找，找到就停：

1. 用户显式给的 `--token` 参数
2. 环境变量 `CODEX_LB_API_KEY` 或 `MEMHUB_API_KEY`
3. MemHub Skill Detail 的 Environment 配置（脚本自动查，见 `references/gateway-and-fallback.md`）
4. 都没有 → 停下来问用户，不要猜、不要编一个

网关地址同理：`--base-url` → `CODEX_BASE_URL` → Environment 配置 → 默认 `https://memhub.vvicat.dev/api/llm/v1`。

第 3 步的 Environment 配置走 MemHub 接口：`GET {site_root}/api/skills/enabled/codex-gateway-fallback/env`，header `X-API-Key: <memhub key>`。memhub key 与站点根从 `MEMHUB_API_KEY` / `MEMHUB_BASE_URL` 或 `~/.memhub/credentials.json` 读，`base_url` 是站点根不要拼 `/api`。字段名与匹配规则见 `references/gateway-and-fallback.md` 第 7 节。

**任何时候不要把 token 打印出来、不要写进回复、不要存进 skill 目录。**

## 2. 摸清这台机器的 Codex

```bash
which codex
ls /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
ls "${CODEX_HOME:-$HOME/.codex}/config.toml"
```

记下 `CODEX_HOME`（默认 `~/.codex`）。全新机器上这个目录可能都还没有，脚本会自己建。

## 3. 先看模型列表

```bash
python3 <skill>/scripts/setup_codex_gateway.py --list-models
```

这一步会直接问网关 `/models`。**不要用 `codex debug models` 判断可选模型**——它只返回 Codex 内置模型（通常 9 个），看不到网关上的私有模型，这是换机器时最常见的踩坑点。

## 4. 安装

最快路径（一行命令，零交互，读取现有主模型并自动挑两级 fallback）：

```bash
curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh | bash -s -- --auto
```

该脚本公开托管在 GitHub（public），不含任何密钥。

交互式（会列出网关全部模型让你挑）：

```bash
python3 <skill>/scripts/setup_codex_gateway.py
```

非交互：

```bash
python3 <skill>/scripts/setup_codex_gateway.py --no-probe --yes \
  --main deepseek-flash --fb1 gpt-6.1-terra --fb2 glm-5.3 --effort low
```

脚本按顺序做：探测网关 → 问主模型 / 一级 fallback / 二级 fallback → 档位实测 → 备份 → 写 `config.toml`（含 provider 段）→ 写 `fb.config.toml`、`fb2.config.toml` → 生成 `bin/codex-fb` → 校验。

`--no-probe` 会跳过档位实测，快很多，但可能拿到一个被网关拒绝的档位；不加则要对每个模型真实发请求，三模型可能跑几分钟。

其它开关：`--dry-run` 只看要写什么，`--restore` 回滚上一次安装。

### 检查更新

```bash
python3 ~/.codex/bin/setup_codex_gateway.py --check-update
```

打印本地已装版本、远端最新版本。只读，不写任何文件，没网也能跑（会提示读不到远端）。

### 更新到新版本

```bash
curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh | bash -s -- --update
```

`--update` 先比对版本：已是最新就直接退出；有新版本才重新安装。

**不要**用 `~/.codex/bin/setup_codex_gateway.py --update`：那是本地副本，
本身就是要更新掉的旧版，用它更新等于把旧版再装一遍。脚本检测到这种情况会拦住
并提示正确的 curl 命令。

只更新脚本、不动模型选择，也可以直接重跑安装命令（不加 `--update`）。重跑是幂等的：
受管注释块只有一份，不会重复堆叠，安装前会备份到 `$CODEX_HOME/backups/codex-config-*`。
`--auto` 重跑时沿用现有 fb1/fb2，只在缺失或模型已下线时才重新挑。

更新完必须让用户重启 Codex：配置只在进程启动时读取，没有热重载。

### 卸载 / 还原 Codex 原配置

```bash
python3 ~/.codex/bin/setup_codex_gateway.py --uninstall
```

会先列出要删什么，回车确认（输入 `yes`）后执行。执行前自动备份到
`$CODEX_HOME/backups/codex-config-pre-uninstall-*`。

删除的东西只有本工具造的：受管注释头、`fb.config.toml`、`fb2.config.toml`、
`bin/codex-fb`、`bin/setup_codex_gateway.py`、`bin/.codex-gf-version`。

`config.toml` 顶部（第一个 `[table]` 之前）整段还原为**首次安装前**的值——
包括 `model`、`model_reasoning_effort`、`model_provider`。安装器新建的
`[model_providers.*]` 段会一并删除；原本就有的 provider 段保留不动
（那可能属于用户自己的配置）。

`--uninstall --keep-model` 则只移除受管注释头，`model` 保持当前值。

离线可用：`install.sh --uninstall` 会优先用本地副本，连不上 GitHub 也能卸。
如果本地副本也丢了，`install.sh` 会明确报错而不是卡在下载。

## 5. 验证

安装完必须自己验，不要只报「装好了」：

```bash
# a) 主模型能跑
codex exec --skip-git-repo-check -s read-only "只回复：MAINOK"

# b) fallback 真会触发（临时把主模型改成不存在的名字）
CODEX_HOME=/tmp/fbtest cp -R "$HOME/.codex" /tmp/fbtest
sed -i '' 's/^model = ".*"/model = "gpt-9-does-not-exist"/' /tmp/fbtest/config.toml
CODEX_HOME=/tmp/fbtest "$HOME/.codex/bin/codex-fb" exec "只回复：FBOK"
```

第二条要能看到 `codex-fb: 主模型 失败 (exit=1)，准备切到下一个模型…`，并且实际执行的模型变成一级 fallback。

判据说明：`codex exec` 的正常输出走 **stderr**，stdout 是空的，所以验证时用 `2>&1` 再 grep。

## 6. 排查 fallback 不生效

按这个顺序查，别乱试：

1. 是不是用 `codex` 而不是 `codex-fb`？自动 fallback 只在 wrapper 里。
2. `features.unbounded_connection_retries` 是不是还开着？开着时网络故障会无限 `Reconnecting...` 永不退出，wrapper 拿不到返回值，也就永远不切换。wrapper 启动时会显式关掉它。
3. 错误是不是真的「可重试」？wrapper 只认 `unexpected status 4xx/5xx`、`Connection failed`、`stream`、`Reconnecting...`。模型答错内容、代码写错属于业务问题，不会切换。
4. 看日志：`${CODEX_HOME}/logs/codex-fb-*.log`。

完整错误形态对照表在 `references/gateway-and-fallback.md`。

## 7. 告诉用户怎么用

App 和命令行是两套行为，必须说清楚：

- **App（ChatGPT.app）**：读的是 `~/.codex/config.toml` 里的主模型，直接用；切模型手动 `/model`。App 没有读 profile 的入口，**自动 fallback 在 App 里不生效**。
- **命令行**：`~/.codex/bin/codex-fb exec "..."` 有自动 fallback；`codex -p fb exec "..."` 直接指定某一级。
- 改完配置要让用户 **Cmd+Q 完全退出 App 再重开**，否则 App 内存里还是旧配置。

## 8. 校验与自检

```bash
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py <skill-dir>
python3 ~/.codex/skills/memhub-skill-creator/scripts/check_memhub_skill.py <skill-dir>
python3 <skill>/scripts/setup_codex_gateway.py --help
```

改了这个 skill 之后必须发布并验证线上版本，commit 不等于发版。

## 边界

- 不写 token 到任何文件副本，只在安装时由用户输入或环境变量提供。
- 不删除用户 `config.toml` 里已有的 `mcp_servers` / `projects` / `hooks` 等段落，只改 `model*` 三个顶层键并补 provider 段。
- 遇到网关不支持的档位就降级，不要硬写一个会被拒的值。
- 子 agent / `spawn_agent` 不在本 skill 范围内：实测只有 Codex 官方内置模型名能派发子 agent，网关私有模型（deepseek-flash / glm-5.3）会返回 `unsupported call`。

细节见 `references/gateway-and-fallback.md`。
