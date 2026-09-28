#pragma once
// ============================================================================
//  AsioServer —— 服务端主循环（第二阶段重写）
//
//  【替换了什么】
//    旧版：主线程 while(true) 阻塞 accept，accept 到一个就起一组线程，
//          整个进程只能服务一个客户端；任何阻塞都会让服务端"假死"。
//    新版：单 io_context + N 线程跑事件循环，async_accept 永不阻塞。
//          客户端数量不再与线程数量挂钩（旧版是 1 客户端 ≙ 4 线程）。
//
//  【为什么抓屏要单独一个线程池】
//    GDI 抓屏 + PNG 编码是几十毫秒级的 CPU 活。如果直接在 io 线程里做，
//    这段阻塞期间整个 io_context 的这个线程都停摆，其他客户端的心跳/输入
//    都会跟着卡住 —— 这就是要把"重活"与"事件循环"分开的原因。
//    GDI+/DC 不是线程安全的，所以这个池固定 1 个线程，天然串行。
//
//  【可测试性】
//    input_ / screen_ 都是接口指针，测试可以替换成"记录型输入"和"假屏幕源"，
//    于是集成测试既不会真的去动用户鼠标，也不依赖真实桌面内容。
// ============================================================================

#include "asio_common.hpp"
#include "config.hpp"
#include "input_sink.hpp"
#include "screen_source.hpp"
#include "session.hpp"
#include "session_registry.hpp"

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace rc::server {

class AsioServer {
public:
    explicit AsioServer(const ServerConfig& cfg);
    ~AsioServer();

    AsioServer(const AsioServer&)            = delete;
    AsioServer& operator=(const AsioServer&) = delete;

    /// 注入替身实现（集成测试用）。必须在 start() 之前调用。
    void set_input_sink(IInputSink* sink) { input_ = sink; }
    void set_screen_source(IScreenSource* src) { screen_ = src; }

    /// 【2B 第二刀 TLS】注入 ssl 上下文。必须在 start() 之前调用。
    /// 传 nullptr（默认）= 不启用 TLS：每个会话走裸 socket，行为与引入本刀之前**逐字一致**。
    /// 上下文由 main 构建（含自签证书准备），这里只持有 —— 让"证书从哪来"留在启动流程里，
    /// 服务端循环本身不关心文件系统。
    void set_tls_context(std::shared_ptr<asio::ssl::context> ctx) { ssl_ctx_ = std::move(ctx); }

    /// 绑定端口并开始 accept。失败时通过 error 返回原因。
    bool start(std::string* error);

    /// 阻塞运行直到 stop() 被调用（或收到 Ctrl+C）。
    void run();

    /// 请求优雅关闭：停止 accept -> 关闭所有会话 -> 退出事件循环。
    void stop();

    std::uint32_t       io_threads() const noexcept { return io_threads_; }
    std::size_t         session_count() const { return registry_.size(); }
    SessionRegistry&    registry() noexcept { return registry_; }
    const ServerConfig& config() const noexcept { return cfg_; }
    tcp::endpoint       local_endpoint() const;

    /// 实际选定的抓屏后端名（如 "gdi+png+delta" / "dxgi+png+delta"）。
    ///
    /// 存在的理由：启动期的 DPI 告警必须结合**真实后端**才能说对话。
    /// DPI 虚拟化对 GDI 是"只看得到左上 44%"的功能缺陷，对 DXGI 则不影响画面
    ///（DXGI 给物理像素，§6.14 实测 12/12 次不受调用方 DPI 上下文影响）——
    /// 同一句告警在这两个后端下含义完全相反。后端未就绪时返回 "(none)"。
    const char* screen_name() const noexcept { return screen_ != nullptr ? screen_->name() : "(none)"; }

private:
    void do_accept();
    void on_accept(const error_code& ec, Session::socket_type socket);
    void on_signal(const error_code& ec, int signal_number);

    ServerConfig cfg_;
    asio::io_context io_;
    tcp::acceptor    acceptor_;
    asio::signal_set signals_;
    /// 【2B 第二刀 TLS】为空 = 不启用。非空时每个新会话都建在它之上。
    std::shared_ptr<asio::ssl::context> ssl_ctx_;
    /// 抓屏专用线程池（固定 1 线程：GDI+/DC 非线程安全）
    asio::thread_pool capture_pool_{1};

    SessionRegistry registry_;

    /// 默认实现由自己持有；测试可换成外部对象
    std::unique_ptr<IInputSink>    owned_input_;
    std::unique_ptr<IScreenSource> owned_screen_;
    IInputSink*                    input_  = nullptr;
    IScreenSource*                 screen_ = nullptr;

    /// 让 io_context.run() 在没有任何异步操作时也不立即返回
    /// （否则 run() 会在启动瞬间就退出 —— 这是 asio 新手的经典坑）
    asio::executor_work_guard<asio::io_context::executor_type> work_;

    std::atomic<bool> stopping_{false};
    std::uint32_t     next_session_id_ = 1;
    std::uint32_t     io_threads_      = 1;
};

} // namespace rc::server
