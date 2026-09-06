# Guildhall

单人用的委托流水线:把「我想改个东西」这句模糊的话,变成一份可验收的委托单,
交给编码 Agent 去做,做完先过一道自动验收,最后才递到你面前。

比喻是冒险家公会:你去公会前台(**receptionist**)说想干点什么,前台问清楚、写成委托书(**quest.md**)贴上告示板;
冒险者(**adventurer**)接单去干;回来先由鉴定人(**appraiser**)验货——
**鉴定人不听冒险者吹牛,只看货**(它看不到 adventurer 的任何输出,这是自动验收唯一的价值来源);
过了才轮到你最后过目。

本轮可靠性修订与明确的规格调整见 [docs/reliability.md](docs/reliability.md)。接受操作现在会预览并快进合并已验收代码，历史单据需先重验生成快照。

## 三条设计底线

- 委托书必须有可执行的验收步骤,且至少一条负向测试。
- 鉴定人不读实现者的自述(全新 session、只有 quest.md / git diff / 自己跑命令的输出)。
- 自动验收通过 ≠ 完成,人 review 是流水线的最后一段。

## 结构

```
backend/    guildhall-server:Python 3.12 + FastAPI(uv 管理)
frontend/   guildhall-web:Vite + React + TypeScript + TailwindCSS(pnpm 管理)
docs/       m0-sandbox-agent-api.md —— sandbox-agent 真实 HTTP 契约考古结论
scripts/    e2e_check.sh —— 验收脚本
```

磁盘状态在 `~/.guildhall/`:项目记忆(`lore.md`)、每张 quest 的
`quest.md` / `state.json` / `events/*.jsonl` / `appraisal.json`、以及 worktree。

## 运行

前置:`uv`、`pnpm`、`claude` CLI(已登录)、`git`。

一键启动前后端（自动同步依赖，不打开浏览器）：

```bash
./scripts/dev.sh
# 前端端口被占用时可指定其他端口
./scripts/dev.sh --port 5175
```

后端端口读取 `~/.guildhall/config.toml` 的 `[server].port`（默认 8420），前端代理自动跟随；默认前端端口 5173。按 Ctrl+C 同时停止两端；任一服务退出会清理另一端。只清理脚本自己创建的进程组，不停止其他已运行的服务。模型设置保存后，重启此脚本即可生效。

首次使用仍需先安装下述 sandbox-agent 并登录 Claude。也可以分别启动：

```bash
# 1. 安装 sandbox-agent(M0 考古版:0.4.x)
curl -fsSL https://releases.rivet.dev/sandbox-agent/0.4.x/install.sh | sh
sandbox-agent install-agent --all        # 预装 agent 二进制

# 2. 起后端(会自动拉起 sandbox-agent server --no-token --host 127.0.0.1 --port 2468)
cd backend && uv run python -m guildhall.main

# 3. 起前端
cd frontend && pnpm install && pnpm dev   # http://127.0.0.1:5173
```

浏览器开 `http://127.0.0.1:5173`:注册项目 → 进前台 → 和 receptionist 对话 →
生成需求单(可手改)→ 张贴 → 派单 → review 页过目 → 接受/放弃。全程不开终端。

## 测试与验收

```bash
cd backend
uv run pytest tests/ -q        # 状态机、契约闸门、恢复互斥、SSE 续读及真实 Git 交付回归
../scripts/e2e_check.sh        # 上面全部 + 真模型 M5 负向(手工构造偷改测试的 diff,真 appraiser 必须报 touched_tests=true)
```

M5 负向二(appraiser 弄脏 worktree → 结论整份作废)是确定性闸门,由
`tests/test_api_flow.py::test_appraiser_pollution_invalidates` 覆盖。

## 实现要点(与规格书的对应)

- **状态机是封闭转移表**,表外转移 409;进入 `posted` 前闸门校验 quest.md 含非空验收、负向条目及范围白名单;
  `appraised` 不自动进 `settled`。
- **三份角色 prompt**(`backend/src/guildhall/prompts.py`),保留角色分工与报告隔离；修订项和新增输入见 docs/reliability.md。
- **worktree 隔离**:进入 `in_progress` 才建 worktree 和分支 `guildhall/<quest-id>`,
  diff 一律相对创建时刻的 HEAD(记在 `state.json.base_commit`)。
- **「进出一致」校验**(§6.2):`git diff HEAD` 的 sha256 对比,不是 `git status --porcelain`。
  receptionist 弄脏工作区只记 `error` 流程继续;appraiser 弄脏则 `appraisal.json` 标
  `invalidated: true` 并转 `disputed`,前端不展示绿勾。
- **事件流**:sandbox-agent 的 ACP SSE `id:` 是权威游标;guildhall 旁路写
  `events/<role>.jsonl` 并把上游游标快照进 `state.json.offsets`;
  Web 端使用 JSONL 行号，Last-Event-ID 从下一条续读；后端仅跳过订阅与重放的重叠部分。
- **前台长轮次不阻塞 HTTP**:新建后只等 ACP session 就绪就进入对话页;角色契约与
  用户开场白合在同一首轮。运行态由 thought/message/tool/usage 事件实时推导,
  工作中追加消息走 claude-agent-acp 的 `_session/steering` 注入当前 turn。
- **对话可审计**:Agent 文本按 Markdown 渲染;工具前后的文本保持为独立段落;
  Terminal/ReadFile 等工具可展开查看完整参数、命令与输出。
- **appraisal 解析失败**:转 `disputed`、原始输出进 `error`,不重试(§6.4)。
- **轮末信号丢失的兜底**(真实环境实测:模型后端偶发静默挂起、轮末响应丢失):
  prompt 带总超时看门狗;生成需求单有 30 秒流空闲兜底,冒险者/鉴定人有 2 分钟兜底
  (已有产出后静默到阈值会 cancel 收口,保留已收到的结果);
  Review 页常驻「推进到验收」「重跑验收」两个恢复按钮,对应 `in_progress→appraising`
  转移与 `POST /api/quests/{id}/reappraise` 恢复入口,卡住时无需进终端。

## 对规格书的两处勘误(M0 考古结论,详见 docs/m0-sandbox-agent-api.md)

1. agent id 是 `claude`,不是规格书猜的 `claude-code`;
2. `streamEvents(name, {offset})` 的真实对应物是 `GET /v1/acp/{server_id}` 的 SSE
   `id:`/`Last-Event-ID` 游标,语义不变。

## 已知限制(demo 优先)

- 单用户、单机、无鉴权、同一时刻只允许一张单 `in_progress`。
- sandbox-agent 的 session 是内存态:guildhall-server 重启后,同一 server_id 会
  复用旧 adapter 会话(含排队中的旧 prompt),对话历史可能错乱——demo 阶段的
  处理办法是放弃该单重开一张;重启不影响已张贴单据的 posted→in_progress→appraising 流程。
- 模型后端偶发长轮次静默挂起(轮末响应丢失):已用看门狗 + 流空闲兜底 + 恢复按钮
  三层缓解,但如果挂起发生在「输出完成之前」,兜底会以解析失败进 disputed 收场,交人工。
- quest id 的 slug 取自开场白的前几个 ASCII 词元(中文开场白会得到 `draft`),
  规格书「slug 由 receptionist 给出」在实现里让位给了 id 与目录的一致性。
- worktree 不自动清理,留给你看 diff。

## 像素公会与设置

首页现在是可交互的像素酒馆：点击前台、委托板、冒险者、鉴定台和档案柜即可进入对应操作；窄屏可使用场景下方的设施快捷入口。设置页提供外观切换、减少动画，以及三角色的模型/API 配置。模型配置保存后需重启后端生效，已有密钥不会回显或因留空被清除。

皮肤配置、素材制作约定及设置 API 说明见 [docs/tavern-frontend.md](docs/tavern-frontend.md)。
