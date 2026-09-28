#include "asio_server.hpp"

#include "dxgi_capturer.hpp"
#include "input_executor.hpp"
#include "logger.hpp"
#include "screen_capturer.hpp"
#include "trace.hpp"

#include <thread>

namespace rc::server {

namespace {

/// 按 capture_backend 选抓屏后端。
///
/// 【为什么降级一定要留 WARN，而且必须能一行 grep 到】
///   静默回退会让人以为 DXGI 正在跑 —— 于是抓到的 1707×960 被当成 DXGI 的产出，
///   整轮 A/B 的结论就此作废，而日志上一切正常。本项目已经在"配置静默失效"上
///   栽过三次（§8.6 的"降采样"、§8.12 的夹具出画、§6.14 的自称 unaware 实则 aware），
///   所以这里立的规矩是：**降级必须显式可见**。
///
/// 三种取值对应两种失败策略，刻意不同：
///   dxgi = "我要的就是它，拿不到就报错"（返回 nullptr，由 start() 拒绝启动）
///   auto = "能用就用，不能用告诉我一声再退回去"
std::unique_ptr<IScreenSource> make_screen_source(const ServerConfig& cfg) {
    if (cfg.capture_backend == "gdi") {
        return std::make_unique<GdiScreenCapturer>(cfg.capture_cursor, cfg.capture_delta);
    }

    // dxgi / auto：都先试 DXGI（构造不抛异常，失败信息在 init_error()）
    auto dxgi = std::make_unique<DxgiScreenCapturer>(cfg.capture_cursor, cfg.capture_delta);
    if (dxgi->ok()) {
        return dxgi;
    }

    if (cfg.capture_backend == "dxgi") {
        RC_LOG_ERROR("capture_backend=dxgi，但 DXGI 初始化失败：{}", dxgi->init_error());
        RC_LOG_ERROR("  若希望这种情况自动回退到 GDI，请把配置改为 capture_backend=auto");
        return nullptr; // 交给 start() 报错退出，绝不静默降级
    }

    RC_LOG_WARN("capture_backend=auto：DXGI 不可用（{}），已回退 GDI", dxgi->init_error());
    RC_LOG_WARN("  注意回退后的画面仍只覆盖桌面左上（非 100% 缩放时约 44%），"
                "且与输入坐标不在同一空间（见 docs §6.13）");
    return std::make_unique<GdiScreenCapturer>(cfg.capture_cursor, cfg.capture_delta);
}

} // namespace

AsioServer::AsioServer(const ServerConfig& cfg)
    : cfg_(cfg),
      io_(),
      acceptor_(io_),
      signals_(io_, SIGINT, SIGTERM),
      work_(asio::make_work_guard(io_)) {
    // 默认实现：真实 SendInput 注入 + 按配置选定的抓屏后端（含差异帧）
    owned_input_  = std::make_unique<InputExecutor>();
    owned_screen_ = make_screen_source(cfg_);
    input_        = owned_input_.get();
    screen_       = owned_screen_.get(); // 为 nullptr 时由 start() 拦住

    // 【诊断】反向对照用的"故意泄漏"（默认 0 = 不泄漏，与本字段存在之前逐字等价）。
    // 用 dynamic_cast 而不是改 make_screen_source 的返回类型：这是**诊断**注入，
    // 不该让"所有后端都必须是 DeltaCapturerBase"变成一个编译期约束。
    if (auto* dc = dynamic_cast<DeltaCapturerBase*>(screen_)) {
        dc->set_debug_leak_gdi_per_frame(cfg_.debug_leak_gdi_per_frame);
        if (cfg_.debug_leak_gdi_per_frame > 0) {
            // 配了就要能读回来：这个开关一旦非 0，服务端就是在**漏资源**，
            // 任何"资源不泄漏"的观测都会失真 —— 必须显眼，否则会被人当成真结果。
            RC_LOG_WARN("⚠️ debug_leak_gdi_per_frame = {} > 0：抓屏**每帧故意泄漏 {} 个 GDI 对象**"
                        "（~3 分钟后配额耗尽、泄漏自行停止）。"
                        "这是资源判据的反向对照，**不是可用配置**，别把它读成真结果",
                        cfg_.debug_leak_gdi_per_frame, cfg_.debug_leak_gdi_per_frame);
        }
    }

    io_threads_ = cfg_.io_threads != 0
                      ? cfg_.io_threads
                      : std::max<std::uint32_t>(1, std::thread::hardware_concurrency());
}

AsioServer::~AsioServer() {
    stop();
    io_.stop();
}

bool AsioServer::start(std::string* error) {
    // 抓屏后端不可用（=capture_backend=dxgi 且 DXGI 初始化失败）时**拒绝启动**。
    // 为什么不在构造里就抛：构造发生在 logger 初始化之后但在 main 的 try 里，
    // 抛出来只能看到一行 "fatal:"；而这里能给出可操作的下一步（改配置）。
    // 为什么不允许"没后端也照跑"：那会得到一个能连上、但画面永远不来的服务端 ——
    // 客户端的表现是"白窗口"，排查方向会完全跑偏。
    if (screen_ == nullptr) {
        const std::string what = "capture backend unavailable (capture_backend=" +
                                 cfg_.capture_backend + "); 见上一条 dxgi 错误详情";
        if (error) {
            *error = what;
        }
        RC_LOG_ERROR("server start failed: {}", what);
        return false;
    }

    auto fail = [&](const std::string& what, const error_code& ec) {
        if (error) {
            *error = what + ": " + rc::net::describe(ec);
        }
        RC_LOG_ERROR("server start failed: {} ({})", what, rc::net::describe(ec));
        return false;
    };

    error_code ec;
    auto       address = asio::ip::make_address(cfg_.listen_host, ec);
    if (ec) {
        // 允许写主机名而不是 IP（例如 "localhost"）
        tcp::resolver resolver(io_);
        const auto    results = resolver.resolve(cfg_.listen_host, std::to_string(cfg_.listen_port), ec);
        if (ec || results.empty()) {
            return fail("resolve listen_host '" + cfg_.listen_host + "'", ec);
        }
        address = results.begin()->endpoint().address();
    }

    const tcp::endpoint endpoint(address, cfg_.listen_port);
    acceptor_.open(endpoint.protocol(), ec);
    if (ec) {
        return fail("open acceptor", ec);
    }
    // 避免 TIME_WAIT 期间重启服务端报地址占用（开发期反复重启很常见）
    acceptor_.set_option(asio::socket_base::reuse_address(true), ec);
    acceptor_.bind(endpoint, ec);
    if (ec) {
        return fail("bind " + cfg_.listen_host + ":" + std::to_string(cfg_.listen_port), ec);
    }
    acceptor_.listen(asio::socket_base::max_listen_connections, ec);
    if (ec) {
        return fail("listen", ec);
    }

    // Ctrl+C / kill 走 asio 的信号处理，而不是让进程被硬杀 —— 这样能优雅关闭会话
    signals_.async_wait([this](const error_code& e, int sig) { on_signal(e, sig); });

    do_accept();
    RC_LOG_INFO("server listening on {}:{} (io_threads={}, max_clients={}, screen_max_fps={}, "
                "capture={}, cursor_overlay={}, dpi_aware={})",
                cfg_.listen_host, cfg_.listen_port, io_threads_, cfg_.max_clients,
                cfg_.screen_max_fps, screen_->name(), cfg_.capture_cursor ? "on" : "off",
                cfg_.dpi_aware ? "on" : "off");
    RC_LOG_INFO("capture mode: delta={} (差异帧只传变化区域；关掉即每帧整屏)",
                cfg_.capture_delta ? "on" : "off");
    // 【为什么另起一行而不是挂到上面那条 listening 上】那条行的字段位置被旧日志与部分
    // 用例依赖（"改造前 vs 改造后"要能逐字对齐），改它等于让历史数据失去可比性。
    // 本项目对老日志行的规矩是：**要加就另起一行**。
    RC_LOG_INFO("input priority capture: {} (输入到达即抓一帧，不等客户端请求与限流时刻；"
                "长期平均帧率不变，只是拍子跟着输入走；预支深度 {} 拍)",
                cfg_.input_priority_capture ? "on" : "off", cfg_.input_priority_max_borrow);
    return true;
}

void AsioServer::run() {
    std::vector<std::thread> threads;
    threads.reserve(io_threads_);
    for (std::uint32_t i = 0; i < io_threads_; ++i) {
        threads.emplace_back([this] {
            try {
                io_.run();
            } catch (const std::exception& e) {
                // 事件循环线程里绝不能让异常逃逸：逃出去就是 std::terminate
                RC_LOG_ERROR("io thread terminated by exception: {}", e.what());
            }
        });
    }
    for (auto& t : threads) {
        if (t.joinable()) {
            t.join();
        }
    }

    // io 线程全部结束后，抓屏池里可能还有在途任务；停掉它再返回，
    // 保证析构 source/executor 时不会有线程还在用它们。
    capture_pool_.stop();
    capture_pool_.join();
    RC_LOG_INFO("server stopped, sessions left={}", registry_.size());
}

void AsioServer::stop() {
    if (stopping_.exchange(true)) {
        return;
    }
    asio::post(io_, [this] {
        error_code ec;
        acceptor_.close(ec);
        signals_.cancel();

        // 快照后再逐个关闭：绝不能在持有 registry 互斥锁时回调 Session 的方法
        const auto sessions = registry_.snapshot();
        for (const auto& s : sessions) {
            s->close("server shutdown");
        }
        RC_LOG_INFO("shutting down, {} session(s) closed", sessions.size());

        // 释放 work guard：让 run() 在所有会话收尾后自然返回
        work_.reset();
    });
}

tcp::endpoint AsioServer::local_endpoint() const {
    error_code ec;
    return acceptor_.local_endpoint(ec);
}

void AsioServer::do_accept() {
    RC_TRACE("accept.submit");
    // 关键：把加速器接受的连接直接建在"每个会话自己的 strand"上。
    // 这样该 socket 的所有异步回调都自动串行执行，会话内部状态无需加锁。
    acceptor_.async_accept(asio::make_strand(io_),
                           [this](const error_code& ec, Session::socket_type socket) {
                               on_accept(ec, std::move(socket));
                           });
}

void AsioServer::on_accept(const error_code& ec, Session::socket_type socket) {
    RC_TRACE("accept.handler_enter", static_cast<unsigned long>(ec.value()));
    if (ec) {
        if (ec == asio::error::operation_aborted) {
            return; // accept 被 close() 取消：正常关闭路径
        }
        RC_LOG_WARN("accept failed: {}", rc::net::describe(ec));
    } else if (registry_.at_capacity(cfg_.max_clients)) {
        // 超限直接拒绝：不创建会话、不占资源，并给出明确日志
        error_code ignored;
        socket.close(ignored);
        RC_LOG_WARN("connection rejected: max_clients={} reached", cfg_.max_clients);
    } else {
        // 【网络层 Nagle，§6.29】accepted socket 上**立即**设/不设 TCP_NODELAY。
        // 必须在 Session 接管 socket 之前做 —— Session 拿到 socket 后才会发任何字节，
        // 到那时再设就已经晚了。设不设由 cfg_.tcp_nodelay 决定（默认 false = 与引入
        // 本字段之前逐字等价）。
        //
        // 为什么是"立即"而不是放在 Session 构造里：Session 构造里 socket 已被 move、
        // 而且 tls 路径下还会被再 move 一次进 ssl 流；那时再设就多了一次 lowest_layer()
        // 间接。把它贴在 accept 回调里、Session 拿到之前的最后一刻，路径最短、最直观。
        //
        // 为什么记到 trace 而不是 log：这是**每个连接都发生**的事，写 log 会污染日志；
        // trace 只在排查时显式打开，写得"几乎看不见、需要时看得见"。
        if (cfg_.tcp_nodelay) {
            error_code ignored;
            socket.set_option(asio::ip::tcp::no_delay(true), ignored);
            RC_TRACE("accept.nodelay_on");
        }
        auto session = std::make_shared<Session>(io_,
                                                std::move(socket),
                                                next_session_id_++,
                                                cfg_,
                                                registry_,
                                                *input_,
                                                *screen_,
                                                 capture_pool_,
                                                ssl_ctx_.get());   // 2B 第二刀 TLS（nullptr = 关）
        RC_TRACE("accept.session_created", next_session_id_ - 1);
        session->start();
        RC_TRACE("accept.session_started", next_session_id_ - 1);
    }

    if (!stopping_.load()) {
        RC_TRACE("accept.rearm");
        do_accept(); // 继续等待下一个连接
    }
}

void AsioServer::on_signal(const error_code& ec, int signal_number) {
    if (ec) {
        return; // 被 cancel（正常关闭路径）
    }
    RC_LOG_INFO("received signal {}, shutting down gracefully", signal_number);
    stop();
}

} // namespace rc::server
