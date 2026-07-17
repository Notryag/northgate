# Northgate

Northgate 是一个可自托管、供应商中立的开源 AI 网关，提供可观测性、流量治理、限流、成本控制和模型路由能力。应用通过 Northgate 发送模型请求，无需分别实现供应商凭证管理和治理逻辑。

## 核心能力

- 透明转发流式和非流式 AI 请求，保持 SSE 顺序且不缓冲完整响应。
- 使用应用密钥认证调用方，不向业务应用暴露上游供应商凭证。
- 记录请求、tokens、成本、延迟、供应商请求 ID 和错误结果。
- 执行请求量、token、并发以及日/月消费限额。
- 使用版本化模型价格核算成本，并按请求 ID 幂等结算。
- 提供用量汇总、时序分析 API 和 React 运维控制台。
- 为多供应商路由、重试、fallback 和健康感知提供独立数据面。

Northgate 不是账号池、订阅转 API 服务、模型运行时或 Agent 框架。

## 项目状态

当前状态：M1-M3 已完成，正在推进现有系统接入、渐进切流和回滚闭环。

已经实现 OpenAI-compatible chat-completions 透明代理、PostgreSQL 持久化配置、加密供应商凭证、Redis 原子配额、消费预算、多供应商可靠性路由、用量分析 API 和 React 运维控制台。详细进度见 [项目路线图](docs/roadmap.md)。

## 架构概览

```text
AI 应用
   |
   | 应用密钥 + 归因元数据
   v
Northgate 数据面
   |-- 认证与策略准入
   |-- 路由与供应商凭证
   |-- 流式转发
   |-- usage / 成本结算
   |
   v
模型供应商

PostgreSQL：配置、加密凭证、价格版本、请求账本
Redis：请求窗口、并发 lease、token 与消费预留
```

## 开发环境

需要 Python 3.11、[uv](https://docs.astral.sh/uv/)、Node.js 22、Docker 和 Docker Compose。

安装依赖并运行检查：

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q

cd apps/console
npm ci
npm run check
npm run build
```

启动独立的 PostgreSQL 和 Redis：

```sh
docker compose up -d postgres redis
```

PostgreSQL 暴露在 `5433` 端口，Redis 暴露在 `6380` 端口，避免与共享平台服务冲突。

复制配置并执行迁移：

```sh
cp .env.example .env
uv run alembic upgrade head
```

启动服务：

```sh
uv run northgate
```

健康检查地址：

```text
GET http://127.0.0.1:8080/health/live
GET http://127.0.0.1:8080/health/ready
```

也可以构建并启动完整环境：

```sh
docker compose up --build
```

## 通过控制 API 接入应用

数据库路由模式下，Operator API 可以完成新应用接入，不需要直接修改数据库。
先设置 `NORTHGATE_OPERATOR_KEY_SHA256`、`NORTHGATE_CREDENTIAL_ENCRYPTION_KEY`
并启动服务，然后使用原始 Operator Key 依次创建 organization、project、gateway、
application key、provider credential 和 route：

```text
POST /api/v1/organizations
POST /api/v1/projects
POST /api/v1/gateways
POST /api/v1/application-keys
POST /api/v1/provider-credentials
POST /api/v1/routes
PUT  /api/v1/policies/{gateway_id}
Authorization: Bearer <operator key>
```

应用密钥明文只在创建响应中返回一次；供应商 API key 经加密后写入 PostgreSQL，
创建、查询和轮换接口都不会回显。完整字段及生命周期接口见 [API 设计](docs/api-design.md)。

策略接口使用完整替换：请求速率、并发、每日 token、日/月预算和精确缓存 TTL
都必须提供，正整数表示启用，`null` 表示关闭。数据库路由会在后续请求中读取新策略。

## 通过环境变量配置首个网关

生成应用密钥摘要，并在 `.env` 中设置摘要和供应商凭证：

```sh
printf '%s' 'ng_live_example' | sha256sum
# 将摘要写入 NORTHGATE_APPLICATION_KEY_SHA256
# 设置 NORTHGATE_PROVIDER_API_KEY
# 不要将任何明文凭证提交到源码仓库
```

生成供应商凭证加密密钥：

```sh
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

将输出保存到 `NORTHGATE_CREDENTIAL_ENCRYPTION_KEY`，然后初始化数据库配置：

```sh
uv run alembic upgrade head
uv run northgate-bootstrap
```

`northgate-bootstrap` 对默认 organization、project、gateway、应用密钥、供应商凭证和 route 幂等。它从环境读取秘密，只存储应用密钥摘要，并在写入 PostgreSQL 前加密供应商凭证。

初始化成功后启用数据库路由和 usage 持久化：

```text
NORTHGATE_ROUTING_SOURCE=database
NORTHGATE_USAGE_PERSISTENCE_ENABLED=true
```

## 调用代理

OpenAI-compatible 客户端只需替换 base URL 和 API key：

```text
OPENAI_BASE_URL=http://127.0.0.1:8080/v1/gateways/default/openai
OPENAI_API_KEY=ng_live_example
```

客户端继续追加 `/chat/completions`。工具定义、tool calls 和工具结果会原样通过，不会被 Northgate 重写。

配置模式可以设置一个 OpenAI-compatible fallback：

```text
NORTHGATE_PROVIDER_MAX_RETRIES=1
NORTHGATE_PROVIDER_RETRY_STATUS_CODES=429,500,502,503,504
NORTHGATE_PROVIDER_RETRY_BACKOFF_MS=100
NORTHGATE_FALLBACK_PROVIDER_NAME=backup
NORTHGATE_FALLBACK_PROVIDER_BASE_URL=https://backup.example.com/v1
NORTHGATE_FALLBACK_PROVIDER_API_KEY=<backup key>
NORTHGATE_FALLBACK_PROVIDER_MAX_RETRIES=0
NORTHGATE_ROUTE_HEALTH_ENABLED=true
NORTHGATE_ROUTE_HEALTH_FAILURE_THRESHOLD=3
NORTHGATE_ROUTE_HEALTH_RECOVERY_SECONDS=30
NORTHGATE_ROUTE_HEALTH_FAILURE_STATUS_CODES=500,502,503,504
```

Provider adapter 决定上游 URL 和认证方式。默认 `openai_compatible` 使用 `{base_url}/chat/completions` 与 Bearer API key。Azure OpenAI 使用 resource 根地址、deployment 路径和 `api-key`：

```text
NORTHGATE_PROVIDER_ADAPTER=azure_openai
NORTHGATE_PROVIDER_BASE_URL=https://<resource>.openai.azure.com
NORTHGATE_PROVIDER_API_KEY=<azure key>
NORTHGATE_PROVIDER_API_VERSION=<api-version>
```

Azure adapter 将 OpenAI 请求体的 `model` 解释为 deployment 名称，并请求 `/openai/deployments/{deployment}/chat/completions`。数据库模式在 `provider_credentials.adapter` 和 `adapter_config` 中保存相同的非秘密配置；API key 仍加密存储。两种 adapter 都返回 OpenAI-compatible 响应，因此 streaming、tool calls、usage、重试、熔断和缓存共享同一主流程。

只有在响应头尚未发送给客户端时才允许 retry 或 fallback。流开始后不会跨供应商拼接响应。数据库路由按 `priority` 依次尝试所有启用 route，并将每次供应商调用独立写入 attempt 账本。

启用健康感知后，连接错误、超时和配置的状态码会累计 route 失败次数。达到阈值后该 route 在恢复窗口内被跳过；窗口结束时仅放行一个半开探测请求，成功后恢复流量，失败则重新进入恢复窗口。健康状态存储在 Redis，Redis 不可用时网关拒绝继续执行受健康策略保护的 route。

数据库 route 还支持 `weight` 和 `match_metadata`。`priority` 越小越先尝试；同一优先级中，更具体的 metadata 规则优先，再按正整数权重选择首个 route。选择使用 Northgate request ID 作为稳定输入，通用规则和未被选中的同级 route 仍保留在 fallback 队列中。例如 `match_metadata={"environment":"production"}` 只匹配带有相同已授权 metadata 的请求。

精确请求缓存是 gateway 级 opt-in 功能：

```text
NORTHGATE_EXACT_CACHE_TTL_SECONDS=300
NORTHGATE_CACHE_MAX_ENTRY_BYTES=1048576
```

缓存键由 gateway、原始请求体、已验证 metadata 和 route 配置共同计算，不在 Redis key 中存储请求内容。只有完整的 `2xx` 响应才会写入；超过大小限制、客户端中断或供应商错误均不缓存。命中响应带有 `Northgate-Cache: HIT` 和 `Northgate-Attempts: 0`，usage 账本记录 `cache_hit`，token 与成本为零。Redis 缓存不可用时请求会旁路到供应商。

直接调用示例：

```http
POST /v1/gateways/default/openai/chat/completions
Authorization: Bearer ng_live_example
Content-Type: application/json
Northgate-Metadata: {"tenant_id":"tenant-1","run_id":"run-1"}
```

`Northgate-Metadata` 只接受应用密钥允许的归因维度，不会作为授权依据，也不会透传给供应商。

## 限流与消费预算

bootstrap 可以持久化以下网关策略：

```text
NORTHGATE_REQUEST_LIMIT_PER_MINUTE=60
NORTHGATE_CONCURRENCY_LIMIT=10
NORTHGATE_TOKEN_LIMIT_PER_DAY=1000000
NORTHGATE_DAILY_SPEND_LIMIT_MICROUSD=5000000
NORTHGATE_MONTHLY_SPEND_LIMIT_MICROUSD=100000000
```

Northgate 在转发前预留 token 和消费容量。token 预估值为请求 UTF-8 字节数除以三，再加上 `max_completion_tokens` / `max_tokens`；请求未提供输出上限时使用 4096。响应完成后，预留会根据供应商报告的实际 usage 按请求 ID 精确结算一次。供应商结果不明确且缺少 usage 时，保留保守预留。

消费限额需要版本化模型价格。初始价格可以通过以下配置写入：

```text
NORTHGATE_PRICE_PROVIDER=openai
NORTHGATE_PRICE_MODEL=gpt-4o-mini
NORTHGATE_INPUT_PRICE_MICROUSD_PER_MILLION=150000
NORTHGATE_OUTPUT_PRICE_MICROUSD_PER_MILLION=600000
```

价格单位为每百万 token 的 micro-USD，`1 USD = 1,000,000 micro-USD`。

## 分析与运维控制台

分析接口使用独立 operator key，应用密钥不能读取跨项目 usage 和成本数据：

```text
GET /api/v1/usage/summary
GET /api/v1/usage/timeseries?interval=hour
GET /api/v1/usage/routes
GET /api/v1/usage/requests/{request_id}/attempts
Authorization: Bearer <operator key>
```

React 运维控制台地址：

```text
http://127.0.0.1:8080/console
```

控制台的 Provider traffic 表按实际上游 attempt 统计 route 占比、成功、失败、
tokens、成本和延迟。retry 与 fallback 会分别计入对应 route，不会被最终成功请求掩盖。

前端开发模式会将 `/api` 请求代理到本地 Northgate：

```sh
cd apps/console
npm ci
npm run dev
```

## Prometheus 指标

Prometheus endpoint 默认关闭。启用并配置独立抓取密钥摘要：

```text
NORTHGATE_METRICS_ENABLED=true
# printf '%s' 'metrics-secret' | sha256sum
NORTHGATE_METRICS_KEY_SHA256=<sha256>
```

抓取请求使用原始密钥：

```http
GET /metrics
Authorization: Bearer metrics-secret
```

当前指标覆盖 HTTP 请求量、在途请求与端到端延迟，稳定 gateway error，provider attempt/延迟/token/成本，精确缓存命中与写入，以及熔断 route 跳过。HTTP route 使用 FastAPI 模板，指标不包含 request ID、gateway slug、metadata 值、模型输入、响应内容或凭证。未设置 `NORTHGATE_METRICS_KEY_SHA256` 时 endpoint 不鉴权，只应暴露在受控私有网络。

## OpenTelemetry traces

Tracing 默认关闭，通过 OTLP/HTTP exporter 发送到 collector：

```text
NORTHGATE_TRACING_ENABLED=true
NORTHGATE_TRACE_SERVICE_NAME=northgate
NORTHGATE_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces
NORTHGATE_TRACE_SAMPLE_RATIO=1.0
NORTHGATE_TRACE_EXPORT_TIMEOUT_SECONDS=10.0
```

Exporter 认证头使用标准 `OTEL_EXPORTER_OTLP_HEADERS` 环境变量。Northgate 为每个 HTTP 请求创建覆盖完整响应流的 server span，接受并向供应商传播 W3C `traceparent`，并记录 gateway error、cache、provider attempt 和熔断跳过事件。Span 使用路由模板，不记录 prompt、response、metadata、API key 或原始 Authorization；`northgate.request_id` 用于与结构化日志和 usage 账本关联。

## 备份、恢复与升级

Compose 环境提供可执行运维入口：

```sh
./scripts/compose-backup.sh
NORTHGATE_RESTORE_CONFIRM=northgate ./scripts/compose-restore.sh <backup.dump>
./scripts/compose-upgrade.sh
```

恢复会删除并重建目标数据库，校验数据库名和 SHA-256 后才执行，并在完成后保持 Northgate 停止。升级使用维护窗口，先创建验证过的备份，再停止应用、运行单一 Alembic head 并等待 readiness。生产回滚依赖恢复升级前备份和匹配的旧版本，不使用 Alembic downgrade。

完整流程和秘密材料要求见[配置与迁移政策](docs/operations/configuration-and-migrations.md)、[备份与恢复](docs/operations/backup-and-restore.md)和[升级与回滚](docs/operations/upgrades.md)。

## 文档

从 [文档索引](docs/README.md) 开始，根据任务只读取相关设计页：

- [产品范围](docs/product-scope.md)
- [系统架构](docs/architecture.md)
- [API 设计](docs/api-design.md)
- [集成边界](docs/integration-boundaries.md)
- [项目路线图](docs/roadmap.md)
- [开发与验证流程](docs/development.md)
- [架构决策](docs/decisions/README.md)
