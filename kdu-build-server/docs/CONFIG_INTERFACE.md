# 平台配置与 provider 接口

本文档描述构建服务如何获得"目标平台"配置，以及其他应用如何通过配置文件或 Python provider 扩展配置来源。

## 1. 配置来源与优先级

同一次构建的最终平台配置按以下顺序合并，后者覆盖前者：

1. `kdu_build_server/platform_config.py` 中的内置默认值 `DEFAULT_PLATFORM_CONFIG`。
2. `config/platforms.json` 的 `defaults`。
3. `config/platforms.json` 的 `platforms.<platform>`。
4. `build.provider` 指向的 Python 模块返回值。
5. HTTP 构建请求中的覆盖字段（`configuration`、`msbuild_platform`、`platform_toolset`、`windows_sdk_version`、`extra_properties`）。

合并规则：

- 普通键直接覆盖。
- 字典键（`artifacts`、`extra_properties`）逐键合并，不会整体替换。
- 请求只能覆盖白名单字段，不能修改 `solution`、`artifacts`、`dist_dir` 等路径或加载 provider。

## 2. platforms.json

示例见 `config/platforms.json`：

```json
{
  "default_platform": "windows-x64",
  "defaults": {
    "solution": "Source/KDU.sln",
    "windows_sdk_version": "10.0",
    "artifacts": {
      "kdu.exe": "Source/Hamakaze/output/{msbuild_platform}/{configuration}/kdu.exe",
      "drv64.dll": "Source/Tanikaze/output/{msbuild_platform}/{configuration}/drv64.dll"
    },
    "required_artifacts": ["kdu.exe"],
    "dist_dir": "dist/{platform}"
  },
  "platforms": {
    "windows-x64": {
      "description": "Windows x64, Release, MSVC v145",
      "msbuild_platform": "x64",
      "configuration": "Release",
      "platform_toolset": "v145"
    }
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `default_platform` | string | 请求未指定 `platform` 时使用。 |
| `defaults` | object | 所有平台共享的默认配置。 |
| `platforms` | object | 平台名到平台配置的映射。平台名可以自定义。 |
| `description` | string | 展示用说明，出现在 `GET /api/v1/platforms`。 |
| `msbuild_platform` | string | MSBuild `/p:Platform`，例如 `x64`。 |
| `configuration` | string | MSBuild `/p:Configuration`，例如 `Release`、`Debug`。 |
| `platform_toolset` | string/null | `/p:PlatformToolset`。`null` 表示使用工程文件中的值。 |
| `windows_sdk_version` | string/null | `/p:WindowsTargetPlatformVersion`。 |
| `solution` | string | 传给 MSBuild 的 `.sln`/`.vcxproj`，相对于下载后的源码根目录。 |
| `artifacts` | object | 产物名到"相对源码根的产物路径"的映射。 |
| `required_artifacts` | array | 编译后必须存在的产物名；缺一即判定构建失败。 |
| `dist_dir` | string | 本地构建脚本使用的输出目录；服务构建时产物统一复制到任务目录，此字段仅为兼容保留。 |
| `extra_properties` | object | 额外 MSBuild 属性，生成 `/p:<key>=<value>`。 |

路径字段支持三个占位符：

- `{platform}`：平台配置名，如 `windows-x64`
- `{msbuild_platform}`：`msbuild_platform` 的值，如 `x64`
- `{configuration}`：`configuration` 的值，如 `Release`

产物路径必须位于源码根目录之内，否则构建会以配置错误失败。

## 3. Provider 接口

Provider 用于从外部系统（数据库、配置中心、其他应用、环境变量等）读取平台配置。它是一段**在服务进程内运行的可信代码**，只能在服务端配置，API 客户端无法指定。

### 3.1 配置方式

在 `config/service.json` 中：

```json
{
  "build": {
    "provider": "examples/env_provider.py"
  }
}
```

或使用环境变量：

```text
KDU_PROVIDER=examples/env_provider.py
```

相对路径相对于服务启动目录。文件不存在时服务启动失败。

### 3.2 两种写法

**函数方式（最简单）**

```python
def get_platform_config(platform: str, context: dict) -> dict:
    return {
        "configuration": "Release",
        "platform_toolset": "v143",
        "extra_properties": {"RunCodeAnalysis": "false"},
    }
```

**类方式（推荐复杂逻辑）**

```python
from kdu_build_server.platform_config import PlatformConfigProvider

class Provider(PlatformConfigProvider):
    def get_platform_config(self, platform: str, context: dict) -> dict:
        # 从其他系统读取配置……
        return {"configuration": "Debug"}
```

加载器优先查找模块级 `Provider` 类，其次查找 `get_platform_config` 函数。

### 3.3 context 参数

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `repo_root` | string/None | 服务中固定为 `None`；本地脚本调用时可传入仓库根目录。 |
| `platform` | string | 本次请求的平台名。 |
| `config_path` | string | `platforms.json` 的绝对路径。 |
| `base_config` | dict | 已完成"内置默认 + JSON"合并后的配置，可据此做二次决策。 |
| `env` | dict | 服务进程环境变量的快照。 |

### 3.4 返回值与错误

- 必须返回 dict（可以是部分字段），会与已解析配置递归合并。
- 返回 `None` 视为无覆盖（等价于空 dict）。
- 抛出任何异常都会导致该次配置解析失败；提交构建时返回 HTTP 422，错误信息包含异常文本。
- Provider 可以声明 JSON 中不存在的平台名。例如 provider 根据 `platform == "windows-x64-fast"` 返回一套配置，即使 `platforms.json` 没有该平台，请求也能成功。

### 3.5 示例：环境变量 Provider

`examples/env_provider.py` 读取以下环境变量并返回覆盖项：

| 环境变量 | 对应字段 |
| --- | --- |
| `KDU_MSBUILD_PLATFORM` | `msbuild_platform` |
| `KDU_CONFIGURATION` | `configuration` |
| `KDU_PLATFORM_TOOLSET` | `platform_toolset` |
| `KDU_EXTRA_PROPERTIES` | `extra_properties`，格式 `KEY1=VALUE1;KEY2=VALUE2` |

单独运行查看解析结果：

```bat
set KDU_CONFIGURATION=Debug
python examples\env_provider.py windows-x64
```

作为服务 provider 使用：

```bat
set KDU_PROVIDER=examples\env_provider.py
set KDU_PLATFORM_TOOLSET=v143
python -m kdu_build_server --config config\service.json serve
```

### 3.6 示例：从其他应用读取配置

下面的 provider 从同一台机器上的 JSON 文件读取配置中心下发的值；也可以把 `json.load` 换成查询数据库或调用其他应用的 HTTP 接口：

```python
from pathlib import Path
import json

from kdu_build_server.platform_config import PlatformConfigProvider

SHARED_CONFIG = Path(r"C:\ProgramData\kdu-platforms.json")

class Provider(PlatformConfigProvider):
    def get_platform_config(self, platform: str, context: dict) -> dict:
        if not SHARED_CONFIG.is_file():
            return {}
        document = json.loads(SHARED_CONFIG.read_text(encoding="utf-8"))
        entry = document.get(platform)
        if not isinstance(entry, dict):
            return {}
        return {
            "configuration": entry.get("configuration", "Release"),
            "platform_toolset": entry.get("toolset", "v145"),
            "extra_properties": entry.get("properties", {}),
        }
```

Provider 内需要做超时和异常处理，避免外部系统故障导致所有构建请求被拒绝；也可以在不影响编译的前提下返回 `{}` 使用默认配置。

## 4. 程序化调用

如果需要在服务外部复用同一套解析逻辑（例如自研调度程序），可以直接调用：

```python
from pathlib import Path
from kdu_build_server.platform_config import resolve_platform_config

config = resolve_platform_config(
    platform="windows-x64",
    platforms_file=Path("config/platforms.json"),
    provider=Path("examples/env_provider.py"),
    overrides={"configuration": "Debug"},
)
print(config["platform_toolset"], config["extra_properties"])
```

结果与 HTTP 服务在 worker 中使用的配置完全一致。
