#include "async_client.hpp"
#include "logger.hpp"

#include <algorithm>

#include <windows.h>

// 2B 第二刀 TLS：teardown() 里尽力发 close_notify，并在日志里报出协商到的版本。
#include <openssl/ssl.h>

#include <stdexcept>
#include <string>

namespace rc::client {
namespace {

constexpr const char* kClientVersion = "2.0.0";

/// 取机器名做客户端标识（仅用于服务端日志区分多客户端；拿不到就退化成 unknown）
///
/// 【为什么用 Win32 API 而不是 std::getenv("COMPUTERNAME")】
///   1) 环境变量是可以被父进程覆盖甚至清空的，API 才是机器名的权威来源；
///   2) MSVC 把 std::getenv 标成不安全（C4996），本项目要求零警告构建，
///      与其用 _CRT_SECURE_NO_WARNINGS 把它按下去，不如直接换成正确的 API。
std::string host_name() {
    char  buf[MAX_COMPUTERNAME_LENGTH + 1] = {};
    DWORD len                              = static_cast<DWORD>(sizeof(buf));
    if (::GetComputerNameA(buf, &len) != 0 && len > 0) {
        return std::string(buf, len);
    }
    return "unknown";
}

} // namespace

AsyncClient::AsyncClient(const ClientConfig& cfg)
    : cfg_(cfg),
      work_(asio::make_work_guard(io_)),
      resolver_(io_),
      socket_(io_),
      heartbeat_timer_(io_),
      hello_timer_(io_),
      reconnect_timer_(io_),
      frame_timer_(io_) {
    // 【2B 第二刀 TLS】启用时在构造期就把上下文建好，失败**直接抛**（由 WinMain 的
    // try/catch 弹窗 + 非 0 退出），绝不静默降级到明文 —— 静默降级会把"加密"变成
    // 一句空话，而日志上看起来一切正常（本项目在"配置静默失效"上栽过多次）。
    //
    // 放在构造函数而不是 start() 里：这样"配置非法"在**窗口出现之前**就暴露，
    // 而不是连上以后才发现自己在裸奔。
    if (cfg_.tls_enable) {
        std::string err;
        ssl_ctx_ = rc::tls::make_client_context(cfg_.tls_pin_sha256, &err);
        if (!ssl_ctx_) {
            throw std::runtime_error("TLS 客户端上下文创建失败: " + err);
        }
    }
}

AsyncClient::~AsyncClient() {
    stop();
}

// ---------------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------------

void AsyncClient::start() {
    // work guard 保证 io_context.run() 在"正在重连、暂无异步操作"的窗口期不会提前返回
    io_thread_ = std::thread([this] {
        try {
            io_.run();
        } catch (const std::exception& e) {
            RC_LOG_ERROR("client io thread terminated by exception: {}", e.what());
        }
    });
    asio::post(io_, [this] { do_resolve(); });
}

void AsyncClient::stop() {
    if (state_ == State::kStopped) {
        if (io_thread_.joinable()) {
            io_thread_.join();
        }
        return;
    }
    asio::post(io_, [this] {
        teardown("client stopping");
        state_ = State::kStopped;
        work_.reset(); // 释放后 io_context.run() 才能自然返回
    });
    if (io_thread_.joinable()) {
        io_thread_.join();
    }
    RC_LOG_INFO("client stopped");
}

// ---------------------------------------------------------------------------
// 对外发送接口（UI 线程调用 → 投递到 io 线程）
// ---------------------------------------------------------------------------

void AsyncClient::send_mouse(const rc::input::MouseEvent& ev) {
    asio::post(io_, [this, ev] {
        if (state_ != State::kReady) {
            return;
        }
        // 【2B 第三刀 权限模型】只读会话**自律不发**（省的是上行带宽与一次注定被拒的往返）。
        //
        // ⚠️ 这一层**不是安全边界** —— 判据必须能把它与服务端那道拦截分开证明，
        //    所以 `debug_ignore_role` 能绕过这里（见 ClientConfig 的说明）。
        //
        // 注意这里**不能**自增 input_epoch_：那个计数与服务端的 inputs_applied_
        // 是同一个整数序列（靠 TCP 保序不丢），而服务端对被拒的输入也不会自增。
        // 两边都不计数，等式才继续成立 —— 只读会话因此天然不参与延迟配对。
        if (view_only_ && !cfg_.debug_ignore_role) {
            suppressed_inputs_.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        enqueue(rc::proto::make_mouse(ev));
        notify_input_sent();
    });
}

void AsyncClient::send_keyboard(const rc::input::KeyboardEvent& ev) {
    asio::post(io_, [this, ev] {
        if (state_ != State::kReady) {
            return;
        }
        // 【2B 第三刀 权限模型】同上。键盘与鼠标走同一个判定，
        // 但**分开不做计数区分**：这一侧的数只回答"客户端自律拦了多少"，
        // 而"哪一类被拦得多"归服务端的 input_denied_mouse_ / _kb_ 回答。
        if (view_only_ && !cfg_.debug_ignore_role) {
            suppressed_inputs_.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        enqueue(rc::proto::make_keyboard(ev));
        notify_input_sent();
    });
}

void AsyncClient::request_frame() {
    asio::post(io_, [this] { schedule_next_frame_request(); });
}

// ---------------------------------------------------------------------------
// 连接流程
// ---------------------------------------------------------------------------

void AsyncClient::do_resolve() {
    if (state_ == State::kStopped || fatal_) {
        return;
    }
    state_ = State::kResolving;
    RC_LOG_INFO("resolving {}:{}", cfg_.server_host, cfg_.server_port);
    emit_state();

    resolver_.async_resolve(cfg_.server_host, std::to_string(cfg_.server_port),
                            [this](const error_code& ec, tcp::resolver::results_type results) {
                                if (ec) {
                                    fail("resolve", ec);
                                    return;
                                }
                                endpoints_ = std::move(results);
                                do_connect();
                            });
}

void AsyncClient::do_connect() {
    if (state_ == State::kStopped || fatal_) {
        return;
    }
    state_ = State::kConnecting;
    emit_state();

    error_code ec;
    // 【2B 第二刀 TLS】先丢掉上一轮的 ssl 流。它持有的是**已经关掉的**那个 socket，
    // 留着会让 transport_open() / remote_endpoint 读到过期状态，而下一轮握手又必须
    // 建立在一个全新的连接上（TLS 会话不可跨连接复用）。
    tls_.reset();
    socket_.close(ec);
    // 每次重连都换一个全新的 socket：避免上一次失败留下的半关闭状态影响新连接
    socket_ = tcp::socket(io_);

    // 【网络层 Nagle，§6.29】新 socket 一创建、**还没连上对端**就立即按配置设 TCP_NODELAY。
    // TCP_NODELAY 是 IPPROTO_TCP 级别的选项，不依赖连接状态（no_delay(true) 在已 open
    // 但未 connected 的 socket 上也允许），所以这里就是最早的、最早的"立即"。
    // 之后无论走 TLS 还是明文都生效 —— TLS 路径下 socket 会被 move 进 ssl 流，但底层
    // TCP socket 的选项被原样带走；服务端对称地也在 accept 回调里设一次。
    if (cfg_.tcp_nodelay) {
        error_code ignored;
        socket_.set_option(asio::ip::tcp::no_delay(true), ignored);
    }

    RC_LOG_INFO("connecting ...");
    asio::async_connect(socket_, endpoints_,
                        [this](const error_code& e, const tcp::endpoint& ep) {
                            if (e) {
                                fail("connect", e);
                                return;
                            }
                            RC_LOG_INFO("tcp connected to {}:{}",
                                        ep.address().to_string(), ep.port());
                            on_connected();
                        });
}

void AsyncClient::on_connected() {
    state_ = State::kHandshaking;
    emit_state();

    if (ssl_ctx_) {
        // 【2B 第二刀 TLS】TCP 之上先做一次 TLS 握手，握手完成前**不读、不发 Hello**
        // —— 明文读循环读到的会是密文（会直接判成 bad frame header 并 fatal）。
        //
        // 先武装"预就绪"超时：TCP 连上不等于对端在工作，而 TLS 握手有一个纯明文的
        // 客户端不存在的卡法 —— 对端接受连接后**不再推进握手**。没有这条上限，
        // 本机会永远停在 kHandshaking（这正是本项目记过的四个异步重连坑之一）。
        // 握手成功进 begin_session() 时会**重新**武装，于是两个阶段各有完整窗口。
        start_hello_timeout();
        do_tls_handshake();
        return;
    }

    begin_session();
}

void AsyncClient::do_tls_handshake() {
    // 把已连上的裸 socket move 进 ssl 流；此后一切收发都经 tls_。
    // （socket_ 被搬空后只剩 executor —— 所以下面所有 dispatch 都**必须先判 tls_**，
    //   这不是风格问题，是正确性要求。见 transport_open()。）
    tls_ = std::make_unique<ssl_stream_type>(std::move(socket_), *ssl_ctx_);

    tls_->async_handshake(asio::ssl::stream_base::client, [this](const error_code& ec) {
        if (ec) {
            // 【为什么分两类，而不是一律 fatal_】
            //   ① **配置分歧**（证书指纹不匹配、协议版本太低、对端根本不是 TLS、
            //      或客户端 pin 的证书换了）—— 重连一万次结果一样，所以不重试：
            //      置 fatal_ 后 schedule_reconnect() 会打 "not reconnecting (unrecoverable)"
            //      并停在 kStopped。这条与「认证被拒」走的是同一个语义。
            //   ② **对端消失**（握手途中进程被杀/网线拔了/被 RST）—— 是暂时性的，
            //      必须照常重连；否则"服务端重启时恰好在握手"会让客户端永久躺死。
            //   两者的区分标准就是 is_disconnect()：它认的是"对端关闭"，不是"故障"。
            if (rc::net::is_disconnect(ec)) {
                RC_LOG_WARN("TLS handshake interrupted: {}", rc::net::describe(ec));
            } else {
                RC_LOG_ERROR("TLS handshake failed: {} —— 证书指纹/协议版本不匹配，"
                             "重连也不会变（客户端只需改 tls_pin_sha256 或 tls_enable）",
                             rc::net::describe(ec));
                fatal_ = true;
            }
            teardown("tls handshake failed");
            schedule_reconnect("tls handshake failed");
            return;
        }
        // 协商到的版本要打出来：它是"确实走了 TLS"最直接的一条证据
        //（判据靠它区分"TLS 生效"与"配置写了但没生效"）。
        RC_LOG_INFO("TLS handshake ok: {} / {}",
                    ::SSL_get_version(tls_->native_handle()),
                    ::SSL_get_cipher(tls_->native_handle()));
        begin_session();
    });
}

void AsyncClient::begin_session() {
    // 先开读循环，再发 Hello：服务端的 HelloAck 可能很快就到，
    // 如果先发后读，理论上存在"回包比读发起更早"的窗口（虽然内核有缓冲，
    // 但顺序上先挂读更稳）。
    do_read_header();
    send_hello();
    start_heartbeat();
    // 握手（含握手后等 HelloAck）的独立时间上限：没有它，对端"连上但不回话"
    // 会让本机永远停在 kHandshaking，而这条路径此前是完全静默的。
    start_hello_timeout();
}

void AsyncClient::send_hello() {
    // 【2B 认证】把配置里的共享密钥随首包发出。空配置 = 不提供凭据 ——
    // 对"未启用认证的服务端"这正是正确语义（老行为逐字不变）。
    enqueue(rc::proto::make_hello(host_name(), kClientVersion, rc::net::kProtocolMajor,
                                  cfg_.auth_token));
}

void AsyncClient::start_hello_timeout() {
    hello_timer_.expires_after(std::chrono::milliseconds(cfg_.hello_timeout_ms));
    hello_timer_.async_wait([this](const error_code& ec) {
        if (ec) {
            return; // 被取消：握手已成功，或连接已被 teardown
        }
        if (state_ != State::kHandshaking) {
            return; // 状态已经走过去了（防御性判断，正常不会命中）
        }
        // 走到这里说明：TCP 连上了、Hello 也发出去了，但对端没有回 HelloAck。
        // 心跳看门狗在握手阶段是主动跳过的，所以这条路径此前是完全静默的。
        RC_LOG_WARN("handshake timeout: no HelloAck within {} ms, connection considered dead",
                    cfg_.hello_timeout_ms);
        teardown("handshake timeout");
        schedule_reconnect("handshake timeout");
    });
}

void AsyncClient::emit_state() {
    if (callbacks_.on_state) {
        callbacks_.on_state(state_name());
    }
}

// ---------------------------------------------------------------------------
// 读循环
// ---------------------------------------------------------------------------

void AsyncClient::do_read_header() {
    async_read_stream(asio::buffer(header_buf_),
                      [this](const error_code& ec, std::size_t /*n*/) {
                         if (ec) {
                             if (is_local_abort(ec)) {
                                 return; // 我们在 teardown 里关的 socket，不是新故障
                             }
                             fail("read header", ec);
                             return;
                         }
                         const auto decoded = rc::net::decode_header(header_buf_.data());
                         if (!decoded.ok()) {
                             RC_LOG_ERROR("server sent invalid frame header: {}",
                                          rc::net::to_string(decoded.error));
                             teardown("bad frame header");
                             fatal_ = true; // 对端不是本协议，重连也没用
                             schedule_reconnect("bad frame header");
                             return;
                         }
                         payload_buf_.resize(decoded.header.payload_len);
                         do_read_body();
                     });
}

void AsyncClient::do_read_body() {
    async_read_stream(asio::buffer(payload_buf_),
                      [this](const error_code& ec, std::size_t n) {
                         if (ec) {
                             if (is_local_abort(ec)) {
                                 return; // 同上：本地关闭产生的"中止"，忽略
                             }
                             fail("read payload", ec);
                             return;
                         }
                         dispatch(payload_buf_.data(), n);
                         if (state_ != State::kStopped) {
                             do_read_header();
                         }
                     });
}

void AsyncClient::dispatch(const std::uint8_t* payload, std::size_t len) {
    rc::proto::ParsedEnvelope env;
    std::string               why;
    if (!rc::proto::parse_envelope(payload, len, env, why)) {
        RC_LOG_ERROR("server payload rejected: {}", why);
        teardown("malformed payload from server");
        fatal_ = true;
        schedule_reconnect("malformed payload from server");
        return;
    }

    switch (env.body_type()) {
    case rc::proto::v2::Body::HelloAck: {
        const auto* ack = env.as_hello_ack();
        if (ack == nullptr) {
            break;
        }
        if (!ack->accepted()) {
            const std::string reason =
                ack->reject_reason() ? ack->reject_reason()->str() : std::string("rejected");
            RC_LOG_ERROR("handshake rejected by server: {}", reason);
            teardown(reason.c_str());
            fatal_ = true; // 版本不匹配之类的问题，重连一万次也是一样的结果
            schedule_reconnect("handshake rejected");
            break;
        }
        state_        = State::kReady;
        connected_    = true;
        attempts_     = 0;
        last_pong_at_ = std::chrono::steady_clock::now();
        hello_timer_.cancel(); // 握手完成，撤掉握手超时看门狗

        // 【2B 第三刀 权限模型】读出服务端下发的角色。
        // 老服务端不发这个字段 ⇒ `role()` 返回默认 Control(=0) ⇒ 与改动前逐字等价。
        const auto role = ack->role();
        view_only_      = (role == rc::proto::v2::Role::View);
        RC_LOG_INFO("handshake ok: session_id={}, server=v{}, role={}", ack->session_id(),
                    ack->protocol_version(), rc::proto::to_string(role));
        if (view_only_) {
            // WARN 而不是 INFO：这是一次**权限降级**，且它会让"输入没反应"看起来像故障。
            // 客户端自己喊一句，避免用户从"界面没反应"反推到"链路坏了"。
            RC_LOG_WARN("本会话是 **只读**（服务端 role=view）：画面照常，"
                        "鼠标/键盘**不会**被发送。这不是输入链路故障，是服务端未授予写权限。");
            if (cfg_.debug_ignore_role) {
                // 诊断开关打开时必须大声说 —— 它让"服务端拦截"这一道单独暴露出来，
                // 拿到日志的人第一眼就该知道"这些输入是故意发上去让人拒的"。
                RC_LOG_WARN("⚠️ debug_ignore_role=on：**仍然会**把输入发上去（会被服务端拒绝）。"
                            "这是判据用来证明\"服务端真的在拦\"的诊断开关，不是生产配置。");
            }
        }
        if (callbacks_.on_role) {
            callbacks_.on_role(rc::proto::to_string(role));
        }
        if (callbacks_.on_connected) {
            callbacks_.on_connected();
        }
        // 握手完成立刻开始拉第一帧
        next_frame_at_ = std::chrono::steady_clock::now();
        schedule_next_frame_request();
        break;
    }

    case rc::proto::v2::Body::Pong: {
        const auto* pong = env.as_pong();
        if (pong == nullptr) {
            break;
        }
        last_pong_at_ = std::chrono::steady_clock::now();
        const auto rtt = static_cast<std::int64_t>(rc::net::now_ms() - pong->client_time_ms());
        last_rtt_ms_   = rtt;
        if (callbacks_.on_rtt) {
            callbacks_.on_rtt(rtt);
        }
        break;
    }

    case rc::proto::v2::Body::ScreenFrame: {
        const auto* frame = env.as_screen_frame();
        if (frame == nullptr) {
            break;
        }
        // 先判"这一帧有没有在途请求"，再清标志 —— 顺序不能反：清掉之后就分不出来了。
        // 见 ScreenFrameView::requested 的注释（为什么这个分类必须自证）。
        const bool had_outstanding_request = frame_in_flight_;
        frame_in_flight_ = false;
        ++frames_received_;

        ScreenFrameView view;
        view.width        = frame->width();
        view.height       = frame->height();
        view.timestamp_ms = frame->timestamp_ms();
        view.input_epoch  = frame->input_epoch();
        view.requested    = had_outstanding_request;
        if (const auto* data = frame->data()) {
            view.data = data->data();
            view.size = data->size();
        }
        // 脏矩形非空 = 增量帧：data 只是那块区域的内容（本项目当前最多 1 个）。
        // 空数组 = 整帧；这与第二阶段的行为一致，因此新旧两端可以互通。
        if (const auto* rects = frame->dirty_rects(); rects != nullptr && rects->size() > 0) {
            if (const auto* r = rects->Get(0); r != nullptr) {
                view.delta  = true;
                view.rect_x = r->x();
                view.rect_y = r->y();
                view.rect_w = r->w();
                view.rect_h = r->h();
            }
        }
        // 归因：请求发出 → 本帧到达。放在回调之前算，避免把 on_frame 里的
        // memcpy 也算进"链路时间"里（那部分归客户端自己的账）。
        //
        // ⚠️ 只在"这一帧确实是在途请求的应答"时算（见 ScreenFrameView::requested）：
        // 没有在途请求的帧是服务端主动推的，last_screen_request_at_ 指向一个已经
        // 结算过的旧请求，算出来会是个**看似正常的错数**。宁可留 0 并让上层计数。
        if (had_outstanding_request && last_screen_request_at_.time_since_epoch().count() != 0) {
            view.wire_ms = std::chrono::duration<double, std::milli>(
                               std::chrono::steady_clock::now() - last_screen_request_at_)
                               .count();
        }
        // 注意零拷贝：view.data 指向读缓冲，回调返回后即失效
        if (callbacks_.on_frame) {
            callbacks_.on_frame(view);
        }
        schedule_next_frame_request();
        break;
    }

    default:
        RC_LOG_DEBUG("ignored body type {}", static_cast<int>(env.body_type()));
        break;
    }
}

// ---------------------------------------------------------------------------
// 写队列
// ---------------------------------------------------------------------------

void AsyncClient::notify_input_sent() {
    // 时刻取在**入队之后**：这里记的是"输入已经交给内核发送"的时刻。取在入队之前
    // 会把 make_mouse() 的编码耗时也算进"输入→显示"里 —— 那是客户端自己的 CPU 时间，
    // 会让这个指标对编码实现的变化敏感，而它本该只反映链路与显示。
    //
    // 注意 enqueue() 内部还有一次 state_ == kStopped 的检查：极端时序下（本帧投递之后、
    // 执行之前连接被拆掉）它可能直接返回、事件并未真的发出，而我们仍然记了一笔。
    // 这种情况只出现在连接正在关闭时，届时整个测量已经作废（判据会看到两端计数不一致
    // 或配对率过低而报"没测到"），所以不值得为此引入额外的耦合。
    const auto at = std::chrono::steady_clock::now();
    ++input_epoch_;
    if (callbacks_.on_input_sent) {
        callbacks_.on_input_sent(input_epoch_, at);
    }
}

void AsyncClient::enqueue(flatbuffers::DetachedBuffer body) {
    if (state_ == State::kStopped) {
        return;
    }
    write_queue_.push_back(std::make_shared<rc::net::OutgoingMessage>(std::move(body)));
    if (!writing_) {
        do_write();
    }
}

void AsyncClient::do_write() {
    if (write_queue_.empty()) {
        writing_ = false;
        return;
    }
    writing_ = true;
    auto       msg  = write_queue_.front();
    const auto bufs = msg->buffers();
    async_write_stream(bufs, [this, msg](const error_code& ec, std::size_t /*n*/) {
        if (ec) {
            if (is_local_abort(ec)) {
                return; // 本地关闭产生的"中止"，忽略
            }
            fail("write", ec);
            return;
        }
        // ---- 先确认"这条消息还在队首"，再出队 ----
        //
        // ec == 0 只说明**这次写成功了**，不说明队列里还躺着它。
        // 数据一旦交给内核，完成处理器就只是排在 io_context 队列里等着被执行；
        // 若在那之前有别的处理器（读失败、心跳超时、用户关窗）先跑了并调用
        // teardown()，write_queue_ 就已经被 clear() 掉。这个处理器稍后照常执行，
        // 于是一头撞进空的 deque —— Debug 下直接触发
        //   _STL_VERIFY(!empty(), "pop_front() called on empty deque")
        // 弹"运行时检查失败 #0"并中断（Release 下是未定义行为）。
        //
        // 第二种情况同样要挡：期间已经重连、并且 enqueue() 又启动了新一轮写。
        // 此时队首是新消息而不是 msg，若继续往下走就会在已有 async_write 在途时
        // 再发一次 —— 两条写并发交错会直接损坏协议流（帧头与负载错位）。
        //
        // 用"队首是不是我这条"做判据，一个判断同时挡住上面两种情况，
        // 而且不需要额外的世代计数器：write_queue_ 只在 teardown() 里被清空、
        // 只在下面这一行被出队，所以"队首仍是 msg"等价于"什么都没发生过"。
        if (write_queue_.empty() || write_queue_.front() != msg) {
            RC_LOG_DEBUG("write completed after queue reset, dropping stale completion");
            return;
        }
        write_queue_.pop_front();
        do_write();
    });
}

// ---------------------------------------------------------------------------
// 心跳
// ---------------------------------------------------------------------------

void AsyncClient::start_heartbeat() {
    heartbeat_timer_.expires_after(std::chrono::milliseconds(cfg_.heartbeat_interval_ms));
    heartbeat_timer_.async_wait([this](const error_code& ec) {
        if (ec) {
            return; // 被取消（正常关闭路径）
        }
        if (state_ != State::kReady) {
            // 还没握手完成（或正在重连）：继续下一轮，别在这里判超时
            start_heartbeat();
            return;
        }

        const auto timeout = std::chrono::milliseconds(cfg_.heartbeat_timeout_ms);
        if (std::chrono::steady_clock::now() - last_pong_at_ >= timeout) {
            // 关键：TCP 半开连接可能几十秒都不报错，只有心跳能及时发现"链路其实已经死了"
            RC_LOG_WARN("heartbeat timeout ({} ms without Pong), connection considered dead",
                        cfg_.heartbeat_timeout_ms);
            heartbeat_timer_.cancel();
            teardown("heartbeat timeout");
            schedule_reconnect("heartbeat timeout");
            return;
        }

        send_ping();
        start_heartbeat();
    });
}

void AsyncClient::send_ping() {
    enqueue(rc::proto::make_ping(++ping_seq_, rc::net::now_ms()));
}

// ---------------------------------------------------------------------------
// 拉屏
// ---------------------------------------------------------------------------

void AsyncClient::schedule_next_frame_request() {
    if (state_ != State::kReady || frame_in_flight_) {
        return;
    }
    const auto interval = std::chrono::milliseconds(1000 / std::max<std::uint32_t>(1, cfg_.target_fps));
    const auto now      = std::chrono::steady_clock::now();

    if (now < next_frame_at_) {
        // 达到目标帧率上限：等到允许的时刻再请求（保持"收到一帧才请求下一帧"的行为）
        frame_timer_.expires_at(next_frame_at_);
        frame_timer_.async_wait([this](const error_code& ec) {
            if (!ec) {
                schedule_next_frame_request();
            }
        });
        return;
    }

    frame_in_flight_ = true;
    next_frame_at_   = now + interval;
    // 记下请求"发出"的时刻（enqueue 是同步入队，紧接着就会开始写）。
    // 用它和收帧时刻算 wire_ms —— 端到端帧周期里唯一跨进程的那一段。
    last_screen_request_at_ = now;
    // max_width/quality 目前传 0（不限制）——第三阶段用于带宽自适应
    enqueue(rc::proto::make_screen_request(0, 0));
}

// ---------------------------------------------------------------------------
// 失败处理与重连
// ---------------------------------------------------------------------------

bool AsyncClient::is_local_abort(const error_code& ec) const noexcept {
    // 两个条件任一成立即认定是"本地关闭"，而不是对端出问题：
    //   ① 错误码就是 operation_aborted —— asio 对"操作被取消"的标准约定；
    //   ② 底层 TCP 已经不在打开状态 —— teardown() 里 close 掉了它。
    // 同时看两项，是因为不同平台/asio 版本对这类错误码的归类并不完全一致，
    // 而"还开着吗"是确定的事实。对端主动断开时连接仍然打开，所以那条正常路径不会被误伤。
    //
    // ⚠️ 【2B 第二刀 TLS】条件 ② 必须走 transport_open() 而不是 socket_.is_open()：
    //    走 TLS 时 socket_ 已被 move 进 ssl 流、只剩一个空的 executor，
    //    is_open() 会**恒为 false** ⇒ 每一次真实的对端断开都会被误判成"我们自己关的"，
    //    于是 fail() 不执行、日志里既没有 "ended" 也没有重连 —— 一次故障被**静默吞掉**。
    //    这是把传输层换掉时最容易漏、又最难在日志上看出来的一处。
    return ec == asio::error::operation_aborted || !transport_open();
}

void AsyncClient::fail(const char* what, const error_code& ec) {
    if (state_ == State::kStopped) {
        return;
    }
    if (rc::net::is_disconnect(ec)) {
        RC_LOG_INFO("{} ended: {}", what, rc::net::describe(ec));
    } else {
        RC_LOG_WARN("{} failed: {}", what, rc::net::describe(ec));
    }
    teardown(what);
    schedule_reconnect(what);
}

void AsyncClient::teardown(const char* reason) {
    const bool was_active = (state_ == State::kReady || state_ == State::kHandshaking);

    error_code ec;
    heartbeat_timer_.cancel();
    hello_timer_.cancel();
    frame_timer_.cancel();
    if (tls_) {
        // 【2B 第二刀 TLS】关之前尽力发一个 close_notify。非阻塞语义：它把
        // close_notify 写出去、返回 0 表示还在等对端那个；本工具不做"两次
        // SSL_shutdown 的优雅流程"（那是阻塞/异步语义混用，收益只有"对端日志好看"）。
        // 效果：对端看到的是正常的 TLS 关闭，而不是"stream_truncated"。
        ::SSL_shutdown(tls_->native_handle());
        tls_->lowest_layer().shutdown(tcp::socket::shutdown_both, ec);
        tls_->lowest_layer().close(ec);
    } else {
        socket_.shutdown(tcp::socket::shutdown_both, ec);
        socket_.close(ec);
    }
    write_queue_.clear();
    writing_         = false;
    frame_in_flight_ = false;
    connected_       = false;
    // 【2B 第三刀 权限模型】只读自律的**读回来**。
    //
    // 为什么必须在复位 view_only_ 之前打：复位之后这一行就没法知道"刚才是不是只读会话"，
    // 于是"客户端没发输入"与"客户端发了但服务端拒了"在日志上分不出来 —— 而判据的
    // 全部意义就在于把这两件事分开（前者是自律，后者是服务端拦截）。
    // 同样地，`[acl]` 这个前缀是本刀新开的一族观测行，与老行不冲突（老规矩：另起一行）。
    if (view_only_ || suppressed_inputs_.load() > 0) {
        RC_LOG_INFO("[acl] 只读自律：本地丢弃输入 {} 条（服务端 role=view；"
                    "这些输入**没有**发出去，服务端的 input_denied 不会因此增长）",
                    suppressed_inputs_.load());
    }

    // 【2B 第三刀 权限模型】角色是"**这次**会话的属性"，不是"这台机器的身份"。
    // 不复位的话，连过一次只读服务端之后，重连到普通服务端仍会自作主张地不发输入 ——
    // 而日志上完全看不出原因（那条只读 WARN 属于上一条连接）。
    view_only_       = false;

    if (was_active) {
        RC_LOG_INFO("disconnected: {}", reason);
        if (callbacks_.on_disconnected) {
            callbacks_.on_disconnected(reason);
        }
    }
}

void AsyncClient::schedule_reconnect(const char* reason) {
    if (state_ == State::kStopped) {
        return;
    }
    if (fatal_) {
        RC_LOG_ERROR("not reconnecting (unrecoverable): {}", reason);
        state_ = State::kStopped;
        // 【2026-09-27】把"不会再重连"送到界面。
        // 为什么必须在这里发：标题在"无中间态"时的默认文案是「重连中…」，而这条分支
        // 恰恰是**唯一**决定"放弃重连"的地方 —— 不发的话，客户端已经彻底停了，
        // 界面却在说"正在重连"，用户会一直等一件不会发生的事（而且去查一条完全正常的链路）。
        // 放在这里（而不是 teardown）还有一个理由：teardown 在四个调用点里，
        // 有一个是 fatal_**之后**调用的（握手被拒），另三个是之前 —— 顺序不一致，
        // 从那里读 fatal_ 会得到一种"看运气"的行为。这条分支没有这个问题。
        emit_state(); // → state_name() == "stopped" ⇒ 界面文案「不会再重连」
        work_.reset();
        return;
    }
    if (cfg_.reconnect_max_attempts != 0 && attempts_ >= cfg_.reconnect_max_attempts) {
        RC_LOG_ERROR("giving up after {} reconnect attempts: {}", attempts_, reason);
        state_ = State::kStopped;
        emit_state(); // 同上：次数用尽也是"不会再重连"，不是"正在重连"
        work_.reset();
        return;
    }

    ++attempts_;
    ++reconnect_count_;

    // 指数退避：initial * 2^(n-1)，封顶 max_delay
    std::uint64_t delay_ms = cfg_.reconnect_initial_delay_ms;
    for (std::uint32_t i = 1; i < attempts_ && delay_ms < cfg_.reconnect_max_delay_ms; ++i) {
        delay_ms *= 2;
    }
    delay_ms = std::min<std::uint64_t>(delay_ms, cfg_.reconnect_max_delay_ms);

    // ±20% 抖动：服务端重启后所有客户端会同时重连（惊群），抖动把重连时刻打散
    // 用 xorshift 而不是 rand()：不依赖全局状态，也不会被别的代码影响
    rng_ ^= rng_ << 13;
    rng_ ^= rng_ >> 17;
    rng_ ^= rng_ << 5;
    const double jitter = 0.8 + 0.4 * (static_cast<double>(rng_ % 1000) / 1000.0);
    const auto   delay  = std::chrono::milliseconds(static_cast<std::int64_t>(
        static_cast<double>(delay_ms) * jitter));

    state_ = State::kReconnecting;
    emit_state();
    RC_LOG_INFO("reconnecting in {} ms (attempt {}, reason={})",
                std::chrono::duration_cast<std::chrono::milliseconds>(delay).count(),
                attempts_, reason);

    reconnect_timer_.expires_after(delay);
    reconnect_timer_.async_wait([this](const error_code& ec) { on_reconnect_timer(ec); });
}

void AsyncClient::on_reconnect_timer(const error_code& ec) {
    if (ec || state_ == State::kStopped) {
        return;
    }
    do_resolve();
}

const char* AsyncClient::state_name() const noexcept {
    switch (state_) {
    case State::kIdle:         return "idle";
    case State::kResolving:    return "resolving";
    case State::kConnecting:   return "connecting";
    case State::kHandshaking:  return "handshaking";
    case State::kReady:        return "ready";
    case State::kReconnecting: return "reconnecting";
    case State::kStopped:      return "stopped";
    }
    return "unknown";
}

} // namespace rc::client
