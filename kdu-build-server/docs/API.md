# HTTP API 文档

基础路径：`http://<host>:<port>`，默认 `http://127.0.0.1:8600`。所有响应为 UTF-8 JSON（文件下载除外）。

## 1. 认证

除 `GET /healthz` 外，所有接口都要求认证。服务端配置了 `server.api_token` 时支持两种请求头：

```http
Authorization: Bearer <token>
```

或：

```http
X-API-Key: <token>
```

Token 为空时服务不做鉴权。认证失败返回 `401`：

```json
{
  "error": {
    "code": "unauthorized",
    "message": "A valid bearer token is required"
  }
}
```

## 2. 通用约定

- 请求体只接受 `Content-Type: application/json`，最大 `server.max_request_bytes` 字节。
- 必须提供 `Content-Length`，不支持 chunked 请求体。
- 所有接口都会返回 `Content-Length` 并关闭连接。
- 错误统一为：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "人类可读的错误信息",
    "details": {}
  }
}
```

`details` 可选。

## 3. 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查，无需认证。 |
| GET | `/api/v1/info` | 服务版本、当前源码版本、平台配置路径。 |
| GET | `/api/v1/platforms` | 平台列表与默认平台。 |
| GET | `/api/v1/source` | 当前源码版本信息。 |
| POST | `/api/v1/source/sync` | 立即检查并同步最新源码。 |
| GET | `/api/v1/schema/build-request` | 构建请求的 JSON Schema。 |
| POST | `/api/v1/builds` | 提交构建任务。 |
| GET | `/api/v1/builds` | 查询任务列表。 |
| GET | `/api/v1/builds/{id}` | 查询单个任务。 |
| GET | `/api/v1/builds/{id}/log` | 查看 MSBuild 日志。 |
| GET | `/api/v1/builds/{id}/artifact` | 下载产物 zip。 |
| GET | `/api/v1/builds/{id}/artifact?file=kdu.exe` | 下载单个产物文件。 |

## 4. 构建请求

### 4.1 请求字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `platform` | string | 否 | 平台配置名；省略时使用 `platforms.json` 的 `default_platform`。 |
| `configuration` | string | 否 | 覆盖 MSBuild `Configuration`。 |
| `msbuild_platform` | string | 否 | 覆盖 MSBuild `Platform`。 |
| `platform_toolset` | string | 否 | 覆盖 `PlatformToolset`。 |
| `windows_sdk_version` | string | 否 | 覆盖 `WindowsTargetPlatformVersion`。 |
| `extra_properties` | object | 否 | 额外 MSBuild 属性，键必须匹配 `[A-Za-z_][A-Za-z0-9_]*`，值必须为字符串。数量上限为 `build.max_extra_properties`。 |
| `callback_url` | string | 否 | 绝对 `http/https` URL，任务结束时接收回调。 |

未知字段会被拒绝，避免拼写错误被静默忽略。

### 4.2 提交构建

```http
POST /api/v1/builds
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "platform": "windows-x64",
  "configuration": "Release",
  "extra_properties": {
    "RunCodeAnalysis": "false"
  },
  "callback_url": "https://ci.example.com/kdu/callback"
}
```

响应 `202 Accepted`，`Location` 头指向任务详情：

```json
{
  "id": "9899541ac65f406c8fab36f50d6cc66f",
  "status": "queued",
  "created_at": "2026-11-19T08:48:17+00:00",
  "started_at": null,
  "finished_at": null,
  "platform": "windows-x64",
  "source_sha": null,
  "request": {
    "platform": "windows-x64",
    "overrides": {
      "configuration": "Release",
      "extra_properties": {"RunCodeAnalysis": "false"}
    },
    "callback_url": "https://ci.example.com/kdu/callback"
  },
  "result": null,
  "error": null,
  "links": {
    "self": "/api/v1/builds/9899541ac65f406c8fab36f50d6cc66f"
  }
}
```

校验失败示例（`422`）：

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Unknown platform 'arm64'. Available platforms: windows-x64, windows-x64-debug"
  }
}
```

### 4.3 任务状态

```text
queued ──> running ──> succeeded
                    └> failed
```

| 状态 | 说明 |
| --- | --- |
| `queued` | 已入队，等待 worker。 |
| `running` | 正在检查源码/复制源码/编译。 |
| `succeeded` | 构建成功，`result` 和产物可用。 |
| `failed` | 构建失败、超时、源码下载失败或服务重启中断；`error` 有原因。 |

### 4.4 成功结果

任务成功后的 `result` 字段：

```json
{
  "platform": "windows-x64",
  "configuration": "Release",
  "msbuild_platform": "x64",
  "platform_toolset": "v145",
  "source_ref": "master",
  "source_sha": "ead2f6466e2eb1101c474929c7f970a929442aae",
  "duration_seconds": 42.7,
  "artifacts": [
    {"name": "kdu.exe", "size": 1048576},
    {"name": "drv64.dll", "size": 524288}
  ],
  "artifact_file": "kdu-windows-x64-ead2f646-9899541a.zip",
  "log_file": "build.log"
}
```

`links` 中会额外出现：

```json
{
  "self": "/api/v1/builds/<id>",
  "log": "/api/v1/builds/<id>/log",
  "artifact": "/api/v1/builds/<id>/artifact"
}
```

配置了 `server.public_url` 时，链接会自动加上该前缀。

## 5. 其他接口

### 5.1 健康检查

```http
GET /healthz
```

```json
{"status": "ok", "time": "2026-11-19T08:48:12+00:00"}
```

### 5.2 服务信息

```http
GET /api/v1/info
```

返回服务名、版本、当前源码版本和平台配置文件路径。

### 5.3 平台列表

```http
GET /api/v1/platforms
```

```json
{
  "default_platform": "windows-x64",
  "platforms": [
    {
      "name": "windows-x64",
      "description": "Windows x64, Release, MSVC v145 (Visual Studio 2026)",
      "msbuild_platform": "x64",
      "configuration": "Release",
      "platform_toolset": "v145",
      "required_artifacts": ["kdu.exe"]
    }
  ]
}
```

若配置了 provider，provider 还可以接受这里未列出的平台名。

### 5.4 源码状态与同步

```http
GET /api/v1/source
```

```json
{
  "repo": "hfiref0x/KDU",
  "ref": "master",
  "current": {
    "repo": "hfiref0x/KDU",
    "ref": "master",
    "sha": "ead2f6466e2eb1101c474929c7f970a929442aae",
    "downloaded_at": "2026-11-19T08:47:53+00:00",
    "archive_sha256": "8cb5a0cc...",
    "etag": null
  }
}
```

立即检查更新（首次运行且尚未下载源码时也会下载）：

```http
POST /api/v1/source/sync
POST /api/v1/source/sync?force=1
```

```json
{
  "changed": true,
  "source": {
    "repo": "hfiref0x/KDU",
    "ref": "master",
    "sha": "ead2f6466e2eb1101c474929c7f970a929442aae",
    "downloaded_at": "2026-11-19T08:47:53+00:00",
    "archive_sha256": "8cb5a0cc...",
    "etag": null
  }
}
```

该接口是同步执行的，源码较大或网络较慢时可能耗时数秒到数十秒。GitHub 无法访问时返回 `502 source_unavailable`。

### 5.5 查询任务

```http
GET /api/v1/builds?limit=50&status=succeeded
```

| 查询参数 | 说明 |
| --- | --- |
| `limit` | 1–200，默认 50。 |
| `status` | `queued` / `running` / `succeeded` / `failed`。 |

### 5.6 日志

```http
GET /api/v1/builds/{id}/log
Content-Type: text/plain; charset=utf-8
```

任务尚未开始或日志文件不存在时返回 `404`。

### 5.7 产物下载

```http
GET /api/v1/builds/{id}/artifact
```

返回 `application/zip`，包含本次构建收集到的全部产物（默认 `kdu.exe`、`drv64.dll`）。

只下载单个文件：

```http
GET /api/v1/builds/{id}/artifact?file=kdu.exe
```

返回 `application/octet-stream`，`Content-Disposition` 为附件。文件名必须出现在任务的 `result.artifacts` 中，否则返回 `422 invalid_artifact`。

### 5.8 请求 JSON Schema

```http
GET /api/v1/schema/build-request
```

返回可直接用于客户端校验的 JSON Schema。`extra_properties.maxProperties` 来自服务配置。

## 6. 完成回调

请求中提供 `callback_url` 且 `callbacks.enabled=true` 时，任务进入终态后服务会向该地址发送 `POST`：

```http
POST /your/callback
Content-Type: application/json
X-KDU-Signature: sha256=<hex>
```

请求体为任务对象，额外包含 `event` 字段：

```json
{
  "event": "build.finished",
  "id": "9899541ac65f406c8fab36f50d6cc66f",
  "status": "succeeded",
  "platform": "windows-x64",
  "source_sha": "ead2f6466e2eb1101c474929c7f970a929442aae",
  "created_at": "2026-11-19T08:48:17+00:00",
  "started_at": "2026-11-19T08:48:17+00:00",
  "finished_at": "2026-11-19T08:48:20+00:00",
  "request": {"platform": "windows-x64", "overrides": {}, "callback_url": "..."},
  "result": {"artifacts": [{"name": "kdu.exe", "size": 1048576}], "...": "..."},
  "error": null,
  "links": {"self": "/api/v1/builds/<id>", "artifact": "/api/v1/builds/<id>/artifact"}
}
```

签名规则：`X-KDU-Signature = "sha256=" + HMAC-SHA256(raw_body_bytes, server.api_token)`，与 GitHub Webhook 的风格一致。未配置 `server.api_token` 时不会发送该请求头。校验示例：

```python
import hashlib
import hmac

def verify(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

回调失败不会改变任务状态，仅在服务日志中记录 warning。服务最多等待 `callbacks.timeout_seconds` 秒，不重试；需要可靠通知时，建议调用方轮询任务接口或自行实现重试。

## 7. 错误码

| HTTP | code | 说明 |
| --- | --- | --- |
| 400 | `invalid_json` | 请求体不是合法 JSON。 |
| 400 | `invalid_request` | 请求体为空或 `Content-Length` 非法。 |
| 401 | `unauthorized` | Token 缺失或不匹配。 |
| 404 | `not_found` | 路由不存在。 |
| 404 | `job_not_found` | 任务不存在。 |
| 404 | `log_not_found` | 日志尚不可用。 |
| 404 | `artifact_not_found` | 产物尚不可用。 |
| 405 | `method_not_allowed` | HTTP 方法不支持。 |
| 411 | `length_required` | 缺少 `Content-Length`。 |
| 413 | `payload_too_large` | 请求体超过上限。 |
| 415 | `unsupported_media_type` | 请求体不是 `application/json`。 |
| 422 | `invalid_request` | 字段类型、平台名或属性名不合法。 |
| 422 | `invalid_query` | 查询参数不合法。 |
| 422 | `invalid_configuration` | 平台配置或 provider 返回结果不合法。 |
| 422 | `invalid_artifact` | 请求的产物名不存在。 |
| 502 | `source_unavailable` | GitHub 源码检查或下载失败（仅 `source/sync`）。 |
| 500 | `internal_error` | 未预期的服务端错误。 |

## 8. 调用示例

### curl

```bash
TOKEN=change-me
BASE=http://127.0.0.1:8600

# 提交
JOB=$(curl -sS -X POST "$BASE/api/v1/builds" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"platform":"windows-x64"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 轮询
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/builds/$JOB"

# 下载 kdu.exe
curl -sS -H "Authorization: Bearer $TOKEN" -o kdu.exe \
  "$BASE/api/v1/builds/$JOB/artifact?file=kdu.exe"
```

### Python

见 `examples/submit_build.py`，仅使用标准库，支持提交、等待、下载产物：

```bash
python examples/submit_build.py --token change-me --platform windows-x64 \
  --wait --download kdu.zip
```
