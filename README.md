# RemoteControl

> 一个**轻量级**的 Windows 屏幕串流 + 远程输入程序：服务端抓屏、编码、经 TCP 推流，客户端显示画面并把鼠标/键盘回注到服务端。
>
> 自有 C++ 源码约 **1.07 万行 / 39 个文件**（不含生成的协议头与测试夹具；算上夹具约 1.33 万行），第三方依赖**全部离线固化**，
> 自带 **20 项一键回归** —— 是一个**学习 C++ 与现代工程技能的绝佳项目**。
>
> A **lightweight** Windows screen-streaming and remote-input tool, built as a hands-on **C++ learning project** —
> async Asio networking, FlatBuffers wire protocol, delta frames, token / TLS authentication and role-based access control.

**它小，但不玩具。** 单个进程里同时有异步网络、二进制协议、跨线程数据传递、Win32 抓屏与 DWM、
以及一整套性能与延迟的测量方法 —— 每一个结论都带**判据、反向对照和实测数字**（记录在 `refactored/docs/`，500+ KB）。
同时它又足够小：**一次通读用不了一天**，改一行就能跑回归看结果，非常适合拿来动手。

📌 **使用范围**：这是一个**本地学习与实验**用的程序，完整验证都在同机 `127.0.0.1` 上完成。
默认配置不启用任何认证（`auth_token` 为空、`tls_enable=false`），且默认 `listen_host` 是 `0.0.0.0`
—— 请在受信任的本地网络里使用，**不要**把默认配置接到公网（见「安全说明」与文末「范围与边界」）。

---

## 它能做什么

| 分类 | 能力 |
|---|---|
| **网络** | standalone Asio 全异步 IO · 多客户端并发 · 双向心跳 + 空闲超时 · 指数退避自动重连 · 握手超时可见 |
| **画面** | GDI `BitBlt` 或 **DXGI Desktop Duplication**（默认 `auto` 自动选）· 鼠标光标合成 · **差异帧 / 脏矩形** + 周期性关键帧 · 客户端**按脏区重绘** · 输入优先抓屏 |
| **输入** | 鼠标移动 / 按键 / 滚轮与键盘事件上行注入；远端坐标读回自校验 |
| **安全** | 共享密钥认证（Token）· **TLS 加密 + 证书指纹 pin**（自签自动生成）· **按客户端分角色**（`control` 全权 / `view` 只读，**在服务端拦截**）· **独立审计日志** |
| **可观测** | `[capture]` / `[decode]` / `[paint]` / `[latency]` 分段归因日志 · 20 项一键回归（每项 0/1/2 三态退出码） |

## 这个项目能练到什么

它不是一份"照着敲一遍"的教程代码，而是一套**可以自己拆开、改坏、再修好**的工程实践：

| 方向 | 项目里对应的真实场景 |
|---|---|
| **现代 C++ 基础** | C++17、RAII 与智能指针、`std::shared_ptr` 跨线程生命周期、移动语义、`std::chrono`、容器与迭代器 |
| **异步 IO 与回调生命周期** | standalone Asio：`async_read`/`async_write` 的**部分读写**、对象在回调期间被销毁、`operation_aborted` 的区分 |
| **二进制协议设计** | 8 字节帧头 + FlatBuffers schema、版本演进、消息分发的兼容分支、生成的代码何时该入库 |
| **并发与线程模型** | IO 线程池、抓屏/编码/发送三段的生产者-消费者、跨线程队列、`strand` 的使用边界 |
| **平台 API** | Win32 窗口与消息循环、GDI `BitBlt`/`StretchBlt`、**DXGI Desktop Duplication**、光标合成、输入注入 |
| **网络健壮性** | 心跳 + 空闲超时、指数退避重连、握手超时、TLS 握手与证书指纹 pin、Nagle 行为的实测 |
| **认证与授权** | 共享密钥、恒定时间比较、应用层角色（`control`/`view`）、审计日志与日志注入清洗 |
| **性能与延迟测量** | 拉屏预算拆解、差异帧/脏矩形、端到端归因、延迟与抖动判据 —— **含"判据本身出错、产出假绿"的复盘** |
| **构建与工具链** | CMake + Ninja + 直接调用 MSVC、跨盘符工具链探测、依赖固化与离线构建、Python 测试夹具 |

其中最难得的一块是 **测量方法论**：`refactored/docs/02` 里记着若干次"判据方向反了 / 阈值取错了对象 /
读数本身零分辨力"的自我更正 —— 这类经验在普通练手项目里基本遇不到。

## 它是怎么长出来的

项目经历了三个阶段，每个阶段的**论证、实测数据和踩过的坑**都完整记录在 `refactored/docs/` 里
（那两份文档共 500+ KB，是这个仓库最主要的知识资产）：

1. **现代化与工程化** —— 把旧实现（`malloc/free` + `PostThreadMessage` 传裸指针、`send` 忽略部分发送、固定 10 MB 接收缓冲）重写为 C++17 + RAII + CMake。
2. **异步网络（2A）+ 认证与加密（2B）** —— Asio 异步 IO、FlatBuffers 协议、心跳/重连；Token、TLS+pin、角色 ACL 与审计日志。
3. **性能与延迟** —— 拉屏预算、差异帧、端到端归因、DXGI；以及「抖动/冻结」「整帧优先」「输入→显示」三个延迟维度。

重构前的旧实现保留在 `legacy/`，**只作论证对照，不参与构建**。

---

## 构建

### 前置

- **Windows 10 / 11 x64**（本项目依赖 Win32 GDI / DXGI，CMake 里对非 Windows 直接 `FATAL_ERROR`）
- **Visual Studio**（含「使用 C++ 的桌面开发」工作负载）+ **Windows SDK**
- **Ninja**（随 VS 一起安装）
- **Python 3.8+**（跑测试夹具与依赖脚本用）

> **不需要联网。** 第三方依赖已固化在 `refactored/third_party/`（含**预编译的 OpenSSL 3.5.0**），
> 开箱即可离线构建。只有在该目录缺失时才需要跑 `tools/fetch_*.py` 重新拉取。

### 工具链路径：自动探测，换机器不用改文件

CMake 的 VS 生成器在「VS 与 Windows SDK 装在非默认盘符 / 跨盘」的布局下探测不到 `cl.exe`，
所以本项目统一走 **Ninja + 工具链文件**（把 `INCLUDE`/`LIB` 直接写成 `/I` 与 `/LIBPATH:` 选项，
不依赖 `vcvarsall.bat`）。

`cmake` / `ninja` / MSVC / Windows SDK 的路径**都由脚本自己解析**，固定三级顺序：

| 级 | 手段 |
|---|---|
| ① | 显式指定：环境变量 `RC_CMAKE` `RC_NINJA` `RC_PY`；或 `-DRC_MSVC_ROOT=… -DRC_SDK_ROOT=… -DRC_SDK_VER=… -DRC_NINJA=…` |
| ② | 文件顶部的默认值（`cmake/msvc-ninja-toolchain.cmake`）——**磁盘上确实存在才用** |
| ③ | 自动探测：`vswhere`（VS 官方安装位置查询器，装在非默认盘符也认得出）→ 环境变量 `VCToolsInstallDir` / `WindowsSdkDir` → 常见安装目录 |

三级都落空时会直接打印"缺什么、该装什么、怎么指定路径"，而不是让 CMake 抛那句
`No CMAKE_CXX_COMPILER could be found`（它不指向任何可操作的动作）。

> 作者本机的布局是 VS 在 `E:\vs`、Windows SDK 在 `D:\Windows Kits`（都不在默认盘符）。
> 这两个路径就是第 ② 级的默认值 —— **别人不需要改**：在别人的机器上它们不存在，
> 会自动落到第 ③ 级去探测。

想看它到底解析到了什么：

```bat
build.bat -DCMAKE_MESSAGE_LOG_LEVEL=STATUS          :: 看 [toolchain] 开头的行
```
```bash
RC_PRINT_TOOLCHAIN=1 bash tests/run_all_verify.sh   # 只打印解析结果，不跑回归
```

### 构建（命令行）

```bat
cd refactored
build.bat
```

产物：

```
refactored\build-ninja\server\rc_server.exe
refactored\build-ninja\client\rc_client.exe
```

**也可以在 Visual Studio 里构建**：打开根目录的 `RemoteControl.slnx`（F5 调试）。
`slnx` 挂的两个 `.vcxproj` 都把源码指向 `refactored/`，产物输出到 `bin\(Platform)\(Configuration)\`。

> ⚠️ 两条构建路径的产物是**分开的**：`build-ninja/` 是 Ninja 的，`bin/x64/…` 是 VS 的。
> 测试夹具默认用前者。

---

## 运行

**必须在 `refactored` 目录下启动**（配置文件按相对路径读取）：

```bat
cd refactored
build-ninja\server\rc_server.exe config\server.json
build-ninja\client\rc_client.exe config\client.json
```

服务端先起，再起客户端；客户端窗口标题会显示连接状态（`[已连接]` / `[只读]` / `[重连中…]` / `[不会再重连]`）。

## 配置

配置是 JSON，两端各一份（`refactored/config/`）。

**`server.json` 常用字段**

| 字段 | 默认 | 说明 |
|---|---|---|
| `listen_host` / `listen_port` | `0.0.0.0` / `9999` | 监听地址与端口。**默认监听所有网卡**，见「安全说明」 |
| `io_threads` | `0` | IO 线程数，`0` = 按核数自动 |
| `max_clients` | `32` | 最大并发客户端 |
| `idle_timeout_ms` | `30000` | 空闲超时踢人 |
| `screen_max_fps` | `30` | 抓屏节拍上限 |
| `capture_cursor` | `true` | 是否把系统光标合成进画面（`BitBlt` 本身不含光标） |
| `capture_delta` | `true` | 差异帧（脏矩形）开关 |
| `input_priority_capture` | `true` | **输入优先抓屏**：收到输入事件时立刻补一次抓屏，缩短「按键 → 画面变化」的可见延迟 |
| `dpi_aware` | `false` | 抓屏进程的 DPI 感知。`false` 时抓到的可能是物理画面**左上角的 1:1 裁剪**而非整屏降采样 |
| `auth_token` | `""` | 共享密钥。**空 = 不启用认证** |
| `tls_enable` / `tls_auto_self_signed` | `false` / `true` | TLS 开关；开启且无证书时自动自签 |

**`client.json` 常用字段**

| 字段 | 默认 | 说明 |
|---|---|---|
| `server_host` / `server_port` | `127.0.0.1` / `9999` | 服务端地址 |
| `heartbeat_interval_ms` / `heartbeat_timeout_ms` | `2000` / `6000` | 心跳周期与超时 |
| `hello_timeout_ms` | `5000` | 握手超时（对端连得上但不回话时会走到这里） |
| `reconnect_initial_delay_ms` / `reconnect_max_delay_ms` | `500` / `10000` | 重连退避区间 |
| `target_fps` | `30` | 客户端目标帧率 |
| `auth_token` | `""` | 必须与服务端一致 |
| `tls_enable` / `tls_pin_sha256` | `false` / `""` | 启用 TLS 并 pin 服务端证书指纹（留空 = 不 pin） |

> 角色 ACL 通过 `auth_clients` 授权表启用（每个凭据绑 `name` + `role`），
> 设计说明见 `refactored/docs/02-phase2-async-network.md` §6.31。

---

## 测试与回归

一键跑 20 项定版回归（需要 bash，Git Bash 即可）：

```bash
bash refactored/tests/run_all_verify.sh
```

`cmake` / `ninja` / `python3` 会自动探测（可用环境变量 `RC_CMAKE` / `RC_NINJA` / `RC_PY` 覆盖）。
若 `build-ninja/` 还没配置过（**全新 clone 就是这样**），脚本会先按 `build.bat` 的等价命令配置一次，
再编译 —— 不需要手动先跑一遍构建。

判定方式：**逐项读输出里的 `*_EXIT=` 行**（共 20 行，全 `0` 才算全绿）。每项是**三态**而非布尔：
`0` = 通过，`1` = 不通过（机制真的失效），`2` = **没测到**（前置不变式不成立，本轮无判别力）。
**看到 `2` 要先读它给出的理由，不要当成「低频失败」重跑。**

> 屏幕上的 `[x/17]` 是**段号**，不是判据行号：17 段产出 20 行 `*_EXIT=`（因为 DELTA 段要跑
> `gdi` / `dxgi` 两个后端、DPI 预检与收尾各算一行）。两个数都真，量的是不同的东西。

> ⚠️ 脚本**整体**的退出码目前恒为 `0`（末尾是一条 `echo`），因此它**不能直接当 CI 判据**；
> 判通过与否必须逐项读 `*_EXIT=`。这是已知待办，不是设计意图。

只想看工具链解析结果（不跑回归）：`RC_PRINT_TOOLCHAIN=1 bash refactored/tests/run_all_verify.sh`。

⚠️ 跑之前注意：

- 其中几项会**驱动物理光标与鼠标输入**（同机自测时服务端 `SetCursorPos` 动的就是你的光标）⇒ **跑回归期间别用鼠标**。
- AUTH / TLS / ACL 三项会各弹 4～6 次客户端窗口。
- **跑回归或长跑期间不要并行跑别的进程**：它们都建同名窗口类，且 CPU 负载会把判据推出健康带、产出假红。
- **一次性产物优先落在 `E:\WBdata\_temp\`**（各脚本顶部的 `PREFERRED_WORKDIR`；作者本机的约定是"项目目录只留能长期复用的东西"）。
  **别人的机器上没有这块盘也没关系**：所有夹具在默认目录建不出来时都会**自动回退到系统临时目录**，并打印实际运行目录
  （2026-09-29 补齐了最后一批写死路径的脚本 —— 至此覆盖全部 20 项回归与下面列出的单项/专项，不再依赖任何作者本机盘符）。
  想换目录仍可用各自的 `--workdir` / `--work` / `--out` 出口。

单项与专项（在 `refactored/` 下执行）：

```bash
python tests/run_frame_rate_probe.py --seconds 40          # 性能归因（抓屏/比对/编码分段）
python tests/run_latency_check.py --seconds 12             # 抖动与冻结
python tests/run_input_latency_check.py --seconds 12       # 输入→显示延迟（30/10 fps 对照）
python tests/run_input_priority_check.py --seconds 12      # 输入优先抓屏（off/on/off 三轮）
python tests/run_soak_check.py --seconds 1800              # 长时间稳定性（资源泄漏）
python tests/run_auth_check.py                             # Token 认证（四轮）
python tests/run_tls_check.py                              # TLS + pin（四轮）
python tests/run_audit_check.py                            # 角色 ACL + 审计日志（六轮）
python tests/run_local_e2e.py                              # 端到端自检（自动拉起服务端跑探针）
```

多项夹具带 `--reverse-control`（故意写坏实现，断言判据**必须**失败）与 `--selftest`
（用合成数据回归判据自身）—— 这是本项目的核心纪律：**判据必须被证伪过才算数**。

---

## 目录结构

```
RemoteControl/
├─ RemoteControl.slnx              Visual Studio 解决方案
├─ RemoteControl/                  VS 工程：服务端（源码指向 refactored/）
├─ client/                         VS 工程：客户端（源码指向 refactored/）
├─ legacy/                         重构前的旧实现，仅作论证对照，不参与构建
├─ LICENSE                         MIT
└─ refactored/                     当前主源码与文档
   ├─ CMakeLists.txt               CMake 入口（Windows-only）
   ├─ build.bat                    一键构建（Ninja + 直接调用 MSVC）
   ├─ cmake/msvc-ninja-toolchain.cmake
   ├─ common/                      双端共享：8 字节帧头、FlatBuffers 编解码、配置、日志
   ├─ server/                      服务端：AsioServer / SessionRegistry / 抓屏 / 输入执行 / 审计
   ├─ client/                      客户端：AsyncClient / RemoteWindow
   ├─ proto/                       FlatBuffers schema + 已生成的 C++ 头（入库，构建期不需要 flatc）
   ├─ config/                      server.json / client.json
   ├─ docs/                        02 设计论证与踩坑 · 03 状态与路线图
   ├─ tests/                       25 个夹具与工具（其中 20 项构成一键回归）
   ├─ tools/                       依赖获取脚本与一致性校验工具
   └─ third_party/                 固化的第三方依赖（可离线构建）
```

## 实测基线

以下是这台开发机上的实测值，**引用时请注意口径**：

| 指标 | 值 | 条件 |
|---|---|---|
| 端到端帧周期 | 41.0 ms（≈ 24.4 fps） | 2026-09-23，同机 40 s |
| 其中 `BitBlt` 抓屏 | 26 ms（占 63%） | 同上 |
| DXGI 抓屏 | 16.4 → **6.2 ms** | 同上 |
| 差异帧：编码耗时 | 38.9 → **1.0 ms**（39×） | 2026-09-22 |
| 差异帧：每帧字节 | 1646 → **27 KB**（61×） | 2026-09-22 |
| 客户端按脏区重绘 `StretchBlt` | 13.7 → **2.8 ms** | 2026-09-25 |
| 长时间稳定性 | 30 min 无 GDI / 句柄泄漏 | 2026-09-27 |

> ⚠️ **`BitBlt` 对机器负载极敏感**：同一台机器、同一份代码，实测到过 20.1 / 26 / 30 ms。
> **跨会话比绝对值没有意义，只有配对比较（同一轮内 A/B）才有意义。**
> 另外帧率上限是配置项 `target_fps`（默认 30），实测约 26 fps —— 瓶颈已经不在抓屏，而在编码与比对。

---

## 范围与边界

项目**刻意保持轻量**：只覆盖「抓屏 → 编码 → 传输 → 显示 → 回注」这一条主链路，
不做产品级远程桌面才需要的周边（多显示器、音频、剪贴板、文件传输、会话管理…）。
下面这些是**当前的适用范围**，写出来是为了让边界可见 —— 同时也顺手标出了「想继续往下练」的方向。

### 适用到哪一步

| 方面 | 当前覆盖 | 想继续往下练 |
|---|---|---|
| 网络环境 | 完整验证在同机 `127.0.0.1` 上完成；延迟 / 抖动判据建立在 RTT ≈ 0.01 ms 之上 | `tests/netem_relay.py` 可在单机注入 RTT / 丢包；要更真就是 Linux 路由虚机 + `tc netem` |
| 输入注入效果 | 代码路径、坐标自校验与日志断言齐备 | 把服务端指到一台虚机，两边各开一个记事本互相对照 |
| 延迟测点 | 测到「客户端把画面 `StretchBlt` 到窗口」为止（即下界） | 真实显示器扫描输出需要外部采集设备 |

> 真实跨机网络与鼠标键盘注入的**实际效果**还留在这里 —— 它们也正是下一步最值得动手的题。

### 有意保持的轻量范围

| 方面 | 现状 |
|---|---|
| 授权与限流 | 角色 ACL 改授权表要重启；未做按客户端限流；审计日志不带防篡改 |
| TLS | 自签证书；未做双向认证与证书轮换（换证书要手工同步客户端 pin） |
| 平台 | 仅 Windows（依赖 Win32 GDI / DXGI，CMake 对非 Windows 直接 `FATAL_ERROR`） |

### 两个已知细节

- 认证被拒后客户端**标题**会显示 `[重连中…]`，而它**实际已停止重连**（日志里的
  `not reconnecting (unrecoverable)` 才是准确的）。已显式留档；修它要同时改客户端文案与对应判据 ——
  取舍见 `docs/02` §6.33。
- **高 DPI**：默认 `capture_backend=auto` 会走 DXGI（物理像素，与输入同一套坐标空间）。
  若 DXGI 不可用（**锁屏 / UAC 安全桌面 / 独占全屏 / RDP**）会退回 GDI，此时有启动告警提示。

## 安全说明

**先看清定位**：这个项目是**用来练 C++ 的**。其中的认证 / TLS / 角色 ACL / 审计日志，
存在的意义是**把这几套机制亲手实现一遍**（Asio + OpenSSL 怎么接、协议字段怎么设计、
边界卡在哪，都写进了 `docs/`）—— **不是**一套经过审计、可以对外的方案。请按「教学实现」而不是「安全产品」来读它。

这个程序的设计目的就是**让远端控制本机的鼠标键盘**，所以**配置就是安全边界**：

| 风险 | 默认状态 | 建议 |
|---|---|---|
| 任何人可连接并操作 | `auth_token: ""` ⇒ **不启用认证**（fail-open，启动会有 WARN） | 设一个随机密钥，两端一致 |
| 流量明文 | `tls_enable: false` | 打开 TLS（`tls_auto_self_signed: true` 会自动生成自签证书） |
| 中间人冒充服务端 | `tls_pin_sha256: ""` ⇒ **不校验证书** | 填上服务端证书指纹 |
| 监听所有网卡 | `listen_host: "0.0.0.0"` | 只在本机用时改成 `127.0.0.1` |

**默认配置等于没有任何防护** —— 那是为了让「第一次跑起来」足够简单。**不要**把默认配置接到不可信网络。

几点实现上的说明（这几条同时也是本项目刻意练到的东西）：

- **只读角色是在服务端拦截的**（`on_mouse` / `on_keyboard` 顶部）。客户端侧的「只读自律」只是省带宽，
  **不是安全边界** —— 任何程序都能绕开客户端直接构造输入消息。这是一个被刻意做出来并留档的
  「客户端自律 ≠ 安全边界」例子。
- **审计日志**记录连接、认证、被拒输入与会话结束；日志行做了注入清洗（客户端自称的名字不会被解析成字段）。
- 这套机制**不是**为对抗敌意网络设计的：没有限流、没有防重放、审计不可防篡改。

**如果你只是想 clone 下来跑跑看**，最低限度做这两件事：
① 把 `listen_host` 改成 `127.0.0.1`；② 确实要跨机时，设 `auth_token` 并把 `tls_enable` 打开。

---

## 文档

| 文档 | 内容 | 规模 |
|---|---|---|
| [`refactored/docs/02-phase2-async-network.md`](refactored/docs/02-phase2-async-network.md) | 第二阶段设计与踩坑全记录（按小节编号引用，如 §6.31） | 415 KB |
| [`refactored/docs/03-status-and-roadmap.md`](refactored/docs/03-status-and-roadmap.md) | 项目状态、实测基线、能力清单、待办与验收口径 | 111 KB |

这两份文档是这个仓库里信息密度最高的部分：每个结论都带着**判据、反向对照和实测数字**，
包括若干次「判据本身出错、产出假绿/假红」的复盘。文档编号从 `02` 开始是历史原因。

## 许可证

本项目以 **MIT License** 发布，见 [`LICENSE`](LICENSE)。

第三方依赖各自遵循其原许可，且**已随仓库一起分发**（见下表）—— 再分发时请保留这些声明：

| 依赖 | 版本 | 许可 | 许可文件 |
|---|---|---|---|
| [Asio](https://github.com/chriskohlhoff/asio)（standalone） | 1.30.2 | Boost Software License 1.0 | [`third_party/asio/LICENSE_1_0.txt`](refactored/third_party/asio/LICENSE_1_0.txt) |
| [FlatBuffers](https://github.com/google/flatbuffers) | 24.3.25 | Apache-2.0 | [`third_party/flatbuffers/LICENSE`](refactored/third_party/flatbuffers/LICENSE) |
| [spdlog](https://github.com/gabime/spdlog) | 1.14.1 | MIT | [`third_party/spdlog/LICENSE`](refactored/third_party/spdlog/LICENSE) |
| [nlohmann/json](https://github.com/nlohmann/json) | 3.11.3 | MIT | [`third_party/json/LICENSE.MIT`](refactored/third_party/json/LICENSE.MIT) |
| [OpenSSL](https://www.openssl.org/)（预编译二进制） | 3.5.0 | Apache-2.0 | [`third_party/openssl/LICENSE.txt`](refactored/third_party/openssl/LICENSE.txt) |
