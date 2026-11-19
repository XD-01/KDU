# KDU
[![Build status](https://img.shields.io/appveyor/build/hfiref0x/kdu?logo=appveyor)](https://ci.appveyor.com/project/hfiref0x/kdu)
[![Visitors](https://api.visitorbadge.io/api/visitors?path=https%3A%2F%2Fgithub.com%2Fhfiref0x%2Fkdu&countColor=%23263759&style=flat)](https://visitorbadge.io/status?path=https%3A%2F%2Fgithub.com%2Fhfiref0x%2Fkdu)

## Kernel Driver Utility

#### System Requirements

+ x64 Windows 7/8/8.1/10/11;
+ Administrative privilege is required.

# Purpose and Features

The purpose of this tool is to provide a simple way to explore the Windows kernel/components without requiring extensive setup or a local debugger.
It features:
+ Protected Process Hijacking via arbitrary PPL or arbitrary Process Handles, both through EPROCESS object modification;
+ Driver Signature Enforcement Overrider (similar to DSEFix);
+ Driver loader for bypassing Driver Signature Enforcement (similar to TDL/Stryker);
+ Support for various vulnerable drivers used as functionality "providers".

#### Usage

###### KDU -list
###### KDU -listcsv
###### KDU -diag
###### KDU -prv ProviderID
###### KDU -ps ProcessID
###### KDU -pse Commandline
###### KDU -psw Commandline
###### KDU -pho ProcessID
###### KDU -dmp ProcessID
###### KDU -dse value
###### KDU -map filename
* -list - list currently available providers;
* -listcsv [file] - list available providers in CSV format, optionally write to file;
* -diag - run system diagnostics for troubleshooting;
* -prv  - optional, select vulnerable driver provider;
* -ps   - modify process object of given ProcessID, downgrading any protections;
* -pse  - launch program as ProtectedProcessLight-AntiMalware (PPL);
* -psw  - launch program as ProtectedProcessLight-WinTcb (PPL);
* -pho  - open an arbitrary process with full access
  * -pht - also open all threads in this process with full access
  * -phc - commandline (child process) to inherit the flag, default powershell
  * -phe - also start the child process as ppl
* -dmp  - dump virtual memory of the given process;
* -dse  - write user-defined value to the system DSE state flags;
* -map  - map driver to the kernel and execute its entry point; this command has dependencies listed below;
  * -scv version - optional, select shellcode version, default 1;
  * -drvn name - driver object name (only valid for shellcode version 3);
  * -drvr name - optional, driver registry key name (only valid for shellcode version 3).

Example:
+ kdu -ps 1234
+ kdu -map c:\driverless\mysuperhack.sys
+ kdu -dmp 666
+ kdu -prv 1 -ps 1234
+ kdu -prv 1 -map c:\driverless\mysuperhack.sys
+ kdu -prv 6 -scv 3 -drvn DrvObj -map c:\install\e3600bm.sys
+ kdu -prv 6 -scv 3 -drvn edrv -drvr e3600bl -map c:\install\e3600bl.sys
+ kdu -dse 0
+ kdu -dse 6
+ kdu -pse "C:\Windows\System32\notepad.exe C:\TEMP\words.txt"
+ kdu -psw "C:\Windows\System32\cmd.exe"
+ kdu -pho 1234 -pht -phe 3
+ kdu -listcsv "c:\kdu\out.csv"

Run on Windows 11 24H2*

<img src="https://raw.githubusercontent.com/hfiref0x/kdu/master/Help/kdu5.png" width="600" />

Run on Windows 10 20H2*

<img src="https://raw.githubusercontent.com/hfiref0x/kdu/master/Help/kdu1.png" width="600" />

Compiled and run on Windows 8.1*

<img src="https://raw.githubusercontent.com/hfiref0x/kdu/master/Help/kdu2.png" width="600" />

Run on Windows 7 SP1 fully patched (precompiled version)*

<img src="https://raw.githubusercontent.com/hfiref0x/kdu/master/Help/kdu3.png" width="600" />

Run on Windows 10 19H2 (precompiled version, SecureBoot enabled)*

<img src="https://raw.githubusercontent.com/hfiref0x/kdu/master/Help/kdu4.png" width="600" />

###### Most screenshots are from version 1.0X.

#### Limitations of -map command

Due to the unusual way of loading that does not involve the standard kernel loader, but uses overwriting already loaded modules with shellcode, there are some limitations:

+ Loaded drivers MUST BE specially designed to run as "driverless";

That means you cannot use parameters specified at your DriverEntry as they won't be valid. That also means you cannot load *any* drivers but only specially designed ones, or you need to alter shellcode routines.

+ No SEH support for target drivers;

There is no SEH code in x64. Instead, you have a table of try/except/finally regions described by a pointer in the PE header. If there is an exception, it may result in a BSOD.

+ No driver unloading;

Mapped code can't unload itself; however, you can release all resources allocated by your mapped code.
DRIVER_OBJECT->DriverUnload should be set to NULL.

+ Only ntoskrnl import resolved, everything else is up to you;

If your project needs another module dependency, you must rewrite this loader part.

+ Several Windows primitives are banned by PatchGuard from usage by dynamic code.

Because of the unusual way of loading, mapped driver won't be inside PsLoadedModulesList. That means any callback registered by such code will have its handler located in memory outside this list. PatchGuard may detect this and crash the system.

In general, if you want to know what you *should not do* in kernel, look at https://github.com/hfiref0x/KDU/tree/master/Source/Examples/BadRkDemo which contains a few examples of forbidden things.

#### Kernel traces note
This tool does not change (and will not change in future) internal Windows structures of MmUnloadedDrivers and/or PiDDBCacheTable. That's because:
+ KDU is not designed to circumvent third-party security software or various dubious software (e.g. anti-cheats);
+ These data can be a target for PatchGuard protection in the next major Windows 10 update.

You use it at your own risk. Some lazy AV may flag this tool as hacktool/malware.

# Supported Providers

Full list including all metadata available here:
+ https://github.com/hfiref0x/KDU/blob/master/Help/providers.md

More providers may be added in the future.

# How it works

It uses known vulnerable (or wormhole by design) drivers from legitimate software to access arbitrary kernel memory with read/write primitives.

Depending on the command, KDU will either work as TDL/DSEFix or modify kernel mode process objects (EPROCESS). 

When in -map mode, KDU for most available providers will by default use a 3rd party signed driver from SysInternals Process Explorer and hijack it by placing a small loader shellcode inside its IRP_MJ_DEVICE_CONTROL routine.

# Shellcode versions

KDU uses shellcode to map input drivers and execute their DriverEntry. There are a few shellcode variants embedded into KDU. Shellcode V1, V2, and V3 are used together with 3rd party victim driver (Process Explorer, etc.).

# Build and Notes

KDU comes with full source code.
To build from source, you need Microsoft Visual Studio 2019 or later. For driver builds, you need Microsoft Windows Driver Kit 10 and/or above.

Complete working binaries include: kdu.exe (main executable) and drv64.dll (drivers database). They must reside in the same directory with R/W access enabled for kdu.exe. All binaries MUST be unblocked from the system zone.

# Automated build

`build/build_kdu.py` compiles `Source/KDU.sln` for a selected target platform and collects the result as `kdu.exe` (and `drv64.dll` when present) into `dist/<platform>/`.

## Requirements

+ Windows with Visual Studio 2019 or later (C++ workload and MASM).
+ The projects pin `PlatformToolset` to `v145` (Visual Studio 2026). Use Visual Studio 2026 or override the toolset, for example `--toolset v143`.
+ Python 3.9 or later, no third-party packages.
+ MSBuild is located from `--msbuild`, `MSBUILD_EXE_PATH`, `vswhere`, or `PATH`. Running from a Developer Command Prompt always works.

## Usage

```bat
python build\build_kdu.py --list-platforms
python build\build_kdu.py
python build\build_kdu.py --platform windows-x64 --configuration Debug
python build\build_kdu.py --platform windows-x64 --show-config --dry-run
python build\build_kdu.py --clean --no-postbuild
```

Main options:

| Option | Description |
| --- | --- |
| `--platform NAME` | Target platform key from the config file. Defaults to `default_platform`. |
| `--config PATH` | Platform configuration file, default `build/platforms.json`. |
| `--provider PATH` | Custom Python provider that overrides/extends the platform configuration. |
| `--configuration NAME` | Override MSBuild configuration (`Release`, `Debug`, ...). |
| `--msbuild-platform NAME` | Override MSBuild platform (`x64`). |
| `--toolset NAME` | Override `PlatformToolset`. |
| `--sdk VERSION` | Override `WindowsTargetPlatformVersion`. |
| `--dist-dir PATH` | Override the artifact output directory. |
| `--msbuild PATH` | Explicit path to `MSBuild.exe`. |
| `--clean` | Rebuild instead of an incremental build. |
| `--no-postbuild` | Disable `PostBuildEvent` (skips `GenAsIo2Unlock`, so the AsIO2 provider will not work). |
| `--show-config` | Print the fully resolved platform configuration. |
| `--dry-run` | Print the resolved configuration and MSBuild command without executing anything. |
| `--list-platforms` | List platform profiles declared in the config file. |

The build is successful only when every artifact listed in `required_artifacts` is produced. `kdu.exe` is always required; missing optional artifacts (such as `drv64.dll`) only produce a warning.

## Platform configuration

Settings are resolved in this order, with later layers overriding earlier ones:

1. Built-in defaults in `build/platform_config.py`.
2. `defaults` from the JSON configuration file.
3. `platforms.<platform>` from the same JSON file.
4. The object returned by the `--provider` module.
5. Command line flags (`--configuration`, `--toolset`, `--dist-dir`, ...).

### JSON file schema

`build/platforms.json` has a `default_platform` key, an optional `defaults` object shared by all platforms, and a `platforms` map:

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

Per-platform keys:

| Key | Description |
| --- | --- |
| `description` | Free-form text shown by `--list-platforms`. |
| `msbuild_platform` | Value for `/p:Platform`. |
| `configuration` | Value for `/p:Configuration`. |
| `platform_toolset` | Value for `/p:PlatformToolset`. `null` keeps the project value. |
| `windows_sdk_version` | Value for `/p:WindowsTargetPlatformVersion`. `null` keeps the project value. |
| `solution` | Solution or project passed to MSBuild, relative to the repository root. |
| `artifacts` | Map of artifact file name to the path (relative to the repository root) where MSBuild produces it. |
| `required_artifacts` | Artifact names that must exist after the build. All of them must also be present in `artifacts`. |
| `dist_dir` | Directory where artifacts are collected. |
| `extra_properties` | Extra MSBuild properties, emitted as `/p:<key>=<value>`. |

Path values (and `dist_dir`) support three placeholders: `{platform}` (the selected profile name), `{msbuild_platform}` and `{configuration}`. This makes it possible to describe any target architecture or configuration, including additional profiles such as `windows-x64-debug`, without touching the build script.

### Provider interface (obtaining platform configuration)

A provider is a Python file passed with `--provider`. It receives the platform name and a context, and returns a partial configuration that is merged over the JSON configuration:

```python
def get_platform_config(platform: str, context: dict) -> dict:
    return {
        "configuration": "Release",
        "platform_toolset": "v143",
        "extra_properties": {"RunCodeAnalysis": "false"},
    }
```

Alternatively the file can expose a class named `Provider` with the same `get_platform_config(platform, context)` method; this is the formal interface (`PlatformConfigProvider` in `build/platform_config.py`) and is useful when configuration is read from a service, a registry, or a file next to the provider.

The `context` argument contains:

| Key | Description |
| --- | --- |
| `repo_root` | Absolute path of the repository root, or `None`. |
| `platform` | Selected platform name. |
| `config_path` | Absolute path of the JSON configuration file, or `None`. |
| `base_config` | Configuration already resolved from the defaults and the JSON file. |
| `env` | A snapshot of the process environment. |

Returned dictionaries are merged recursively, key by key (`artifacts` and `extra_properties` are extended rather than replaced); every other value replaces the previous one. A provider may also introduce platform names that do not exist in the JSON file. Raising any exception aborts the build with a configuration error.

### Provider demo

`build/examples/env_provider.py` is a complete, minimal example that reads overrides from environment variables (`KDU_MSBUILD_PLATFORM`, `KDU_CONFIGURATION`, `KDU_PLATFORM_TOOLSET`, `KDU_DIST_DIR`, `KDU_EXTRA_PROPERTIES`).

```bat
REM Use the provider during a real build (here with a dry run)
set KDU_CONFIGURATION=Debug
python build\build_kdu.py --provider build\examples\env_provider.py --platform windows-x64 --dry-run

REM Run the provider on its own and print the configuration it resolves
python build\examples\env_provider.py windows-x64
```

The same pattern applies to other configuration sources: read them inside `get_platform_config`, then return only the keys that should override the defaults.

# Utils and Notes

GenAsIo2Unlock is a special utility used to generate "unlocking" resources required for working with the AsIO2 driver. Full source of this utility is included in Source\Utils\GenAsIo2Unlock.

# Reporting bugs and incompatibilities

If you experience a bug or incompatibility while using KDU with 3rd party software or OS, feel free to fill an issue. However, if this incompatibility is caused by your own actions, such reports will be ignored.

Anticheat, antimalware incompatibilities will be ignored, that's your own responsibility.

# Disclaimer

Using this program might crash your computer with a BSOD. Compiled binary and source code are provided AS-IS in the hope they will be useful BUT WITHOUT WARRANTY OF ANY KIND. Since KDU relies on completely bugged, vulnerable drivers, it is highly recommended to use it on virtual machines only.

# Changelog

https://github.com/hfiref0x/KDU/tree/master/CHANGELOG.txt

# Third party code usage

* TinyAES, https://github.com/kokke/tiny-AES-c
* whirlpool, https://github.com/mabako/mta-whirlpool

# References

* DSEFix, https://github.com/hfiref0x/DSEFix
* Turla Driver Loader, https://github.com/hfiref0x/TDL
* Stryker, https://github.com/hfiref0x/Stryker
* Unwinding RTCore, https://swapcontext.blogspot.com/2020/01/unwinding-rtcore.html
* CVE-2019-16098, https://github.com/Barakat/CVE-2019-16098
* CVE-2015-2291, https://www.exploit-db.com/exploits/36392
* CVE-2018-19320, https://seclists.org/fulldisclosure/2018/Dec/39
* ATSZIO64 headers and libs, https://github.com/DOGSHITD/SciDetectorApp/tree/master/DetectSciApp
* ATSZIO64 ASUS Drivers Privilege Escalation, https://github.com/LimiQS/AsusDriversPrivEscala
* CVE-2019-18845, https://www.activecyber.us/activelabs/viper-rgb-driver-local-privilege-escalation-cve-2019-18845
* DEFCON27: Get off the kernel if you can't drive, https://eclypsium.com/wp-content/uploads/2019/08/EXTERNAL-Get-off-the-kernel-if-you-cant-drive-DEFCON27.pdf
* CVE-2019-8372: Local Privilege Elevation in LG Kernel Driver, http://www.jackson-t.ca/lg-driver-lpe.html
* CVE-2021-21551, https://attackerkb.com/topics/zAHZGAFaQX/cve-2021-21551
* KDU v1.1 release and bonus (AsIO3.sys unlock), https://swapcontext.blogspot.com/2021/04/kdu-v11-release-and-bonus-asio3sys.html
* GhostEmperor: From ProxyLogon to kernel mode, https://securelist.com/ghostemperor-from-proxylogon-to-kernel-mode/104407/
* KDU v1.2 release and the wonderful world of Microsoft incoherency, https://swapcontext.blogspot.com/2022/02/kdu-v12-release-and-wonderful-world-of.html
* How to exploit a vulnerable Windows driver, https://github.com/stong/CVE-2020-15368
* CVE-2022-3699, https://github.com/alfarom256/CVE-2022-3699
* LOLDrivers, https://www.loldrivers.io
* ECHOH NO, https://github.com/kite03/echoac-poc/
* NVDrv, https://github.com/zer0condition/NVDrv
* CVE-2023-41444, https://blog.dru1d.ninja/windows-driver-exploit-development-irec-sys-a5eb45093945
* CVE-2023-20598, https://www.amd.com/en/resources/product-security/bulletin/amd-sb-6009.html
* CVE-2020-12928, https://h0mbre.github.io/RyzenMaster_CVE/
* CVE-2025-45737, https://github.com/smallzhong/NeacController
* CVE-2025-7771, https://securelist.com/av-killer-exploiting-throttlestop-sys/117026/
* CVE-2025-8061, https://github.com/spawn451/CVE-2025-8061-Exploit

# Wormhole drivers code

They are used in multiple products from hardware vendors, mostly in unmodified state. They all break the OS security model and are additionally bugged. Links are for educational purposes on how not to do things in driver development.

* WinIo 3.0 BSOD/CVE generator, https://github.com/hfiref0x/Misc/tree/master/source/WormholeDrivers/WINIO
* WinRing0 BSOD/CVE generator, https://github.com/hfiref0x/Misc/tree/master/source/WormholeDrivers/WINRING0
* PhyMem BSOD/CVE generator, https://github.com/hfiref0x/Misc/tree/master/source/WormholeDrivers/PHYMEM
* MapMem BSOD/CVE generator, https://github.com/hfiref0x/Misc/tree/master/source/WormholeDrivers/MAPMEM
* InpOut BSOD/CVE generator, https://github.com/hfiref0x/Misc/tree/master/source/WormholeDrivers/INPOUT
* Intel infamous driver, https://github.com/hfiref0x/Misc/tree/master/source/IntelNal

# Authors

(c) 2020 - 2026 KDU Project
