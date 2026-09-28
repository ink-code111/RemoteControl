#include "session.hpp"
#include "audit.hpp"
#include "logger.hpp"
#include "trace.hpp"

// GetCursorPos / CURSOR_SHOWING（输入打点的"读回来"与"看得见吗"两条都要它）。
// asio_common.hpp 通常已经带进来过，这里是幂等的二次包含；
// 之所以仍然显式写上，是因为本文件对这两个 API 的依赖是语义性的，不该靠别人捎带。
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>

// 2B 第二刀 TLS：do_close() 里用 SSL_shutdown() 尽力发 close_notify。
#include <openssl/ssl.h>

#include <algorithm>
#include <string>
#include <string_view>
#include <utility>

namespace rc::server {
namespace {

/// 写队列上限。慢客户端（例如把窗口拖到网络很差的链路上）如果不设上限，
/// 屏幕帧会持续堆积在服务端内存里，最终把服务端吃爆。
/// 队列里最多存 2 帧：屏幕画面本来就是"最新一帧才有意义"，堆多了纯属浪费。
constexpr std::size_t kMaxQueuedWrites = 2;

/// 抓屏连续失败时的日志抑制
constexpr std::uint32_t kCaptureFailLogEvery = 20;

/// 【坐标读回诊断】第一次读回不符时，最多再读几次看它会不会收敛到请求点。
///
/// 只在这一条样本上付出代价（正常路径一次读回就匹配、不进这个循环），
/// 换来的是"把报 2 变成可查"。取值只要够覆盖"让出一次时间片"的量级即可：
/// 竞态会在下一次调度就收敛，坐标空间错位则**读多少次都不会**收敛 —— 两者一眼可分。
constexpr std::int64_t kPosDiagTries = 16;

/// 【输入优先抓屏】的预支深度上限在 2026-09-24 从这里的编译期常量
/// （`kMaxBorrowIntervals = 1`）升成了配置项 `input_priority_max_borrow` ——
/// 理由只有一条：**它没法被扫**。而"预支 1 拍到底值多少"这件事只能靠扫出来，
/// 不是能靠读代码推出来的（改的是节奏，肉眼可见）。推导与取值理由见 config.hpp
/// 里该字段的注释，这里不再重复。
///
/// 生效值由启动日志与 `[input-prio]` 行自报 —— 配置生效 ≠ 机制生效（§8.22.2）。

/// 【2B 认证】恒定时间字符串比较（token 校验专用）。
///
/// 为什么不用 `a == b`：`std::string::operator==` 在**第一个不同的字节**就返回，
/// 于是"前 k 个字节猜对了"与"第 1 个字节就错"的耗时不同 —— 攻击者可以逐字节把
/// 凭据试出来（计时侧信道）。局域网 + 自设 token 的现实威胁不高，但正确写法只有
/// 几行、不依赖任何前提，没理由留一个"将来挪到公网就得重写"的坑。
///
/// 两个刻意的写法：
///   · 循环跑 `max(size)` 次、越界位补 0 —— 若按 `min(size)` 跑，短的那个会先结束，
///     "长度"就从耗时里漏出去了；
///   · 长度差**先并进累积值**，不做提前返回。
/// ⚠️ 残留：耗时仍与 `max(两个长度)` 相关，即**长度本身**没有完全隐藏。
///    要连长度一起隐藏得先比较固定长度的哈希（需要 crypto 库），不在本轮范围。
///    本字段（`auth_token`）眼下走明文，链路上本来就能直接看到长度 —— 先隐藏**内容**
///    才是这里该防的事。
inline bool constant_time_equals(std::string_view a, std::string_view b) {
    std::size_t diff = a.size() ^ b.size();
    const std::size_t n = a.size() > b.size() ? a.size() : b.size();
    for (std::size_t i = 0; i < n; ++i) {
        const auto ca = i < a.size() ? static_cast<unsigned char>(a[i]) : 0U;
        const auto cb = i < b.size() ? static_cast<unsigned char>(b[i]) : 0U;
        diff |= static_cast<std::size_t>(ca ^ cb);
    }
    return diff == 0;
}

} // namespace

Session::Session(asio::io_context&   io,
                 socket_type         socket,
                 std::uint32_t       id,
                 const ServerConfig& cfg,
                 SessionRegistry&    registry,
                 IInputSink&         input,
                 IScreenSource&      screen,
                 asio::thread_pool&  capture_pool,
                 asio::ssl::context* ssl_ctx)
    : io_(io),
      // 顺序要紧：strand_ 取的是**参数**的 executor，而 socket_ 会把它 move 走。
      // 成员按声明顺序初始化，声明顺序已按此排列（见 session.hpp）。
      strand_(socket.get_executor()),
      socket_(std::move(socket)),
      capture_pool_(capture_pool),
      id_(id),
      cfg_(cfg),
      registry_(registry),
      input_(input),
      screen_(screen),
      frame_timer_(strand_),
      idle_timer_(strand_) {
    connected_at_ms_ = rc::net::now_ms();
    last_rx_         = std::chrono::steady_clock::now();

    // 【2B 第二刀 TLS】把已接受的裸 socket move 进 ssl 流；此后一切收发都经 tls_。
    // socket_ 被搬空后只剩 executor（strand_ 已经从它那儿取过，见上），
    // 所以下面所有 dispatch 都**必须先判 tls_** —— 这不是风格问题，是正确性要求。
    if (ssl_ctx != nullptr) {
        tls_ = std::make_unique<ssl_stream_type>(std::move(socket_), *ssl_ctx);
    }

    error_code ec;
    const auto endpoint = tls_ ? tls_->lowest_layer().remote_endpoint(ec)
                               : socket_.remote_endpoint(ec);
    peer_ = ec ? std::string("<unknown>")
               : endpoint.address().to_string() + ":" + std::to_string(endpoint.port());
}

void Session::start() {
    auto self = shared_from_this();
    // 切回自己的 strand 再开始，保证"登记会话"与"开始读"之间不会被别的回调插队
    asio::post(strand_, [self] {
        self->registry_.add(self);
        RC_LOG_INFO("session {} started, peer={}, protocol=v{}{}", self->id_, self->peer_,
                    static_cast<int>(rc::net::kProtocolMajor),
                    self->tls_ ? ", TLS" : " (plaintext)");
        // 【2B 第三刀 审计】连上即记 —— 不等到认证。
        // 为什么连"没通过认证的连接"也要记：认证失败的那些**恰恰是审计最该有的内容**
        // （谁在试探这个端口）。若只在认证成功后记，攻击性的连接在审计里就完全隐身。
        rc::audit::event("conn_accept", "session=" + std::to_string(self->id_) + " peer=" +
                                            rc::audit::detail::sanitize(self->peer_) +
                                            " tls=" + (self->tls_ ? "on" : "off"));
        // 【2B 第二刀 TLS】启用时先握手，握手完成才读第一帧。
        // 握手失败一律断开，**绝不降级到明文** —— 静默降级会把"加密"变成一句空话，
        // 而且日志上看起来一切正常（正是本项目反复栽的那类失效）。
        if (self->tls_) {
            self->do_tls_handshake();
        } else {
            self->do_read_header();
        }

        // 空闲看门狗：只启动一次，之后每次触发自我重排（避免反复 cancel/async_wait）
        self->idle_timer_.expires_after(std::chrono::milliseconds(self->cfg_.idle_timeout_ms));
        self->idle_timer_.async_wait([self](const error_code& ec) { self->on_idle_tick(ec); });
    });
}

// ---------------------------------------------------------------------------
// 2B 第二刀：TLS 握手
// ---------------------------------------------------------------------------

void Session::do_tls_handshake() {
    auto self = shared_from_this();
    tls_->async_handshake(asio::ssl::stream_base::server, [self](const error_code& ec) {
        if (ec) {
            // 常见成因：对端根本不是 TLS（客户端 tls_enable 没开）、协议版本太低、
            // 或客户端拒绝了我们的证书。三者**处置相同**（这个连接不合法），
            // 所以只在日志里区分描述，行为一律是断开。
            RC_LOG_WARN("session {} TLS handshake failed: {}", self->id_, rc::net::describe(ec));
            self->do_close("tls handshake failed");
            return;
        }
        RC_LOG_INFO("session {} TLS handshake ok (peer={})", self->id_, self->peer_);
        self->do_read_header();
    });
}

void Session::close(const char* reason) {
    auto self = shared_from_this();
    asio::post(strand_, [self, reason = std::string(reason ? reason : "closed")] {
        self->do_close(reason.c_str());
    });
}

// ---------------------------------------------------------------------------
// 读循环
// ---------------------------------------------------------------------------

void Session::do_read_header() {
    auto self = shared_from_this();
    // async_read 的语义是"读满缓冲区才回调"，这一点直接消除了旧版手工
    // index/memmove 处理粘包/半包的全部复杂度与出错空间。
    async_read_stream(asio::buffer(header_buf_),
                      [self](const error_code& ec, std::size_t /*n*/) {
                         if (ec) {
                             self->fail("read header", ec);
                             return;
                         }

                         const auto decoded = rc::net::decode_header(self->header_buf_.data());
                         if (!decoded.ok()) {
                             // 帧头非法说明对端不是本协议或流已被污染，直接断开而不是"猜"
                             RC_LOG_WARN("session {} rejected frame header: {}", self->id_,
                                         rc::net::to_string(decoded.error));
                             self->do_close("bad frame header");
                             return;
                         }
                         if (decoded.header.payload_len == 0) {
                             RC_LOG_WARN("session {} got empty payload", self->id_);
                             self->do_close("empty payload");
                             return;
                         }

                         // 长度上限已在 decode_header 内校验，这里可以放心 resize
                         self->payload_buf_.resize(decoded.header.payload_len);
                         self->do_read_body();
                     });
}

void Session::do_read_body() {
    auto self = shared_from_this();
    async_read_stream(asio::buffer(payload_buf_),
                      [self](const error_code& ec, std::size_t n) {
                         if (ec) {
                             self->fail("read payload", ec);
                             return;
                         }
                         self->bytes_rx_ += n;
                         self->last_rx_ = std::chrono::steady_clock::now();
                         self->dispatch(self->payload_buf_.data(), n);
                         if (!self->closed_.load()) {
                             self->do_read_header(); // 继续下一帧
                         }
                     });
}

void Session::dispatch(const std::uint8_t* payload, std::size_t len) {
    rc::proto::ParsedEnvelope env;
    std::string               why;
    if (!rc::proto::parse_envelope(payload, len, env, why)) {
        // 不可信输入的第一道安全边界：Verifier 没过就说明载荷被人为构造过。
        // 不要尝试"尽力解析"——带着畸形偏移量继续读字段就是越界读。
        RC_LOG_WARN("session {} rejected payload ({} bytes): {}", id_, len, why);
        do_close("malformed payload");
        return;
    }

    const auto type = env.body_type();

    if (state_ == State::kAwaitHello) {
        if (type != rc::proto::v2::Body::Hello) {
            RC_LOG_WARN("session {} sent {} before Hello", id_, rc::proto::to_string(type));
            do_close("handshake required");
            return;
        }
        on_hello(env);
        return;
    }

    switch (type) {
    case rc::proto::v2::Body::Ping:          on_ping(env);        break;
    case rc::proto::v2::Body::MouseEvent:    on_mouse(env);       break;
    case rc::proto::v2::Body::KeyboardEvent: on_keyboard(env);    break;
    case rc::proto::v2::Body::ScreenRequest: on_screen_request(); break;
    case rc::proto::v2::Body::Hello:
        RC_LOG_DEBUG("session {} duplicate Hello ignored", id_);
        break;
    default:
        // 向前兼容的关键：老服务端收到新客户端发来的新消息类型时，
        // 必须安全忽略并继续服务，而不是断言失败或断开连接。
        RC_LOG_DEBUG("session {} ignored unsupported body type {}", id_, static_cast<int>(type));
        break;
    }
}

// ---------------------------------------------------------------------------
// 消息处理
// ---------------------------------------------------------------------------

void Session::on_hello(const rc::proto::ParsedEnvelope& env) {
    const auto* hello = env.as_hello();
    if (hello == nullptr) {
        do_close("empty hello");
        return;
    }

    const auto        client_major = hello->protocol_version();
    const std::string name = hello->client_name() ? hello->client_name()->str() : std::string("?");
    const std::string ver  = hello->client_version() ? hello->client_version()->str() : std::string("?");

    // ---- 【2B 认证 / 权限模型】先认证与授权，再看版本协商 ----
    //
    // 顺序是刻意的：**未通过认证的客户端不该拿到服务端内部状态的任何信息**，
    // 包括"本服务端的协议版本是多少"。版本不匹配也是一条可被用来做指纹识别的信息。
    //
    // 【三层回落 —— 每一层都必须是"老配置不受影响"】
    //   ① `auth_clients` 非空      -> 按表鉴权：凭据命中哪一行，就得到那一行的
    //                                 name（审计用）与 role（授权用）。
    //   ② `auth_clients` 空、`auth_token` 非空 -> 老路径：单一共享密钥，角色恒 control。
    //   ③ 两者都空                 -> 既不认证也不授权（默认配置）。这一整段不执行，
    //                                 与这两个字段存在之前**逐字等价**。
    // ①② 互斥由配置解析期保证（同时配会直接拒绝启动），所以这里不必再判冲突。
    hello_name_ = name;   // 客户端自称的名字。只进日志与审计，**不参与鉴权**。

    const std::string got_token =
        hello->auth_token() ? hello->auth_token()->str() : std::string();

    if (!cfg_.auth_clients.empty()) {
        // ---- ① 授权表路径 ----
        //
        // 【为什么遍历完所有条目、不提前 break（性能上完全可以提前返回）】
        //   一旦"命中第几条"能从耗时上看出来，攻击者就得到了一张**按可能性排序的密钥表**：
        //   逐条试探时，命中越靠前的条目耗时越短，于是搜索空间被切成一层层。
        //   这不是理论洁癖 —— 它和下面 constant_time_equals 存在的理由是同一个，
        //   只是层次不同（那个防"内容"，这个防"位置"）。
        //   代价是每次都跑满整表；本工具的客户端数是几个，可以忽略。
        const rc::AuthClientEntry* hit = nullptr;
        for (const auto& entry : cfg_.auth_clients) {
            const bool eq = constant_time_equals(got_token, entry.token);
            // 只记录命中（不跳出循环）。这个分支本身依赖秘密，但它不改控制流，
            // 所以"跑满整表"这条性质保住了。
            if (eq && hit == nullptr) {
                hit = &entry;
            }
        }

        if (hit == nullptr) {
            // 刻意区分"没带"与"带了但不在表里"：这两种情况用户的排查方向完全不同
            // —— 前者是客户端压根没配凭据，后者是配错了/没在这台服务端登记。
            // 两者都**不含任何凭据内容**，也不泄露"表里有哪些密钥"。
            const std::string reason = got_token.empty()
                ? std::string("authentication required (no token provided)")
                : std::string("authentication failed (unknown credential)");
            RC_LOG_WARN("session {} rejected: {} (peer={} claimed_name={})", id_, reason, peer_,
                        rc::audit::detail::sanitize(name));
            rc::audit::event("auth_fail", "session=" + std::to_string(id_) + " peer=" +
                                              rc::audit::detail::sanitize(peer_) +
                                              " reason=" + (got_token.empty() ? "no_token"
                                                                              : "unknown_credential") +
                                              " claimed_name=" + rc::audit::detail::sanitize(name));
            enqueue(rc::proto::make_hello_ack(id_, false, reason, rc::net::kProtocolMajor));
            close_after_flush_ = true;
            return;
        }

        role_ = (hit->role == "view") ? rc::proto::v2::Role::View : rc::proto::v2::Role::Control;
        client_label_ = hit->name;
        RC_LOG_INFO("session {} authenticated as \"{}\" (role={}, peer={})", id_, client_label_,
                    rc::proto::to_string(role_), peer_);
    } else if (!cfg_.auth_token.empty()) {
        // ---- ② 老路径：单一共享密钥，角色恒 control ----
        if (!constant_time_equals(got_token, cfg_.auth_token)) {
            // ⚠️ 这两条理由串是**既有回归的断言目标**（tests/run_auth_check.py 的
            //    REASON_MISSING / REASON_BAD）。改一个字符就会让那项判据报 1 ——
            //    所以新路径（上一条分支）用的是**另一个**理由串，两者互不干扰。
            //
            // ⚠️ 不要把收到的 token 写进日志或拒绝理由。那样做确实方便调试，
            //    但等于把凭据抄进了日志文件（而日志是最常被整份发出去排障的东西）。
            const std::string reason = got_token.empty()
                ? std::string("authentication required (no token provided)")
                : std::string("authentication failed (bad token)");
            RC_LOG_WARN("session {} rejected: {} (peer={})", id_, reason, peer_);
            rc::audit::event("auth_fail", "session=" + std::to_string(id_) + " peer=" +
                                              rc::audit::detail::sanitize(peer_) +
                                              " reason=" + (got_token.empty() ? "no_token"
                                                                              : "bad_token"));
            enqueue(rc::proto::make_hello_ack(id_, false, reason, rc::net::kProtocolMajor));
            close_after_flush_ = true;
            return;
        }
        // 老路径没有身份可言（所有人共用一把密钥）—— 身份名刻意留空，
        // 审计里会显示 client=<none>，而不是编一个"anonymouse"让人以为那是真身份。
        RC_LOG_INFO("session {} authenticated (peer={})", id_, peer_);
    }
    // ③ 两者都空：不认证不授权，role_ 保持构造时的 Control。**这一段不产生任何日志**。

    if (client_major != rc::net::kProtocolMajor) {
        const std::string reason = "protocol major mismatch (server v" +
                                   std::to_string(rc::net::kProtocolMajor) + ", client v" +
                                   std::to_string(client_major) + ")";
        RC_LOG_WARN("session {} rejected: {}", id_, reason);
        // 先把"为什么被拒"发回去再关，否则客户端只能看到一条莫名其妙的断线
        enqueue(rc::proto::make_hello_ack(id_, false, reason, rc::net::kProtocolMajor));
        close_after_flush_ = true;
        return;
    }

    state_ = State::kReady;
    RC_LOG_INFO("session {} handshake ok: client={} v{} peer={} role={}", id_, name, ver, peer_,
                rc::proto::to_string(role_));
    rc::audit::event("auth_ok", "session=" + std::to_string(id_) + " peer=" +
                                    rc::audit::detail::sanitize(peer_) +
                                    " client=" + (client_label_.empty()
                                                      ? std::string("<none>")
                                                      : rc::audit::detail::sanitize(client_label_)) +
                                    " claimed_name=" + rc::audit::detail::sanitize(name) +
                                    " role=" + rc::proto::to_string(role_) +
                                    " tls=" + (tls_ ? "on" : "off"));
    // HelloAck 里带上角色 —— 客户端据此显示"只读"并自律不再发输入（见 Role 的注释：
    // 可见性与省带宽，但**这不是安全边界**）。
    enqueue(rc::proto::make_hello_ack(id_, true, "", rc::net::kProtocolMajor, role_));
}

void Session::on_ping(const rc::proto::ParsedEnvelope& env) {
    const auto* ping = env.as_ping();
    if (ping == nullptr) {
        return;
    }
    ++ping_count_;
    // 立即回 Pong，并把客户端时间戳原样带回，客户端据此算 RTT
    enqueue(rc::proto::make_pong(ping->seq(), ping->client_time_ms(), rc::net::now_ms()));
}

void Session::on_mouse(const rc::proto::ParsedEnvelope& env) {
    const auto* ev = env.as_mouse();
    if (ev == nullptr) {
        return;
    }
    // 【2B 第三刀 权限模型】只读会话：输入在**服务端**被拒绝。
    //
    // 【为什么拦截点在这里，而不是在客户端】
    //   客户端**也会**知道自己只读、默认就不发（见 AsyncClient）—— 但那只是省带宽，
    //   **不是安全边界**：任何能连上端口的程序都能直接构造 MouseEvent 发过来。
    //   所以"不许"这件事只能由服务端说了算，客户端那道是"别浪费"。
    //   判据必须能分开证明这两件事 ⇒ 诊断开关 `debug_ignore_role` 让客户端假装不知道，
    //   把这一道单独暴露出来（见 ClientConfig::debug_ignore_role）。
    //
    // 【为什么拦在这里而不是更早（dispatch 里）】
    //   放在 dispatch 会让"被拒"与"不认识的消息类型"混在一起（那一条是 DEBUG 级的向前兼容
    //   分支），而这两件事的性质完全不同：一个是**授权决定**，一个是**版本兼容**。
    //   审计要记的是前者。
    if (view_only()) {
        note_input_denied(/*is_mouse=*/true);
        return;
    }
    rc::input::MouseEvent dom = rc::proto::mouse_from_wire(*ev);
    // 时刻必须在**调用之前**取：这一段的语义是"从会话收到事件到注入完成"，
    // 取在后面会把 proto 解析与 map 也算进去（那些是别的账）。
    const auto t0 = std::chrono::steady_clock::now();
    input_.apply_mouse(dom);
    const auto t1 = std::chrono::steady_clock::now();
    // 只有 Move / Wheel 会移动系统光标（见 InputExecutor），也只有它们能读回校验。
    const bool moves_cursor = (dom.action == rc::input::MouseAction::kMove ||
                               dom.action == rc::input::MouseAction::kWheel);
    note_input_applied(t0, t1, moves_cursor ? &dom : nullptr);
}

void Session::on_keyboard(const rc::proto::ParsedEnvelope& env) {
    const auto* ev = env.as_keyboard();
    if (ev == nullptr) {
        return;
    }
    // 【2B 第三刀 权限模型】同上：只读会话的键盘输入一律拒绝。
    // 键盘与鼠标分开计数 —— 成因完全不同（见 session.hpp 里两个计数的说明）。
    if (view_only()) {
        note_input_denied(/*is_mouse=*/false);
        return;
    }
    const auto t0 = std::chrono::steady_clock::now();
    input_.apply_keyboard(rc::proto::keyboard_from_wire(*ev));
    const auto t1 = std::chrono::steady_clock::now();
    // 键盘不移动光标，无位置可读回 —— 但仍是**一次输入应用**，必须计入序号：
    // 客户端的序号是"发出的事件数"，服务端必须是"应用的事件数"，两者一一对应。
    note_input_applied(t0, t1, nullptr);
}

/// 【2B 第三刀 权限模型】记一次"输入被授权模型拒绝"。
///
/// 【为什么要限频】真实鼠标拖动的原生频率是 125~1000 Hz，而拖动的每一次移动都是一条
/// MouseEvent。每条都写日志/审计会把文件瞬间撑爆，真正要看的那几条被冲走 ——
/// 这跟 `kCaptureFailLogEvery` 那条抑制是同一类处置。
///
/// 【但第一条必须完整写】"这个只读会话到底有没有人在试图操作"这个问题的答案就在第一条
/// 上（它同时给出对端地址与自称名）。所以第一次用 WARN 全量写，之后按 5 秒节流。
void Session::note_input_denied(bool is_mouse) {
    const auto n = ++(is_mouse ? input_denied_mouse_ : input_denied_kb_);

    const auto now     = std::chrono::steady_clock::now();
    const bool is_first = !deny_logged_first_;
    if (!is_first && (now - last_deny_log_) < std::chrono::seconds(5)) {
        return;   // 计数照涨，只是不写日志 —— 计数才是判据要读的东西
    }
    deny_logged_first_ = true;
    last_deny_log_     = now;

    const std::string kind = is_mouse ? "mouse" : "keyboard";
    if (is_first) {
        // WARN 而不是 INFO：这是**有人在试图操作一台只读会话**，值得被一眼看见。
        // 措辞上刻意写明"这不是故障"—— 否则读日志的人会去查输入链路。
        RC_LOG_WARN("session {} is **view-only**: {} input DENIED "
                    "(client=\"{}\" peer={}) —— 客户端在发输入但本会话未被授权，"
                    "这是权限拦截不是故障",
                    id_, kind, client_label_, peer_);
    } else {
        RC_LOG_INFO("session {} view-only: {} input denied (累计 {} mouse / {} keyboard)",
                    id_, kind, input_denied_mouse_.load(), input_denied_kb_.load());
    }
    rc::audit::event("input_denied",
                     "session=" + std::to_string(id_) + " role=view kind=" + kind +
                         " denied=" + std::to_string(n) + " client=" +
                         (client_label_.empty() ? std::string("<none>")
                                                : rc::audit::detail::sanitize(client_label_)));
}

void Session::note_input_applied(const std::chrono::steady_clock::time_point& t0,
                                 const std::chrono::steady_clock::time_point& t1,
                                 const rc::input::MouseEvent* pos_check) {
    const auto dur_us = std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count();
    apply_us_sum_.fetch_add(dur_us, std::memory_order_relaxed);
    // 最大值用 CAS 循环：std::atomic 没有 fetch_max（C++26 才有）。
    // 竞态下可能漏掉一两次同时发生的最大值更新，而最大值的用途只是"看看有没有离谱值"，
    // 漏一次不影响判断 —— 平均值不受这个影响（它用的是 fetch_add）。
    for (auto cur = apply_us_max_.load(std::memory_order_relaxed);;) {
        if (dur_us <= cur || apply_us_max_.compare_exchange_weak(cur, dur_us,
                                                                 std::memory_order_relaxed)) {
            break;
        }
    }

    // 登记"这一次输入在等下一次抓屏"。**每一次都登记**（环表），不能只留最近一次 ——
    // 抓屏是按客户端请求节拍走的，一个帧间隔里可能落进好几个输入，
    // 只留最近那个会让"等得更久的那些"永远不被采样（实测 10 fps 那轮空档被低估
    // 约 15 ms，正是这个原因；见 session.hpp 里 kPendingCap 的说明）。
    //
    // 顺序：先把时刻写进表项、**再**推进 head（release）。抓屏侧按 acquire 读 head，
    // 于是它看到的每个 head 值都保证对应的表项已经写好。
    {
        const auto t_ms = rc::net::now_ms();
        const int  h    = pending_head_.load(std::memory_order_relaxed);
        pending_apply_ms_[h % kPendingCap].store(t_ms, std::memory_order_relaxed);
        pending_head_.store(h + 1, std::memory_order_release);
    }

    inputs_applied_.fetch_add(1, std::memory_order_release);

    // 【输入优先抓屏】在**应用完成之后**立刻试一次：输入刚落下来是抓屏性价比最高的时刻，
    // 此刻抓就能把它收进最近的一帧；晚一拍就要多等一个帧周期。
    // 注意必须在 inputs_applied_ 自增**之后**调用 —— 上面那个比较要能看到这一次输入。
    maybe_start_input_capture();

    // 坐标读回：SetCursorPos 之后立刻问系统"光标现在在哪"。
    // 这是"输入落在正确位置"的直接证据，也是"抓屏与输入在同一坐标空间"的判据 ——
    // 服务端 DPI 未对齐时，SetCursorPos 会被系统按 1.5 倍虚拟化，这个数会大面积不符，
    // 而画面看起来一切正常（正是 docs §6.13 那条"点击系统性偏移"）。
    if (pos_check != nullptr) {
        POINT p{};
        const bool first_ok = (::GetCursorPos(&p) != FALSE) &&
                              p.x == pos_check->x && p.y == pos_check->y;
        if (first_ok) {
            pos_readback_ok_.fetch_add(1, std::memory_order_relaxed);
        } else {
            pos_readback_bad_.fetch_add(1, std::memory_order_relaxed);

            // ---- 【诊断，不参与判定】----
            // 判定已经在上面那条 fetch_add 完成了，这里只是把"不符"记成**可查的**：
            // 第一次读回的值、再读若干次能否收敛到请求点、最后读到什么。
            // 三种成因（竞态 / 外部干扰 / 坐标空间错位）在这三个量上长得完全不同，
            // 详见 session.hpp 里 pos_diag_count_ 的注释。**一个计数什么也说明不了。**
            POINT q{};
            std::int64_t tries = -1;
            for (std::int64_t i = 1; i <= kPosDiagTries; ++i) {
                if (::GetCursorPos(&q) != FALSE && q.x == pos_check->x && q.y == pos_check->y) {
                    tries = i;
                    break;
                }
                ::SwitchToThread();   // 让出时间片，给系统输入线程处理那次投递的机会
            }
            pos_diag_req_x_.store(pos_check->x, std::memory_order_relaxed);
            pos_diag_req_y_.store(pos_check->y, std::memory_order_relaxed);
            pos_diag_first_x_.store(p.x, std::memory_order_relaxed);
            pos_diag_first_y_.store(p.y, std::memory_order_relaxed);
            pos_diag_last_x_.store(q.x, std::memory_order_relaxed);
            pos_diag_last_y_.store(q.y, std::memory_order_relaxed);
            pos_diag_tries_.store(tries, std::memory_order_relaxed);
            pos_diag_count_.fetch_add(1, std::memory_order_relaxed);
        }
    }
}

void Session::report_input_stats() {
    // 挂在 on_capture_done 上做"每 5 秒一次"，而不是再开一个定时器：
    // 抓屏本身就是持续的节拍源，多一个定时器只会多一处需要收尾的东西。
    const auto now = std::chrono::steady_clock::now();
    if (last_input_report_.time_since_epoch().count() != 0 &&
        (now - last_input_report_) < std::chrono::seconds(5)) {
        return;
    }
    last_input_report_ = now;

    const auto applied = inputs_applied_.load(std::memory_order_relaxed);
    const auto us_sum  = apply_us_sum_.load(std::memory_order_relaxed);
    const auto us_max  = apply_us_max_.load(std::memory_order_relaxed);
    const auto g_sum   = gap_us_sum_.load(std::memory_order_relaxed);
    const auto g_max   = gap_us_max_.load(std::memory_order_relaxed);
    const auto g_n     = gap_count_.load(std::memory_order_relaxed);
    const auto rb_ok   = pos_readback_ok_.load(std::memory_order_relaxed);
    const auto rb_bad  = pos_readback_bad_.load(std::memory_order_relaxed);
    const auto cur_vis = cursor_visible_.load(std::memory_order_relaxed);
    const auto cur_hid = cursor_hidden_.load(std::memory_order_relaxed);

    const double apply_avg = applied > 0 ? static_cast<double>(us_sum) / applied / 1000.0 : 0.0;
    const double gap_avg   = g_n > 0 ? static_cast<double>(g_sum) / g_n / 1000.0 : 0.0;

    // 字段顺序被 tests/run_input_latency_check.py 按位置解析，改动要同步改脚本
    // （本项目的 [capture]/[decode] 也遵守这条：老日志行格式不可动，要加就另起一行）。
    RC_LOG_INFO("[input] 应用 {} 次 平均 {:.2f} / 最大 {:.2f} ms | "
                "应用→抓屏 空档 n={} 平均 {:.1f} / 最大 {:.1f} ms | "
                "坐标读回 一致 {} / 不符 {} | 光标 可见 {} / 隐藏 {}",
                applied, apply_avg, static_cast<double>(us_max) / 1000.0,
                g_n, gap_avg, static_cast<double>(g_max) / 1000.0,
                rb_ok, rb_bad, cur_vis, cur_hid);

    // 坐标不符必须吵：它不是"数值不好看"，而是"这一整轮的数字都不成立"
    // （输入落到了别处 → 画面里根本没有这次输入的效果 → 延迟测的是别的东西）。
    if (rb_bad > 0) {
        RC_LOG_WARN("[input] 坐标读回有 {} 次**不符**（SetCursorPos 之后 GetCursorPos 读回的不是请求的点）"
                    " —— 抓屏与输入不在同一坐标空间（DPI/虚拟化），"
                    "本轮的输入→显示延迟不可信", rb_bad);
        // 判据报 2 时必须自己说明"往哪查"：把诊断量原样打出来。
        // 读法（判定仍然只看上面的 rb_bad）：
        //   · tries 小（1~几次）+ first 是"另一个端点"  ⇒ 竞态，无害，可考虑重读一次即可
        //   · tries = -1 且 last 与请求点无关           ⇒ 外部干扰（真实鼠标动了）
        //   · tries = -1 且 last ≈ req/1.5              ⇒ 坐标空间错位，整轮作废（真问题）
        const auto diag_n = pos_diag_count_.load(std::memory_order_relaxed);
        if (diag_n > 0) {
            const auto req_x  = pos_diag_req_x_.load(std::memory_order_relaxed);
            const auto req_y  = pos_diag_req_y_.load(std::memory_order_relaxed);
            const auto f_x    = pos_diag_first_x_.load(std::memory_order_relaxed);
            const auto f_y    = pos_diag_first_y_.load(std::memory_order_relaxed);
            const auto l_x    = pos_diag_last_x_.load(std::memory_order_relaxed);
            const auto l_y    = pos_diag_last_y_.load(std::memory_order_relaxed);
            const auto tries  = pos_diag_tries_.load(std::memory_order_relaxed);
            RC_LOG_WARN("[input]   读回诊断（共 {} 次，下面是**最后一次**的现场）："
                        "请求 ({},{}) -> 第一次读回 ({},{}) -> 重读 {} 次后 {}",
                        diag_n, req_x, req_y, f_x, f_y, kPosDiagTries,
                        tries < 0 ? ("**始终没收敛**，最后读到 (" + std::to_string(l_x) + "," +
                                     std::to_string(l_y) + ")")
                                  : ("第 " + std::to_string(tries) + " 次收敛到请求点（= 竞态）"));
        }
    }
    // 光标被隐藏同理：capture_cursor 开着也合成不出光标，
    // "移动光标"这类输入在画面里没有任何痕迹，延迟数字测的是"帧到了"而不是"输入被看见了"。
    if (cur_hid > 0 && cur_vis == 0) {
        RC_LOG_WARN("[input] 抓屏期间系统光标一直是**隐藏**的（隐藏 {} 次 / 可见 0 次）—— "
                    "capture_cursor 开着也合成不出光标；用光标当可见响应的输入延迟判据"
                    "在本次测量里**没有测到东西**（可能命中 Windows 的\"打字时隐藏指针\"）", cur_hid);
    }

    // 【输入优先抓屏】单独一条汇总行，**不追加到上面那行**：本条是 backlog A 新增的，
    // 而上面 [input] 的字段位置被 tests/run_input_latency_check.py 按位置解析、且旧版二进制
    // 打的是同样的行 —— 动老行等于让"改造前 vs 改造后"失去可比性（本项目的老规矩）。
    // 这条行的用途只有一个：让开关**能自证生效**（写了 true 却一次都没触发 = 开关失效）。
    const auto prio = input_triggered_captures_.load(std::memory_order_relaxed);
    RC_LOG_INFO("[input-prio] 输入优先抓屏 {} | 输入触发抓屏 {} 次 / 总发帧 {} 次"
                " | 预支深度 {} 拍",
                cfg_.input_priority_capture ? "on" : "off", prio, frames_sent_,
                cfg_.input_priority_max_borrow);

    // 【2B 第三刀 权限模型】只读会话的拦截汇总，同样**另起一行**（理由同上：
    // 老行 `[input]` / `[input-prio]` 的字段位置都被判据按位置解析）。
    //
    // ⚠️ **只在真的有内容时才打这一行**：`role=control` 且一条都没拒过时**一行都不输出**。
    //    这不是为了好看 —— 是这样才能保证"权限模型未启用的会话，日志与改动前逐字相同"，
    //    而那正是回归 19 项不受影响的前提。若无条件打这行，所有既有判据的日志里都会
    //    凭空多出一行，风险不需要冒。
    const auto deny_m = input_denied_mouse_.load(std::memory_order_relaxed);
    const auto deny_k = input_denied_kb_.load(std::memory_order_relaxed);
    if (view_only() || deny_m > 0 || deny_k > 0) {
        RC_LOG_INFO("[acl] role={} client={} | 输入被拒 鼠标 {} / 键盘 {} （已应用 {}）",
                    rc::proto::to_string(role_),
                    client_label_.empty() ? "<none>" : client_label_, deny_m, deny_k,
                    inputs_applied_.load(std::memory_order_relaxed));
    }
}

void Session::on_screen_request() {
    if (closed_.load()) {
        return;
    }
    // 只允许一帧在途：客户端本来就是"收到上一帧才请求下一帧"，
    // 这里再兜一层，防止恶意客户端疯狂请求把服务端 CPU 拉满
    if (capture_in_flight_ || capture_scheduled_) {
        return;
    }

    const auto now = std::chrono::steady_clock::now();
    // 记下"请求是什么时候到的"。它与抓屏开始的间隔就是本节流等待的实际代价，
    // 抓屏器会把它算进 [capture-x] 汇总行（见 CapturedFrame::request_at）。
    request_at_ = now;
    if (now < next_capture_at_) {
        // 达到帧率上限：不丢弃请求，而是延后到允许的时刻再抓（行为与旧版一致）
        capture_scheduled_ = true;
        auto self          = shared_from_this();
        frame_timer_.expires_at(next_capture_at_);
        frame_timer_.async_wait([self](const error_code& ec) {
            self->capture_scheduled_ = false;
            if (ec) {
                return; // 定时器被取消（会话关闭）
            }
            self->schedule_capture();
        });
        return;
    }
    schedule_capture();
}

void Session::schedule_capture(bool input_triggered) {
    if (closed_.load() || capture_in_flight_) {
        return;
    }
    capture_in_flight_ = true;

    // 限流时刻在这里推进，即按"抓屏**开始**"计时，而不是按"抓屏完成"。
    //
    // 这个位置是本机实测出来的帧率腰斩点。原先写在 on_capture_done() 里
    // （完成时刻 + 间隔），于是节流等待与抓屏耗时是**相加**的：
    //     33 ms（限流等待）+ 35 ms（抓屏+编码）= 68 ms/帧 ≈ 14.7 fps
    // 而 screen_max_fps=30 的字面意思是"每秒最多 30 帧"（上限），
    // 不是"每帧额外空转 33 ms"。改成按开始时刻推进后两者**重叠**，
    // 实际周期变成 max(间隔, 抓屏耗时)，同一台机器上帧率直接翻倍。
    const auto interval =
        std::chrono::milliseconds(1000 / std::max<std::uint32_t>(1, cfg_.screen_max_fps));
    const auto now      = std::chrono::steady_clock::now();
    // 【输入优先抓屏】`max(next_capture_at_, now)` 与原来的 `now` 的区别只有一个：
    // 当这一拍是**提前**抓的（now < 下一拍）时，把后续拍子从"原来那一拍"继续往后推，
    // 而不是从"现在"重新起算。于是"提前"预支了多少，后面就顺延多少 ——
    // 每个抓屏恒消耗一拍配额，**长期平均帧率不变**（仍由 screen_max_fps 决定），
    // 改变的只是相位：从"上一帧之后"移到"输入之后"。
    // 开关关掉时不存在 now < next_capture_at_ 的调用（on_screen_request 已挡在前面），
    // 所以这一行在开关关闭时与旧行为**逐字等价**。
    next_capture_at_ = std::max(next_capture_at_, now) + interval;
    if (input_triggered) {
        input_triggered_captures_.fetch_add(1, std::memory_order_relaxed);
    }

    auto self  = shared_from_this();
    auto frame = std::make_shared<CapturedFrame>();
    // 随帧带上下单时刻，供抓屏器量"请求到达 → 抓屏开始"的空档。纯观测字段。
    frame->request_at = request_at_;
    // 抓屏 + PNG 编码是几十毫秒级的阻塞重活，放到专门的线程池去做；
    // 做完必须 post 回自己的 strand 才能改会话状态。
    asio::post(capture_pool_, [self, frame] {
        // 【输入→显示延迟】快照必须在 capture() **之前**取，理由见 CapturedFrame::input_epoch：
        // 它给出"采集开始前已应用的输入数"这个**下界**，客户端靠这个下界才敢把该帧
        // 认成那些输入的"首次可见帧"。取在 capture() 之后会把"抓屏期间才应用的输入"
        // 也算进去 —— 客户端于是把一帧并不包含该输入的画面当成它的显示时刻，
        // 延迟被系统性报小，而报小是唯一危险的方向（报大只是保守）。
        frame->input_epoch = self->inputs_applied_.load(std::memory_order_acquire);
        // 记下"这一拍已经覆盖到哪个输入序号"，供 maybe_start_input_capture() 判断
        // "还有没有输入落在所有已开始抓屏之后"。必须紧挨着上面那行写：
        // 两者读的是同一个时刻的同一个量，分开写就会互相矛盾（一个说覆盖到 5、
        // 另一个说覆盖到 3），而那种矛盾的表现是"输入明明应用了却永远等不到抓屏"。
        self->captured_epoch_.store(frame->input_epoch, std::memory_order_release);

        // "应用→抓屏"空档：把**本帧之前所有尚未采样的输入**逐个结算，而不是只看最近那一次。
        // 为什么必须逐个（见 session.hpp 里 kPendingCap 的说明）：抓屏按客户端请求的节拍走，
        // 一个帧间隔里可能落进好几个输入 —— 只结算最近那个，等于只统计了"等得最短的那些"，
        // 空档均值会系统性偏小，于是"端到端 = 空档 + 净工作 + 链路 + 解码"这本账对不上。
        {
            const auto now_ms = rc::net::now_ms();
            const int  head   = self->pending_head_.load(std::memory_order_acquire);
            int        from   = self->sampled_head_;
            if (head - from > Session::kPendingCap) {
                from = head - Session::kPendingCap; // 表被绕了一圈：只认最近 kPendingCap 个
            }
            double last_gap = -1.0;
            for (int i = from; i < head; ++i) {
                const auto t =
                    self->pending_apply_ms_[i % Session::kPendingCap].load(std::memory_order_relaxed);
                const auto gap = now_ms - t;
                // 过滤负值（跨线程读到的竞态产物）与"很久以前那次输入"（> 5 s 没有意义）
                if (t <= 0 || gap < 0 || gap > 5000) {
                    continue;
                }
                const auto gap_us = gap * 1000;
                self->gap_us_sum_.fetch_add(gap_us, std::memory_order_relaxed);
                for (auto cur = self->gap_us_max_.load(std::memory_order_relaxed);;) {
                    if (gap_us <= cur ||
                        self->gap_us_max_.compare_exchange_weak(cur, gap_us,
                                                                std::memory_order_relaxed)) {
                        break;
                    }
                }
                self->gap_count_.fetch_add(1, std::memory_order_relaxed);
                last_gap = static_cast<double>(gap);
            }
            self->sampled_head_ = head;
            if (last_gap >= 0.0) {
                frame->input_gap_ms = last_gap; // 随帧带一个样例值（单帧可读，汇总另算）
            }
        }

        // 光标可见性：被系统隐藏时（"打字时隐藏指针"、全屏应用接管指针），
        // capture_cursor 开着也合成不出光标 —— 那时"移动光标"在画面里毫无痕迹，
        // 用光标当可见响应的延迟判据就是**没有测到东西**，必须能识别出来。
        CURSORINFO ci{};
        ci.cbSize = sizeof(ci);
        if (::GetCursorInfo(&ci) != FALSE && (ci.flags & CURSOR_SHOWING) != 0) {
            self->cursor_visible_.fetch_add(1, std::memory_order_relaxed);
            frame->cursor_visible = true;
        } else {
            self->cursor_hidden_.fetch_add(1, std::memory_order_relaxed);
        }

        // 传会话 id：差异帧的基准帧是**按消费者**保存的（同一个 capturer 被所有会话共享，
        // 而每个会话手上已有的那一帧各不相同）。见 IScreenSource::capture 的注释。
        const bool ok = self->screen_.capture(*frame, self->id_);
        asio::post(self->strand_, [self, frame, ok] { self->on_capture_done(frame, ok); });
    });
}

// ---------------------------------------------------------------------------
// 输入优先抓屏（backlog A）
// ---------------------------------------------------------------------------

void Session::maybe_start_input_capture() {
    if (!cfg_.input_priority_capture || closed_.load()) {
        return;
    }
    // 已经有抓屏在跑 / 已经排定：等它收尾时再看。这里**不能**插队 ——
    // 抓屏本身是捕获线程池上的阻塞重活，同时跑两份只会互相拖慢，而且
    // "一帧在途"正是这套协议不会把服务端内存撑爆的根本原因（kMaxQueuedWrites = 2）。
    if (capture_in_flight_) {
        return;
    }
    // 有输入落在"所有已开始抓屏"之后吗？没有就不值得为它多抓一帧。
    // 这两个计数分别是 strand 与 capture 线程的单调计数，比较一次即可，
    // 不需要额外的 bool 标志在两个线程之间"置位—清零"（那样有丢失更新的窗口）。
    if (inputs_applied_.load(std::memory_order_acquire) <=
        captured_epoch_.load(std::memory_order_acquire)) {
        return;
    }
    // 客户端还没走过一次拉屏（连握手之后一帧都没请求过）就别推帧：
    // 对方此刻未必已经准备好接收屏幕帧，"主动推"会变成对协议状态机的越界。
    // 正常场景下这只影响连接刚建立的那几十毫秒。
    if (frames_sent_ == 0) {
        return;
    }

    const auto interval =
        std::chrono::milliseconds(1000 / std::max<std::uint32_t>(1, cfg_.screen_max_fps));
    const auto now = std::chrono::steady_clock::now();
    // 预支深度上限（配置项，见 config.hpp 的字段注释）。
    // 注意 next_capture_at_ 可能已经落在过去（客户端长时间没请求），
    // 那种情况下这一项为负，必然通过 —— 那是应该的：没有欠账就没有理由不抓。
    if (next_capture_at_ - now > interval * cfg_.input_priority_max_borrow) {
        return;
    }

    // 顶掉"已经排定、但还在等下一个允许时刻"的那一拍。它本来就要抓，只是被抓到前面去了，
    // 所以不算额外帧；取消之后立刻自己抓，两件事都发生在 strand 上，不会同时成立。
    //
    // 【等的是什么 —— 实测钉死过】有两种等待会在这里被跨过：
    //   ① 限流（on_screen_request 的定时器分支）；
    //   ② **服务端发完一帧后空转、等客户端收帧→贴图→再请求下一帧**。
    // 后者才是这一段的大头：服务端自己报的「请求到达 → 抓屏开始」只有 0.03 ms
    // （[capture-x]，30 fps 配置）—— 限流定时器根本没在挡人。
    // 所以这次预支省下的主要是 ②，"不等限流"只是顺带。
    //
    // 这里可以放心调 frame_timer_.cancel()：被取消的那个 handler 只做
    // `capture_scheduled_ = false; if (ec) return;`，而它会排在下面 schedule_capture()
    // 投出去的捕获任务**之前**执行（strand 上的投递是 FIFO），那时 capture_in_flight_
    // 已经是 true，on_screen_request() 不可能在中间新建一个排定项。所以那个迟到的
    // handler 只会把已经为 false 的标志再置一次 false。
    if (capture_scheduled_) {
        frame_timer_.cancel();
        capture_scheduled_ = false;
    }

    // 这一拍没有客户端请求做归因起点，用"现在"占位：抓屏器的「等请求」于是记 0，
    // 语义正确 —— 它确实没等任何人。
    request_at_ = now;
    schedule_capture(/*input_triggered=*/true);
}

void Session::on_capture_done(const std::shared_ptr<CapturedFrame>& frame, bool ok) {
    capture_in_flight_ = false;
    if (closed_.load()) {
        return;
    }

    // 注意：next_capture_at_ 不在这里推进——它已在 schedule_capture() 里按
    // "抓屏开始时刻"推进过了。写在"完成时刻"会让限流等待与抓屏耗时相加，
    // 帧率平白少一半（详见 schedule_capture() 的注释）。
    // 抓屏失败时同样不必额外补偿：下次请求进来时若还没到允许时刻，会照常走
    // on_screen_request() 的定时器分支，不会退化成忙等。

    // "空数据"要分两种情形看：抓屏失败（ok=false）是真失败；
    // 而 ok=true 且 delta=true 的空载荷是**差异帧的"本帧无变化"**——
    // 它不是错误，而且必须照常发出去，否则客户端收不到回应就永远不会请求下一帧。
    if (!ok) {
        // 抓屏失败常见于锁屏/安全桌面，属可恢复情况，不关连接
        if (++capture_fail_count_ % kCaptureFailLogEvery == 1) {
            RC_LOG_WARN("session {} capture failed (count={})", id_, capture_fail_count_);
        }
        return;
    }

    // 差异帧：dirty 非空时，data 只是那块脏区域的内容，客户端要自行合成到累积画面。
    // 注意**增量帧一律带矩形**，包括"本帧无变化"那种（此时 rect 为 0×0）：
    // 只有"整帧"才配空数组。早先把无变化编码成空数组，等于对客户端谎报成整帧，
    // 客户端既数不到这一帧、也认不出这是增量链的一环（详见 proto_codec.cpp 的注释）。
    rc::proto::DirtyRect dirty;
    const rc::proto::DirtyRect* dirty_ptr = nullptr;
    if (frame->delta) {
        dirty.x = frame->rect_x;
        dirty.y = frame->rect_y;
        dirty.w = frame->rect_w;
        dirty.h = frame->rect_h;
        dirty_ptr = &dirty;
    }

    enqueue(rc::proto::make_screen_frame(frame->width,
                                         frame->height,
                                         rc::net::now_ms(),
                                         rc::proto::v2::ImageFormat::Png,
                                         reinterpret_cast<const std::uint8_t*>(frame->data.data()),
                                         frame->data.size(),
                                         dirty_ptr,
                                         frame->input_epoch));
    ++frames_sent_;

    // 【输入优先抓屏】第二个触发点：上一拍的抓屏刚结束、通道空出来了。
    // 为什么要有这个点：输入的等待有两种 —— ① 通道空着、在等下一拍的限流时刻（这里立刻抓）；
    // ② 有抓屏在途（那时 maybe_start_input_capture() 直接返回，输入要等这一拍做完）。
    // 情形 ② 只有在这个回调里才有机会被补救，否则那个输入要一直等到客户端下一次请求。
    maybe_start_input_capture();

    // [input] 汇总（内部自己判断是否已过 5 秒）。挂在这里而不是另开定时器：
    // 抓屏本身就是持续节拍源，多一个定时器就多一处要在会话收尾时取消的东西。
    report_input_stats();
}

// ---------------------------------------------------------------------------
// 写队列
// ---------------------------------------------------------------------------

void Session::enqueue(flatbuffers::DetachedBuffer body) {
    if (closed_.load()) {
        return;
    }
    if (write_queue_.size() >= kMaxQueuedWrites) {
        RC_LOG_WARN("session {} write queue overflow ({} pending), closing slow client",
                    id_, write_queue_.size());
        do_close("write queue overflow");
        return;
    }

    write_queue_.push_back(std::make_shared<rc::net::OutgoingMessage>(std::move(body)));
    if (!writing_) {
        do_write();
    }
}

void Session::do_write() {
    if (write_queue_.empty()) {
        writing_ = false;
        if (close_after_flush_) {
            do_close("close after flush");
        }
        return;
    }

    writing_ = true;
    auto self = shared_from_this();
    auto msg  = write_queue_.front();
    // 帧头 + 载荷两段 buffer 一次 gather-write 发出：顺序由 asio 保证；
    // 同一时刻只有一个未完成的写操作，因此两条消息不会交错。
    const auto bufs = msg->buffers();
    async_write_stream(bufs, [self, msg](const error_code& ec, std::size_t n) {
        if (ec) {
            self->fail("write", ec);
            return;
        }
        self->bytes_tx_ += n;
        self->write_queue_.pop_front();
        self->do_write();
    });
}

// ---------------------------------------------------------------------------
// 空闲看门狗（服务端侧心跳兜底）
// ---------------------------------------------------------------------------

void Session::on_idle_tick(const error_code& ec) {
    if (ec || closed_.load()) {
        return; // 定时器被取消
    }
    const auto timeout = std::chrono::milliseconds(cfg_.idle_timeout_ms);
    const auto idle    = std::chrono::steady_clock::now() - last_rx_;
    if (idle >= timeout) {
        // 客户端进程被强杀时不会发 FIN；没有这条规则，半开连接会一直占着会话槽位
        RC_LOG_INFO("session {} idle for {} ms, closing", id_,
                    std::chrono::duration_cast<std::chrono::milliseconds>(idle).count());
        do_close("idle timeout");
        return;
    }

    auto self = shared_from_this();
    idle_timer_.expires_after(timeout - idle);
    idle_timer_.async_wait([self](const error_code& ec2) { self->on_idle_tick(ec2); });
}

// ---------------------------------------------------------------------------
// 关闭
// ---------------------------------------------------------------------------

void Session::do_close(const char* reason) {
    if (closed_.exchange(true)) {
        return; // 幂等：读/写/定时器可能同时报错，只处理第一次
    }
    state_ = State::kClosing;

    error_code ignored;
    // 分段打点：会话收尾一旦卡住，外在现象是"服务端整体不再响应任何连接"，
    // 而普通日志上只会看到最后一条 read/write 记录，毫无线索。
    // 这里刻意用 trace_point（无锁、立即落盘）而不是 RC_LOG_*：
    // 卡死时日志本身也可能是被卡住的那一环，必须用独立的通道才能分辨。
    RC_TRACE("close.enter", id_);
    frame_timer_.cancel();
    idle_timer_.cancel();
    RC_TRACE("close.timers_cancelled", id_);
    if (tls_) {
        // 【2B 第二刀 TLS】关之前尽力发一个 close_notify（SSL_shutdown 的非阻塞含义：
        // 它把 close_notify 写出去，返回 0 表示还在等对端的那个）。
        // 不做"两次 SSL_shutdown 的优雅流程"——那是阻塞/异步语义混用，且本工具不需要：
        // 该说的话已经由 close_after_flush_ 保证先发完了，这里只是让对端不只是看到截断。
        ::SSL_shutdown(tls_->native_handle());
        RC_TRACE("close.ssl_notify_done", id_);
        tls_->lowest_layer().shutdown(asio::ip::tcp::socket::shutdown_both, ignored);
        RC_TRACE("close.shutdown_done", id_);
        tls_->lowest_layer().close(ignored);
        RC_TRACE("close.socket_closed", id_);
    } else {
        socket_.shutdown(asio::ip::tcp::socket::shutdown_both, ignored);
        RC_TRACE("close.shutdown_done", id_);
        socket_.close(ignored);
        RC_TRACE("close.socket_closed", id_);
    }
    registry_.remove(id_);
    RC_TRACE("close.registry_removed", id_);
    // 归还差异帧的基准帧（6.5 MB/客户端）。不还的话，每个连过的客户端都会在
    // 服务端留下一份基准位图——运行几天就是一条只增不减的内存曲线。
    screen_.release_consumer(id_);
    RC_TRACE("close.delta_base_released", id_);
    RC_LOG_INFO("session {} closed ({}) rx={} B tx={} B frames={} pings={}",
                id_, reason, bytes_rx_, bytes_tx_, frames_sent_, ping_count_);
    // 【2B 第三刀 审计】会话结束 —— 一条记录就把"这次会话干了什么"封口：
    // 谁（client）、什么权限（role）、多久（dur）、传了多少（frames/tx/rx）、
    // 有没有被拒的输入（denied）、以及**为什么结束**（reason）。
    //
    // 为什么 reason 必须记：它区分了三种性质完全不同的收尾 ——
    //   正常断开 / 空闲超时 / 被权限模型或协议拒绝。少了它，审计里
    //   "一个只连了 0.2 秒就断的会话"看不出是被拒还是网络抖了一下。
    {
        const auto dur_ms =
            std::max<std::int64_t>(0, rc::net::now_ms() - connected_at_ms_);
        rc::audit::event(
            "session_end",
            "session=" + std::to_string(id_) + " reason=" + rc::audit::detail::sanitize(reason) +
                " client=" + (client_label_.empty() ? std::string("<none>")
                                                    : rc::audit::detail::sanitize(client_label_)) +
                " claimed_name=" + rc::audit::detail::sanitize(hello_name_) +
                " role=" + rc::proto::to_string(role_) +
                " dur_ms=" + std::to_string(dur_ms) + " frames=" + std::to_string(frames_sent_) +
                " tx=" + std::to_string(bytes_tx_) + " rx=" + std::to_string(bytes_rx_) +
                " denied_mouse=" + std::to_string(input_denied_mouse_.load()) +
                " denied_kb=" + std::to_string(input_denied_kb_.load()));
    }
    RC_TRACE("close.logged", id_);
}

void Session::fail(const char* what, const error_code& ec) {
    if (rc::net::is_disconnect(ec)) {
        RC_LOG_INFO("session {} {} ended: {}", id_, what, rc::net::describe(ec));
    } else {
        RC_LOG_WARN("session {} {} failed: {}", id_, what, rc::net::describe(ec));
    }
    do_close(what);
}

} // namespace rc::server
