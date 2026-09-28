#pragma once
// ============================================================
// 配置管理：IP / 端口 / 日志级别全部外置到 JSON（nlohmann/json）
//
// 旧代码把 "127.0.0.1":9999 硬编码在源码里，换部署环境就要重新编译。
// 现在一份二进制 + 不同 config/*.json 即可部署。
// 读取策略：文件不存在时全部走默认值（默认值与旧代码行为一致），
// 文件存在但 JSON 非法时抛异常快速失败——配置错误不该被静默吞掉。
// ============================================================

#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>

#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace rc {

/// 【2B 第三刀 权限模型】一条客户端授权记录。
///
/// 【为什么是"列表"而不是再多一个 token 字段】
///   2B 第二刀解决的是"**谁能连上这个端口**"（单一共享密钥）。
///   但共享密钥天然回答不了两件事：
///     ① **它是谁** —— 所有人共用一个密钥 ⇒ 日志里只能写"某个持密钥的人"，
///        出了事无法归因到具体设备；
///     ② **它能做什么** —— 密钥只分"对/错"两态，没法表达"这台只能看不能动"。
///   所以把「凭据 → 身份 + 权限」做成一张表：凭据命中哪一行，就得到那一行的
///   name（用于日志/审计）与 role（用于授权）。
///
/// 【为什么 role 存成字符串而不是 bool `read_only`】
///   bool 只有两态，将来要加第三种（例如"能看能点但不能敲键盘"）时字段含义会变，
///   而配置文件里已经写下的 `true/false` 会**静默改变语义**。
///   字符串则可以只追加取值。代价是拼错的风险 —— 所以解析期**当场拒绝**未知取值
///   （同 `capture_backend` 那条：拼错必须启动失败，不能默默当成默认值）。
struct AuthClientEntry {
    /// 身份名。只用于**日志与审计**，不参与鉴权 —— 客户端自称的名字不作数，
    /// 真正决定身份的是"命中了哪一条 token"。
    std::string name;
    /// 共享密钥（该客户端的）。空 token 的条目在解析期就被拒绝（见 load_server_config）。
    std::string token;
    /// "control"（可注入输入）| "view"（只读：能看到画面，输入被服务端拒绝）
    std::string role = "control";
};

struct ServerConfig {
    std::string   listen_host = "0.0.0.0";
    std::uint16_t listen_port = 9999;
    std::string   log_file    = "logs/server.log";
    std::string   log_level   = "info";

    // ---- 第二阶段：异步网络 ----
    /// io_context 工作线程数。0 = 自动取硬件并发数。
    /// 不再是"每客户端一个线程"——那套模型在客户端数上百后线程切换开销会失控，
    /// 而且线程里一旦有阻塞调用就会拖垮整个 accept 循环。
    std::uint32_t io_threads = 0;

    /// 最大并发客户端数（超过则直接拒绝连接，防止被轻易打满）
    std::uint32_t max_clients = 32;

    /// 会话空闲超时：超过这么久没收到任何包视为掉线，主动关闭。
    /// 这是心跳机制的服务端侧兜底——客户端进程被强杀时不会发 FIN，
    /// 没有这个超时，半开连接会一直占着 session 直到耗尽资源。
    std::uint32_t idle_timeout_ms = 30000;

    /// 屏幕帧最大发送频率（服务端侧限流）。
    /// 客户端请求过快时服务端做节流，避免把自己 CPU 与下行带宽打满。
    std::uint32_t screen_max_fps = 30;

    /// 是否把鼠标光标合成进抓屏画面。
    ///
    /// 为什么必须有这一步：BitBlt 只拷贝"桌面位图"的内容，而光标是系统在显示
    /// 管线末端单独合成的对象，不属于桌面 DC 的像素。不显式画进去，客户端收到的
    /// 画面里就永远没有鼠标指针——第一阶段一路继承下来就是这个表现。
    ///
    /// 留成开关有两个实际用途：一是能做 A/B 差分验证（光标回归用例正是靠它
    /// 对比"画/不画"两帧来判定，否则只能靠肉眼看）；二是需要"纯净桌面画面"时
    /// 可以关掉。
    bool capture_cursor = true;

    /// 是否让服务端进程声明"DPI 感知"。
    ///
    /// 默认 false —— DPI 不感知的进程向系统要屏幕 DC 时，拿到的是**物理画面
    /// 左上角 1:1 的裁剪**，不是"缩小的整张桌面"。
    /// **2026-09-23 实测更正**（详见 docs §6.13，三个实验一致，最后一个是
    /// 不依赖任何坐标系推理的逐像素比对：aware 的左上 1707×960 与 unaware 全帧
    /// 抽样 17280 点 0 点不同）：
    ///   本机 150% 缩放、物理屏 2560×1440 时，BitBlt 抓到 **1707×960**，
    ///   而这 1707×960 就是物理画面的左上角那一块 1:1 像素。
    /// 后果两条（**都是功能缺陷，不是"糊一点"**）：
    ///   ① 远端**只看得到桌面的左上 44%**（右边/下边各 33% 永不入帧，
    ///      任务栏在物理 y≈1392，所以帧里根本没有任务栏）；
    ///   ② `SetCursorPos`/`GetCursorInfo` **仍按 1.5 倍被虚拟化**，
    ///      与 1:1 的抓屏不在同一坐标空间 → **点击系统性偏移 1.5 倍**。
    ///      （服务端合成的光标画在帧坐标里，而 `GetCursorInfo` 也被同样虚拟化，
    ///        两个 1.5 倍互相抵消 —— 画面里的光标恰好落在指针下，把错误掩盖住了。）
    ///
    /// 打开它能同时修掉这三条，**代价是抓屏 +69%、端到端 −20%**（实测，见 §6.12）：
    /// 帧尺寸从 1.64 M 像素涨到 3.69 M，且 BitBlt 是"≈12 ms 固定 + 边际成本随像素涨"。
    /// 也就是"清晰与完整"换"帧率"。100% 缩放的机器上两者等价，不受影响。
    ///
    /// **终局方案是 DXGI**（第 1 步）：它给物理 2560×1440、与输入同一套坐标，
    /// 而且只要 ~2.2 ms（vs GDI 13.7–14.9 ms）—— 三条缺陷一起消失且更快。
    /// 在 DXGI 落地之前，本开关是唯一的兜底手段。
    bool dpi_aware = false;

    /// 【网络层】是否在 accepted socket 上关闭 Nagle（设 `TCP_NODELAY`）。
    ///
    /// 默认 **false** —— 与引入本字段之前**逐字等价**：Windows 默认 Nagle 是开的，
    /// 而本项目此前两端都没设 no_delay（`grep no_delay` 在 server/ + client/ 零命中），
    /// 所以默认值不能让既有行为发生任何变化，否则 19 项回归里那些把"loopback 上 RTT ≈ 0"
    /// 当前提的判据就会静悄悄全绿一遍、再以另一种方式静悄悄全错。
    ///
    /// 【为什么需要这个开关 —— loopback 上永远看不到的代价】
    ///   Nagle 把每个方向上**不到 MSS（~1460 B）的最后一个段**压住等 ACK，
    ///   链路 RTT ≈ 0 时这个等待 ≈ 0，于是 loopback 上"开没开 Nagle 数字完全一样"。
    ///   一旦链路有真实 RTT，那个等待会**与注入的延迟同向叠加** —— §6.29 的实测：
    ///   把中继 rtt=50 ms 插在 client ↔ server 之间、其余不动，差异帧帧周期
    ///   从 loopback 的 41 ms 涨到 **~95 ms**（净 +54 ms，几乎是整整一个 RTT）。
    ///   这条代价在 loopback 上一行日志都看不到，是真实部署才会踩的那种"测不出来"的退化。
    ///
    /// 【打开它会换来什么、失去什么】
    ///   关闭 Nagle → 小包立刻发 → 真实 RTT 下端到端帧周期变小、抖动收敛更快；
    ///   关闭 Nagle → 单方向 syscall 变多 → 服务端在大量小消息（输入/心跳/控制帧）下
    ///   CPU 与系统调用次数涨（在本机空载端到端数字上几乎看不出，但真实高并发 + 小包场景
    ///   下会成为瓶颈，这是 TCP_CORK / Nagle 当年留下来的全部理由）。
    ///   翻默认值前必须先做 A/B：在本机（loopback）+ 真实 RTT（中继）两组条件下各跑一遍
    ///   19 项回归，全绿才算站得住 —— 这是 §6.29 的反向对照条款（被记在 §6.30 的设计里）。
    ///
    /// ⚠️ 本字段只动 socket 选项，**不改协议、也不改画面** —— 与 tls_enable 同一种姿态：
    /// "默认关 = 老路径逐字等价；打开后必须能在日志里读回来"。
    bool tcp_nodelay = false;

    /// 是否启用差异帧（只传变化区域 + 脏矩形）。
    ///
    /// 默认 true。第二阶段每帧都重编整张 1707×960（编码 ~17.5 ms / 475 KB），
    /// 而桌面绝大部分时间只有光标、输入框、滚动条那一小片在变。改成只编脏区域后，
    /// 编码耗时与下行字节数都**按变化面积缩放**，静态桌面上能省掉一个数量级。
    ///
    /// 代价是引入了**有状态**的画面传递：客户端必须按顺序应用增量帧，一旦丢帧，
    /// 累积画面就会错位 —— 因此服务端会周期性强制发整帧做恢复（抓屏器内部每 60 帧一次）。
    /// 关掉它即退化为第二阶段的"每帧整屏"，用于 A/B 对照。
    bool capture_delta = true;

    /// 【输入优先抓屏】允许"输入到达"把下一次抓屏**提前**（预支下一拍的配额）。
    ///
    /// 【它治的是 2b 量出来的头号项】拉屏是"客户端收到一帧才请求下一帧"的请求-应答模型。
    /// 于是**延迟里最大的一块不是编码也不是解码，而是"输入落在两次抓屏之间白等"**：
    /// 30 fps 配置下实测空档 19.6 ms，占端到端 63.8 ms 的 31%（输入本身只花 0.25 ms
    /// 就被应用了，剩下全在等下一次抓屏）。
    ///
    /// 【等的究竟是限流、还是客户端的请求 —— 实测把这条钉死了】
    /// 服务端自己报的「请求到达 → 抓屏开始」只有 **0.03 ms**（[capture-x]），
    /// 也就是 `screen_max_fps` 的限流定时器**根本没在挡**。真正的原因在另一侧：
    /// 服务端发完一帧就空转，一直空到客户端收帧 → 贴图 → 再请求下一帧。
    /// 所以打开这个开关省下的主要是"等客户端下一次请求"那一段，
    /// **不是**"等限流时刻"（后者在当前配置下是 0）。
    /// 早先的版本把它记成"由 target_fps / screen_max_fps 的固定节拍决定"，
    /// 那是没量到 0.03 ms 之前的推断，已按实测更正（docs §6.19）。
    ///
    /// 打开后：输入一被应用、且没有抓屏在途，就立刻抓一帧发过去 —— 不等客户端的
    /// 下一次请求，也不等限流时刻。
    ///
    /// 【为什么这不等于"取消限流"】限流时刻的推进从
    ///     `now + interval`   改成   `max(next_capture_at_, now) + interval`
    /// 即"每个抓屏消耗一拍配额，提前了就把**后续**拍子按同样的量顺延"。
    /// 于是拍子的**相位**跟着输入走，而总量仍受 screen_max_fps 约束。
    /// 实测（同机 A/B/A2，见 tests/run_input_priority_check.py）：输入→显示 P50
    /// 63.8 → **35.2** ms（−40%），而服务端抓屏帧率 25.5 → 26.4 fps（**几乎不变**）——
    /// 输入触发的那一帧不是"多出来的"，它顶替掉了本来要等下一次请求的那一帧。
    /// 预支深度另有一道上限（见下面的 `input_priority_max_borrow`）：不设上限的话，
    /// 密集输入下会连着以"抓屏耗时"为周期猛抓几帧、再停一大段 —— 总量不变，画面却一跳一跳的。
    ///
    /// 默认 **true**（2026-09-24 用 A/B/A2 定版：收益 25.4 ms、开销 +8% 抓屏帧率，
    /// 且客户端丢帧/失步均为 0）。
    bool input_priority_capture = true;

    /// 【输入优先抓屏】允许预支的深度，单位 = 抓屏节拍（1000 / screen_max_fps）。
    ///
    /// 为什么必须有上限（而不是"输入一来就抓"）：限流推进用的是
    /// `max(next_capture_at_, now) + interval`，所以**长期平均帧率天然守恒**，
    /// 但瞬时可以连抓。若完全没有上限，密集输入（真实鼠标拖动是 125–1000 Hz）下会演变成
    /// "以抓屏耗时为周期猛抓几帧、再停一大段"——平均没问题，画面却一跳一跳的。
    ///
    /// 取 1 拍是权衡后的结果，两侧都推过：
    ///   · 上限 0 拍（完全不许预支）：输入来得不巧时（上一拍刚抓完、欠账还很深）根本挤不进
    ///     提前的名额，等于白装 —— 实测这就是最容易退化成"开关似乎没生效"的形态。
    ///   · 上限 3 拍：允许一次性连抓 4 帧，随后必然接一段上百毫秒的停顿。
    /// 1 拍的效果：最多连抓 3 帧（间隔 ≈ 抓屏耗时），之后停不超过 2 个节拍。
    ///
    /// 0 是合法值（= 只在"欠账已经落到过去"时才提前抓），用来量"预支到底值多少"。
    /// ⚠️ 改这个数**必须先做 A/B**（改的是"节奏"这类肉眼可见的东西，本项目规矩）。
    int input_priority_max_borrow = 1;

    /// 抓屏后端（第 1 步）。
    ///
    /// "auto" —— **默认值**。首选 DXGI；不可用时记 WARN 后回退 GDI（不静默）。
    /// "dxgi" —— 只要 DXGI。初始化失败即**启动失败**，不静默降级。
    /// "gdi"  —— 强制老路径（BitBlt）。跑 A/B 对照、或需要"与上一阶段逐字节一致"时用。
    ///
    /// 【为什么默认是 auto（2026-09-23，用户拍板）】
    ///   在非 100% 缩放下（本机 150%），GDI 路径是一个**功能缺陷**而不是取舍：
    ///   远端只能看到桌面左上 44%，且输入坐标与画面不在同一空间 → 点击系统性偏移 1.5 倍。
    ///   DXGI 把两条一起修掉（§6.13 / §6.15 实测），所以"默认 gdi"等于把已知缺陷当默认行为。
    ///   auto 让新机器自动拿到"画面完整 + 坐标一致"，而 DXGI 用不了的老环境仍能work，
    ///   代价只是多一行 WARN。
    ///
    /// 【为什么是 auto 而不是 dxgi】
    ///   dxgi 在 DXGI 不可用（已有别的进程占着 duplication / RDP / 无物理输出）时**拒绝启动**，
    ///   那会让本来能用 GDI 跑起来的机器彻底起不来。auto 只是"能力探测"。
    ///
    /// 【降级必须显式可见】
    ///   静默回退会让人以为 DXGI 在跑，于是把 1707×960 当成 DXGI 的产出 ——
    ///   整轮 A/B 的结论就此作废，而日志上一切正常。本项目已在"配置静默失效"上栽过三次
    ///   （§8.6 降采样、§8.12 夹具出画、§6.14 自称 unaware 实则 aware），所以这条不让步。
    ///
    /// 【翻默认值的代价（实测，别忘）】
    ///   dxgi 的帧是 **2560×1440（物理像素）= GDI 1707×960 的 2.25 倍**，
    ///   且抓屏与输入都走物理坐标。所以：默认带宽上行、以及"回归矩阵实际跑的是哪个后端"
    ///   都变了 —— 各测试**必须显式写明自己要哪个后端**，不能吃默认值（见 tests/ 里的
    ///   `capture_backend` 显式注入）。
    ///
    /// 【两个后端的差别（§6.13 / §6.14 实测）】
    ///   gdi ：物理画面左上角 1:1 的 1707×960（远端只见 44%），且输入坐标被虚拟化
    ///         ×1.5 → 点击系统性偏移；抓屏 ≈ 12 ms 固定 + 0.47 GB/s 边际。
    ///   dxgi：物理 2560×1440，且尺寸**不随调用方 DPI 上下文变化**（12/12 次一致）
    ///         → 覆盖与坐标两个缺陷一起消失；读回 2.0–2.5 ms。
    std::string capture_backend = "auto";

    // ---- 诊断：反向对照用的"故意泄漏"开关（默认 0 = 不泄漏）----
    /// 每个抓屏帧**故意泄漏**这么多个 GDI 对象（`CreateCompatibleDC(nullptr)` 后不删）。
    ///
    /// 【为什么这不是"脏代码"，而是本项目的一贯做法】
    ///   一条"资源不泄漏"的判据最怕的是它**本来就看不见泄漏** —— 那样它永远报绿，
    ///   而绿没有信息量。所以必须先能**造出**一个确定的泄漏，证明判据抓得到
    ///   （同 `debug_stall_every_n` / `debug_decode_stall_every_n` / `paint_dump_*`）。
    ///   —— 这是"诊断开关必须能造出来，才谈得上验"的落地。
    ///
    /// 【量级为什么是"2"】
    ///   历史上真出过的那个 bug 就是**每帧漏 2 个**（`GetIconInfo` 新建的掩码 + 彩色位图
    ///   没归还，见 capture_internal.hpp 的 `composite_system_cursor`）。所以反向对照
    ///   用同样的量级，测的是"判据能不能看见**真实发生过的那种**泄漏"。
    ///
    /// 【怎么用】
    ///   `tests/run_soak_check.py --reverse-control` 把它设成 2、只跑 1~2 分钟；
    ///   判据**必须报不通过**（GDI 斜率 > 0）。抓不到 ⇒ 这条判据是死的。
    ///
    /// ⚠️ 泄漏速率 = 本值 × 抓屏 fps。30 fps × 2 = 60 个/秒 ⇒ 进程 GDI 配额
    ///    （默认 10000/进程）约 3 分钟耗尽，此后创建失败、泄漏**自行停止**并在
    ///    日志里表现为"创建失败"。所以**别看它涨到天上去**，反向对照跑 1~2 分钟足够。
    ///
    /// 0 = 关闭（默认）。默认值必须与引入本字段之前**逐字等价** —— 这是回归不受影响的前提。
    std::uint32_t debug_leak_gdi_per_frame = 0;

    // ---- 2B：认证 ----
    /// 共享密钥。**空 = 不启用认证**（与本字段存在之前的行为逐字一致）。
    ///
    /// 【为什么用"配置为空即关闭"而不是再加一个 `auth_required` 开关】
    /// 两个字段会造出一个自相矛盾的状态（`required=true` 但 token 为空），
    /// 那种状态需要第三段代码解释它，而"到底听谁的"会成为下一个人的坑。
    /// 单一字段不会自相矛盾。
    ///
    /// ⚠️ 代价是失败方向偏"开"（fail-open）：忘了配 = 不认证。所以**启动时必须把
    ///    生效状态打出来**（见 server/main.cpp 的启动日志），让"以为开了其实没开"可见 ——
    ///    这是本项目 §6.12 / §8.22.2 立下的规矩：配了就要能自证生效。
    ///
    /// ⚠️ 本字段只解决"谁能连上这个端口"，它**明文传输**（TLS 是 2B 的第二刀）。
    std::string auth_token;

    // ---- 2B：第三刀 权限模型（谁可连、可否只读）----
    /// 客户端授权表。**空 = 不启用权限模型**，回落到上面 `auth_token` 的单凭据路径
    /// （该路径下角色恒为 control，行为与本字段存在之前**逐字等价**）。
    ///
    /// 【三层回落，每一层都必须是"老配置不受影响"】
    ///   ① `auth_clients` 非空  -> 按表鉴权，得到 name + role；`auth_token` 被忽略
    ///      （两者同时配属于配置错误，解析期直接拒绝 —— 见 load_server_config）；
    ///   ② `auth_clients` 空、`auth_token` 非空 -> 老路径：单密钥、角色恒 control；
    ///   ③ 两者都空 -> 不认证、角色 control。**这是默认配置，回归 19 项全走这条**。
    ///
    /// ⚠️ 第 ③ 层是 fail-open（忘配 = 不设防）。这是刻意的：默认值不能让既有行为
    ///    发生任何变化。所以启动日志必须把生效层级喊出来（见 server/main.cpp）。
    std::vector<AuthClientEntry> auth_clients;

    // ---- 2B：第三刀 审计日志 ----
    /// 是否写审计日志（独立于调试日志的、只追加的、机器可解析的事件流）。
    ///
    /// 默认 **false** —— 与引入本字段之前**逐字等价**：不建文件、不占任何开销。
    /// 这是回归不受影响的前提（19 项里没有一项期待多出一个文件）。
    ///
    /// 【为什么审计不并进调试日志】
    ///   调试日志是**给人看的**：级别会调、格式会变、内容会随排障需要增删。
    ///   审计是**给人查的**：需要稳定字段、需要只追加、需要"发生过什么"完整。
    ///   混在一起的两个后果都很实际：① 日志级别一调（例如调成 warn），
    ///   `auth_ok` 这类 info 事件**整批消失**；② 调试行里随便插一个字段就可能
    ///   打断审计解析。所以独立 sink、独立级别、自己的文件。
    bool audit_enable = false;

    /// 审计日志文件路径（按运行目录解析，与 log_file 同规矩）。
    std::string audit_log_file = "logs/audit.log";

    // ---- 2B：TLS（第二刀，加密整条链路）----
    /// 是否启用 TLS。
    ///
    /// 默认 **false** —— 与引入本组字段之前的行为**逐字一致**，这是回归 17 项不受影响的
    /// 前提（同 `auth_token` 为空即不启用那条思路：默认值不能让既有行为发生任何变化）。
    bool tls_enable = false;

    /// 服务端证书 / 私钥（PEM）。启用 TLS 而文件缺失时，若 `tls_auto_self_signed` 为真，
    /// 则**自签一套写入这两个路径**（见 tls.hpp 的证书模型说明）。
    /// 路径按运行目录解析（与 config 本身一样），所以默认值写成 config/ 下。
    std::string tls_cert_file = "config/server_cert.pem";
    std::string tls_key_file  = "config/server_key.pem";

    /// 允许在证书缺失时自动生成自签名证书。默认 true（自用工具的最省事路径）。
    ///
    /// 设为 false 时：文件缺失 = **拒绝启动**，而不是静默换一张新证书 ——
    /// 后者会让所有已 pin 的客户端突然连不上，而日志上看不出发生过什么。
    bool tls_auto_self_signed = true;
};

struct ClientConfig {
    std::string   server_host = "127.0.0.1";
    std::uint16_t server_port = 9999;
    std::string   log_file    = "logs/client.log";
    std::string   log_level   = "info";

    // ---- 第二阶段：心跳与重连 ----
    /// 心跳发送间隔
    std::uint32_t heartbeat_interval_ms = 2000;
    /// 心跳应答超时：超过这个时间没收到 Pong 就判定链路已死并重连。
    /// 注意它必须大于 heartbeat_interval_ms，否则会误判。
    std::uint32_t heartbeat_timeout_ms = 6000;

    /// 握手超时：TCP 连上并发出 Hello 之后，超过这个时间还没收到 HelloAck
    /// 就判定对端不可用（不是本协议、或已经僵死），主动断开并进入重连。
    ///
    /// 为什么要单独一条：心跳看门狗在 kHandshaking 阶段是刻意跳过的
    /// （避免把"还没握完手"误判成掉线），于是"连上但不回话"的对端会让客户端
    /// 永远停在握手中 —— 不重连、不提示，界面上只有一个白窗口。
    /// 这个坑本项目实测撞过一次，所以握手阶段必须有独立的时间上限。
    std::uint32_t hello_timeout_ms = 5000;

    /// 重连退避：首次延迟、上限、倍数（指数退避，带抖动）
    std::uint32_t reconnect_initial_delay_ms = 500;
    std::uint32_t reconnect_max_delay_ms     = 10000;
    /// 最大重连尝试次数，0 = 无限重试
    std::uint32_t reconnect_max_attempts     = 0;

    /// 屏幕拉取目标帧率（拉屏模型下，收到一帧才请求下一帧，这个值给上限）
    std::uint32_t target_fps = 30;

    /// 【整帧优先通道】整帧（关键帧）不再排进增量队列，而是走一条**溢出清不掉**的通道。
    ///
    /// 【为什么必须这样】
    ///   差异帧是**接力**的：中间少一帧，后面全错位。恢复手段是服务端周期性发的整帧。
    ///   可整帧原本和增量帧挤在**同一条队列**里，而队列满了是**整段清空**的 ——
    ///   于是"越需要整帧的时候，它越容易被一起丢掉"（实测最长一次冻结 3795 ms，
    ///   远超"关键帧间隔 60 帧 × 37 ms ≈ 2.2 s"的推算，见 docs §8.17）。
    ///   客户端持续跟不上时，resync 可能**长期收敛不了** —— 画面一直冻着。
    ///
    /// 【为什么这样做是安全的】
    ///   整帧是**完整画面**，排在它前面的增量帧全部被它整体取代（贴 N 个小块再贴一张整图
    ///   = 直接贴那张整图）。所以"跳过若干排队的增量帧、直接用整帧"不丢任何信息，
    ///   只是省掉了中间过程 —— 而在落后的场景下，那些中间过程本来就没人看得到。
    ///
    /// 关掉它就退回旧行为（整帧和增量帧同队列），用来做 A/B 反向对照：
    /// **不改坏一次，就不知道原来看起来正常的链路里真的会丢整帧。**
    bool keyframe_priority = true;

    // ------------------------------------------------------------------------
    // 【UI 绘制】整帧缩到窗口客户区时用的 GDI StretchBlt 模式
    // ------------------------------------------------------------------------
    /// 取值："halftone"（GDI 高质量插值）| "coloroncolor"（删行删列，最近邻）
    ///
    /// 【为什么会有这个配置项】把 2026-09-24 的实测摆出来就清楚了：
    /// 客户端 `[paint]` 行显示 2560×1440 → 1002×664 这一次缩放
    ///   · 消息投递 + WM_PAINT 调度：**0.1 ms**
    ///   · StretchBlt 本身：**12.2 ms**（P95 13.5 / max 13.8，分布极窄）
    /// 也就是说此前被叫作"UI 消息调度"的那 12.6 ms，
    /// **几乎全部是这次软件插值缩放**，不是等待调度。它是每帧都要付一次的稳定开销
    /// （约占 40 ms 帧周期的 30%）。
    ///
    /// 【为什么能当开关】它是一个**真实的用户权衡**：HALFTONE 缩小的文字/细线干净，
    /// 但每帧 12 ms；COLORONCOLOR 快得多（最近邻），代价是缩小后文字容易断线。
    /// 而且按本项目规矩，"改画质/节奏这类肉眼可见的东西必须先 A/B" ——
    /// 只有做成开关才可能在同一台机器上对着比。
    ///
    /// ⚠️ 默认值保持 "halftone"（与引入本配置项之前的行为**逐字等价**）。
    /// 要翻默认值必须先跑 A/B 拿到数据，并**重跑全套回归**（改默认值就等于改了
    /// 所有回归项实测的东西 —— docs §8.22.3）。
    std::string stretch_mode = "halftone";

    // ------------------------------------------------------------------------
    // 【UI 绘制】是否只重绘**变化的那一块**（按脏区重绘）
    // ------------------------------------------------------------------------
    /// 默认开。
    ///
    /// 【它省的是什么】客户端的 `image_` 永远是**完整**的累积画面（增量帧用 1:1 BitBlt
    /// 贴进去），所以"按脏区重绘"动的不是合成，而是 `WM_PAINT` 那一次
    /// `StretchBlt` 的**落笔范围**：原来每帧整窗失效 ⇒ 2560×1440 → 1002×664
    /// 那 12.2 ms 全额付出；而脏区本来就在手里（协议每帧带 1 个 rect）。
    ///
    /// 【为什么代价为零】§6.20 的对比图回答的是**采样模式**（halftone vs coloroncolor）
    /// 的画质差异；本项**不改采样模式**，画的仍是同一次缩放，只是不画没变的地方。
    /// 唯一需要证的是"没变的地方确实没变"—— 见下面的光晕注释与 §6.25 的判据。
    ///
    /// 【为什么实现了也留着开关】它是"每帧都要付"的开销，属于必须能 A/B 的东西；
    /// 而且判据要能在"关掉时确实退回旧行为"上做反向对照。
    bool partial_repaint = true;

    // ------------------------------------------------------------------------
    // 【第 2 步 2b】输入→显示延迟的夹具与前提
    //
    // 这一组**全是诊断项**，默认值就是生产行为（自动源关、输入照常转发）。
    // ------------------------------------------------------------------------

    /// 自动输入源：每 interval_ms 毫秒发一次鼠标移动，在两个目标点之间来回。
    /// 0 = 关闭（生产路径）。
    ///
    /// 【为什么判据必须自带输入源，而不是让人去动鼠标】
    ///   延迟判据的核心是**对照**（改前/改后、或两个配置）。靠人工动鼠标，两轮的
    ///   输入节奏必然不同，于是"延迟变了"分不清是代码变了还是手速变了。
    ///   这与 §8.18 是同一条纪律：**夹具必须自带信号源，不能依赖环境恰好提供信号。**
    std::uint32_t auto_input_interval_ms = 0;

    /// 自动源的两个目标点，用**远端画面的比例**（0..1）表示，纵坐标共用。
    /// 用比例而不是绝对像素：换一台分辨率不同的机器，夹具不用改；
    /// 而且坐标天然落在屏内 —— 出屏会被系统 clamp，于是服务端"读回的位置"与
    /// "请求的位置"不符，那条前置不变式（坐标读回必须一致）会大面积失败。
    ///
    /// 为什么要来回两个点：鼠标停在原地时 SetCursorPos 不产生任何可见变化，
    /// "输入被看见了"这件事就无从验证。每次输入都对应一次真实的光标位移才有意义。
    double auto_input_x0 = 0.3;
    double auto_input_x1 = 0.7;
    double auto_input_y  = 0.5;

    /// 是否把本地鼠标/键盘消息转发到远端（默认 true = 生产行为）。
    ///
    /// 【关掉它的唯一用途】切断**同机自测的输入回灌闭环**。
    ///   服务端按远端坐标调 SetCursorPos —— 在两机部署时移动的是远端那台机器的光标，
    ///   与客户端毫无关系；但在**同一台机器**上自测时，它移动的正是本机的物理光标。
    ///   那个光标一旦落到客户端窗口上，客户端就会收到 WM_MOUSEMOVE、把它当用户操作
    ///   再发给服务端，于是输入流里混进一路既不可控也不可预期的反馈。
    ///   关掉它 = 还原两机部署的真实拓扑，而不是绕过被测链路：
    ///   自动输入源走 net_.send_mouse()，与这条开关毫无交集。
    bool input_forwarding = true;

    // ---- 诊断（默认关闭，只给回归脚本用）----
    /// 累积画面落盘（前缀）。
    ///
    /// 为什么需要它：差异帧的正确性没法靠"看着差不多"来判——增量贴歪了、贴反了、
    /// 少贴一块，画面**依然"有图像"**，肉眼很可能看不出来，但它已经错了，而且不会自愈。
    /// 唯一能钉死它的办法，是把客户端**真实**的累积画面取出来，与服务端的整帧逐像素对照。
    ///
    /// 触发时机很讲究：客户端会在**收到整帧（关键帧）的那一刻**同时落下两份图 ——
    ///   <前缀>_accum.png  这一帧应用**之前**的累积画面（由前面若干个增量帧拼出来的）
    ///   <前缀>_key.png    服务端刚发来的整帧
    /// 两者只差一个帧周期（约 40 ms），所以"桌面在这段时间自己变了多少"这个干扰项
    /// 被压到最小。如果改成"客户端落盘、再由另一个进程稍后去抓一张整帧"，
    /// 两者要差半秒以上，浏览器滚动一下就是十几个百分点的假差异 —— 这是实测踩过的坑。
    /// 默认空 = 不写任何文件。
    std::string   dump_frame_path;
    /// 至少应用过多少帧之后才允许落盘（保证累积画面确实是由增量帧拼出来的）。
    /// 要明显大于服务端关键帧间隔（60），否则可能刚连上就落盘、什么都没验证。
    std::uint32_t dump_frame_after = 0;

    /// 【诊断】把"窗口实际画出来的内容"与"同一时刻整幅重绘的参考"成对落盘
    ///   <前缀>_paint_N.png  窗口客户区当前内容（= 裁剪之后真正落笔的结果）
    ///   <前缀>_ref_N.png    用**同一份 image_**、在**同一个临界区**里做的整幅缩放
    /// 两者只差"这次的裁剪区"这一件事 ⇒ **逐像素相等**正是"按脏区重绘没留残影"。
    /// 为什么非要客户端自己落盘：换一个进程稍后去抓窗口，时间间隔会引入假差异（§8.12）。
    /// 默认空 = 不写任何文件。
    std::string   paint_dump_path;
    /// 画过多少次之后才开始落（避开刚显示时那段整窗失效）。
    std::uint32_t paint_dump_after = 20;

    /// 【诊断】失效矩形四边外扩的像素数（见 RemoteWindow::set_invalidate_halo）。
    /// 默认 2（实测需要 1，取 2 是往安全侧留余量）。**负值**会把失效矩形缩进，
    /// 一定会留下残影 —— 那是判据的反向对照，不是可用配置。
    int           invalidate_halo_px = 2;

    // ---- 诊断：制造"抖动"与"丢帧"（第三阶段第 2 步，默认 0 = 全关）----
    //
    // 【为什么这两件事需要一个开关才能测】
    //   延迟维度的判据有两类，它们的"被测现象"在本机回环上**几乎不会自然发生**：
    //     · 抖动：回环 + 静止桌面下到达间隔非常整齐，P95 与 P50 差不出一个像素；
    //     · 冻结：TCP 回环不丢包，而 resync 只在丢帧时才被走到。
    //   一个从没被走到过的路径 = 一行从没被执行过的代码。要验它，必须先能造出来。
    //   所以这两个开关的作用不是"功能"，而是**让判据有东西可判**（也就是反向对照）。

    /// 【诊断】每 N 帧让**解码线程**停顿 X ms（0 = 关闭）。
    ///
    /// 用来制造**真实的队列溢出**：解码跟不上 → `pending_frames_` 攒到 8 帧上限 →
    /// 客户端自己整段丢掉、转等下一个整帧（resync）。这正是"seq 缺口"的**唯一**
    /// 真实来源。
    std::uint32_t debug_decode_stall_every_n = 0;
    std::uint32_t debug_decode_stall_ms      = 0;

    /// 【为什么不做一个"每 N 帧直接丢一帧"的开关 —— 第一版就是这么写的，实测失败】
    ///
    /// 第一版在 `on_frame` 最顶部直接 `return` 掉一帧，以为这样就能造出 seq 缺口。
    /// 实测 12 秒丢了 6 帧（开关确实生效，日志里也打了 WARN），可是客户端
    /// **一次缺口都没检测到**：`失步 0`、`丢弃 0`、冻结 0，整轮判据无从判定。
    ///
    /// 原因在**序号是谁生成的**：`pf.seq = ++frame_seq_next_` 由**客户端自己**编，
    /// 只在"帧被真正投递进队列"时才自增。在投递之前把帧丢掉，等于"这一帧从未存在过"，
    /// 序号自然连续。所以客户端能发现的只有"**投递过、又被整段清掉**"（= 队列溢出），
    /// 而不是"链路上少了一帧"。
    ///
    /// 顺带钉住了一件值得写进文档的事：**本协议的"帧序号"覆盖不了链路丢帧**。
    /// 在 TCP 上这不要紧（不会静默丢包），但一旦换成 UDP、或中间加了会丢帧的一层，
    /// **现有校验完全发现不了画面错位** —— 那时必须让服务端把序号带上。
    /// 这次是"我们想造一个故障，才发现自己的检测机制根本看不见它"。
    ///
    /// 所以开关改成"让解码线程变慢"：让被测代码走**它自己的溢出路径**
    /// （`pending_frames_.clear()` + 序号校验），而不是在旁边插一条只有测试才走的旁路。

    /// 【诊断】每 N 帧在解码完成后停顿 X ms（0 = 关闭）。
    ///
    /// 用来**制造抖动**，从而验证抖动判据真的能把它判出来。拉屏模型下客户端处理慢
    /// 会推迟下一次请求 → 服务端晚发 → 到达间隔变大，所以它直接抬高 gap 分布。
    ///
    /// 必须是"每 N 帧一次"而不是"每帧都停"：固定停顿只是把整个分布平移
    /// （P50 和 P95 一起涨，比值不变），只有**间歇性**停顿才会造出
    /// "P50 正常、P95 爆炸"那种真实抖动该有的形状。
    std::uint32_t debug_stall_every_n = 0;
    std::uint32_t debug_stall_ms      = 0;

    // ---- 2B：认证 ----
    /// 与服务端 `auth_token` 一致的共享密钥（空 = 不提供凭据）。
    ///
    /// 服务端要求认证而这里为空 / 不匹配时，服务端会明确回一条拒绝理由
    /// （"authentication required" / "authentication failed"），客户端把它显示出来，
    /// 并把这次失败当成**不可重试** —— 凭据不对，重连一万次结果一样。
    std::string auth_token;

    // ---- 2B：TLS（第二刀，加密整条链路）----
    /// 是否启用 TLS。默认 false（与老行为**逐字一致**）。
    /// 必须与服务端一致：客户端开、服务端没开 ⇒ 握手在第一帧就失败。
    bool tls_enable = false;

    /// 期望的服务端证书 SHA-256 指纹（冒号、大小写、空格随意写）。
    ///
    /// 从哪来：**服务端启动日志会打印它自己证书的指纹**，抄过来即可。
    ///
    /// 空字符串 = 只加密、**不认证对端** —— 防得住链路上的窃听，**防不住中间人**
    /// （攻击者可以拿自己的证书来冒充服务端）。本项目不允许"看起来安全"，
    /// 所以这种配置在客户端启动时会用 WARN 大声报出来。判据里一律用非空 pin。
    std::string tls_pin_sha256;

    /// 【网络层】是否在 connected socket 上关闭 Nagle（设 `TCP_NODELAY`）。
    ///
    /// 默认 **false** —— 与引入本字段之前**逐字等价**（同上 tcp_nodelay 服务端字段的说明）。
    /// 这条字段的真正代价同样在真实 RTT 下才显形；loopback 上开不开数字一模一样。
    /// 要翻默认值前先做 §6.29 / §6.30 设计的两组 A/B（loopback + 中继 rtt=50），
    /// 全绿才算站得住。
    bool tcp_nodelay = false;

    // ---- 2B 第三刀：权限模型（客户端侧）----
    /// 【诊断，默认 false = 生产行为】服务端下发 `role=view` 时，**照常把输入发上去**。
    ///
    /// 【为什么必须有这个开关 —— 否则判据证明不了它以为自己证明的事】
    ///   只读会话的输入有**两道**处置：客户端自律（知道自己只读，就不发）与服务端拦截。
    ///   如果只有前者，"服务端真的在拦"这件事就**永远没有被走到过** ——
    ///   而一条从没被走到的代码路径等于没有。判据若只看"服务端 `input_denied` 没涨"，
    ///   那在"客户端压根没发"时也会绿 —— 那是**用客户端的自律冒充服务端的安全**。
    ///   打开本开关 = 让探针假装自己不知道自己是只读 ⇒ 把服务端那道拦截**单独暴露出来**。
    ///   同 `debug_stall_*` / `debug_leak_gdi_per_frame` 那一族：诊断开关唯一的用途是
    ///   让判据有东西可判。
    ///
    /// ⚠️ 打开它**不会**让输入真的生效（服务端照样拒）—— 它只是把请求发出去。
    ///    生产路径永远是 false。
    bool debug_ignore_role = false;
};

inline spdlog::level::level_enum log_level_from_string(const std::string& s) {
    if (s == "trace")         return spdlog::level::trace;
    if (s == "debug")         return spdlog::level::debug;
    if (s == "warn" || s == "warning") return spdlog::level::warn;
    if (s == "error")         return spdlog::level::err;
    if (s == "off")           return spdlog::level::off;
    return spdlog::level::info;
}

namespace detail {

inline nlohmann::json read_json(const std::string& path) {
    std::ifstream f(path);
    if (!f.is_open()) {
        return nlohmann::json::object(); // 配置缺失 -> 全部使用默认值
    }
    try {
        nlohmann::json j;
        f >> j;
        return j;
    } catch (const std::exception&) {
        throw std::runtime_error("JSON config parse failed: " + path);
    }
}

inline std::uint16_t to_port(int v) {
    if (v <= 0 || v > 65535) {
        throw std::runtime_error("invalid port number: " + std::to_string(v));
    }
    return static_cast<std::uint16_t>(v);
}

} // namespace detail

inline ServerConfig load_server_config(const std::string& path) {
    const auto        j = detail::read_json(path);
    ServerConfig      c;
    c.listen_host = j.value("listen_host", c.listen_host);
    c.listen_port = detail::to_port(j.value("listen_port", static_cast<int>(c.listen_port)));
    c.log_file    = j.value("log_file", c.log_file);
    c.log_level   = j.value("log_level", c.log_level);

    c.io_threads          = j.value("io_threads", c.io_threads);
    c.max_clients         = j.value("max_clients", c.max_clients);
    c.idle_timeout_ms     = j.value("idle_timeout_ms", c.idle_timeout_ms);
    c.screen_max_fps      = j.value("screen_max_fps", c.screen_max_fps);
    // 【网络层 Nagle】默认关 = 与引入本字段之前逐字等价（Windows 默认 Nagle 开；
    // 产品两端此前都没设 no_delay）。打开的代价/收益见字段注释。
    c.tcp_nodelay         = j.value("tcp_nodelay", c.tcp_nodelay);
    c.capture_cursor      = j.value("capture_cursor", c.capture_cursor);
    c.dpi_aware           = j.value("dpi_aware", c.dpi_aware);
    c.capture_delta       = j.value("capture_delta", c.capture_delta);
    c.capture_backend     = j.value("capture_backend", c.capture_backend);
    // 【诊断】反向对照用的故意泄漏，默认 0（见字段注释）。上限设个门槛纯粹是为了
    // 挡住手滑写出 2000 这种把配额瞬间打满的值 —— 那会让判据的"斜率"根本没机会成形。
    c.debug_leak_gdi_per_frame =
        j.value("debug_leak_gdi_per_frame", c.debug_leak_gdi_per_frame);
    if (c.debug_leak_gdi_per_frame > 64) {
        throw std::runtime_error("debug_leak_gdi_per_frame too large (max 64)");
    }
    c.input_priority_capture = j.value("input_priority_capture", c.input_priority_capture);
    c.input_priority_max_borrow =
        j.value("input_priority_max_borrow", c.input_priority_max_borrow);
    // 【2B 认证】空 = 不启用（见字段注释）。这里不做"太短就拒绝启动"之类的校验：
    // 凭据强度是部署方的判断，工具不该替他定长度门槛 —— 但启动日志会如实报出状态。
    c.auth_token          = j.value("auth_token", c.auth_token);

    // 【2B 第三刀 权限模型】授权表。
    // 每一行都在解析期校验 —— 理由是这一族配置的失效形态**全是静默的**：
    // 拼错 role 会被当成默认值、空 token 会让那一行永远匹配不上（= 该设备连不上，
    // 而日志里只显示"认证失败"，看不出是配置写错）。宁可启动失败，也不要静默建一张
    // 和用户想的不一样的表。
    if (j.contains("auth_clients") && !j.at("auth_clients").is_null()) {
        const auto& arr = j.at("auth_clients");
        if (!arr.is_array()) {
            throw std::runtime_error("auth_clients must be an array");
        }
        std::size_t idx = 0;
        for (const auto& e : arr) {
            if (!e.is_object()) {
                throw std::runtime_error("auth_clients[" + std::to_string(idx) +
                                         "] must be an object {name, token, role}");
            }
            AuthClientEntry ent;
            ent.name = e.value("name", std::string());
            ent.token = e.value("token", std::string());
            ent.role = e.value("role", std::string("control"));
            if (ent.name.empty()) {
                throw std::runtime_error("auth_clients[" + std::to_string(idx) +
                                         "].name is empty（审计日志靠它区分是谁）");
            }
            if (ent.token.empty()) {
                throw std::runtime_error("auth_clients[" + std::to_string(idx) +
                                         "].token is empty（空 token 永远匹配不上，"
                                         "会让这台设备连不上而日志只显示\"认证失败\"）");
            }
            if (ent.role != "control" && ent.role != "view") {
                throw std::runtime_error("auth_clients[" + std::to_string(idx) +
                                         "].role must be \"control\" or \"view\" (got \"" +
                                         ent.role + "\")");
            }
            c.auth_clients.push_back(std::move(ent));
            ++idx;
        }
    }

    // 两张表同时配 = 自相矛盾：鉴权时"到底听谁的"没有正确答案，而无论选哪边都会
    // 让另一半静默失效。所以直接拒绝（与"不加 auth_required 开关"那段的理由同源）。
    if (!c.auth_clients.empty() && !c.auth_token.empty()) {
        throw std::runtime_error(
            "auth_clients 与 auth_token 不能同时配置 —— auth_clients 非空时按表鉴权，"
            "auth_token 会被静默忽略；请只留一个（单凭据就把 auth_token 清空）");
    }
    // 同一把密钥出现两次 = 身份无法归因（审计里会出现两条同名不同意的记录）。
    for (std::size_t a = 0; a < c.auth_clients.size(); ++a) {
        for (std::size_t b = a + 1; b < c.auth_clients.size(); ++b) {
            if (c.auth_clients[a].token == c.auth_clients[b].token) {
                throw std::runtime_error(
                    "auth_clients 里有重复的 token（\"" + c.auth_clients[a].name + "\" 与 \"" +
                    c.auth_clients[b].name + "\"）—— 凭据重复会让身份无法归因");
            }
        }
    }

    // 【2B 第三刀 审计日志】默认关 = 不建文件、零开销（见字段注释）。
    c.audit_enable   = j.value("audit_enable", c.audit_enable);
    c.audit_log_file = j.value("audit_log_file", c.audit_log_file);
    if (c.audit_enable && c.audit_log_file.empty()) {
        throw std::runtime_error("audit_enable=true 但 audit_log_file 为空");
    }
    // 【2B TLS】默认关。这里的校验只做"形态"层面的（文件是否存在交给启动期，
    // 因为路径是按运行目录解析的，配置解析时未必是最终的工作目录）。
    c.tls_enable           = j.value("tls_enable", c.tls_enable);
    c.tls_cert_file        = j.value("tls_cert_file", c.tls_cert_file);
    c.tls_key_file         = j.value("tls_key_file", c.tls_key_file);
    c.tls_auto_self_signed = j.value("tls_auto_self_signed", c.tls_auto_self_signed);
    if (c.tls_enable && c.tls_cert_file.empty()) {
        throw std::runtime_error("tls_enable=true 但 tls_cert_file 为空");
    }
    if (c.tls_enable && c.tls_key_file.empty()) {
        throw std::runtime_error("tls_enable=true 但 tls_key_file 为空");
    }

    // 配置错误快速失败：这些约束被违反时，运行期行为会变得难以解释
    if (c.io_threads > 64)          throw std::runtime_error("io_threads too large (max 64)");
    if (c.max_clients == 0)         throw std::runtime_error("max_clients must be > 0");
    if (c.screen_max_fps == 0)      throw std::runtime_error("screen_max_fps must be > 0");
    // 拼错的后端名必须当场拒绝。若只是"不认识就默默用 GDI"，那么一次手误就会让
    // 整轮 A/B 变成"GDI vs GDI"，而日志里那个 capture_backend 字段看起来完全正常。
    if (c.capture_backend != "gdi" && c.capture_backend != "dxgi" && c.capture_backend != "auto") {
        throw std::runtime_error("capture_backend must be one of: gdi | dxgi | auto (got \"" +
                                 c.capture_backend + "\")");
    }
    return c;
}

inline ClientConfig load_client_config(const std::string& path) {
    const auto   j = detail::read_json(path);
    ClientConfig c;
    c.server_host = j.value("server_host", c.server_host);
    c.server_port = detail::to_port(j.value("server_port", static_cast<int>(c.server_port)));
    c.log_file    = j.value("log_file", c.log_file);
    c.log_level   = j.value("log_level", c.log_level);

    c.heartbeat_interval_ms      = j.value("heartbeat_interval_ms", c.heartbeat_interval_ms);
    c.heartbeat_timeout_ms       = j.value("heartbeat_timeout_ms", c.heartbeat_timeout_ms);
    c.hello_timeout_ms           = j.value("hello_timeout_ms", c.hello_timeout_ms);
    c.reconnect_initial_delay_ms = j.value("reconnect_initial_delay_ms", c.reconnect_initial_delay_ms);
    c.reconnect_max_delay_ms     = j.value("reconnect_max_delay_ms", c.reconnect_max_delay_ms);
    c.reconnect_max_attempts     = j.value("reconnect_max_attempts", c.reconnect_max_attempts);
    c.target_fps                 = j.value("target_fps", c.target_fps);
    c.keyframe_priority          = j.value("keyframe_priority", c.keyframe_priority);
    // 【网络层 Nagle】默认关 = 老路径逐字等价；解释见 ServerConfig::tcp_nodelay。
    c.tcp_nodelay                = j.value("tcp_nodelay", c.tcp_nodelay);
    c.stretch_mode               = j.value("stretch_mode", c.stretch_mode);
    c.partial_repaint            = j.value("partial_repaint", c.partial_repaint);
    c.auto_input_interval_ms     = j.value("auto_input_interval_ms", c.auto_input_interval_ms);
    c.auto_input_x0              = j.value("auto_input_x0", c.auto_input_x0);
    c.auto_input_x1              = j.value("auto_input_x1", c.auto_input_x1);
    c.auto_input_y               = j.value("auto_input_y", c.auto_input_y);
    c.input_forwarding           = j.value("input_forwarding", c.input_forwarding);
    c.dump_frame_path            = j.value("dump_frame_path", c.dump_frame_path);
    c.dump_frame_after           = j.value("dump_frame_after", c.dump_frame_after);
    c.paint_dump_path            = j.value("paint_dump_path", c.paint_dump_path);
    c.paint_dump_after           = j.value("paint_dump_after", c.paint_dump_after);
    c.invalidate_halo_px         = j.value("invalidate_halo_px", c.invalidate_halo_px);

    // 【2B 认证】空 = 不提供凭据（见字段注释）
    c.auth_token                 = j.value("auth_token", c.auth_token);

    // 【2B 第三刀 权限模型】诊断开关，默认 false = 生产行为（见字段注释）。
    c.debug_ignore_role          = j.value("debug_ignore_role", c.debug_ignore_role);

    // 【2B TLS】默认关。
    c.tls_enable                 = j.value("tls_enable", c.tls_enable);
    c.tls_pin_sha256             = j.value("tls_pin_sha256", c.tls_pin_sha256);
    // 指纹写错必须**当场拒绝启动**：它的失效形态是"握不上手"，而原因看起来像
    // "网络/服务端有问题" —— 一个必然失败的配置不该被带到运行期（同 capture_backend
    // 拼错就拒绝那条思路）。长度按"去掉分隔符后 64 个十六进制位"判。
    if (c.tls_enable && !c.tls_pin_sha256.empty()) {
        std::string norm;
        for (char ch : c.tls_pin_sha256) {
            const auto u = static_cast<unsigned char>(ch);
            if ((u >= '0' && u <= '9') || (u >= 'a' && u <= 'f') || (u >= 'A' && u <= 'F')) {
                norm.push_back(ch);
            }
        }
        if (norm.size() != 64) {
            throw std::runtime_error(
                "tls_pin_sha256 不是合法的 SHA-256 指纹（去掉分隔符后应有 64 个十六进制位，实际 " +
                std::to_string(norm.size()) + " 位）");
        }
    }

    c.debug_stall_every_n        = j.value("debug_stall_every_n", c.debug_stall_every_n);
    c.debug_stall_ms             = j.value("debug_stall_ms", c.debug_stall_ms);
    c.debug_decode_stall_every_n = j.value("debug_decode_stall_every_n", c.debug_decode_stall_every_n);
    c.debug_decode_stall_ms      = j.value("debug_decode_stall_ms", c.debug_decode_stall_ms);

    // 心跳超时必须大于发送间隔，否则每一轮都会误判掉线并疯狂重连
    if (c.heartbeat_timeout_ms <= c.heartbeat_interval_ms) {
        throw std::runtime_error("heartbeat_timeout_ms must be greater than heartbeat_interval_ms");
    }
    // 0 等于关掉握手保护，那正是本项要防的失效模式，因此直接拒绝
    if (c.hello_timeout_ms == 0) {
        throw std::runtime_error("hello_timeout_ms must be > 0");
    }
    if (c.reconnect_initial_delay_ms == 0) {
        throw std::runtime_error("reconnect_initial_delay_ms must be > 0");
    }
    if (c.reconnect_max_delay_ms < c.reconnect_initial_delay_ms) {
        throw std::runtime_error("reconnect_max_delay_ms must be >= reconnect_initial_delay_ms");
    }
    if (c.target_fps == 0 || c.target_fps > 240) {
        throw std::runtime_error("target_fps must be in 1..240");
    }

    // 诊断开关（第 2 步的延迟判据靠它们做反向对照）。
    // 它们的失效模式很特别：**写了但没生效**。那时判据以为自己在测一条被弄坏的链路，
    // 实际测的是好链路，于是"顺利通过"—— 又是一次虚假安全感（本项目为此付过三次学费，
    // 见 docs §8.6/§8.12/§6.14）。所以这里只放行语义明确的组合：
    //
    // 这条校验不是形式主义：**第一版的丢帧开关就是这么栽的** —— 开关生效了、
    // 日志也打了，但它造出的现象与预期无关（见上面那段长注释）。配置项"生效"
    // 和"生效成你以为的样子"是两件事。
    if (c.debug_stall_every_n == 1 || c.debug_decode_stall_every_n == 1) {
        // 每帧都停 = 只是把整个分布平移（P50 与 P95 一起涨），造不出抖动，
        // 反而会把帧率压死。要的是**间歇**停顿。
        throw std::runtime_error(
            "debug_stall_every_n / debug_decode_stall_every_n must be 0 or >= 2 (1 = 每一帧都停)");
    }
    if ((c.debug_stall_every_n == 0) != (c.debug_stall_ms == 0)) {
        // 只设一个 = 另一半默认 0 = 开关根本不生效。宁可拒绝启动，也不要静默不生效。
        throw std::runtime_error(
            "debug_stall_every_n and debug_stall_ms must both be 0 or both > 0");
    }
    if ((c.debug_decode_stall_every_n == 0) != (c.debug_decode_stall_ms == 0)) {
        throw std::runtime_error(
            "debug_decode_stall_every_n and debug_decode_stall_ms must both be 0 or both > 0");
    }

    // 自动输入源（第 2 步 2b）：坐标是比例，必须落在画面内。
    // 越界坐标会被系统 clamp，于是服务端"读回的位置"与"请求的位置"不符 ——
    // 那条前置不变式会大面积失败，而现象看起来像"输入链路坏了"（其实是配置写错了）。
    // 所以宁可拒绝启动，也不让它带着一个必然失败的夹具跑起来。
    if (c.auto_input_x0 < 0.0 || c.auto_input_x0 > 1.0 || c.auto_input_x1 < 0.0 ||
        c.auto_input_x1 > 1.0 || c.auto_input_y < 0.0 || c.auto_input_y > 1.0) {
        throw std::runtime_error(
            "auto_input_x0 / auto_input_x1 / auto_input_y must be fractions in [0, 1] "
            "(they are scaled by the remote frame size)");
    }
    if (c.auto_input_interval_ms > 0 && c.auto_input_interval_ms < 5) {
        // 比 UI 消息节流（16 ms）还密没有意义，而且会把人手无法复现的节奏写进配置。
        throw std::runtime_error("auto_input_interval_ms must be 0 (off) or >= 5");
    }
    return c;
}

} // namespace rc
