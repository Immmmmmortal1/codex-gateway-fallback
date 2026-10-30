# codex-gateway-fallback

给 Codex（ChatGPT.app 内置 CLI 或独立 `codex`）接入任意 OpenAI 兼容网关，并装上一主两备的模型链：主模型调用失败时自动切一级 fallback，再失败切二级。

Codex 本身没有原生 fallback 链（`model_fallbacks` 之类的键会被 strict-config 拒绝），所以这里用「独立 profile 文件 + shell wrapper」实现。

## 一条命令

```bash
curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh | bash -s -- --auto
```

跑完按提示重启 Codex。默认零交互：读取你现有的主模型，自动挑两个 fallback。

## 不需要填 token

- **已经配好网关的机器**：token 就在 `~/.codex/config.toml` 的 provider 段里，脚本直接复用，不会问你。
- **本机环境变量有 `CODEX_LB_API_KEY`**：用它。
- **只有这两样都没有**（真正的全新机器，从没配过任何网关）才需要你提供，脚本会停下来提示。

配 fallback 本身不需要 token —— `fb.config.toml` 只声明模型和档位，provider 和凭证继承自主配置。

## 常用参数

```bash
# 零交互（默认）
... | bash -s -- --auto

# 指定模型
... | bash -s -- --main deepseek-flash --fb1 gpt-6.1-terra --fb2 glm-5.3

# 交互式挑选（会列出网关上的全部模型）
... | bash -s --

# 只看有哪些模型可选
... | bash -s -- --list-models

# 预演，不落盘
... | bash -s -- --auto --dry-run

# 回滚上一次安装
... | bash -s -- --restore
```

`--no-probe` 跳过档位实测，快很多；不加则对每个模型真实发请求试 low/medium/high，慢但更准。

## 它做了什么

1. 定位 `codex` 可执行文件与 `CODEX_HOME`
2. 读取或询问网关地址与 token，直连网关 `/models` 拿真实模型列表
3. 确定主模型与两级 fallback，并实测可用档位
4. 备份现有配置
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

读的是主模型，直接用；切模型手动 `/model`。App 不读 profile，所以自动 fallback 只在命令行生效。改完配置要 `Cmd+Q` 完全退出再重开。

## 什么情况会切，什么不会

| 情况 | 是否切换 |
|---|---|
| 模型下线 / 4xx / 5xx | 切 |
| 网络断开 / 连接失败 | 切 |
| 响应流中断 | 切 |
| Ctrl-C | 不切，立即退出 |
| 模型答错内容、代码写错 | 不切（这是业务问题） |

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

## 安全

仓库里没有任何密钥。token 来自本机配置、环境变量，或你运行时输入（不回显），会被写进 `~/.codex/config.toml` 的 `experimental_bearer_token`——这是 Codex 的字段要求，所以 `config.toml` 属于敏感文件，不要提交或外传。

用 `curl | bash` 之前建议先看一眼：

```bash
curl -fsSL https://raw.githubusercontent.com/Immmmmmortal1/codex-gateway-fallback/main/install.sh
```
