// ============================================================
// 重构版服务端入口（第二阶段：Asio 异步 + 多客户端 + 心跳/重连）
//
// 与第一阶段的差别：
//   - 第一阶段：WSAStartup + 阻塞 accept 循环 + 每连接一组线程；
//   - 现在：Asio 自动管理 Winsock 生命周期，io_context 线程池跑异步事件，
//           客户端数量与线程数解耦；Ctrl+C 触发优雅关闭（先关所有会话再退出）。
//
// 用法：
//   rc_server.exe [config/server.json]
// 配置按相对路径读取，因此请在 refactored 目录下运行（或把 config 拷到 exe 旁边）。
// ============================================================

#include "asio_server.hpp"
#include "audit.hpp"
#include "config.hpp"
#include "dpi_state.hpp"
#include "logger.hpp"
#include "tls.hpp"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>

#include <cstring>
#include <exception>
#include <memory>
#include <string>

namespace {

/// 启动期 DPI 状态汇报。
///
/// 【为什么设置完还要再测一次，而不是相信配置项】
///   §6.14 实测：进程级 `SetProcessDpiAwarenessContext` 在本机不可靠 —— 连测 12 次，
///   早几次成功、随后全部 `ACCESS_DENIED`。若只看"我设过了"，一个"自称 aware、
///   实际 unaware"的进程会让 A/B 两轮测的其实是同一个东西，而结论看起来完全正常。
///   所以这里一律以**实测帧尺寸**为准（docs/03 方法论 #5：改了配置就要有办法证明它生效）。
///
/// 【为什么必须测两次（构造前 / 构造后）—— 2026-09-23 实测新增】
///   抓屏拿到物理像素时，"awareness 是谁给的"有两种可能，而**可处置性相反**：
///     · 构造**期间**被顶起来 -> 来源是 DXGI 的 DuplicateOutput（进程内、我们自己的行为，可解释）；
///     · 构造**之前**就已 aware -> 来源在**进程外**（exe 自带的 DPI 清单，或 Windows 按 exe 路径
///       的兼容层 HIGHDPIAWARE）—— 代码里撤销不了，而且它会让 `dpi_aware=false` 名不副实。
///   只测一次就分不清这两者，会把"进程外覆盖"误报成"本进程已感知 DPI"，让人以为配置生效了。
///   （2026-09-23 那次误判就是这么发生的：把 `rc_server.exe` 的兼容层覆盖读成了正常状态。）
///
/// @param backend 实际选定的后端名（来自 AsioServer::screen_name()）
/// @param before  进入 AsioServer 构造**之前**测的几何（代表"启动时环境给的是什么"）
/// @param after   构造**之后**测的几何（代表"抓屏线程实际会看到的"）
void report_dpi_state(const rc::ServerConfig& cfg, const char* backend,
                      const rc::server::ScreenGeometry& before,
                      const rc::server::ScreenGeometry& after) {
    // "本机在缩放"的判据是**同一个 API 在两个上下文下读数不同**，与进程是否 aware 无关。
    const bool machine_scales = after.context_switch_effective;
    const bool backend_is_dxgi = (backend != nullptr) && (std::strstr(backend, "dxgi") != nullptr);

    if (!machine_scales) {
        if (after.phys_confirmed()) {
            RC_LOG_INFO("未检出 DPI 虚拟化：抓屏尺寸 {}x{}（aware 读数与显示模式两个来源一致）——"
                        "若本机确实不是非 100% 缩放，则无需任何处置",
                        after.frame_w, after.frame_h);
        } else {
            // 未检出 ≠ 没有缺陷。物理尺寸没被第二来源证实，说明"线程级 DPI 上下文切换"
            // 在这台机器上没起作用 —— 于是 phys 退化成 frame，virtualized() 恒为 false，
            // **检测手段本身坏了，却会报出"一切正常"**。这正是本项目反复栽的那个坑
            // （§8.6 的"降采样"、§8.12 的夹具出画、§6.14 的自称 unaware 实则 aware 三例），
            // 所以必须显式报出来，而不是安静地当成"没有虚拟化"。
            RC_LOG_WARN("未检出 DPI 虚拟化，但物理尺寸**没能被两个来源共同证实**"
                        "（aware 读数 {}x{}，显示模式 {}x{}，读到模式={}）—— 本机线程级 DPI "
                        "上下文切换未生效，检测手段失效：\"未检出\"不等于\"没有缺陷\"。"
                        "请人工核对画面右侧/下侧是否缺失",
                        after.phys_w, after.phys_h, after.mode_w, after.mode_h, after.mode_ok ? 1 : 0);
        }
        return;
    }

    // ---- 本机确在缩放 ----

    if (after.virtualized()) {
        // 抓屏确实被虚拟化了。按 §6.14 的实测，DXGI 给物理像素、不受调用方 DPI 上下文影响，
        // 所以这条路径只可能出现在 GDI 后端上。若 dxgi 也走到这里，说明那条前提被推翻了 ——
        // 必须喊出来，而不是继续按 GDI 的剧本往下讲。
        if (backend_is_dxgi) {
            RC_LOG_WARN("**前提被推翻**：后端是 {}，却抓到虚拟化尺寸 {}x{}（物理 {}x{}）——"
                        "§6.14 的\"DXGI 不受调用方 DPI 上下文虚拟化\"在这台机器上不成立，"
                        "请勿据旧结论做判断",
                        backend, after.frame_w, after.frame_h, after.phys_w, after.phys_h);
            return;
        }

        RC_LOG_WARN("=========== DPI 虚拟化已检出：这是功能缺陷，不是取舍 ===========");
        RC_LOG_WARN("屏幕缩放约 {}%：抓屏实际只能拿到 {}x{}，物理屏是 {}x{}{}",
                    after.scaling_percent(), after.frame_w, after.frame_h, after.phys_w, after.phys_h,
                    after.phys_confirmed() ? "（aware 读数与显示模式两个来源一致）" : "（未能交叉验证）");
        // §6.13：这 1707x960 是物理画面**左上角 1:1 的裁剪**，不是"整个桌面缩 1.5 倍"。
        RC_LOG_WARN("  ① 远端只能看到桌面左上 {:.0f}%（右侧 {:.0f}% 与下侧 {:.0f}% 永不入帧）",
                    100.0 * after.visible_area(), 100.0 * (1.0 - after.visible_x()),
                    100.0 * (1.0 - after.visible_y()));
        RC_LOG_WARN("  ② 输入坐标（SetCursorPos/GetCursorInfo）仍被系统虚拟化，与 1:1 的抓屏"
                    "不在同一坐标空间 -> 点击会系统性偏移");
        if (cfg.dpi_aware) {
            RC_LOG_WARN("  * 配置里 dpi_aware 已经是 true 却仍被虚拟化 —— 说明设置没生效"
                        "（见上一条 WARN 的 GetLastError），当前状态等同于关闭它");
        } else {
            RC_LOG_WARN("  临时兜底：把 config 里 dpi_aware 设为 true（代价：抓屏 +69%、"
                        "端到端 -20%，实测见 docs §6.12）");
        }
        RC_LOG_WARN("  终局：capture_backend=dxgi/auto（第 1 步）—— DXGI 给物理像素，"
                    "并且 DuplicateOutput 会把进程提为 per-monitor aware，输入坐标随之变成物理坐标，"
                    "上面 ①② 两条会一起消失（2026-09-23 实测，见下一条分支）");
        RC_LOG_WARN("==============================================================");
        return;
    }

    // ---- 本机在缩放，但抓屏拿到的是物理像素 = awareness 生效 ----

    if (before.virtualized()) {
        // 构造**之前**还是 unaware（before.frame_w x before.frame_h），构造期间变成 aware。
        // 来源只能是 DXGI 的 DuplicateOutput —— 2026-09-23 用单进程实验钉死了：
        //   D3D11CreateDevice          -> 进程仍 PROCESS_DPI_UNAWARE、仍 1707x960
        //   IDXGIOutput1::DuplicateOutput -> 进程变 PER_MONITOR_DPI_AWARE、变 2560x1440
        // 这就是"DXGI 看得全"的机理，同时也说明**它顺带修掉了输入侧的虚拟化**。
        RC_LOG_INFO("抓屏 {}x{} = 物理像素：进程在 AsioServer 构造期间被顶成 {}",
                    after.frame_w, after.frame_h, rc::server::dpi_awareness_name());
        RC_LOG_INFO("  来源：DXGI 的 IDXGIOutput1::DuplicateOutput —— 它会把进程强行提为 "
                    "per-monitor aware（构造前实测 {}x{}，构造后 {}x{}）",
                    before.frame_w, before.frame_h, after.frame_w, after.frame_h);
        RC_LOG_INFO("  实测后果：**输入坐标也随之变成物理坐标**（SetCursorPos(2000,700) 读回 "
                    "(2000,700)；unaware 时会被夹到 1706）-> 抓屏与输入在同一坐标空间，"
                    "§6.13 的 ①裁剪 ②点击偏移 在这个后端下都不存在");
        RC_LOG_WARN("  但配置 dpi_aware=false 在这条路径上**已被覆盖**（不是本进程主动设的，"
                    "是 DuplicateOutput 的既定行为，代码里无法撤销）—— 不要再用这个开关做 DPI 对照实验");
        RC_LOG_WARN("  另有一处**进程外**副作用：Windows 会因此为该 exe 路径记一条 HIGHDPIAWARE "
                    "兼容层，**下一次启动就直接是 aware**（那时连 gdi 后端也会拿到 {}x{}）-> "
                    "跑 A/B 前先执行 python tests/check_dpi_override.py",
                    after.frame_w, after.frame_h);
        return;
    }

    // 构造前就已经 aware。
    if (cfg.dpi_aware) {
        RC_LOG_INFO("dpi_aware=on 生效：抓屏 {}x{} == 物理尺寸（当前级别 {}）",
                    after.frame_w, after.frame_h, rc::server::dpi_awareness_name());
        return;
    }

    // 配置说"关"，进程却是 aware，而且**构造之前就已经是** —— 覆盖来自进程外。
    // 这不是小瑕疵：它让 dpi_aware 这个开关名不副实，而日志上一切正常。
    RC_LOG_WARN("=========== 配置 dpi_aware=false 已被**进程外**覆盖 ===========");
    RC_LOG_WARN("进程在 AsioServer 构造**之前**就已经是 {}（当时抓屏 {}x{}），构造后 {}x{} ——"
                "这个覆盖不是本进程设的，代码里无法撤销",
                rc::server::dpi_awareness_name(),
                before.frame_w, before.frame_h, after.frame_w, after.frame_h);
    RC_LOG_WARN("  来源只可能有两个：① exe 自带 DPI 清单；② Windows 按 exe 路径的兼容层"
                "（HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\AppCompatFlags\\Layers "
                "里的 HIGHDPIAWARE）");
    RC_LOG_WARN("  第 ② 条本机已实测：跑过一次 DXGI 后端后，该 exe 路径会被自动加上 HIGHDPIAWARE，"
                "此后每次启动都 aware —— 于是同一份二进制、同一份配置，gdi 会给出 {}x{} 而不是它本该抓的"
                "虚拟化尺寸，A/B 结论会随**运行顺序**变化",
                after.frame_w, after.frame_h);
    RC_LOG_WARN("  检查/清除：python tests/check_dpi_override.py [--clean]");
    RC_LOG_WARN("  影响评估：抓屏与输入都变成物理坐标（两者一致，反而没有裁剪与点击偏移），"
                "但不能再拿 dpi_aware 做对照实验");
    RC_LOG_WARN("==============================================================");
}

} // namespace

int main(int argc, char** argv) {
    const std::string config_path = (argc > 1) ? argv[1] : "config/server.json";

    try {
        const auto cfg = rc::load_server_config(config_path);
        rc::init_logger(cfg.log_file, rc::log_level_from_string(cfg.log_level));
        // 【2B 第三刀 审计】独立 sink，必须在 server.start() 之前建好 ——
        // io 线程一起来就会有并发写，那时再 init 就有竞态。
        // enable=false 时**不创建任何文件**（见 audit.hpp）。
        rc::audit::init(cfg.audit_log_file, cfg.audit_enable);

        RC_LOG_INFO("==============================================");
        RC_LOG_INFO("rc_server v2 (asio async, protocol v{})",
                    static_cast<int>(rc::net::kProtocolMajor));
        RC_LOG_INFO("config={}", config_path);
        RC_LOG_INFO("==============================================");

        // 【2B 第二刀 TLS】先自证"链的是哪一份 OpenSSL"。
        // 本依赖是本工程唯一的**预编译二进制**，其特有失效模式就是"头文件与 lib/DLL
        // 版本不一致"——那能编过、能链接，运行期行为却不可解释。两行都打出来，配错了一眼可见。
        RC_LOG_INFO("TLS: OpenSSL 运行期 [{}]", rc::tls::runtime_version());
        RC_LOG_INFO("TLS: OpenSSL 头文件 [{}]", rc::tls::build_version());

        // ---- 2B 第二刀：TLS 上下文（含自签证书准备）----
        std::shared_ptr<asio::ssl::context> ssl_ctx;
        if (cfg.tls_enable) {
            std::string err;
            if (cfg.tls_auto_self_signed) {
                if (!rc::tls::ensure_self_signed_cert(cfg.tls_cert_file, cfg.tls_key_file, &err)) {
                    RC_LOG_ERROR("TLS 自签证书准备失败：{}", err);
                    return 1;
                }
            }
            ssl_ctx = rc::tls::make_server_context(cfg.tls_cert_file, cfg.tls_key_file, &err);
            if (!ssl_ctx) {
                // 证书加载不了就**拒绝启动**，绝不静默降级到明文 ——
                // 那会把"加密"变成一句空话，而日志上一切正常。
                RC_LOG_ERROR("TLS 上下文创建失败：{}", err);
                return 1;
            }
            RC_LOG_INFO("TLS: 已启用（证书 {}）", cfg.tls_cert_file);
            // 【指纹必须能被读回来】客户端 config 里的 tls_pin_sha256 就抄这一行 ——
            // 没有它，用户只能靠外部 openssl 命令去算，而那命令目标机器未必有
            // （本机上它只是 PortableGit 顺手带的）。这是 §6.12「配了要能自证生效」的落点。
            std::string ferr;
            const auto  fp = rc::tls::fingerprint_of_pem_file(cfg.tls_cert_file, &ferr);
            if (!fp.empty()) {
                RC_LOG_INFO("TLS: 证书指纹 SHA-256 = {}", fp);
                RC_LOG_INFO("TLS:   把上面这串填进客户端 config 的 tls_pin_sha256 即可");
            } else {
                RC_LOG_WARN("TLS: **证书指纹读取失败**（{}）—— 客户端将无法 pin 这张证书。", ferr);
            }
        } else {
            RC_LOG_WARN("=========== TLS：未启用 ===========");
            RC_LOG_WARN("整条链路**明文**（鼠标/键盘/画面，以及 auth_token）。");
            RC_LOG_WARN("启用：服务端与客户端都设 tls_enable=true；再把服务端日志里打印的证书");
            RC_LOG_WARN("      指纹填进客户端 tls_pin_sha256。");
            RC_LOG_WARN("===================================");
        }

        // 【2B 认证】启动时把**生效状态**喊出来 —— 这一段针对的失效模式与上面 DPI 那段同类：
        // 认证的失败方向偏"开"（`auth_token` 忘配 = 不认证），而"配了但没生效"与"压根没配"
        // 在日志里长得**一模一样**（都是静默）。所以未启用的情况刻意用 WARN 起头，让它显眼。
        //
        // ⚠️ 这里**不打 token 内容，也不打长度** —— "已启用"这三个字本身就是配置读到了的证明
        //    （读不到就会走另一分支），而结合上面那行 `config=<路径>` 已足够定位"改错了文件"。
        if (cfg.auth_token.empty() && cfg.auth_clients.empty()) {
            RC_LOG_WARN("=========== 认证：未启用（auth_token 为空）===========");
            RC_LOG_WARN("能连到 {}:{} 的**任何人**都可以控制本机桌面，包括键盘输入。",
                        cfg.listen_host, cfg.listen_port);
            RC_LOG_WARN("要启用：在服务端与客户端 config 里设同一个 auth_token。");
            if (!cfg.tls_enable) {
                // 有 TLS 时凭据在链路上是加密的，这句就不成立 —— 别喊狼来了。
                RC_LOG_WARN("⚠️ 且当前**明文传输**：只解决\"谁能连上\"，不解决\"链路上看不见\"。");
            }
            RC_LOG_WARN("=======================================================");
        } else if (!cfg.auth_clients.empty()) {
            // ---- 【2B 第三刀 权限模型】授权表生效 ----
            // ⚠️ 注意这行里必须保留子串 "认证：已启用" —— tests/run_auth_check.py 按它
            //    判断"配置生效自证"，改动会连带打断那项回归（本项目的老规矩：老日志行的
            //    匹配目标不许动，要加就加在**新的一行**上）。
            RC_LOG_INFO("认证：已启用（权限模型：auth_clients {} 条，凭据内容不写日志）",
                        cfg.auth_clients.size());
            std::size_t n_control = 0;
            std::size_t n_view    = 0;
            for (const auto& e : cfg.auth_clients) {
                (e.role == "view" ? n_view : n_control) += 1;
            }
            // 逐个把"谁、什么权限"打出来 —— **name 打，token 一个字符都不打**。
            // 这一行是部署时唯一能核对"我配的这张表真的被读到了"的东西：
            // 表读不到就完全不会走到这个分支，而"配了但没生效"和"压根没配"在
            // 运行行为上是一样的（本项目在配置静默失效上栽过四次）。
            for (const auto& e : cfg.auth_clients) {
                RC_LOG_INFO("  · {} -> role={}", e.name, e.role);
            }
            RC_LOG_INFO("  · 合计：control {} 个 / view（只读）{} 个", n_control, n_view);
            if (n_view > 0) {
                RC_LOG_INFO("  · view 会话仍**看得到画面**并占用一个客户端名额；"
                            "它的鼠标/键盘在服务端被拒绝（不是客户端自律）");
            }
        } else {
            RC_LOG_INFO("认证：已启用（auth_token 已配置；凭据内容不写日志）");
            RC_LOG_INFO("  提示：单凭据模式下所有客户端共用一个密钥 ⇒ 审计里身份只能记成"
                        "<none>，也无法区分只读。要区分谁是只读，改用 auth_clients 表。");
        }

        // ---- 【2B 第三刀 审计】启用状态必须喊出来 ----
        // 失效形态在各配置项里最隐蔽的一个：**"没写文件"与"写成功了"在程序行为上
        // 完全一样**（不像认证失败会拒绝连接、不像 DXGI 会改变帧尺寸）。
        // 所以这里不打"审计已启用"就算了 —— 关闭时用 WARN 起头，并且明确写出
        // "这一轮发生的事没有任何留痕"，让读日志的人知道自己在看一份什么样的记录。
        if (cfg.audit_enable) {
            RC_LOG_INFO("审计：已启用 -> {}", cfg.audit_log_file);
            RC_LOG_INFO("  事件：conn_accept / auth_ok / auth_fail / input_denied / session_end");
        } else {
            RC_LOG_WARN("=========== 审计：未启用 ===========");
            RC_LOG_WARN("本轮**没有任何留痕**：谁连过、被谁拒绝过、只读会话有没有被尝试操作，");
            RC_LOG_WARN("服务端关闭后一概查不到（调试日志级别一调连 info 也一起消失）。");
            RC_LOG_WARN("要启用：config 里设 audit_enable=true。");
            RC_LOG_WARN("===================================");
        }
        // 【为什么这一条要单独 WARN：它是"配了一半"】
        // 用户开了认证/权限模型（说明他这次在意安全），却让审计关着 —— 那等于
        // "门锁上了，但没人知道谁来过"。这两个开关的意图高度相关，一边开一边关
        // 更可能是漏配而不是刻意（刻意关的话这条 WARN 也只是多一行，无副作用）。
        if ((!cfg.auth_token.empty() || !cfg.auth_clients.empty()) && !cfg.audit_enable) {
            RC_LOG_WARN("⚠️ 已启用认证/权限模型，但**审计是关的**：认证失败的尝试"
                        "（有人在山试探这个端口）不会留下任何记录。建议同时开 audit_enable。");
        }

        // 【网络层 Nagle，§6.29】启动期自报 ——
        // 失效模式与上面几条同源：默认 false 时 Nagle 是开的（Windows 默认行为），
        // 而开 Nagle 的代价只有在真实 RTT 下才显形（loopback 上 RTT≈0，开了也看不见），
        // 于是"忘配 tcp_nodelay"在测试期间完全静默。喊出来是为了让读日志的人知道
        // "这一轮端到端数字是在 Nagle on 下量出来的"，将来翻默认值再做 A/B 时也找得到
        // 对照点。
        //
        // 默认 false = 与引入本字段之前逐字等价（产品两端此前都没设 no_delay）；
        // 反向对照与翻默认值条款见 config.hpp 与 docs §6.29。
        if (cfg.tcp_nodelay) {
            RC_LOG_INFO("TCP_NODELAY: on（accepted socket 上设 NODELAY）——"
                        "小包不再被 Nagle 压住等 ACK；loopback 上与 off 等价，"
                        "真实 RTT 下差异帧帧周期 / 输入→显示 会显著下降（§6.29）");
        } else {
            RC_LOG_WARN("TCP_NODELAY: **off**（默认；与引入本字段之前的行为逐字等价）——"
                        "Windows 默认 Nagle 是开的，链路有 RTT 时小包（输入 / 心跳 /"
                        "差异帧最后一帧不到 MSS 的尾段）会被压住等 ACK，"
                        "代价在 loopback 上看不见（见 §6.29）");
        }

        // DPI 感知必须在任何屏幕 DC 创建之前声明（服务端到 AsioServer 起抓屏线程时才会用到 DC）。
        // 【为什么这不只是"清晰度"问题 —— 2026-09-23 实测，见 docs §6.13】
        // 不声明感知时，物理屏 2560x1440 抓到的是 **1707x960**，而这 1707x960 是物理画面
        // **左上角 1:1 的裁剪**，不是"整个桌面缩 1.5 倍"（实测：不感知进程抓到的帧，与感知
        // 进程抓到的左上角区域逐像素完全相同，17280 个抽样点 0 个不同）。
        // 于是"关掉 dpi_aware"丢的不只是清晰度，还有右侧/下侧各 33% 的画面，
        // 以及"抓屏 1:1 而输入被虚拟化 ×1.5"这个坐标空间不一致导致的点击偏移。
        if (cfg.dpi_aware) {
            if (::SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)) {
                RC_LOG_INFO("dpi_aware=on (PER_MONITOR_AWARE_V2)");
            } else {
                // 常见原因：exe 已带 DPI 清单、或系统版本过旧。不是致命错误——
                // 抓屏照常可用，但仍是"裁剪 + 坐标空间不一致"的那一套（下面实测会照出来）。
                RC_LOG_WARN("dpi_aware=on but SetProcessDpiAwarenessContext failed, GetLastError={}",
                            ::GetLastError());
            }
        }
        // 【为什么要测两次】awareness 的来源有两种，可处置性相反：
        //   · 构造**之前**就已 aware  -> 覆盖来自**进程外**（exe 清单 / Windows 按路径的
        //     HIGHDPIAWARE 兼容层），代码里撤销不了，且 dpi_aware=false 已名不副实；
        //   · 构造**期间**才变 aware -> 是 DXGI 的 DuplicateOutput 干的（进程内、可解释）。
        // 只测一次分不清这两者。2026-09-23 已经因为分不清而误判过一次。
        const auto geom_before = rc::server::measure_screen_geometry();
        rc::server::AsioServer server(cfg);
        const auto geom_after = rc::server::measure_screen_geometry();

        // DPI 状态汇报放在后端选定**之后**：同一句"被抓屏虚拟化"，对 GDI 是
        // "远端只看得到左上 44%"的功能缺陷，对 DXGI 则完全不影响画面（它给物理像素）。
        // 附带作用：DXGI 在构造里创建了 D3D11 设备并打开 duplication，所以这一步同时实测了
        // "链接 d3d11 之后进程级 DPI 设置是否还能生效"这个风险 —— 结论：能生效，
        // 而且 DuplicateOutput 会把进程**再**顶成 per-monitor aware（见 report_dpi_state）。
        report_dpi_state(cfg, server.screen_name(), geom_before, geom_after);

        // 【2B 第二刀 TLS】把上下文交给服务端循环（nullptr = 不启用）。
        // 必须在 start() 之前 —— start() 之后 accept 随时可能进来。
        server.set_tls_context(ssl_ctx);

        std::string error;
        if (!server.start(&error)) {
            RC_LOG_ERROR("failed to start server: {}", error);
            return 1;
        }
        RC_LOG_INFO("running with {} io thread(s); press Ctrl+C to stop", server.io_threads());

        server.run();
        return 0;
    } catch (const std::exception& e) {
        // 启动期异常（配置非法、端口占用等）必须显式打出来并返回非 0，
        // 否则脚本化启动时只能看到一个"莫名退出"。
        RC_LOG_ERROR("fatal: {}", e.what());
        return 1;
    }
}
