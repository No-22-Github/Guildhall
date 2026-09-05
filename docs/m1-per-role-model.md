# M1 考古:per-role 模型配置的可行性(sandbox-agent 0.4.2,实测 2026-09-05)

> §2.1.1 要求先考古再写代码。本文是对 0.4.2 二进制实测(2026-09-05,
> 本机 127.0.0.1:2468 存活实例,curl 逐条验证)的结论记录。
> 结论:**纯方案 A 不成立;采用方案 B**(per-role 独立 server 实例)。

## 1. `session/new` 返回的 `configOptions` 完整结构(实测)

`session/new` 返回 `{sessionId, modes, configOptions}`。`configOptions` 是四项
`select` 类型配置,实测完整列表(本机环境,值随各自 env 不同而不同):

| id | category | 说明 | 实测可选值 |
|---|---|---|---|
| `mode` | mode | Session permission mode | `default` / `acceptEdits` / `plan` / `auto` / `bypassPermissions` |
| `model` | model | AI model to use | `default` / `opus` / `fable` / `sonnet` / `haiku` / `<自定义别名>`(本机还列出 `glm-5.3-flash[1M]`) |
| `effort` | thought_level | Available effort levels | `default` / `low` / `medium` / `high` / `xhigh` / `max` |
| `agent` | — | 子代理选择 | `default` / `code-simplifier:code-simplifier` |

关键事实:

1. **model 可以 per-session 设置,且已验证生效**。方法:

   ```
   POST /v1/acp/{sid}
   {"jsonrpc":"2.0","id":6,"method":"session/set_config_option",
    "params":{"sessionId":"<uuid>","configId":"model","value":"sonnet"}}
   ```

   注意参数名是 **`configId`**(不是 `categoryId`;实测 `categoryId` 报
   -32602 Invalid input)。响应会回显完整 `configOptions`,其中
   `model.currentValue` 确认变为 `sonnet`。同一连接上新开 `session/new`
   得到的是新 session,不受影响。

2. **但 model 选项只是 adapter 预置的别名档位**,映射关系由 adapter 进程的
   环境变量(settings)决定。它**不能指定任意模型名,更不能指定
   base_url / auth_token**——API 地址与凭据不在 configOptions 的任何一项里。

## 2. 建连接时传 env / base_url / token:不存在

- `sandbox-agent server --help`:只有 `--host/--port/--cors-*/--token/--no-token/--no-telemetry`,
  没有任何 env、model、upstream 相关选项。
- `POST /v1/acp/{sid}` 的 bootstrap query 只有 `?agent=`(m0 已考);其他 query
  参数不存在。
- `/v1/agents` 的 `capabilities` 全是能力布尔位(planMode/permissions/toolCalls/
  streamingDeltas/…),没有 env / model / credential 配置能力。
- adapter 进程由 sandbox-agent server 拉起,**环境变量继承自 server 进程**,
  是进程全局的,不是 per-session。

## 3. 结论:方案 B

§2.1.4 的 config 契约要求三角色各自可配 `model / base_url / auth_token`。
`configOptions` 只覆盖 model(且只是别名档位),base_url 与 auth_token
只能靠进程环境变量 → **方案 B:每个角色(按 env 签名去重后)一个独立的
sandbox-agent server 实例,各带各的 env,按端口区分**:

- `SandboxManager` 管理 `签名 → (端口, Popen)` 的字典;端口从 `base_port`
  起按角色顺序分配(2468/2469/2470)。
- 拉起时 `subprocess.Popen(cmd, env={**os.environ, **role_env})`;env 键用
  Claude Code 官方约定:`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` /
  `ANTHROPIC_MODEL`。
- 两个角色的 env 完全相同 → 复用同一实例,不重复起。
- 无任何 `[agent.<role>]` 配置时,全部角色共享 base_port 上的默认实例
  (即旧行为:该实例若已在本机运行则复用)。
- 启动自检对每个实际使用的实例各做一次 `assert_agent_available`。

`session/set_config_option`(configId=model)作为已验证能力记录在案,
本版不用:方案 B 下模型已由实例 env(`ANTHROPIC_MODEL`)决定,再走
set_config_option 只会在多上游场景叠加一层别名映射,没有收益。

## 4. 实测记录(原样)

```
POST session/set_config_option {"configId":"model","value":"sonnet"}
→ {"id":6,"jsonrpc":"2.0","result":{"configOptions":[... model.currentValue = "sonnet" ...]}}

POST session/set_config_option {"categoryId":"model",...}
→ {"error":{"code":-32602,"message":"Invalid params",
    "data":{"configId":{"_errors":["Invalid input: expected string, received undefined"]}}}}
```
