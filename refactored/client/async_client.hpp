#pragma once
// ============================================================================
//  AsyncClient —— 客户端网络层（第二阶段重写）
//
//  【替换了什么】
//    旧版/第一阶段：一个阻塞接收线程 + 一个发送线程；host 断线只能重启程序。
//    现在：全部异步 —— 连接、握手、心跳、拉屏、重连都由事件驱动，
//          没有阻塞点，因此"网络卡住"不会让 UI 卡住，也不会让线程停摆。
//
//  【线程模型】
//    客户端只跑 1 个 io 线程（吞吐本来就只有几十帧/秒，单线程足够），
//    于是所有网络回调天然串行，不需要 strand、不需要锁。
//    UI 线程要发数据时用 asio::post 投递到 io 线程（跨线程只传值，不共享状态）。
//    需要跨线程读的状态用原子量（连接状态、计数）。
//
//  【心跳与重连】
//    - 每 heartbeat_interval_ms 发一个 Ping，服务端立刻回 Pong；
//    - 超过 heartbeat_timeout_ms 没收到 Pong ⇒ 判定链路已死，主动断开并重连
//      （注意：TCP 的半开连接可能几十秒都不会报错，只靠 socket 错误是不够的）；
//    - 重连采用指数退避 + 抖动，避免服务端重启时所有客户端同时重连造成惊群。
//
//  【零拷贝接收】
//    ScreenFrameView 直接指向网络读缓冲，不拷贝。回调返回后该内存即失效，
//    因此回调里必须"用完即弃"（当前实现是立刻解码进 CImage）。
// ============================================================================

#include "asio_common.hpp"
#include "config.hpp"
#include "input_types.hpp"
#include "message.hpp"
#include "proto_codec.hpp"
// 2B 第二刀 TLS：客户端上下文工厂（内部会拉进 <asio/ssl.hpp>）。
// 必须放在 asio_common.hpp 之后 —— 由它负责先定义 ASIO_STANDALONE / _WIN32_WINNT。
#include "tls.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <string>
#include <thread>
#include <utility>

namespace rc::client {

/// 收到的一帧画面（零拷贝视图：仅在回调返回前有效）
struct ScreenFrameView {
    std::int32_t        width        = 0;
    std::int32_t        height       = 0;
    std::int64_t        timestamp_ms = 0;
    const std::uint8_t* data         = nullptr;
    std::size_t         size         = 0;

    // ---- 差异帧（第三阶段）----
    /// true = data 只是 rect 那一小块，调用方必须把它合成到自己的累积画面上；
    /// false = data 是完整一帧，直接整体替换。
    bool         delta  = false;
    std::int32_t rect_x = 0;
    std::int32_t rect_y = 0;
    std::int32_t rect_w = 0;
    std::int32_t rect_h = 0;

    /// "这一帧没有任何变化"（服务端发来的空增量）。
    /// 它存在的唯一理由是拉屏是**请求-应答**的：服务端不回帧，客户端就不会请求下一帧。
    bool idle() const noexcept { return delta && size == 0; }

    /// 性能归因（第三阶段第二步）：从"上一个 ScreenRequest 发出去"到"这一帧到达"的毫秒数。
    ///
    /// 这段是端到端帧周期里唯一跨进程的一段，也是唯一没法只靠本地计时拆开的一段：
    /// 它包含了 上行链路 + 服务端限流等待 + 服务端抓屏编码 + 下行链路。
    /// 服务端那三段（抓屏/比对/编码）有它自己的 [capture] 汇总行，两者一减，
    /// 剩下的就是"链路 + 服务端空档"——本地回环上链路可以忽略，于是空档现形。
    ///
    /// ⚠️ **这个减法只在 requested == true 的帧上成立**（见 §6.19 / §8.22）：
    /// 服务端可以推**不经过请求**的帧（输入优先抓屏），那时"请求发出→抓屏开始"的
    /// 那一段根本不在这个区间里，相减得到的差额没有意义。
    double wire_ms = 0.0;

    /// 【归因口径的自证字段】收到这一帧时，客户端是否**有 ScreenRequest 在途**。
    ///
    /// 为什么必须显式带出来、而不是让上层猜：wire_ms 的起点是"上一个请求发出的时刻"，
    /// 它只在"这一帧就是那个请求的应答"时才有定义。客户端是**拉屏**的（收到一帧才请求
    /// 下一帧），所以正常情况下每帧到达时都恰好有一个在途请求；但服务端的输入优先抓屏
    /// 会在客户端"请求已发、应答在途"的窗口里**抢先推一帧**（实测占 61%）——
    /// 那种帧到达时在途请求确实存在，wire_ms 因此仍有定义（差额还是"这台客户端发出请求
    /// 到拿回一帧"），只是**相减服务端净工作**不再成立。
    ///
    /// 真正退化的是另一种：帧到达时**没有任何在途请求**（服务端主动推、且客户端的
    /// 限流定时器还在等）。此时 last_screen_request_at_ 指向一个**已经结算过**的旧请求，
    /// 算出来的是"上一轮往返 + 本客户端自己的空等"，是个**看似正常的错数**。
    /// 这正是本仓库"设了就要有办法读回来"那条规矩的对象：分类必须由观测自己给出，
    /// 不能靠读日志的人推断。于是：requested == false 的帧**不计入**往返均值，
    /// 只计数（见 [decode] 行尾的"往返口径"），让读者一眼看见有多少帧被排除。
    bool requested = false;

    /// 【输入→显示延迟】本帧像素**开始采集之前**，服务端已应用的输入事件累计数（下界）。
    /// 0 = 这一帧没携带该观测（老服务端、或本轮没有输入）。
    ///
    /// 它只做一件事：把"客户端发出的第 k 次输入"与"该输入首次可见的那一帧"对上。
    /// 于是端到端延迟可以在**客户端自己的时钟里**闭合（发包时刻 vs 画上窗口的时刻），
    /// 完全绕开了跨进程时钟同步 —— 这是选序号而不是选时间戳的全部理由。
    std::int64_t input_epoch = 0;
};

struct ClientCallbacks {
    /// 收到一帧（在 io 线程调用，必须尽快返回；重活请自行搬走）
    std::function<void(const ScreenFrameView&)> on_frame;
    /// 握手完成、可以开始工作
    std::function<void()> on_connected;
    /// 连接断开（无论是被对端关闭、超时还是本地网络错误），随后会自动重连
    std::function<void(const std::string& reason)> on_disconnected;
    /// 心跳往返时延（毫秒），可用于界面上显示链路质量
    std::function<void(std::int64_t rtt_ms)> on_rtt;
    /// 链路状态的**中间态**迁移（"resolving" / "connecting" / "handshaking" /
    /// "reconnecting"），用于让 UI 在握手期间也有反馈。
    ///
    /// 为什么需要它：on_connected / on_disconnected 只在两端状态跳变时触发，
    /// "正在握手"这段时间原本对外是完全静默的（正是白窗口无提示的成因之一）。
    /// 参数指向静态字符串字面量，回调返回后仍有效，但不要把它当成稳定的 API 枚举。
    ///
    /// ⚠️ 2026-09-27：它现在**也携带一个终止态** `"stopped"` —— 客户端不会再重连了
    /// （凭据被拒 / 协议不合 / 重试次数用尽）。这一条必须送到界面，理由与"只读降级
    /// 必须显眼"同族：标题在无状态文案时的默认值是「重连中…」，不送的话
    /// **"已经放弃"会被显示成"正在重连"**，用户会一直等一件不会发生的事。
    std::function<void(const char* state)> on_state;

    /// 【2B 第三刀 权限模型】服务端在握手应答里下发的角色（"control" / "view"）。
    ///
    /// 【为什么角色必须送到界面】这是本项目「降级必须显式可见」在这条链路上的落点：
    ///   一个只读会话如果界面上看不出来，用户会以为是**输入坏了** ——
    ///   反复点击、反复敲键盘，然后去查输入链路（而输入链路完全正常）。
    ///   静默的权限降级比没有权限模型更糟：它把"没权限"伪装成"功能故障"。
    ///
    /// 参数指向静态字符串，回调返回后仍有效。
    std::function<void(const char* role)> on_role;

    /// 【输入→显示延迟】第 epoch 次输入**真正进入写队列**的时刻（io 线程调用）。
    ///
    /// 为什么必须由 AsyncClient 上报、而不是让 UI 线程在调用 send_mouse 时自己记：
    ///   ① send_mouse 只是 asio::post，到 io 线程真正入队之间还隔着一次投递；
    ///   ② 未连接（state != kReady）时输入**根本没发出去**，UI 线程如果记了时刻、
    ///      又自增了序号，就会出现"客户端说发了 N 次、服务端只收到 M 次"的假不一致 ——
    ///      而这条"两端计数必须一致"的不变式正是判据的前提。计数点只能落在
    ///      "确实进了写队列"这一个地方。
    /// 时刻用 steady_clock（进程内单调），与客户端其它延迟口径同一个时钟。
    std::function<void(std::int64_t epoch, std::chrono::steady_clock::time_point at)> on_input_sent;
};

class AsyncClient {
public:
    explicit AsyncClient(const ClientConfig& cfg);
    ~AsyncClient();

    AsyncClient(const AsyncClient&)            = delete;
    AsyncClient& operator=(const AsyncClient&) = delete;

    void set_callbacks(ClientCallbacks cb) { callbacks_ = std::move(cb); }

    /// 启动 io 线程并发起首次连接（不阻塞）
    void start();
    /// 停止：关闭连接、取消定时器、退出 io 线程（会 join，可安全地在 UI 线程调用）
    void stop();

    // ---- 以下均可在 UI 线程直接调用（内部投递到 io 线程）----
    void send_mouse(const rc::input::MouseEvent& ev);
    void send_keyboard(const rc::input::KeyboardEvent& ev);
    /// 请求下一帧（拉屏模型：收到一帧后由调用方再请求下一帧）
    void request_frame();

    // ---- 状态查询（跨线程安全）----
    bool          connected() const noexcept { return connected_.load(); }
    std::uint64_t frames_received() const noexcept { return frames_received_.load(); }
    std::uint64_t reconnect_count() const noexcept { return reconnect_count_.load(); }
    std::int64_t  last_rtt_ms() const noexcept { return last_rtt_ms_.load(); }

    /// 【2B 第三刀 权限模型】当前会话是不是只读（服务端下发了 role=view）。
    ///
    /// ⚠️ 它**不是**安全边界，只是"别浪费"：任何程序都能绕开客户端直接发输入，
    ///    真正的拦截在服务端（见 server/session.cpp 的 on_mouse）。
    ///    这个访问器的用途是让 UI 能显示、让判据能读出"客户端确实自律了"。
    bool view_only() const noexcept { return view_only_.load(); }

    /// 【可观测性】被"只读自律"丢掉的输入条数。存在的理由与
    /// `Session::input_triggered_captures_` 完全一致：**开关说自己生效了没有用，
    /// 得有个数证明它真的被走到过**。判据据此区分"客户端没发"与"发了但被服务端拒"。
    std::int64_t suppressed_inputs() const noexcept { return suppressed_inputs_.load(); }

private:
    enum class State {
        kIdle,
        kResolving,
        kConnecting,
        kHandshaking,
        kReady,
        kReconnecting,
        kStopped,
    };

    // ---- 传输层（2B 第二刀 TLS）----
    //
    // 客户端只有 1 个 io 线程、没有 strand，所以这里的 ssl 流直接包在裸 socket 上
    // （服务端那边包在 strand 化的 socket 上，见 server/session.hpp）。
    using ssl_stream_type = asio::ssl::stream<tcp::socket>;

    // ---- 连接流程 ----
    void do_resolve();
    void do_connect();
    void on_connected();
    /// TLS 握手（仅 ssl_ctx_ 非空时由 on_connected() 调用），完成后才进 begin_session()。
    void do_tls_handshake();
    /// 传输层就绪后真正开始会话：开读循环 + 发 Hello + 起心跳 + 武装握手超时。
    /// 明文路径由 on_connected() 直接调用；TLS 路径由握手成功回调调用。
    void begin_session();
    void send_hello();
    /// 武装"预就绪"超时计时器（收到 HelloAck 或 teardown 时取消）。
    ///
    /// 它覆盖的是**从 TCP 连上到 kReady 之前**这一段：明文路径上是"等 HelloAck"，
    /// TLS 路径上是"等 TLS 握手完成"（那种情况下先由 on_connected 武装一次）。
    /// 换句话说它就是那条"握手阶段必须有独立超时"的落地 —— 没有它，对端
    /// "连得上但不回话"会让本机永远停在 kHandshaking（静默，无任何反馈）。
    void start_hello_timeout();

    // ---- 传输层 dispatch：TLS 开/关走不同对象，但两者都满足 Asio 的 ------------
    // ---- AsyncReadStream / AsyncWriteStream 概念，模板只负责"选哪个对象"， ----
    // ---- 不引入任何运行期抽象开销。 ----
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

    /// 底层 TCP 是否还开着。
    ///
    /// ⚠️ 走 TLS 时 `socket_` 已经被 move 进 `tls_`（只剩 executor），
    /// 直接问 `socket_.is_open()` 会**恒为 false** —— 于是 is_local_abort()
    /// 会把"对端正常断开"也当成"我们自己关的"，那一次故障被静默吞掉（连重连都不会发生）。
    /// 所以问"还开着吗"必须走 lowest_layer()。
    bool transport_open() const noexcept {
        return tls_ ? tls_->lowest_layer().is_open() : socket_.is_open();
    }

    /// 本连接是否走 TLS（供日志与判据读取；配置生效 ≠ 机制生效，得有地方读回来）。
    bool tls_active() const noexcept { return tls_ != nullptr; }

    // ---- 读循环 ----
    void do_read_header();
    void do_read_body();
    void dispatch(const std::uint8_t* payload, std::size_t len);

    // ---- 写队列 ----
    void enqueue(flatbuffers::DetachedBuffer body);
    void do_write();

    /// 一次输入**刚刚进了写队列**：自增序号并把"发出时刻"上报给上层（见 ClientCallbacks）。
    /// 只有这一个地方能自增 input_epoch_ —— 计数点必须和"真的发出去"是同一件事。
    void notify_input_sent();

    // ---- 心跳 ----
    void start_heartbeat();
    void send_ping();

    // ---- 拉屏 ----
    void schedule_next_frame_request();

    // ---- 失败与重连 ----
    /// 判断这个错误是不是"我们自己关掉的 socket"造成的（而非新的故障）。
    ///
    /// teardown() 会主动 shutdown/close socket，此时所有挂起的异步操作都会以
    /// "操作已中止"完成。那是我们自己造成的，不能再当成一次新故障 ——
    /// 否则一次掉线会走两遍 teardown + 两遍 schedule_reconnect，
    /// 使 attempts_ 翻倍、退避时间被无谓放大（实测多出一行 "reconnecting in ..."）。
    bool is_local_abort(const error_code& ec) const noexcept;
    void fail(const char* what, const error_code& ec);
    void teardown(const char* reason);
    void schedule_reconnect(const char* reason);
    void on_reconnect_timer(const error_code& ec);

    /// 把当前状态名通知给 UI（只在中间态迁移时用）
    void emit_state();

    const char* state_name() const noexcept;

    ClientConfig cfg_;
    asio::io_context io_;
    asio::executor_work_guard<asio::io_context::executor_type> work_;
    std::thread           io_thread_;
    ClientCallbacks       callbacks_;

    tcp::resolver     resolver_;
    tcp::resolver::results_type endpoints_;
    tcp::socket       socket_;
    /// 2B 第二刀 TLS：非空时承担全部收发；此时 `socket_` 已被 move 进它（只剩 executor）。
    /// 为空 = 未启用 TLS，一切走 `socket_` —— 与引入本刀之前**逐字等价**
    /// （不是"行为等价"，是同一段代码：所有 dispatch 都是 `if (tls_)` 分支）。
    /// 每轮 do_connect() 都会先 reset，保证不会拿上一轮那个已关闭的连接做握手。
    std::unique_ptr<ssl_stream_type> tls_;
    /// 客户端 ssl 上下文。为空 = 不启用 TLS。构造期建立（见构造函数）。
    std::shared_ptr<asio::ssl::context> ssl_ctx_;
    asio::steady_timer heartbeat_timer_;
    asio::steady_timer hello_timer_;
    asio::steady_timer reconnect_timer_;
    asio::steady_timer frame_timer_;

    State state_ = State::kIdle;

    // 读缓冲
    std::array<std::uint8_t, rc::net::kFrameHeaderSize> header_buf_{};
    std::vector<std::uint8_t>                           payload_buf_;

    // 写队列
    std::deque<rc::net::OutgoingMessagePtr> write_queue_;
    bool                                    writing_ = false;

    // 心跳 / 拉屏
    std::uint32_t                          ping_seq_ = 0;
    std::chrono::steady_clock::time_point  last_pong_at_{};
    bool                                   frame_in_flight_ = false;
    std::chrono::steady_clock::time_point  next_frame_at_{};
    /// 上一个 ScreenRequest 真正写进写队列的时刻（= 归因里 wire_ms 的起点）
    std::chrono::steady_clock::time_point  last_screen_request_at_{};

    /// 【输入→显示延迟】已真正入队的输入事件累计数。
    ///
    /// **仅在 io 线程读写**（send_mouse/send_keyboard 的投递回调里自增），
    /// 所以不需要原子 —— 它的作用域就是本 io 线程。
    /// 它与服务端的 inputs_applied_ 是同一个整数序列，靠的是 TCP 保序不丢：
    /// 客户端每写一次、服务端就必然收一次、也就必然应用一次。这条等式是判据
    /// "两端计数一致"的来源，也是整个测量不需要在输入消息里带序号的原因。
    std::int64_t input_epoch_ = 0;

    // 重连
    std::uint32_t attempts_ = 0;
    std::uint32_t rng_      = 0x9E3779B9u;
    /// 不可恢复的错误（例如协议主版本不匹配）：继续重连也只会得到同样结果，
    /// 因此置位后不再重试，避免变成"无限重连风暴"。
    bool          fatal_    = false;

    // 跨线程可读状态
    std::atomic<bool>         connected_{false};
    std::atomic<std::uint64_t> frames_received_{0};
    std::atomic<std::uint64_t> reconnect_count_{0};
    std::atomic<std::int64_t>  last_rtt_ms_{-1};

    /// 【2B 第三刀 权限模型】服务端下发的角色是不是只读。
    ///
    /// 默认 **false** —— 与权限模型存在之前**逐字等价**（老服务端不下发 role 字段，
    /// 客户端读到默认 Control，于是照常发输入，与改动前完全一样）。
    ///
    /// ⚠️ 每次 `teardown()` 都要**复位为 false**：它描述的是"**这次**会话的角色"，
    ///    而不是"这台客户端是什么身份"。不复位的话，连过一次只读服务端之后，
    ///    重连到一个普通服务端仍然是只读 —— 而且日志上看不出为什么（角色是上次那条
    ///    连接留下的），是典型的"陈旧状态冒充当前状态"。
    std::atomic<bool>          view_only_{false};

    /// 被"只读自律"丢掉的输入条数（对外可读，见 suppressed_inputs()）。
    std::atomic<std::int64_t>  suppressed_inputs_{0};
};

} // namespace rc::client
