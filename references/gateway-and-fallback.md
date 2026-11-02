# 网关接入与 fallback 契约

本文件是 `codex-gateway-fallback` 的细节参考。`SKILL.md` 里没展开的配置格式、错误形态、迁移注意事项都在这里。

## 1. Codex 配置结构

Codex 读 `$CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`）。接自建网关只需要两块：

```toml
model = "<主模型>"
model_reasoning_effort = "low"      # low / medium / high / max，以网关实测为准
model_provider = "<provider 名>"

[model_providers.<provider 名>]
name = "<随便写的展示名>"
base_url = "https://<网关>/v1"       # 注意结尾通常是 /v1，不要带 /models
wire_api = "responses"              # OpenAI 兼容网关必须是 responses，不是 chat
supports_websockets = false
requires_openai_auth = false
experimental_bearer_token = "<token>"
```

要点：

- `wire_api` 写 `responses`。写成 `chat` 会走到 chat/completions，多数自建网关只在 responses 协议下工作。
- `requires_openai_auth = false`，否则 Codex 会去要 OpenAI 登录态。
- token 只能放 `experimental_bearer_token`。这是 Codex 的字段名，token 会以明文落在文件里，因此 `config.toml` 属于敏感文件，权限保持 `600`。
- Codex 的 strict-config 会**静默忽略**不认识的键（如顶层 `env_key`、整段 `[defaults]`、`[features] rmcp_client`），写了不报错但不生效。

## 2. profile 与 fallback

Codex 没有原生 fallback 链。`model_fallbacks` / `fallback_models` 之类的键会被拒绝。可用做法：

- 每个备用模型一个独立 profile 文件：`$CODEX_HOME/<name>.config.toml`（本 skill 用 `fb.config.toml`、`fb2.config.toml`）。
- profile 文件内容就是最小配置：`model` / `model_reasoning_effort` / `model_provider`。
- 用法是 `codex -p <name> ...`。

**不能用 `[profiles.x]` 表**：老式的表内 profile 会被拒绝。必须用独立文件。

## 3. wrapper 的重试语义

`bin/codex-fb` 是 shell 包装层，行为边界：

| 情况 | 是否切下一级 |
|---|---|
| `ERROR: unexpected status 4xx/5xx`（含模型下线 409） | 切 |
| `ERROR: Connection failed` | 切 |
| `ERROR: Reconnecting...` | 切 |
| `ERROR: ... stream ...` | 切 |
| exit 0（成功） | 不切，直接退出 |
| exit 130（Ctrl-C） | 不切，立即退出 |
| 模型答错内容 / 代码写错但调用成功 | 不切（这是业务问题） |

其它子命令（TUI、`resume`、`review` 等）原样透传给 `codex`，不做重试。只有 `exec` 走三级链。

关键细节：wrapper 启动时显式传 `-c features.unbounded_connection_retries=false`。Codex 默认开着这个开关，网络故障时会无限 `Reconnecting...` 永不退出，wrapper 等不到返回值，fallback 永远不触发。

注意 fallback 会**重新执行整条 prompt**，主模型已经改过的文件不会自动回滚。写文件的场景要提醒用户。

## 4. 错误形态实测对照

| 场景 | 实际报错 | exit |
|---|---|---|
| 模型不存在 / 已下线 | `ERROR: unexpected status 409 Conflict: {"detail":"该模型不存在或已下线..."}` | 1 |
| 网络断 | `ERROR: Reconnecting... 1/5` … `5/5` + `Connection failed` | 1 |
| 用户 Ctrl-C | 无 ERROR | 130 |
| 正常完成 | 无 ERROR | 0 |

`codex exec` 的所有正常输出也走 **stderr**，stdout 为空。任何验证脚本都要 `2>&1`。

## 5. 档位（reasoning effort）

`model_catalog_json` 里的 `supported_reasoning_levels` 不可信，和网关实际接受的不一致。例如某些模型声明支持 `medium`，但网关会回「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」。

所以本 skill 的 `probe_effort()` 用真实请求探测：按 `期望档位 → low → high → medium → max` 顺序试，第一个返回 0 的就是可用档位，并提示发生了降级。

跳过探测（`--no-probe`）时直接写声明档位，可能拿到一个会被拒的值。

## 6. 模型列表的权威来源

| 来源 | 结果 |
|---|---|
| 网关 `GET {base_url}/models`（`Authorization: Bearer <token>`） | **权威**，返回网关真实可用的全部模型 |
| `codex debug models` | 只返回 Codex 内置模型（通常 9 个），看不到网关上自建的模型 |
| `$HOME/.memhub/model-catalog.json` | 本地缓存，可能漂移，只在网关不可达时兜底 |

换机器时最容易踩的坑：拿 `codex debug models` 当清单，然后以为网关上没有 `deepseek-flash`。

`model_catalog_json` 不是必需的。不配它也能跑，只是 Codex 会打一条 `warning: Model metadata for <model> not found. Defaulting to fallback metadata`，通常可以接受。

## 7. MemHub Skill Environment 取凭据

本 skill 不存 token。运行时按顺序找：

1. `--token` 参数
2. `CODEX_LB_API_KEY` / `MEMHUB_API_KEY` 环境变量
3. MemHub Skill Detail 的 Environment 配置
4. 交互式询问（`getpass`，不回显）

第 3 步的接口：

```http
GET {site_root}/api/skills/enabled/codex-gateway-fallback/env
X-API-Key: <memhub api key>
```

MemHub 的 api key 与站点根从 `MEMHUB_API_KEY` / `MEMHUB_BASE_URL` 或 `~/.memhub/credentials.json`（`api_key` / `base_url`）读取。`base_url` 是站点根，不要拼 `/api`。

Environment 配置推荐形状（对象或按项目分组的数组都支持）：

```json
{
  "CODEX_GATEWAY_BASE_URL": "https://memhub.vvicat.dev/api/llm/v1",
  "CODEX_GATEWAY_TOKEN": "replace-in-ui"
}
```

脚本会匹配 `CODEX_GATEWAY_TOKEN` / `CODEX_LB_API_KEY` / `token` / `api_key`，以及 `CODEX_GATEWAY_BASE_URL` / `base_url` / `gateway_url`。取不到就回落到交互询问，不会猜。

## 8. 换机器迁移

`config.toml` **不能整体拷贝**，里面通常混着只属于原机器的东西：

- `model_catalog_json` 指向本机绝对路径的 catalog 文件
- `notify` 里一长串本机 `.app` / `.py` 绝对路径
- `[projects."/Users/<name>/..."]` 一堆本地目录信任项
- `[mcp_servers.*]` 的绝对路径、`cwd`

正确做法：只拷 `scripts/setup_codex_gateway.py` 到新机器跑一遍，让它自己写干净的 provider 段和三个模型文件。

## 9. 子 agent 不在本 skill 范围

实测结论：`spawn_agent` 等协作工具只在模型 slug 属于 **Codex 官方内置模型名** 时才注册。把官方模型条目的元数据整体复制、只改 slug 成 `deepseek-flash` 或 `glm-5.3`，工具表里的 collaboration 命名空间会整组消失，调用返回 `unsupported call`。

因此本 skill 不配置 `[agents.*]`，也不建议在网关上用私有模型名当主模型却期待能派发子 agent。如果用户要子 agent，主模型必须选官方内置名（如 `gpt-6.1-terra`、`gpt-5.6-sol`）。

## 10. 回滚

安装前会备份到 `$CODEX_HOME/backups/codex-config-pre-install-<时间戳>/`，包含 `config.toml`、两个 profile、wrapper 和 MANIFEST。

```bash
python3 <skill>/scripts/setup_codex_gateway.py --restore
```

`--restore` 一次回退一步，备份目录会被改名加 `.restored-<时间戳>` 后缀。连续执行可逐步往前回退。
