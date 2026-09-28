// ============================================================
// 重构版客户端入口（第二阶段：异步客户端 + 心跳 + 断线重连）
//
// 与第一阶段的差别：
//   - 第一阶段：sleep 重试式连接 + 阻塞接收线程；断线只能重启程序；
//   - 现在：连接/握手/心跳/拉屏/重连全部异步，UI 线程永不阻塞。
//           窗口先创建、再启动网络，保证回调一定拿得到有效窗口句柄。
//
// 关闭时序（修复旧版"关窗口后线程卡在 recv"）：
//   消息循环结束 -> client.stop()（取消定时器 + 关 socket + join io 线程）
//   -> 各 RAII 对象析构。
// ============================================================

#include "async_client.hpp"
#include "config.hpp"
#include "logger.hpp"
#include "remote_window.hpp"
#include "tls.hpp"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>

#include <exception>
#include <string>

namespace {

// UTF-8 -> UTF-16（宽字符 API 显示中文，避免 /utf-8 下 ANSI 版乱码）
std::wstring to_wide(const std::string& utf8) {
    if (utf8.empty()) {
        return {};
    }
    const int len = ::MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), -1, nullptr, 0);
    if (len <= 0) {
        return {};
    }
    std::wstring wide(static_cast<std::size_t>(len - 1), L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), -1, wide.data(), len);
    return wide;
}

/// 取命令行第一个参数（配置路径）；为空则返回空串表示用默认值。
///
/// 与 rc_server 的 `argv[1]` 保持对称：此前客户端把它写死成 "config/client.json"，
/// 于是自检脚本传进来的路径被静默忽略 —— 想换端口就得换工作目录，很容易看错对象。
/// 这里只做"去引号"这一层处理；路径含空格时请在命令行里加引号。
std::string first_arg(const char* cmd_line) {
    std::string s = (cmd_line != nullptr) ? cmd_line : "";
    const std::size_t b = s.find_first_not_of(" \t");
    if (b == std::string::npos) {
        return {};
    }
    const std::size_t e = s.find_last_not_of(" \t");
    s = s.substr(b, e - b + 1);
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"') {
        s = s.substr(1, s.size() - 2);
    }
    return s;
}

} // namespace

int WINAPI WinMain(HINSTANCE, HINSTANCE, PSTR lp_cmd_line, int nCmdShow) {
    // 与服务端保持相同的 DPI 感知，保证窗口坐标与远端坐标可映射
    ::SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    try {
        const std::string arg         = first_arg(lp_cmd_line);
        const std::string config_path = arg.empty() ? std::string("config/client.json") : arg;
        const auto        cfg         = rc::load_client_config(config_path);
        rc::init_logger(cfg.log_file, rc::log_level_from_string(cfg.log_level));
        RC_LOG_INFO("rc_client v2 starting, server={}:{}, config={}", cfg.server_host,
                    cfg.server_port, config_path);

        // 【网络层 Nagle，§6.29】与服务端对称地自报。失效模式同源（默认 off = 老路径，
        // Nagle 的代价在真实 RTT 下才显形，loopback 上开不开数字一模一样），喊出来是为
        // 让读日志的人知道这一轮的对照点。
        if (cfg.tcp_nodelay) {
            RC_LOG_INFO("TCP_NODELAY: on（connect 时新 socket 上设 NODELAY）——"
                        "小包立刻发；loopback 上与 off 等价，真实 RTT 下差异帧帧周期 /"
                        "输入→显示 显著下降（§6.29）");
        } else {
            RC_LOG_WARN("TCP_NODELAY: **off**（默认；与引入本字段之前的行为逐字等价）——"
                        "Windows 默认 Nagle 是开的，链路有 RTT 时小包（输入 / 心跳 /"
                        "差异帧最后一帧不到 MSS 的尾段）会被压住等 ACK；loopback 上看不见"
                        "（见 §6.29）");
        }

        // 【2B 第二刀 TLS】把**生效状态**喊出来 —— 与 DPI/认证那两段同一个理由：
        // "配了但没生效"（配置项名写错、改错了文件）与"压根没配"在日志里本来长得一样。
        //
        // ⚠️ 整段只在 tls_enable=true 时执行 ⇒ **TLS 关的时候日志与引入本刀之前逐字相同**
        //    （回归里的 17 项全是 TLS 关，一行新日志都不会多出来）。
        if (cfg.tls_enable) {
            // 与 rc_server 对称地自证"链的是哪一份 OpenSSL"：头文件与 DLL 版本不一致
            // 是本工程唯一预编译依赖的特有失效模式（能编过、能链接、运行期不可解释）。
            RC_LOG_INFO("TLS: OpenSSL 运行期 [{}]", rc::tls::runtime_version());
            RC_LOG_INFO("TLS: OpenSSL 头文件 [{}]", rc::tls::build_version());
            if (cfg.tls_pin_sha256.empty()) {
                // 空 pin = 只加密不认证。这是个**明确弱化**的安全姿态，必须显眼：
                // 防得住链路窃听，防不住中间人。
                RC_LOG_WARN("=========== TLS：只加密，**不校验服务端身份** ===========");
                RC_LOG_WARN("tls_pin_sha256 为空 ⇒ 能防窃听，**不能防中间人**。");
                RC_LOG_WARN("填法：把服务端启动日志里那行 `TLS: 证书指纹 SHA-256 = …` 抄过来。");
                RC_LOG_WARN("=========================================================");
            } else {
                RC_LOG_INFO("TLS: 已启用，pin 服务端证书指纹 {}",
                            rc::tls::normalize_fingerprint(cfg.tls_pin_sha256));
                RC_LOG_INFO("TLS:   握手时比对，不匹配即断开且**不重试**（配置分歧，不是链路故障）");
            }
        }

        // 【2B 第三刀 权限模型】诊断开关必须自曝 —— 它会让"服务端拦截"这一道被单独
        // 走到（见 ClientConfig::debug_ignore_role 的说明：判据靠它把"客户端自律"
        // 与"服务端拦截"分开证明）。拿到一份日志的人要能一眼看出这不是生产配置。
        if (cfg.debug_ignore_role) {
            RC_LOG_WARN("⚠️ debug_ignore_role=on：即使服务端下发 role=view，输入**照常发送**"
                        "（会被服务端拒绝）。这是权限判据的诊断开关，不是生产配置。");
        }

        rc::client::AsyncClient  client(cfg);
        rc::client::RemoteWindow window(client, L"\u8FDC\u7A0B\u63A7\u5236 - \u91CD\u6784\u7248 v2");

        // 诊断钩子：把客户端累积画面在收到第 N 帧后落盘（配置为空则完全关闭）。
        // 差异帧回归脚本靠它取到"客户端眼中的远端桌面"，再与服务端整帧对照。
        if (!cfg.dump_frame_path.empty() && cfg.dump_frame_after > 0) {
            window.set_debug_dump(to_wide(cfg.dump_frame_path), cfg.dump_frame_after);
            RC_LOG_INFO("debug dump enabled: after {} frames -> {}", cfg.dump_frame_after,
                        cfg.dump_frame_path);
        }

        // 诊断钩子：把"窗口实际画出来的"与"同一时刻整幅重绘的参考"成对落盘。
        // 这是"按脏区重绘"那条判据（§6.25）的落点：两者逐像素相等 ⇔ 裁剪没漏掉该重绘的地方。
        if (!cfg.paint_dump_path.empty() && cfg.paint_dump_after > 0) {
            window.set_paint_dump(to_wide(cfg.paint_dump_path), cfg.paint_dump_after);
            RC_LOG_INFO("paint dump enabled: after {} paints -> {}", cfg.paint_dump_after,
                        cfg.paint_dump_path);
        }

        // 诊断钩子：人为制造抖动 / 丢帧（第三阶段第 2 步的延迟判据靠它们做反向对照）。
        // 两者默认为 0，也就是**关**——生产路径上没有任何额外行为。
        //
        // 用 WARN 而不是 INFO：这是**故意把链路弄坏**。将来若有人拿到一份带错帧的日志，
        // 第一件该看见的就是"这不是真故障，是诊断开关打开着"。
        if (cfg.debug_stall_every_n > 0) {
            window.set_debug_stall(cfg.debug_stall_every_n, cfg.debug_stall_ms);
            RC_LOG_WARN("debug stall[io] enabled: 每 {} 帧停顿 {} ms"
                        "（制造抖动，用来验证 P95 判据）",
                        cfg.debug_stall_every_n, cfg.debug_stall_ms);
        }
        if (cfg.debug_decode_stall_every_n > 0) {
            window.set_debug_decode_stall(cfg.debug_decode_stall_every_n, cfg.debug_decode_stall_ms);
            RC_LOG_WARN("debug stall[decode] enabled: 每 {} 帧停顿 {} ms"
                        "（制造队列溢出 -> resync，用来验证冻结时长判据）",
                        cfg.debug_decode_stall_every_n, cfg.debug_decode_stall_ms);
        }

        // 整帧优先通道（产品行为，默认开）。这里必须**明确打出开了还是关了**：
        // 这条链路的行为差别很大（关了会丢整帧、resync 可能收敛不了），
        // 而"配置项生效"和"生效成你以为的样子"是两件事（本项目为此付过三次学费）。
        // 所以关掉时用 WARN：拿到一份"冻结很长"的日志，第一眼就该看到它是旧路径。
        window.set_keyframe_priority(cfg.keyframe_priority);
        if (cfg.keyframe_priority) {
            RC_LOG_INFO("整帧优先通道 on：整帧不再排进增量队列，队列溢出清不到它"
                        "（resync 因此必然收敛）");
        } else {
            RC_LOG_WARN("整帧优先通道 **关闭**：整帧与增量帧挤同一条队列，"
                        "溢出会连整帧一起清掉 —— resync 可能长期收敛不了。"
                        "这条路径只用于 A/B 反向对照，不要用在生产");
        }

        // 【UI 绘制】缩放模式（产品行为，默认 halftone）。必须把**实际生效**的模式打出来：
        // `[paint]` 行报的 StretchBlt 耗时（12 ms 还是 1 ms）只有在知道用的哪个模式时
        // 才有意义 —— 跨轮比较时模式不同就会读错，这正是"出图帧率"那条撤回的教训
        // （docs §8.22.5：引用数字前先问口径）。
        window.set_stretch_mode(cfg.stretch_mode);
        RC_LOG_INFO("StretchBlt 缩放模式 = {}（halftone = GDI 高质量插值；"
                    "coloroncolor = 删行删列/最近邻，快得多但缩小后文字易断线）",
                    window.stretch_mode_name());

        // 【UI 绘制】按脏区重绘（产品行为，默认开）。关掉 = 每帧整窗失效（旧行为）。
        // 这一项**不改采样模式、不改画质**，只是不画没变的地方；能自证的方式是
        // `[paint]` 行里的「实画面积」——开着时应明显小于 100%，关掉时应回到 ≈100%。
        window.set_partial_repaint(cfg.partial_repaint);
        window.set_invalidate_halo(cfg.invalidate_halo_px);
        RC_LOG_INFO("按脏区重绘 = {}（{}）| 失效矩形外扩 {} px"
                    "（实测 halftone 的光晕是 1 px，默认取 2；**负值 = 反向对照**）",
                    cfg.partial_repaint ? "开" : "关",
                    cfg.partial_repaint
                        ? "只失效变化区域；自证看 [paint] 的「实画面积」"
                        : "每帧整窗失效 = 引入该机制之前的行为（A/B 对照用）",
                    cfg.invalidate_halo_px);
        if (cfg.invalidate_halo_px < 0) {
            RC_LOG_WARN("invalidate_halo_px = {} < 0：失效矩形被**故意缩进**，画面会留残影"
                        " —— 这是判据的反向对照，不是可用配置", cfg.invalidate_halo_px);
        }

        // 回调先于 start() 注册：否则第一次连接成功时回调还是空的
        rc::client::ClientCallbacks callbacks;
        callbacks.on_frame = [&window](const rc::client::ScreenFrameView& f) { window.on_frame(f); };
        callbacks.on_connected = [&window] { window.on_connected(); };
        callbacks.on_disconnected = [&window](const std::string& reason) {
            window.on_disconnected(reason);
        };
        callbacks.on_rtt = [](std::int64_t rtt_ms) { RC_LOG_TRACE("rtt={} ms", rtt_ms); };
        // 中间态（连接中/握手中/重连中）也要送到界面：否则"连上但对端不回话"时
        // 标题长时间没有任何变化，看起来就像程序没启动。
        callbacks.on_state = [&window](const char* state) { window.on_state(state); };
        // 【2B 第三刀 权限模型】服务端下发的角色 —— 送到界面（标题上的 [只读]）。
        // 空实现会让"只读"变成界面上的静默降级：用户只会看到输入没反应。
        callbacks.on_role = [&window](const char* role) { window.on_role(role); };
        // 【输入→显示延迟】"输入真正进了写队列"这个瞬间只有网络层知道 ——
        // 让窗口自己记会把 asio::post 的排队时间也算进延迟里，而那不是链路的账。
        callbacks.on_input_sent = [&window](std::int64_t                     epoch,
                                           std::chrono::steady_clock::time_point at) {
            window.on_input_sent(epoch, at);
        };
        client.set_callbacks(std::move(callbacks));

        // 诊断夹具（第 2 步 2b）：输入→显示延迟判据靠它提供**可控且持续**的输入流。
        // 必须在 create() 之前调用 —— 自动输入线程是在 create() 里起的。
        if (cfg.auto_input_interval_ms > 0) {
            window.set_auto_input(cfg.auto_input_interval_ms, cfg.auto_input_x0,
                                  cfg.auto_input_x1, cfg.auto_input_y);
            // WARN 而不是 INFO：这是**往链路里塞测试信号**，拿到日志的人第一眼
            // 就该知道"这些输入不是用户产生的"。
            RC_LOG_WARN("自动输入源 enabled: 每 {} ms 发一次鼠标移动"
                        "（远端比例 x {} <-> {}，y {}）—— 诊断夹具，生产路径不会开",
                        cfg.auto_input_interval_ms, cfg.auto_input_x0, cfg.auto_input_x1,
                        cfg.auto_input_y);
        }
        // 关掉本地输入转发 = 切断同机自测的输入回灌闭环（见 ClientConfig 的说明）。
        // 同样用 WARN：这一轮的输入流是隔离过的，读日志的人必须知道。
        window.set_input_forwarding(cfg.input_forwarding);
        if (!cfg.input_forwarding) {
            RC_LOG_WARN("本地输入转发 **已关闭**：真实鼠标/键盘不再发往远端 —— "
                        "测量输入端到端延迟时用它切断同机回灌（两机部署时不存在这条回路）");
        }

        // 先建窗口再连网：这样网络回调触发时 hwnd_ 一定已经有效
        if (!window.create(nCmdShow)) {
            return 1;
        }

        client.start(); // 异步连接，UI 立刻进入消息循环，不阻塞

        const int exit_code = window.run_message_loop();

        client.stop(); // 取消定时器 + 关 socket + join io 线程（确定性退出）
        RC_LOG_INFO("client exited with code {}", exit_code);
        return exit_code;
    } catch (const std::exception& e) {
        const std::wstring msg = to_wide(std::string("\u542F\u52A8\u5931\u8D25: ") + e.what());
        ::MessageBoxW(nullptr, msg.c_str(), L"Error", MB_OK | MB_ICONERROR);
        return 1;
    }
}
