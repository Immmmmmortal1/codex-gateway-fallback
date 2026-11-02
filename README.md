# codex-gateway-fallback

给 Codex（ChatGPT.app 内置 CLI 或独立 `codex`）接入任意 OpenAI 兼容网关，并装上一主两备的模型链：主模型调用失败时自动切一级 fallback，再失败切二级。

Codex 本身没有原生 fallback 链（`model_fallbacks` 之类的键会被 strict-config 拒绝），所以这里用「独立 profile 文件 + shell wrapper」实现。

## 三条命令

```bash
# 安装（交互式，会列出网关上所有模型让你挑）
curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh | bash

# 更新（先比对版本，已是最新就直接退出）
curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh | bash -s -- --update

# 卸载（把 Codex 配置还原到安装前）
python3 ~/.codex/bin/setup_codex_gateway.py --uninstall
```

装完/更新完都要 `Cmd+Q` 完全退出 Codex 再打开——配置只在进程启动时读一次，没有热重载。

网络访问不了 `raw.githubusercontent.com` 时先设代理：

```bash
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
```

## 安装

```bash
# 交互式（推荐）：按提示依次选主模型 / 一级 fallback / 二级 fallback
... | bash

# 零交互：沿用现有主模型，自动挑两个 fallback
... | bash -s -- --auto

# 指定模型
... | bash -s -- --main deepseek-flash --fb1 gpt-6.1-terra --fb2 glm-5.3

# 先看有哪些模型可选
... | bash -s -- --list-models

# 预演，不落盘
... | bash -s -- --auto --dry-run
```

交互模式下每个菜单都能直接敲模型名，不用记序号；回车保持默认。

`--no-probe` 跳过档位实测，快很多；不加则对每个模型真实发请求试 low/medium/high，慢但更准。

### 不需要填 token

- **已经配好网关的机器**：token 就在 `~/.codex/config.toml` 的 provider 段里，脚本直接复用，不会问你。
- **本机环境变量有 `CODEX_LB_API_KEY`**：用它。
- **只有这两样都没有**（真正的全新机器，从没配过任何网关）才需要你提供，脚本会停下来提示。

配 fallback 本身不需要 token —— `fb.config.toml` 只声明模型和档位，provider 和凭证继承自主配置。

## 更新

```bash
... | bash -s -- --update
```

先比对本地版本与仓库里的 `VERSION`：

- 已是最新 → 直接退出，不写任何文件
- 有新版本 → 原地更新：**保留你现有的主模型和两级 fallback**，不会重新问你，也不会卡在交互提示

想重新挑模型就直接跑不带参数的安装命令（交互式）。重跑是幂等的，受管注释块只有一份，安装前自动备份到 `~/.codex/backups/codex-config-*`。

也可以只检查不安装：

```bash
python3 ~/.codex/bin/setup_codex_gateway.py --check-update
```

**不要**用 `~/.codex/bin/setup_codex_gateway.py --update`：那是本地副本，本身就是要更新掉的旧版，用它更新等于把旧版再装一遍。脚本检测到这种情况会拦住并提示正确的 curl 命令。

重跑安装是安全的：安装前自动备份到 `~/.codex/backups/codex-config-*`。`--auto` 重跑时**沿用现有的 fb1/fb2**（只在模型缺失或已从网关下线时才重新挑），不会把你自己选的模型悄悄换掉。

## 卸载

```bash
python3 ~/.codex/bin/setup_codex_gateway.py --uninstall
```

会先列出要删什么，输入 `yes` 确认后执行。执行前自动备份到 `~/.codex/backups/codex-config-pre-uninstall-*`。

删除的只有本工具造的东西：

- `config.toml` 顶部的受管注释头
- `~/.codex/fb.config.toml`、`~/.codex/fb2.config.toml`
- `~/.codex/bin/codex-fb`
- `~/.codex/bin/setup_codex_gateway.py`、`~/.codex/bin/.codex-gf-version`

`config.toml` **顶部（第一个 `[table]` 之前）整段还原为首次安装前的值**——不只是 `model`，还包括 `model_reasoning_effort`、`model_provider`，因为安装器会补这些键。

网关 provider 段：安装器新建的会一并删除，**你原本就有的保留**（卸载误删它会让 Codex 直接连不上模型）。

只想去掉注释头、保留当前模型：

```bash
python3 ~/.codex/bin/setup_codex_gateway.py --uninstall --keep-model
```

卸载不依赖网络。`install.sh` 遇到 `--uninstall` / `--restore` / `--check-update` 会优先用本地副本，连不上 GitHub 也能卸；本地副本也丢了会明确报错，不会卡在下载。

想反悔：卸载时会打印备份路径，把里面的文件拷回去即可。

## 它做了什么

1. 定位 `codex` 可执行文件与 `CODEX_HOME`
2. 读取或询问网关地址与 token，直连网关 `/models` 拿真实模型列表
3. 确定主模型与两级 fallback，并实测可用档位
4. 留存首次安装前状态，并备份当前配置
5. 写 `config.toml`（顶层三个键 + 缺失时补 provider 段）
6. 写 `fb.config.toml`、`fb2.config.toml`、`bin/codex-fb`
7. 校验 TOML 语法、profile 能否加载、wrapper 语法

不会动你配置里的 `mcp_servers`、`projects`、`hooks` 等段落。

## 怎么用

**命令行（有自动 fallback）**

```bash
~/.codex/bin/codex-fb exec "把 XXX 实现一下"
```

日志在 `~/.codex/logs/codex-fb-*.log`。

**手动指定某一级**

```bash
codex -p fb exec "..."
codex -p fb2 exec "..."
```

**App（ChatGPT.app）**

读的是主模型，直接用；切模型手动 `/model`。App 不读 profile，所以**自动 fallback 只在命令行生效**。

## 什么情况会切，什么不会

| 情况 | 是否切换 |
|---|---|
| 模型下线 / 4xx / 5xx | 切 |
| 网络断开 / 连接失败 | 切 |
| 响应流中断 | 切 |
| 容量不足（`at capacity. Please try a different model`） | 切 |
| 限流 / 过载 / 配额用尽 | 切 |
| Ctrl-C | 不切，立即退出 |
| 模型答错内容、代码写错 | 不切（这是业务问题） |

容量类错误容易被漏掉：Codex 把它渲染成一句普通文案（`Selected model is at capacity. Please try a different model.`），不带 `unexpected status` 前缀。wrapper 从 1.1.3 起按关键词单独识别这一类。

fallback 会重新执行整条 prompt，主模型已改过的文件不会自动回滚。

## 一个重要限制

`spawn_agent` 等子 agent 工具只在模型名属于 **Codex 官方内置模型** 时才会注册。把官方模型的元数据整体复制、只改 slug 成 `deepseek-flash` 或 `glm-5.3`，协作命名空间会整组消失，调用返回 `unsupported call`。

所以：主模型用网关私有名（deepseek-flash / glm-5.3）就没有子 agent；要子 agent 就把主模型设成 `gpt-6.1-terra` 这类官方名。

## 已知细节

- 网关模型列表只认 `/models` 接口。`codex debug models` 只返回 Codex 内置模型，看不到网关上的私有模型。
- `model_catalog_json` 不是必需的；不配也能跑，只是会有一条 `Model metadata not found` 警告。
- 档位以网关实测为准，`supported_reasoning_levels` 声明常与实际不符。
- Codex 默认开着 `features.unbounded_connection_retries`，网络故障时会无限 `Reconnecting...` 永不退出；wrapper 会显式关掉它，否则 fallback 永远不触发。
- `codex exec` 的正常输出走 stderr，stdout 为空。
- 备份目录是滚动保留的，可能被后续安装挤掉；因此首次安装前的配置额外存在 `~/.codex/backups/codex-config-first-install/`，卸载以它为准。

## 安全

仓库里没有任何密钥。token 来自本机配置、环境变量，或你运行时输入（不回显），会被写进 `~/.codex/config.toml` 的 `experimental_bearer_token`——这是 Codex 的字段要求，所以 `config.toml` 属于敏感文件，不要提交或外传。

用 `curl | bash` 之前建议先看一眼：

```bash
curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh
```
