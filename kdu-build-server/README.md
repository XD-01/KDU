# KDU build service

一个可长期运行在服务器上的 KDU 自动构建服务：

- 自动从 GitHub 获取 KDU 最新源码并保存到本地，下一次构建前检查更新；有更新时下载新版本并删除旧版本。
- 通过 HTTP 端口接收其他应用传入的平台/编译配置，校验后调用 MSBuild 编译。
- 产出 `kdu.exe`（以及 `drv64.dll` 等产物），打包为 zip 供下载，支持日志查看和完成回调。
- 任务持久化到 SQLite，服务重启后不会丢失排队任务，运行中的任务会被标记为失败。
- 仅依赖 Python 标准库。

> 构建目标是 Windows PE 可执行文件，服务必须部署在安装了 Visual Studio（含 C++ 工作负载）的 Windows 主机上。

## 目录结构

```text
kdu-build-server/
├─ kdu_build_server/        服务源码
│  ├─ __main__.py           CLI 入口
│  ├─ settings.py           服务配置加载/校验
│  ├─ source.py             GitHub 源码下载、更新检查、原子替换
│  ├─ platform_config.py    平台配置解析与 provider 接口
│  ├─ build_request.py      构建请求校验
│  ├─ builder.py            MSBuild 调用与产物收集
│  ├─ jobs.py               SQLite 任务持久化
│  ├─ service.py            队列、worker、回调、任务生命周期
│  └─ api.py                HTTP API
├─ config/
│  ├─ service.example.json  服务配置示例
│  └─ platforms.json        目标平台配置
├─ examples/
│  ├─ env_provider.py       平台配置 provider 示例
│  └─ submit_build.py       调用服务的示例客户端
└─ docs/
   ├─ CONFIG_INTERFACE.md   平台配置 / provider 接口文档
   └─ API.md                HTTP API 文档
```

## 环境要求

- Windows 10/11 或 Windows Server，安装 Visual Studio 2019 或更高版本，包含 C++ 工作负载和 MASM。
- KDU 工程固定使用 `PlatformToolset=v145`（Visual Studio 2026）。如果服务器上的工具集不同，在 `config/platforms.json` 中修改 `platform_toolset`，或在请求中传 `platform_toolset`。
- Python 3.9 及以上，无需安装第三方包。
- 服务进程需要能访问 `github.com`、`api.github.com`、`codeload.github.com`。如使用代理，直接设置 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量即可（`urllib` 会自动使用）。
- MSBuild 的查找顺序：`build.msbuild` 配置 → `MSBUILD_EXE_PATH` / `MSBUILD` 环境变量 → `vswhere` → `PATH`。

## 快速开始

1. 复制配置文件并修改：

   ```bat
   cd kdu-build-server
   copy config\service.example.json config\service.json
   ```

   至少修改 `server.api_token`。默认监听 `127.0.0.1:8600`。

2. 校验配置：

   ```bat
   python -m kdu_build_server --config config\service.json check
   ```

3. 启动服务：

   ```bat
   python -m kdu_build_server --config config\service.json serve
   ```

   启动时会输出监听地址、MSBuild 路径，并在后台检查一次源码更新。

4. 提交一个构建：

   ```bat
   curl -X POST http://127.0.0.1:8600/api/v1/builds ^
     -H "Authorization: Bearer change-me" ^
     -H "Content-Type: application/json" ^
     -d "{\"platform\":\"windows-x64\",\"configuration\":\"Release\"}"
   ```

   或使用示例客户端：

   ```bat
   python examples\submit_build.py --token change-me --platform windows-x64 --wait --download kdu.zip
   ```

5. 下载产物：根据返回的 `links.artifact` 下载 zip（默认包含 `kdu.exe` 与 `drv64.dll`），也可以只取单个文件：

   ```text
   GET /api/v1/builds/<job-id>/artifact
   GET /api/v1/builds/<job-id>/artifact?file=kdu.exe
   ```

## 常用命令

```text
python -m kdu_build_server serve                 启动 HTTP 服务（默认命令）
python -m kdu_build_server sync-source           检查并下载最新源码后退出
python -m kdu_build_server sync-source --force   强制重新下载
python -m kdu_build_server check                 打印生效配置、平台列表、当前源码版本
```

全局参数：

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | 服务配置文件，默认 `config/service.json`，也可用 `KDU_SERVICE_CONFIG` 指定。 |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` / `ERROR`，默认 `INFO`。 |

## 服务配置

完整示例见 `config/service.example.json`。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `server.host` | `127.0.0.1` | 监听地址。对外提供服务时建议保持本地监听，由反向代理转发。 |
| `server.port` | `8600` | 监听端口。 |
| `server.api_token` | 空 | Bearer Token。为空时 API 不鉴权，仅建议在完全隔离的环境使用。 |
| `server.public_url` | 空 | 对外访问根地址，例如 `https://kdu.example.com`。设置后 API 与回调中的链接会带上该前缀。 |
| `server.max_request_bytes` | `65536` | 请求体大小上限。 |
| `storage.data_dir` | `data` | 数据根目录（源码、构建目录、数据库、日志）。 |
| `storage.keep_builds` | `20` | 保留的已结束任务数量，超出后删除最旧的任务及其构建目录。 |
| `source.repo` | `hfiref0x/KDU` | GitHub 仓库，格式 `owner/repo`。也可以指向自己的 fork。 |
| `source.ref` | `master` | 分支名；也可以填 40 位 commit SHA 固定版本。 |
| `source.github_token` | 空 | 可选 GitHub Token，用于提高 API 限额。也可用环境变量 `GITHUB_TOKEN`。 |
| `source.timeout_seconds` | `180` | 单次网络操作超时。 |
| `source.validate_files` | `Source/KDU.sln` 等 | 下载后必须存在的文件，用于校验源码完整性。 |
| `build.msbuild` | 空 | MSBuild.exe 绝对路径；为空时自动查找。 |
| `build.timeout_seconds` | `1800` | 单次编译超时，超时会终止整个进程树。 |
| `build.workers` | `1` | 并发 worker 数。每个 worker 独立复制源码并在独立目录编译。 |
| `build.disable_postbuild` | `false` | 是否禁用工程 PostBuildEvent。若服务器没有 `GenAsIo2Unlock.exe` 可设为 `true`（此时 AsIO2 provider 不可用）。 |
| `build.platforms_file` | `config/platforms.json` | 平台配置文件。 |
| `build.provider` | 空 | 可选平台配置 provider 模块路径，见 `docs/CONFIG_INTERFACE.md`。 |
| `build.max_extra_properties` | `32` | 单次请求允许的额外 MSBuild 属性数量上限。 |
| `callbacks.enabled` | `true` | 是否允许请求携带 `callback_url`。 |
| `callbacks.timeout_seconds` | `10` | 回调请求超时。 |

所有配置项都可以用环境变量覆盖（空值忽略）：

```text
KDU_SERVICE_HOST, KDU_SERVICE_PORT, KDU_API_TOKEN, KDU_PUBLIC_URL
KDU_DATA_DIR, KDU_KEEP_BUILDS
KDU_SOURCE_REPO, KDU_SOURCE_REF, GITHUB_TOKEN
KDU_MSBUILD, KDU_BUILD_TIMEOUT, KDU_BUILD_WORKERS, KDU_DISABLE_POSTBUILD
KDU_PLATFORMS_FILE, KDU_PROVIDER
```

相对路径（`data_dir`、`platforms_file`、`provider`、`msbuild`）相对于启动服务时的工作目录，因此建议始终在 `kdu-build-server` 目录下启动。

## 源码更新机制

每次构建前、服务启动时以及调用 `POST /api/v1/source/sync` 时都会检查更新：

1. 通过 GitHub API 获取 `source.ref` 当前的 commit SHA，使用 ETag 条件请求；API 限额耗尽（403）时自动回退到公开的 commit Atom 源，再回退到 `git ls-remote`。
2. SHA 未变化：直接复用当前源码，不产生下载。
3. SHA 变化或首次运行：下载该 SHA 的源码压缩包到临时文件，校验并解压到临时目录。
4. 校验 `source.validate_files` 中的文件是否存在。
5. 原子切换到 `data/source/releases/<sha>`，写入 `state.json`，然后删除其他版本和临时文件。
6. 构建 worker 在更新锁内把当前源码复制到 `data/builds/<job-id>/source` 后再编译，因此更新不会影响正在进行的构建。

磁盘上只保留一个源码版本。下载失败时旧版本不会被删除；已有缓存的情况下，构建会记录告警并继续使用旧版本，保证服务长期可用。

建议为生产环境配置 `source.github_token`，既提高限额，也避免频繁走回退链路。

## 数据目录

```text
data/
├─ source/
│  ├─ state.json                       当前源码版本、SHA、下载时间、ETag
│  └─ releases/<sha>/                  当前 KDU 源码
├─ builds/<job-id>/
│  ├─ request.json                     本次构建请求
│  ├─ source/                          本次构建使用的源码快照
│  ├─ build.log                        MSBuild 输出
│  ├─ artifacts/                       收集到的 exe/dll
│  └─ kdu-<platform>-<sha>-<job>.zip   下载包
├─ jobs.sqlite3                        任务持久化
├─ logs/service.log                    轮转日志（5 MB × 3）
```

## 部署到服务器长期运行

### 方式一：NSSM（推荐，Windows 服务）

```bat
nssm install KduBuildService "C:\Python311\python.exe" "-m kdu_build_server --config config\service.json serve"
nssm set KduBuildService AppDirectory "C:\kdu-build-server"
nssm set KduBuildService AppStdout "C:\kdu-build-server\data\logs\stdout.log"
nssm set KduBuildService AppStderr "C:\kdu-build-server\data\logs\stderr.log"
nssm set KduBuildService AppExit Default Restart
nssm set KduBuildService AppRestartDelay 5000
nssm start KduBuildService
```

服务以 LocalSystem 或专用账号运行时，需要确保该账号能访问 Visual Studio 安装目录，并且能找到 MSBuild。若自动查找失败，在配置中填写 `build.msbuild` 的绝对路径。

### 方式二：任务计划程序

创建“启动时运行”的任务，操作设置为：

```text
程序：C:\Python311\python.exe
参数：-m kdu_build_server --config config\service.json serve
起始于：C:\kdu-build-server
```

勾选“不管用户是否登录都要运行”，并设置失败后重新启动。

### 反向代理与 TLS

建议服务只监听 `127.0.0.1`，由 Nginx/IIS 暴露 HTTPS。Nginx 示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:8600;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 128k;
    proxy_read_timeout 3600s;
}
```

如果通过反向代理对外访问，请把 `server.public_url` 设置为外部地址，使 API 和回调中的链接可直接使用。

### systemd（仅限能运行 MSBuild 的 Windows 环境不适用）

KDU 的编译依赖 Windows MSBuild 和 Windows SDK，原生的 Linux systemd 主机无法完成编译。如果使用 Linux 作为控制面，应通过一个 Windows 构建节点执行本服务，Linux 侧只做调度和转发。

## 任务生命周期

```text
queued -> running -> succeeded
                  -> failed
```

- 提交时即校验请求字段、平台名和平台配置，非法请求直接返回 4xx，不进入队列。
- 服务启动时，数据库中 `running` 状态的任务会被标记为失败（错误信息为服务重启），`queued` 任务会重新入队。
- 构建超时或 MSBuild 返回非 0 都会记录 `build.log`，并通过任务详情和回调返回错误摘要。
- 任务成功后会生成 zip 包，保留最近 `storage.keep_builds` 个已结束任务。

## 安全建议

- 必须设置 `server.api_token`，并通过 HTTPS 暴露服务。
- 不要向不可信客户端开放服务：服务会执行 MSBuild 和工程内的 PostBuild 脚本。
- 如果允许不可信客户端提交任务，建议设置 `callbacks.enabled=false`，避免回调地址被用于探测内网。
- 请求只能选择平台和覆盖少量 MSBuild 属性，不能传入任意命令行参数，也不能指定 provider。
- 定期更新 KDU 源码版本（服务会自动检查），并关注 GitHub 上的安全变更。

## 故障排查

| 现象 | 排查方向 |
| --- | --- |
| `MSBuild not found` | 安装 VS C++ 工作负载；或从 Developer Command Prompt 启动；或配置 `build.msbuild`。 |
| `The build tools for v145 cannot be found` | 安装对应 VS 版本，或在平台配置/请求中把 `platform_toolset` 改为服务器上已有的版本（如 `v143`）。 |
| `GenAsIo2Unlock` 相关失败 | 上游源码通常包含 `Source/Hamakaze/utils/GenAsIo2Unlock.exe`；若缺失，设置 `build.disable_postbuild=true`（代价是 AsIO2 provider 不可用）。 |
| GitHub API 403 | 配置 `source.github_token`；否则服务会自动回退到 Atom/git，仅会在日志中提示。 |
| 下载源码不完整 | 检查 `source.validate_files` 与网络代理；失败时会保留旧版本。 |
| 端口被占用 | 修改 `server.port` 或 `KDU_SERVICE_PORT`。 |
| 服务启动即退出 | 用 `check` 子命令查看配置错误；查看 `data/logs/service.log`。 |

## 接口文档

- HTTP API、请求字段、回调签名：见 [docs/API.md](docs/API.md)
- 平台配置结构、provider 接口与示例：见 [docs/CONFIG_INTERFACE.md](docs/CONFIG_INTERFACE.md)
