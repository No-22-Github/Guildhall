# §2.1 回填:sandbox-agent 0.4.2 真实 HTTP 契约(M0 考古结论)

> 规格书 §2.1 预言成真:SDK 方法名(`createSession` / `postMessage` / `streamEvents`)
> 来自旧版文档,仓库里的 `docs/openapi.json` 只有 desktop/fs/processes 等 59 条路径,
> **没有任何 sessions 端点**。0.4.2 的真实会话协议是 **ACP(Agent Client Protocol)
> JSON-RPC,跑在流式 HTTP(SSE)上**。本文档是实测结论(2026-09-04,
> 对 0.4.2 二进制逐条 curl 验证),实现按此执行。

## 会话生命周期(每个角色一条连接)

一次「连接」= 一个客户端自造的 `server_id` 路径。sandbox-agent 收到首个 POST 时,
按 bootstrap query 里的 agent 名拉起对应 adapter 进程。

| 动作 | method + path | 说明 |
|---|---|---|
| 健康检查 | `GET /v1/health` | `{"status":"ok"}` |
| 列出 agent | `GET /v1/agents` | `{agents:[{id, installed, credentialsAvailable, capabilities}]}`。**agent id 是 `claude`,不是 `claude-code`** |
| 建 ACP 连接 | `POST /v1/acp/{server_id}?agent=claude`(仅首个 POST 带 query) | 请求体/响应体都是单条 JSON-RPC envelope |
| 收事件流 | `GET /v1/acp/{server_id}`(`Accept: text/event-stream`) | 标准 SSE;`event: message`;**`id: N` 单调递增整数**,即 offset 游标;重连带 `Last-Event-ID: N` |
| 关闭连接 | `DELETE /v1/acp/{server_id}` | adapter 进程随之结束 |

`server_id` 由客户端生成,只要求路径安全。Guildhall 用 `guildhall-<quest-id>-<role>`,
与 §2.4 的 session 命名规则逐字一致,Inspector(`/ui/`)里也按这个名字显示。

## JSON-RPC 方法(client → agent)

| 方法 | 参数 | 返回 |
|---|---|---|
| `initialize` | `{protocolVersion: 1, clientInfo: {name, version}}` | `agentCapabilities` 等 |
| `session/new` | `{cwd, mcpServers: []}` | `{sessionId, modes, configOptions}`;`configOptions` 含 `mode`(default/acceptEdits/plan/auto/bypassPermissions) |
| `session/prompt` | `{sessionId, prompt: [{type:"text", text}]}` | `{stopReason, _meta.quota}`;**stopReason 到达即一轮结束** |
| `_session/steering` | `{sessionId, prompt, _meta:{steering:{idleBehavior:"promptRequired"}}}` | 运行中返回 `injected`，空闲返回 `promptRequired`；claude-agent-acp 扩展 |
| `session/cancel` | `{sessionId}`(notification) | — |
| `session/set_mode` | `{sessionId, mode}` | — |

实测:`initialize` 与 `session/new` 的响应直接随 POST body 返回;`session/prompt`
的响应**可能走 POST body 也可能走 SSE**,两者都要接,按 JSON-RPC id 去重。

## 事件(agent → client,SSE 上逐条 JSON-RPC envelope)

- **通知** `session/update`,payload 在 `params.update.sessionUpdate`:
  - `agent_message_chunk` / `agent_thought_chunk` / `user_message_chunk`:
    `{content: {type:"text", text}, messageId}`
  - `tool_call`:`{toolCallId, title, kind:"execute", status:"pending", rawInput,
    _meta.claudeCode.toolName}`
  - `tool_call_update`:`{toolCallId, status:"completed", content:[{content:{text}}],
    rawOutput}`
  - `plan`、`usage_update`、`session_info_update`、`available_commands_update`、
    `config_option_update`
- **请求**(必须应答,否则 adapter 挂起):
  - `session/request_permission` → 回
    `{outcome:{outcome:"selected", optionId}}`(选第一个 `allow_once`/`allow_always`
    选项即全自动放行;Guildhall 的策略:一律放行)。
  - 其余(`fs/read_text_file` 等)→ 回 JSON-RPC error `-32601`。
- **响应**:带 `id` + `result`/`error`,用于配对 pending 请求。

## 实测行为(影响实现的三个事实)

1. **工具调用未经 `session/request_permission` 直接执行**(claude adapter 在
   mode=default 下自动放行 Bash)。无人值守成立,但仍要实现 permission 应答兜底。
2. **SSE `id` 是连接级单调整数**(1,2,3…),`Last-Event-ID` 续读有效。
   连接重建后 id 从头计数——重启续读时必须校验连续性,对不上就停机报错,
   不许把新连接的事件混写进旧 jsonl。
3. **claude 凭据走宿主 CLI 登录态**(`credentialsAvailable: true`),
   不需要 `authenticate`。`--no-token` 只关 server 层鉴权。
4. claude-agent-acp 0.4.2 所带 adapter 支持 `_session/steering`：运行中消息以
   priority `now` 注入当前 turn，而不是排队成下一轮；空闲时由 Host 改走普通
   `session/prompt`，这样每一轮仍有可追踪的开始与结束。

## 对规格书的两处勘误(实现按此,其余照旧)

- `[agent] name = "claude"`(规格书 config 样例写的是 `claude-code`,0.4.2 无此 id)。
- §2.1 的 `streamEvents(name, {offset})` 对应物 = `GET /v1/acp/{server_id}` 的
  SSE `id:`/`Last-Event-ID` 游标。语义不变:权威游标,重启续读,不做二次缓存。

## M0 验收记录

`curl` 三连(initialize → session/new → session/prompt)通过;SSE 流 88 个事件,
含 `agent_message_chunk` / `tool_call` / `tool_call_update` / `usage_update`;
Inspector `http://127.0.0.1:2468/ui/` 可见该会话。
