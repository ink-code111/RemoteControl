#pragma once
// ============================================================================
//  Session —— 一个客户端连接的完整生命周期（第二阶段重写）
//
//  【与第一阶段的本质差异】
//    第一阶段：一个会话 = 一个线程，阻塞 recv/send，读写都停在系统调用上。
//              结果：客户端一多线程就爆炸；某个客户端网络卡顿会占住线程；
//              多线程共享 socket 还得靠 send_mutex_ 互斥。
//    第二阶段：一个会话 = 一个 strand（不是线程）。所有异步操作的回调
//              都在自己的 strand 上串行执行 —— 于是：
//                · 没有阻塞，一个 io 线程能同时服务大量会话；
//                · 会话内部状态（读缓冲、写队列、心跳计数）全部免锁；
//                · 天然不会出现"两个回调同时改同一份状态"。
//              这是替换 send_mutex_ 的正解：加锁是"让它不并发"，
//              strand 是"它本来就不并发"，后者没有锁开销也没有死锁面。
//
//  【线程模型】
//    io_context 上的 N 个线程跑事件循环；每个 Session 绑定一个 strand；
//    重活（GDI 抓屏 + PNG 编码，几十毫秒级）丢到 capture 线程池执行，
//    算完再 post 回自己的 strand —— 绝不在 io 线程里做阻塞重活，
//    否则一个客户端的抓屏会拖慢所有客户端（这正是旧版"服务端假死"的同类问题）。
//
//  【生命周期】
//    Session 由 shared_ptr 管理，每个异步操作都捕获 self（shared_from_this），
//    因此只要还有未完成的异步操作，对象就不会被销毁 ——
//    这从根上消除了旧版"线程还在用对象、对象已被 delete"的 UAF 风险。
// ============================================================================

#include "asio_common.hpp"
#include "config.hpp"
#include "input_sink.hpp"
#include "message.hpp"
#include "proto_codec.hpp"
#include "screen_source.hpp"
#include "session_registry.hpp"

// 2B 第二刀 TLS：Session 的可选 ssl 流（见下面 tls_ 成员与 start() 的握手）。
#include <asio/ssl.hpp>

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace rc::server {

class Session : public std::enable_shared_from_this<Session> {
public:
    /// 会话的 socket 直接把 strand 当作 executor：
    /// 这样它的每个异步操作的回调都会自动在 strand 上串行执行，
    /// 不需要到处写 bind_executor —— 少了"漏绑一处就产生数据竞争"的隐患。
    using executor_type = asio::strand<asio::io_context::executor_type>;
    using socket_type   = asio::basic_stream_socket<asio::ip::tcp, executor_type>;
    /// 2B 第二刀 TLS：把裸 socket 包一层 ssl::stream。
    /// 只在 `tls_enable` 时为非空；为空时一切走裸 socket —— 那条路径与引入 TLS 之前
    /// **逐字等价**（不是"等价"，是同一段代码：下面所有 dispatch 都是 `if (tls_)` 分支）。
    using ssl_stream_type = asio::ssl::stream<socket_type>;

    Session(asio::io_context&   io,
            socket_type         socket,
            std::uint32_t       id,
            const ServerConfig& cfg,
            SessionRegistry&    registry,
            IInputSink&         input,
            IScreenSource&      screen,
            asio::thread_pool&  capture_pool,
            asio::ssl::context* ssl_ctx);   ///< nullptr = 本会话不走 TLS

    Session(const Session&)            = delete;
    Session& operator=(const Session&) = delete;

    void start();

    /// 请求关闭（可从任意线程调用）。内部会切回 strand 再动 socket。
    void close(const char* reason);

    std::uint32_t      id() const noexcept { return id_; }
    const std::string& peer() const noexcept { return peer_; }
    std::int64_t       connected_at_ms() const noexcept { return connected_at_ms_; }
    bool               closed() const noexcept { return closed_.load(); }
    std::uint64_t      bytes_rx() const noexcept { return bytes_rx_; }
    std::uint64_t      bytes_tx() const noexcept { return bytes_tx_; }
    std::uint32_t      frames_sent() const noexcept { return frames_sent_; }

    /// 【2B 第三刀 权限模型】本会话是不是只读（输入被拒绝）。
    /// 供 asio_server / 运维查询使用；判定输入该不该注入走的也是它。
    bool view_only() const noexcept { return role_ == rc::proto::v2::Role::View; }

    /// 授权表里命中的身份名（空 = 本会话没走权限模型，即老路径）。
    const std::string& client_label() const noexcept { return client_label_; }

private:
    // ---- 读循环：帧头 → 载荷 → 分派 ----
    void do_read_header();
    void do_read_body();
    void dispatch(const std::uint8_t* payload, std::size_t len);

    /// TLS 握手（仅在 tls_ 非空时由 start() 调用），完成后才开始读第一帧。
    void do_tls_handshake();

    // ---- 传输层 dispatch：TLS 开/关走不同对象，但两者都是 Asio 的 AsyncReadStream / ----
    // ---- AsyncWriteStream，模板在这里只做"选哪个对象"，不引入任何运行期抽象开销。 ----
    template <class MutableBufferSequence, class Handler>
    void async_read_stream(const MutableBufferSequence& bufs, Handler&& handler) {
        if (tls_) {
            asio::async_read(*tls_, bufs, std::forward<Handler>(handler));
        } else {
            asio::async_read(socket_, bufs, std::forward<Handler>(handler));
        }
    }
    template <class ConstBufferSequence, class Handler>
    void async_write_stream(const ConstBufferSequence& bufs, Handler&& handler) {
        if (tls_) {
            asio::async_write(*tls_, bufs, std::forward<Handler>(handler));
        } else {
            asio::async_write(socket_, bufs, std::forward<Handler>(handler));
        }
    }

    // ---- 消息处理（全部运行在本会话 strand 上）----
    void on_hello(const rc::proto::ParsedEnvelope& env);
    void on_ping(const rc::proto::ParsedEnvelope& env);
    void on_mouse(const rc::proto::ParsedEnvelope& env);
    void on_keyboard(const rc::proto::ParsedEnvelope& env);
    void on_screen_request();

    // ---- 抓屏 ----
    /// @param input_triggered 这一拍是不是"输入到达"逼出来的（只影响计数与归因，
    ///        不影响抓屏本身）。默认 false = 客户端的 ScreenRequest 触发的老路径。
    void schedule_capture(bool input_triggered = false);
    /// 输入优先抓屏（第三阶段 backlog A，见 ServerConfig::input_priority_capture）。
    /// 在"输入已应用"与"上一拍抓屏收尾"两个时刻各调一次；不满足条件就直接返回，
    /// 所以两个调用点都安全地幂等。
    void maybe_start_input_capture();
    void on_capture_done(const std::shared_ptr<CapturedFrame>& frame, bool ok);

    // ---- 写队列 ----
    void enqueue(flatbuffers::DetachedBuffer body);
    void do_write();

    // ---- 心跳 / 空闲超时 ----
    void on_idle_tick(const error_code& ec);

    void do_close(const char* reason);
    void fail(const char* what, const error_code& ec);

    asio::io_context&  io_;
    /// strand 必须**声明在 socket_ 之前**：它的初值取自构造参数的 executor，
    /// 而 socket_ 的初值要把它 move 走 —— 成员按声明顺序初始化，顺序反了就会
    /// 从一个已经被搬空的 socket 上取 executor。
    executor_type      strand_;
    socket_type        socket_;
    /// 2B 第二刀 TLS：非空时承担全部收发；此时 socket_ 已被 move 进它（只剩 executor）。
    /// 为空 = 未启用 TLS，一切走 socket_（与引入本刀之前逐字等价）。
    std::unique_ptr<ssl_stream_type> tls_;
    asio::thread_pool& capture_pool_;

    std::uint32_t       id_;
    std::string         peer_;
    std::int64_t        connected_at_ms_ = 0;
    const ServerConfig& cfg_;
    SessionRegistry&    registry_;
    IInputSink&         input_;
    IScreenSource&      screen_;

    enum class State { kAwaitHello, kReady, kClosing };
    State state_ = State::kAwaitHello;

    /// 只在 strand 上读写；closed_ 可能被其他线程读（运维查询），故用原子
    std::atomic<bool> closed_{false};

    // ---- 读缓冲 ----
    /// 固定 8 字节帧头缓冲：async_read 的"读满即回调"语义天然解决粘包/半包
    std::array<std::uint8_t, rc::net::kFrameHeaderSize> header_buf_{};
    /// 载荷缓冲：按帧头声明的长度动态扩张（取代旧版定长 char 数组的越界风险）
    std::vector<std::uint8_t> payload_buf_;

    // ---- 写队列 ----
    std::deque<rc::net::OutgoingMessagePtr> write_queue_;
    bool                                    writing_ = false;
    /// 需要"先把话说完再关"（例如握手被拒时要把拒绝理由发出去，不能直接断）
    bool                                    close_after_flush_ = false;

    // ---- 抓屏节流与在途控制 ----
    asio::steady_timer                    frame_timer_;
    std::chrono::steady_clock::time_point next_capture_at_{};
    bool                                  capture_in_flight_ = false;
    bool                                  capture_scheduled_ = false;
    /// 触发本轮抓屏的那个 ScreenRequest 的到达时刻，随帧传给抓屏器做归因。
    /// 只是观测值：即使不设（默认构造），抓屏器也只是少算一段空档。
    std::chrono::steady_clock::time_point request_at_{};

    // ---- 输入→显示延迟的打点（第三阶段第 2 步 2b）----
    //
    // 分两点记：① 应用一次输入花多久（同进程，精确）；② 应用完成到下一次抓屏开始
    // 隔了多久（同样同进程）。这两点合起来是"输入→显示"里服务端能**独立**给出的部分，
    // 也是客户端那个端到端数字唯一能拿来做交叉验证的东西 —— 客户端测的是总数，
    // 服务端测的是其中一段，两者必须相容（客户端 ≥ 服务端那一段）。
    void note_input_applied(const std::chrono::steady_clock::time_point& t0,
                            const std::chrono::steady_clock::time_point& t1,
                            const rc::input::MouseEvent* pos_check);
    /// 每 5 秒一次的服务端侧输入汇总（挂在 on_capture_done 上，不额外开定时器）
    void report_input_stats();

    /// 【2B 第三刀 权限模型】记一次"输入被授权模型拒绝"（只读会话）。
    /// @param is_mouse true=鼠标事件，false=键盘事件（分开计数，见成员说明）
    void note_input_denied(bool is_mouse);

    /// 已应用的输入事件累计数。**单写多读**：strand 线程在应用后自增，
    /// capture 线程在抓屏前取快照 —— 用原子是为了跨线程读，不是因为有并发写。
    /// 客户端侧对应的那个计数（AsyncClient::input_epoch_）与它必须是同一个序列，
    /// 靠的是 TCP 保序不丢；这条"两端计数一致"是判据的前置不变式。
    std::atomic<std::int64_t> inputs_applied_{0};
    /// **每一个**输入的"应用完成时刻"（steady 毫秒）环形表 + 写指针。
    ///
    /// 【为什么不是"只记最近一次 + 一个 pending 标志"】第一版就是这么写的，实测在
    /// "输入比帧密"的配置下会系统性**低估**空档：抓屏时只能消费到最近那一次输入，
    /// 而同一帧间隔里更早的那些输入（它们等得更久）从来没被采样过。
    /// 10 fps 那轮实测空档均值 38.7 ms，而同一份日志里端到端延迟是 103.9 ms ——
    /// 理论期望是"帧周期的一半 ≈ 50 ms + 净工作 + 链路"，差额的一半就来自这个
    /// "采样集合只覆盖了等得最短的那一半"。口径错了，两本账就对不上；
    /// 而对不上就分不清究竟是漏了哪一段（本项目在 §6.16 为此专门立过规矩）。
    static constexpr int      kPendingCap = 64;
    std::atomic<std::int64_t> pending_apply_ms_[kPendingCap]{};
    std::atomic<int>          pending_head_{0};  ///< 写位置（仅 strand 线程）
    /// 读位置。仅 capture 线程访问 —— 同一会话的抓屏是串行的（capture_in_flight_ 保证），
    /// 所以它不需要原子。
    int                       sampled_head_ = 0;
    /// 应用耗时（微秒）之和 / 最大；同进程内测量，不含任何网络成分
    std::atomic<std::int64_t> apply_us_sum_{0};
    std::atomic<std::int64_t> apply_us_max_{0};
    /// 应用完成 → 抓屏开始 的空档（微秒）之和 / 最大 / 样本数（capture 线程写）
    std::atomic<std::int64_t> gap_us_sum_{0};
    std::atomic<std::int64_t> gap_us_max_{0};
    std::atomic<std::int64_t> gap_count_{0};
    /// 坐标读回校验：SetCursorPos 之后立刻 GetCursorPos，看是不是我们要的那个点。
    /// 这是"输入真的落到正确位置"的**唯一直接证据**，也是"抓屏与输入在同一坐标空间"
    /// 的硬判据（DPI 未对齐时这个数会大面积不符）。本项目纪律：设了就算数的东西，
    /// 都要有办法把它读回来 —— 这条就是那个"读回来"。
    std::atomic<std::int64_t> pos_readback_ok_{0};
    std::atomic<std::int64_t> pos_readback_bad_{0};
    /// 【诊断，不参与判定】第一次读回不符时，再读若干次，看它会不会收敛到请求点。
    ///
    /// 2026-09-24 实测：这个不变式在**历史上 20 次运行里从未触发过**，而某一次全套回归里
    /// 连报两次，且两次都只是"某一轮开局的第一批样本里 1 次不符，之后 80~90 次全部一致"。
    /// 只记一个计数的话，下一次出现仍然只能猜 —— 而"不符"至少有三个**处置完全不同**的成因：
    ///   ① **竞态**：`SetCursorPos` 是把移动**投递进系统输入流**（异步生效），紧接着读回
    ///      有可能读到**旧位置**（上一次请求的另一个端点）；几百微秒内会收敛 ⇒ 无害。
    ///   ② **外部干扰**：真实鼠标 / 别的进程动了光标，收敛到**另一个**与请求无关的点
    ///      ⇒ 这一轮那个样本不可信（但中位数不受影响）。
    ///   ③ **坐标空间错位**（DPI 虚拟化，即 §6.13 那条"点击偏 1.5 倍"）：**永远**收敛不到
    ///      请求点，读回值恒为请求值的 1/1.5、且**大面积**发生 ⇒ 整轮作废。
    /// 所以下面这些量是"把报 2 变成可查"的最小集合；**判定仍然只看 `pos_readback_bad_`**。
    std::atomic<std::int64_t> pos_diag_count_{0};       ///< 触发诊断的次数
    std::atomic<std::int64_t> pos_diag_req_x_{0}, pos_diag_req_y_{0};       ///< 请求的点
    std::atomic<std::int64_t> pos_diag_first_x_{0}, pos_diag_first_y_{0};   ///< **第一次**读回的值
    std::atomic<std::int64_t> pos_diag_last_x_{0}, pos_diag_last_y_{0};    ///< 诊断循环最后一次读回的值
    std::atomic<std::int64_t> pos_diag_tries_{-1};  ///< 第几次重读才收敛；-1 = 始终没收敛
    /// 抓屏时系统光标可见 / 不可见的次数。
    /// 隐藏时光标合成不出东西，"移动光标"这类输入在画面里就没有任何痕迹 ——
    /// 那时延迟数字是假的（测的是"帧到了"，不是"输入被看见了"），必须报"没测到"。
    std::atomic<std::int64_t> cursor_visible_{0};
    std::atomic<std::int64_t> cursor_hidden_{0};
    /// 上一次打 [input] 汇总的时刻（仅 strand 读写）
    std::chrono::steady_clock::time_point last_input_report_{};

    // ---- 输入优先抓屏（backlog A）----
    /// 最近一次**已经开始**的抓屏所覆盖到的输入序号（即该帧的 `input_epoch`）。
    ///
    /// 它和 inputs_applied_ 一起构成一个判据，回答"还有没有输入没被任何一帧覆盖过"：
    ///     inputs_applied_ > captured_epoch_   ⇒ 有输入落在所有已开始抓屏之后
    /// 这才是"该立刻再抓一帧"的**充分**理由。不用一个 bool 标志来记，是因为标志需要
    /// 在两个线程（strand 应用输入 / capture 线程抓屏）之间小心地"置位—清零"，
    /// 而这两个值本身就是那两个线程各自的单调计数，比较一次即可，没有丢失更新的窗口。
    std::atomic<std::int64_t> captured_epoch_{0};
    /// 输入触发的抓屏次数。存在的唯一目的是让开关**能自证生效**：
    /// 配置项写了 true 但一个计数都没有 = 开关被静默忽略（本项目在"配置静默失效"上
    /// 栽过四次，docs §8.6 / §8.12 / §6.14 / §8.21），判据据此报"没测到"。
    std::atomic<std::int64_t> input_triggered_captures_{0};

    // ---- 空闲超时（服务端侧心跳兜底）----
    asio::steady_timer                    idle_timer_;
    std::chrono::steady_clock::time_point last_rx_{};

    // ------------------------------------------------------------------------
    // 【2B 第三刀 权限模型】身份与角色
    // ------------------------------------------------------------------------
    /// 本会话被授予的角色。默认 `Control` —— 三层回落（无表无 token / 只有旧 token /
    /// 表里没配）全都落在这个默认值上，所以"权限模型存在之前"的行为逐字保留。
    rc::proto::v2::Role role_ = rc::proto::v2::Role::Control;

    /// 授权表里命中的那个 `name`（用于日志与审计）。空 = 本会话没走权限模型。
    ///
    /// ⚠️ 它与下面的 `hello_name_` **必须分开**：
    ///    一个是"服务端查表得到、可以拿来做归因"的身份，
    ///    另一个是"客户端自称、只配出现在日志里"的字符串。
    ///    把两者合成一个字段，就是审计不可信的开始（客户端可以自称成任何名字）。
    std::string client_label_;
    /// 客户端 Hello 里自称的名字。**不参与鉴权**，只用于日志与审计的交叉核对。
    std::string hello_name_;

    /// 被拒绝的输入事件计数。鼠标与键盘分开——
    /// 合成一个数就看不出"客户端到底在发什么"，而这两者的成因完全不同
    /// （鼠标多半是自动输入源/误操作，键盘多半是用户真的在敲）。
    /// 计数是**判定只读拦截真的生效**的唯一直接证据（同 input_triggered_captures_ 的作用：
    /// 开关说自己开着了，得有个数证明它确实被走到过）。
    std::atomic<std::int64_t> input_denied_mouse_{0};
    std::atomic<std::int64_t> input_denied_kb_{0};
    /// 上一次因为只读写日志的时刻。**必须限频**：被拒的输入在真实场景下是每秒几十条
    /// （鼠标拖动的原生频率是 125~1000 Hz），每条都写日志会让审计文件被"同一个事件"
    /// 撑爆，而真正要看的那几条被冲走。
    std::chrono::steady_clock::time_point last_deny_log_{};
    /// 被拒输入的第一次日志已经写过没有（第一次必须写，且要写完整）。
    bool deny_logged_first_ = false;

    // ---- 统计 ----
    std::uint64_t bytes_rx_    = 0;
    std::uint64_t bytes_tx_    = 0;
    std::uint32_t frames_sent_ = 0;
    std::uint32_t ping_count_  = 0;
    std::uint32_t capture_fail_count_ = 0;
};

} // namespace rc::server
