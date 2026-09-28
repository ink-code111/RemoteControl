#pragma once
// ============================================================
// 远程桌面窗口（第二阶段：网络层换成 AsyncClient，UI 只负责显示与采集输入）
//
// 相对第一阶段的改进：
//  1) 不再持有"阻塞式网络客户端 + 收图线程"，改为接收 AsyncClient 的回调。
//     窗口类里没有任何网络线程，也没有 sleep/等待 —— UI 线程只做绘制。
//  2) 帧在 io 线程只做一次内存拷贝就转交给**独立解码线程**，解码不再占用 io 线程
//     （旧版直接在 io 线程里 image_.Load()，既拖住读循环/心跳/写完成，又要和
//     WM_PAINT 抢同一把 image_mutex_）。
//  3) 断线不再弹框关窗口：客户端会自动重连，窗口改为在标题上显示链路状态，
//     链路恢复后画面自动继续（这才是"断线重连"应有的用户体验）。
//  4) 补上滚轮支持（协议里已预留，属于纯追加能力，不影响既有行为）。
//
// 线程约定：on_frame/on_connected/on_disconnected 都在客户端 io 线程被调用，
//          因此它们绝不能直接操作窗口句柄 —— 跨线程碰 UI 必须 PostMessage。
//          on_frame 也不例外：它只往邮箱里放数据就返回。
//
// 三条线程各管一段，谁也不等谁：
//   io 线程    ── on_frame：拷贝字节 → 投进邮箱 → 唤醒解码线程
//   解码线程    ── 按序取走一帧 → GDI+ 解码 → 整帧替换 / 增量贴进累积画面（重活在这一段）
//   UI 线程    ── WM_PAINT：只做 StretchBlt 贴图；WM_APP_FRAME_READY：触发重绘
//
// 【差异帧为什么把"单槽覆盖"的邮箱改成了有界 FIFO（第三阶段）】
//   第二阶段每帧都是整帧，所以邮箱可以"只留最新一帧"：过期的帧没有价值，丢掉即可。
//   差异帧不是这样——它是**接力**的：第 N 帧只描述"相对第 N-1 帧的变化"，
//   中间少一帧，后面所有帧都会贴到错误的基准上，画面错位且**不会自行恢复**。
//   于是邮箱必须按序消费。容量仍是有限的（满了就丢并进入"等整帧"的失步状态），
//   因为真正的兜底手段是服务端周期性发整帧，而不是让队列无限增长把延迟堆起来。
// ============================================================

#include "async_client.hpp"
#include "input_types.hpp"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#include <atlimage.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

namespace rc::client {

// 自定义消息：工作线程 -> UI 线程的封送
constexpr UINT WM_APP_DISCONNECTED = WM_APP + 1;
constexpr UINT WM_APP_CONNECTED    = WM_APP + 2;
constexpr UINT WM_APP_STATE        = WM_APP + 3;
/// 解码线程解好一帧后叫 UI 线程重绘（不带数据：图像本体在 image_ 里，受锁保护）
constexpr UINT WM_APP_FRAME_READY  = WM_APP + 4;
/// 【2B 第三刀 权限模型】io 线程发现服务端下发的角色后，通知 UI 更新标题。
/// 新开一个 id 而不是复用 WM_APP_STATE：那条消息的语义是"**握手前**的中间态"，
/// 收到时会被 `connected_` 判掉（见 WM_APP_STATE 的处理），复用它等于新消息永不生效。
constexpr UINT WM_APP_ROLE         = WM_APP + 5;

class RemoteWindow {
public:
    RemoteWindow(AsyncClient& net, std::wstring title);
    ~RemoteWindow();

    RemoteWindow(const RemoteWindow&)            = delete;
    RemoteWindow& operator=(const RemoteWindow&) = delete;

    bool create(int show_cmd);
    int  run_message_loop();

    /// 【诊断】累积画面落盘（前缀）。
    /// 客户端会在"收到整帧"的那一刻同时落下 <前缀>_accum.png（应用前的累积画面）
    /// 与 <前缀>_key.png（刚收到的整帧）——两者只差一个帧周期，可直接逐像素对照。
    /// 差异帧"贴歪了"不会有任何报错，这是唯一能钉死它的办法。必须在 create() 之前调用。
    void set_debug_dump(std::wstring path_prefix, std::uint64_t after_frames);

    /// 【诊断】让解码线程每 every_n 帧停顿 ms 毫秒，用来制造抖动（0 = 关闭）。
    /// 必须在 create() 之前调用（解码线程在 create() 里才起来）。
    void set_debug_stall(std::uint32_t every_n, std::uint32_t ms);

    /// 【诊断】让**解码线程**每 every_n 帧停顿 ms 毫秒，用来制造队列溢出 → 触发 resync
    /// （0 = 关闭）。必须在 create() 之前调用。
    ///
    /// 注意它停的是解码线程、而不是"直接丢一帧"：seq 是客户端自己编的，
    /// 投递之前丢掉的帧不占号，于是根本检测不出缺口（第一版实测踩过，见 ClientConfig）。
    /// 只有让解码真的跟不上、撞上 product 自己的队列溢出路径，缺口才会真的出现。
    void set_debug_decode_stall(std::uint32_t every_n, std::uint32_t ms);

    /// 【整帧优先通道】整帧走一条溢出清不掉的通道（默认开，见 ClientConfig::keyframe_priority）。
    /// 必须在 create() 之前调用。
    void set_keyframe_priority(bool on);

    /// 【UI 绘制】整帧缩到客户区时用的 StretchBlt 模式（见 ClientConfig::stretch_mode）。
    /// "halftone" = GDI 高质量插值；"coloroncolor" = 删行删列（最近邻，快得多）。
    /// 必须在 create() 之前调用。
    void set_stretch_mode(const std::string& mode);

    /// 【UI 绘制】是否只重绘**变化的那一块**（见 ClientConfig::partial_repaint）。
    /// 关掉 = 每帧 `InvalidateRect(hwnd, nullptr, FALSE)`，与引入本机制之前逐字等价。
    /// 必须在 create() 之前调用。
    void set_partial_repaint(bool on);

    /// 【按脏区重绘的判据】诊断落盘：把"窗口实际画出来的"与"同一时刻整幅重绘的参考"
    /// 成对存成 PNG（前缀_ref_N / 前缀_paint_N，最多 kMaxDebugDumps 对）。
    /// 空前缀 = 关。必须在 create() 之前调用。
    void set_paint_dump(std::wstring path_prefix, std::uint32_t after_paints);

    /// 【按脏区重绘】失效矩形四边外扩的像素数（见成员 invalidate_halo_px_ 的说明）。
    /// 负值 = 故意缩进，仅用于判据的反向对照。必须在 create() 之前调用。
    void set_invalidate_halo(int px);

    /// 【诊断】自动输入源：每 interval_ms 毫秒发一次鼠标移动，在两个目标点之间来回。
    /// interval_ms == 0 = 关闭（生产路径就是这个）。
    ///
    /// 为什么要自带输入源：延迟判据必须有一个**可控且持续**的输入流。
    /// 靠"人手去动鼠标"意味着判据依赖人的在场与操作节奏，而"改动前后各跑一轮"的
    /// 那种对照根本没法保证两轮输入节奏一致。这与 §8.18 那条同源：
    /// **夹具必须自带信号源，不能依赖环境恰好提供信号。**
    ///
    /// 为什么来回两个点而不是钉在一个点上：鼠标停在原地时 SetCursorPos 不会产生
    /// 任何可见变化（画面里光标没动），"输入被看见"这件事就无从验证；
    /// 来回移动让每一次输入都对应一次真实的光标位移。
    /// 坐标用**远端画面的比例**（0..1），因此不依赖具体分辨率，换台机器不用改夹具。
    void set_auto_input(std::uint32_t interval_ms, double x0_frac, double x1_frac, double y_frac);

    /// 【诊断】是否把本地鼠标/键盘消息转发到远端（默认开 = 生产行为）。
    ///
    /// 关掉它只有一个用途：**测量输入→显示延迟时切断同机自测的输入回灌闭环**。
    /// 服务端 SetCursorPos 移动的是本机物理光标；它一旦落到客户端窗口上，
    /// 客户端就会收到 WM_MOUSEMOVE、把它当用户操作再发给服务端 —— 输入流里
    /// 于是混进一路既不可控也不可预期的反馈。两台机器部署时这条回路并不存在，
    /// 所以关掉它反而更接近真实拓扑。必须在 create() 之前调用。
    void set_input_forwarding(bool on);

    // ---- AsyncClient 回调（在 io 线程调用）----
    /// 只把帧字节拷进邮箱，不做解码（解码在 decode_loop 那个线程里做）
    void on_frame(const ScreenFrameView& frame);
    /// 第 epoch 次输入**真正进入写队列**的时刻（见 ClientCallbacks::on_input_sent）
    void on_input_sent(std::int64_t epoch, std::chrono::steady_clock::time_point at);
    void on_connected();                         ///< PostMessage 封送到 UI 线程
    void on_disconnected(const std::string& reason);
    /// 中间态（连接中/握手中/重连中）。有了它，"握手还没完成"这段时间
    /// 界面上才有反馈，而不是长时间静默的白窗口。
    void on_state(const char* state);
    /// 【2B 第三刀 权限模型】服务端下发的角色（"control" / "view"）。
    /// 只读必须**在标题上看得见** —— 否则"输入没反应"会被读成"输入坏了"。
    void on_role(const char* role);

    HWND hwnd() const { return hwnd_; }

    /// **实际生效**的缩放模式名（自报用）。配置里写的字符串与真正生效的可能是两回事
    /// （无法识别的取值会被 set_stretch_mode 回落）—— 所以日志要报这个，不是报配置。
    const char* stretch_mode_name() const { return stretch_mode_name_; }

private:
    static LRESULT CALLBACK proc_static(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);
    LRESULT proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);

    // ---- 解码线程 ----
    void start_decode_thread();
    /// 置停止标志 + 唤醒 + join。必须在析构里调用：否则解码线程可能在被销毁的
    /// 对象上取 image_mutex_ / 读成员。
    void stop_decode_thread();
    /// 解码线程主体：按序取一帧 → GDI+ 解码 → 整帧替换或增量贴图 → 通知 UI 重绘
    void decode_loop();

    void send_mouse_action(rc::input::MouseAction action, LPARAM lp);
    void send_mouse_move_throttled(LPARAM lp);
    void send_wheel(WPARAM wp);
    void send_key(int vk, bool up);
    bool map_to_remote(LPARAM lp, int& out_x, int& out_y);
    /// 标题是三个状态的合成结果（已连接 / 中间态 / 断开+原因），统一在这里渲染
    void refresh_title();

    AsyncClient& net_;
    HWND         hwnd_ = nullptr;
    std::wstring base_title_;

    // ---- 解码线程与它的待解帧队列 ----
    // 第一阶段/第二阶段用的是"最新覆盖"的单槽邮箱：解码一旦跟不上，旧帧直接丢掉，
    // 画面只会掉帧不会错位。差异帧打破了这个前提（见文件头注释），所以改成按序 FIFO。
    // 容量取 8 而不是无限：真正的恢复手段是服务端的周期性整帧，队列堆长只会把
    // 画面延迟越积越大（对着三秒前的桌面点鼠标是比花屏更糟的体验）。
    struct PendingFrame {
        std::vector<std::uint8_t> bytes;
        bool          delta = false;   ///< true = 增量帧（bytes 只是 rect 那块）
        std::int32_t  rx    = 0;
        std::int32_t  ry    = 0;
        std::uint64_t seq   = 0;       ///< 投递序号，用于检测"中间丢了一帧"
        /// 归因用（见 decode_loop 的汇总行）：io 线程收到它的时刻、它从"请求发出"
        /// 到"到达客户端"的往返毫秒数，以及它距上一帧到达的间隔。
        ///
        /// 【为什么必须把间隔也带进来】"无变化"的空增量帧在 on_frame 里直接 return，
        /// 进不了这个队列 —— 于是"往返/解码/贴图"只能在**变化帧**上取样，而
        /// "到达间隔"如果对所有帧取平均，两者就不是同一个集合。实测过这个坑：
        /// 混着算会让"四段之和"比周期大出 5~8 ms，被误读成"客户端在限流等待"，
        /// 而真相只是取样集合不同。间隔必须跟帧一起走，才能保证同口径。
        std::chrono::steady_clock::time_point recv_at{};
        double        wire_ms = 0.0;
        /// 收到这一帧时客户端是否有在途 ScreenRequest（= wire_ms 有没有定义）。
        /// 见 ScreenFrameView::requested：无在途请求的帧是服务端主动推的，
        /// 它的 wire_ms 留 0 且**不计入往返均值**，只计数后打出来。
        bool          requested = false;
        double        gap_ms  = 0.0;
        /// 【输入→显示延迟】这一帧的像素采集之前，服务端已应用的输入事件数（下界）。
        /// 跟帧一起走队列，理由与上面的几个字段相同：队列里可能压着多帧，
        /// 放成员变量会把"上一帧的序号"错记到"这一帧"头上。
        std::int64_t  input_epoch = 0;
    };

    /// 一个汇总周期内的到达间隔样本（用来算 P95 / max）。
    ///
    /// 【为什么不能只留累计和】均值的世界里看不出抖动：60 帧里 59 帧 41 ms +
    /// 1 帧 1500 ms，均值是 65 ms —— 读起来"有点慢但还行"，而用户真正感受到的是
    /// 那一次 1.5 秒的卡死。抖动必须看分布，看分布就必须留样本。
    ///
    /// 容量 1024 按最坏情况放：汇总周期 5 s，就算 200 fps 也才 1000 帧。
    /// 真填满了说明有别的异常，用 overflow 记下来，而不是静默覆盖最早的样本
    /// （覆盖会让"这 5 秒里发生过异常"这个事实消失）。
    struct GapSamples {
        static constexpr std::size_t kCap = 1024;
        double        v[kCap]  = {};
        std::size_t   n        = 0;
        std::uint64_t overflow = 0;
        void add(double ms) {
            if (n < kCap) {
                v[n++] = ms;
            } else {
                ++overflow;
            }
        }
        void clear() {
            n        = 0;
            overflow = 0;
        }
    };
    static constexpr std::size_t kMaxPendingFrames = 8;

    /// 【整帧优先通道】等在那儿的整帧（尚未应用的最新一个）。
    ///
    /// 它**不在** `pending_frames_` 里，所以 `pending_frames_` 溢出被整段清空时清不到它 ——
    /// 这正是这个设计的目的：整帧是 resync 唯一的解药，它不能在"最需要它的时候"被丢掉。
    ///
    /// 为什么一个槽就够：更新的整帧直接顶掉旧的（旧整帧所携带的信息被新整帧完全取代），
    /// 所以"被顶掉"不是丢失。为什么必须"优先于队列"：整帧是完整画面，排在它前面的
    /// 增量帧全部被它取代；而排在它**后面**的增量帧是相对它的，必须先应用整帧。
    /// 两种情况都指向同一个动作：**只要有整帧在等，就先应用它。**
    std::optional<PendingFrame> pending_key_; ///< 受 frame_mutex_ 保护

    std::thread               decode_thread_;
    mutable std::mutex        frame_mutex_;   ///< 保护待解队列 / 序号 / 观测计数
    std::condition_variable   frame_cv_;
    std::deque<PendingFrame>  pending_frames_;
    std::uint64_t             frame_seq_next_ = 0;
    bool                      decode_stop_    = false;

    // ---- 观测计数（统一受 frame_mutex_ 保护）----
    // 这些是"差异帧到底有没有生效"的直接证据：增量帧占比高、单帧字节数下降，
    // 说明服务端确实只在传变化区域；失步数持续 > 0 则说明客户端跟不上，
    // 该调的是队列容量或服务端关键帧间隔，而不是继续抠解码实现。
    std::uint64_t frames_received_ = 0; ///< io 线程收到并投递的帧数
    std::uint64_t frames_dropped_  = 0; ///< 因队列溢出被丢掉的帧数（差异帧下会让画面失步）
    std::uint64_t full_frames_     = 0; ///< 其中整帧
    std::uint64_t delta_frames_    = 0; ///< 其中增量帧
    std::uint64_t idle_frames_     = 0; ///< 其中"无变化"的空增量帧
    std::uint64_t bytes_sum_       = 0; ///< 收到的编码字节总数

    // ---- 整帧优先通道的观测（第 2 步修订）----
    // 它是这一改动的**直接证据**，也是判据的落点：开关"看起来生效"没有意义，
    // 要看的是"整帧到底有没有被吞掉"。
    /// 被队列溢出**整段清掉**的整帧数。开了优先通道它就必须恒为 0 ——
    /// 不是 0 就说明整帧还在跟增量帧挤同一条队列（即开关没生效或失效）。
    std::uint64_t key_frames_discarded_  = 0;
    /// 真正被**应用**（贴进画面）的整帧数。它是"通道在起作用"的直接证据：
    /// 解码线程被压得只剩零头的时间片时，还能持续应用整帧，只可能是优先通道送来的。
    std::uint64_t key_frames_applied_    = 0;
    /// 在优先通道里被**更新的整帧顶掉**的整帧数。这不是丢失：新整帧完全取代旧整帧。
    std::uint64_t key_frames_superseded_ = 0;
    /// 从优先通道取出、并**跳过队列里若干增量帧**直接应用的次数（即"整帧救了场"的次数）。
    /// 稳态下它接近 0；持续落后时它会持续增长 —— 正是我们要看到它增长。
    std::uint64_t key_frames_promoted_   = 0;

    /// 整帧优先通道开关（启动前设好、之后只读，所以不需要同步）。
    bool          keyframe_priority_ = true;

    /// 【UI 绘制】缩放模式：GDI 常量 + 用于自报的名字。启动前设好、之后只读。
    ///
    /// 名字必须一路带到 `[paint]` 行去：那一行报的 StretchBlt 耗时（12 ms 还是 1 ms）
    /// **只有在知道用的哪个模式时才有意义**。跨轮比较时模式不同就会读错 ——
    /// 这正是"出图帧率"那条被撤回的教训（docs §8.22.5）：
    /// 引用数字前先问口径，数字必须自己说清它是在什么条件下测的。
    int         stretch_mode_      = HALFTONE;
    const char* stretch_mode_name_ = "halftone";

    /// 【按脏区重绘】开关（见 set_partial_repaint）。启动前设好、之后只读。
    bool partial_repaint_ = true;

    /// 【按脏区重绘】失效矩形在四边各**外扩**多少像素（见 set_invalidate_halo）。
    ///
    /// 光晕：源只在脏区内变化时，目标侧**还会跟着变**的那一圈有多宽。这个量推不出来
    /// —— halftone 的插值核比"盒式映射"宽，所以脏区**外**一圈的目标像素，采样邻域
    /// 也可能与脏区相交 ⇒ 它们的正确值也变了。若失效矩形没覆盖这一圈，重绘区外围
    /// 就会留下一条**永久残影**（只在图像边缘，很难注意到）。
    /// 实测（§6.25 探针：2560×1440 → 1002×664，3 种内容 × 3 种摆放 × 4 档尺寸）
    /// = 四边各 **1 px**。
    ///
    /// 默认取 2 而不是 1：两个方向的风险完全不对称 —— 多失效 2 px 的代价测不出来，
    /// 而少 1 px 就是残影；顺带兜住别的机器上更宽的核。
    ///
    /// ⚠️ 允许**负值**，那是留给反向对照的：把失效矩形**缩进** N 像素，就一定会留下
    /// 一条 N 像素宽的残影 ⇒ 判据必须报 FAIL。不故意写坏一次，就不知道判据是不是死的。
    int invalidate_halo_px_ = 2;

    // ---- 性能归因（第三阶段第二步）----
    // 差异帧做完后，服务端的分段耗时之和（22.4 ms）明显小于端到端帧周期（47.2 ms）。
    // 差额既不在服务端抓屏也不在服务端编码，那就只能在"两次请求之间"。
    // 下面这几个量把那段时间拆开，判据是**各段之和必须接近实测周期** ——
    // 对不上就说明还有没被计到的地方（上一次差异帧的 bug 就是"服务端发了 1638 帧、
    // 客户端只认到 141 帧"，差的正是没被观测到的那部分）。
    std::chrono::steady_clock::time_point last_frame_recv_at_{}; ///< 仅 io 线程读写
    double        gap_ms_sum_ = 0.0; ///< 相邻两帧到达间隔之和（受 frame_mutex_ 保护）
    std::uint64_t gap_count_  = 0;   ///< 上面那个和的样本数

    /// 到达间隔的**样本**。为什么同时留两个口径 —— 它们回答的是两个不同的问题，
    /// 只留一个都会骗人：
    ///   gap_all_：**全部到达帧**（含"无变化"的空增量帧）。空帧也是完整走了一趟
    ///             请求-应答，它决定"下一拍什么时候来"，这才是循环节奏的真身。
    ///             io 线程写、解码线程每 5 秒取走清零 ⇒ 受 frame_mutex_ 保护。
    ///   gap_chg_：**变化帧**（进过解码队列的）。用户眼睛看到的更新节奏是它。
    ///             写和读都在解码线程上 ⇒ 不需要锁。
    /// 只看变化帧，空帧期间的停顿会被漏掉；只看全部帧，一次"每帧都很准时、
    /// 但变化帧之间隔了很久"的退化会被平均掉 —— 那正是卡顿的体感。
    GapSamples gap_all_; ///< 受 frame_mutex_ 保护
    GapSamples gap_chg_; ///< 仅解码线程

    // ---- 输入→显示延迟（第三阶段第 2 步 2b）----
    //
    // 这是本项目第一次把两个进程、三段线程、两条时钟缝在一起量一个数，
    // 所以"谁在哪个线程读写、靠什么同步"必须写清楚，否则必然串号。
    //
    // 测量模型（在客户端**单时钟**里闭合，不需要任何跨进程时钟同步）：
    //     t_send(k)      io 线程：第 k 次输入真正进了写队列
    //     t_composite(k) 解码线程：带 input_epoch >= k 的**首帧**被贴进累积画面
    //     t_present(k)   UI 线程：该帧被 StretchBlt 画到窗口上 —— 软件能测到的最末端
    //                    （真实显示器的扫描输出测不到，那是光传感器/高速相机的活）
    //   延迟 = t_present(k) - t_send(k)，两个时刻都在客户端自己的 steady_clock 上。
    //   服务端只回一个**序号**（本帧像素采集前已应用的输入数），而序号不需要时钟同步 ——
    //   这就是这个设计相对"回传服务端时间戳"的全部优势。
    //
    // 为什么留两个口径（输入→贴图 / 输入→显示）：两者之差恰好是"UI 消息调度"那一段。
    // UI 线程被别的消息压住时它会变大，而那正是用户能感觉到的"系统在忙"。
    // 2a 的教训是口径不能只剩一个（§6.16）；这里同理，只报总数会把 UI 调度这段藏起来。

    /// t_send：按序号索引的环形表（下标 = epoch % 容量），存 steady_clock 的纳秒计数。
    /// io 线程写、UI 线程读 ⇒ 必须是原子；值 0 表示"这一格还没记过"。
    static constexpr std::int64_t kInputRingCap = 4096;
    std::atomic<std::int64_t> input_sent_ns_[kInputRingCap]{};
    /// 已记录的输入序号（io 线程写、其余线程读）。结算是**按它封顶**的：
    /// 客户端对 k > 它的序号根本没有时刻记录，凭空配对会在重连（两端序号各自归零）
    /// 前后产出一大串荒谬的延迟值 —— 那种"数字看起来正常但其实是错的"正是本项目
    /// 最忌讳的失效型态。
    std::atomic<std::int64_t> input_sent_epoch_{0};
    /// 结算时"找不到时刻记录"而跳过的次数（环形表被整圈覆盖后必然出现）。
    /// > 0 说明"已发 N / 已配对 M"这本账不完整 —— 分位数只覆盖了有记录的那部分。
    std::atomic<std::int64_t> lat_missing_time_{0};

    /// 输入→贴图 的样本。写和读都在解码线程 ⇒ 不需要锁。
    GapSamples lat_composite_;
    /// 输入→显示 的样本。UI 线程写（每次 WM_PAINT 结算一批），解码线程每 5 秒取走汇总：
    /// 全项目唯一一处"UI 线程写、解码线程读"的样本集合，所以单独配一把锁。
    GapSamples lat_present_;             ///< 受 lat_mutex_ 保护
    mutable std::mutex lat_mutex_;       ///< 只保护 lat_present_
    /// 超出合理上限而被**丢弃**的样本数（见 remote_window.cpp 的 kInputLatMaxMs）。
    /// 单列一档是为了把"账本身错了"与"链路很慢"区分开：直接算进分布会让
    /// P95/max 变成幻觉，直接丢掉又会让"没测到"被掩盖。两个线程都会加，故用原子。
    std::atomic<std::int64_t> lat_rejected_{0};

    /// 解码线程自己的"已配到第几个"（输入→贴图 口径）
    std::int64_t paired_composite_ = 0;
    /// UI 线程自己的"已结算到第几个"（输入→显示 口径）
    std::int64_t presented_epoch_ = 0;
    /// 与 image_ 内容**一致**的可见输入序号：解码线程贴图后写，UI 线程在同一个临界区里读。
    /// 受 image_mutex_ 保护 —— 这一点很要紧：它必须与"此刻画出来的那张画面"是同一份状态，
    /// 否则会把还没画上去的帧提前算成已显示（延迟报小，而报小是危险方向）。
    std::int64_t visible_input_epoch_ = 0; ///< 受 image_mutex_ 保护

    // ---- UI 绘制分段（第三阶段，2026-09-24）----
    //
    // 【为什么必须拆开】「输入→显示」比「输入→贴图」稳定多出的那 11.8~16.5 ms，
    // 此前一直只被当成一个数报出来（"UI 消息调度"）。可它内部混着两件
    // **修法完全相反**的事：
    //   ① **等** UI 线程来画：解码线程 PostMessage → WM_PAINT 入口。
    //      这是等待（消息投递 + 系统合成 WM_PAINT 的时机），本进程没有在算任何东西；
    //      要缩短它得改"怎么请求重绘"（InvalidateRect 只是标记，WM_PAINT 要排队等合成）。
    //   ② **画**本身：HALFTONE StretchBlt 把 2560×1440 软件插值缩到客户区。
    //      这是计算，正比于目标像素数与缩放比；要缩短它得改缩放模式或尺寸。
    // 只报一个数，下一个人会拿"UI 调度 12.6"去优化错的那一半 ——
    // §8.22.5 那条（**有解释的错数会被引用**）就是这个形态。所以拆成三段、各自留样本。
    //
    // 谁读写：这三组样本都是 **UI 线程写、解码线程每 5 秒取走汇总** ⇒ 与 lat_present_
    // 同一处置，共用 lat_mutex_（全项目只有这两处是"UI 写、解码读"）。

    /// 解码线程在 PostMessage(WM_APP_FRAME_READY) **之前**写下的时刻。
    /// 用"最后一次"而不是"每帧一份"是**语义正确**的：WM_PAINT 画的是当前累积画面，
    /// 本来就对应最新那一帧（多次 InvalidateRect 会被系统合并成一次绘制）。
    /// 0 = 还没投递过任何一帧。写用 release、读用 acquire —— 虽然 PostMessage 的
    /// 消息投递自身就带跨线程同步，但不写清楚必然有人要重推一遍。
    std::atomic<std::int64_t> frame_posted_ns_{0};

    /// 投递 → WM_PAINT 入口（**等待**：消息投递 + 重绘调度）
    GapSamples paint_wait_;              ///< 受 lat_mutex_ 保护
    /// WM_PAINT 入口 → StretchBlt 开始（BeginPaint + 抢 image_mutex_ + 设缩放模式）
    GapSamples paint_prep_;              ///< 受 lat_mutex_ 保护
    /// StretchBlt 本身（**计算**：HALFTONE 软件插值缩放）
    GapSamples paint_blt_;               ///< 受 lat_mutex_ 保护
    /// 本段绘制次数 / 其中配上投递时刻的 / 投递时刻晚于绘制开始而无定义的。
    /// 第 2 与第 1 之差 = "有绘制没能配上投递"（首帧、或系统要求的重绘）；
    /// 第 3 个 > 0 = 那一刻的"等待"没有定义（算出来是负值）—— 必须计数，
    /// 不能静默按 0 算进分布（那会拉低均值，是"看起来正常"的错数）。
    std::int64_t  paint_count_   = 0;    ///< 受 lat_mutex_ 保护
    std::int64_t  paint_paired_  = 0;    ///< 受 lat_mutex_ 保护
    std::int64_t  paint_stale_   = 0;    ///< 受 lat_mutex_ 保护
    /// 最近一次绘制时的"帧尺寸 → 客户区尺寸"。StretchBlt 的代价正比于目标像素数，
    /// 不报尺寸的话，跨机器/跨窗口尺寸的数字根本没法比较（本项目老规矩：
    /// 引用性能数字必须带上"在什么条件下测的"）。
    std::atomic<std::int64_t> paint_src_w_{0};
    std::atomic<std::int64_t> paint_src_h_{0};
    std::atomic<std::int64_t> paint_dst_w_{0};
    std::atomic<std::int64_t> paint_dst_h_{0};

    /// 【按脏区重绘的自证】每次绘制时**实际会被写到**的客户区面积占比。
    ///
    /// 取自 `GetClipBox(hdc)` —— `BeginPaint` 已经把窗口的**更新区**设成这次绘制的
    /// 裁剪区（GDI 的行为），所以它就是"真正落笔的范围"。为什么非要这个数：
    /// 「我调用了 InvalidateRect」只是**动作**，不是**结果**；本项目的规矩是
    /// 「配置生效 ≠ 机制生效」，一个每帧都要付的开销类改动必须能自己证明省下来了
    /// （§6.12 / §8.22.2）。关掉开关时它应当回到 ≈100%。
    GapSamples    paint_clip_frac_;      ///< 受 lat_mutex_ 保护
    std::int64_t  paint_clip_full_ = 0;  ///< 其中裁剪≈整窗的次数（整帧失效/首次显示）

    /// UI 线程：把这次 WM_PAINT 的三段耗时记账（见上面 UI 绘制分段的说明）。
    /// `clip_frac` = 本次实际被画的客户区面积占比（见 paint_clip_frac_）。
    void note_paint(std::chrono::steady_clock::time_point t_paint,
                    std::chrono::steady_clock::time_point t_blt0,
                    std::chrono::steady_clock::time_point t_blt1,
                    double clip_frac);

    /// 【诊断】自动输入源（见 set_auto_input）。启动前设好、之后只读。
    std::uint32_t auto_input_interval_ms_ = 0;
    double        auto_input_x0_ = 0.3;
    double        auto_input_x1_ = 0.7;
    double        auto_input_y_  = 0.5;
    std::thread   auto_input_thread_;
    std::atomic<bool>         auto_input_stop_{false};
    /// 自动源自己数的"我发了几次"。它与 input_sent_epoch_ 的差就是"另有别人在发输入"
    /// （同机自测时典型来源是输入回灌）—— 判据靠这个差来验证"输入流是干净的"。
    std::atomic<std::int64_t> auto_input_sent_{0};

    /// 【诊断】是否把本地鼠标/键盘消息转发到远端（默认 true = 生产行为）
    bool input_forwarding_ = true;

    void run_auto_input();

    /// UI 线程：把"截至本次 WM_PAINT，画面已经反映了哪些输入"结算成延迟样本。
    /// 参数是在 WM_PAINT 的临界区里取到的可见序号（必须与刚才画出去的画面一致）。
    void settle_present(std::int64_t visible_epoch);

    // ---- 共享状态：解码线程写 / UI 线程读 ----
    // 注意锁的粒度：解码全程在**临时** CImage 上做，只有"换指针"或"贴一块脏区域"
    // 这一步进锁，所以 WM_PAINT 等这把锁的时间是常数级，不再受解码耗时影响。
    //
    // 差异帧下 image_ 变成**累积画面**：它不再每帧被整体替换，而是由增量帧
    // 一块一块地修补（增量帧的脏矩形坐标就是相对它而言的）。
    mutable std::mutex image_mutex_;
    CImage             image_;
    int                remote_w_ = -1;
    int                remote_h_ = -1;

    // ---- 诊断：把累积画面落盘（默认关闭，见 set_debug_dump）----
    // 每个关键帧落一对图，最多 kMaxDebugDumps 对：
    // 桌面随时可能在动，单次的"累积画面 vs 整帧"差值里既有我们关心的错位、
    // 也有"桌面自己在这一帧周期里变了多少"。多落几对，脚本就能用
    // d1（上一张整帧 → 本对累积画面，判别力）与 d2（累积画面 → 本对整帧，正确性）
    // 的比值来判：正确实现在至少一对上会给出极小的 d2/d1。
    static constexpr int kMaxDebugDumps = 8;

    std::wstring  dump_path_;                 ///< 文件名前缀（仅解码线程读）
    std::uint64_t dump_after_    = 0;         ///< 至少应用过多少帧才允许落盘
    std::uint64_t dump_applied_  = 0;         ///< 已作用到画面的帧数（解码线程自增）
    int           dump_count_    = 0;         ///< 已落盘的对数

    // ---- 诊断：把"窗口实际画出的"与"整幅重绘的参考"成对落盘（见 set_paint_dump）----
    // 这是"按脏区重绘"那条判据的落点：两者取自同一个 image_、同一个临界区，
    // 所以它们逐像素相等 ⇔ 裁剪没有漏掉任何该重绘的地方（没有残影）。
    // 只在 UI 线程读写 ⇒ 不需要锁。
    std::wstring  paint_dump_path_;            ///< 文件名前缀（空 = 关）
    std::uint32_t paint_dump_after_ = 20;      ///< 画过多少次之后才开始落（避开首帧整窗那段）
    std::uint32_t paint_seen_       = 0;       ///< 累计绘制次数（含不落盘的）
    int           paint_dump_count_ = 0;       ///< 已落盘的对数（上限 kMaxDebugDumps）

    // ---- 诊断：制造抖动 / 丢帧（第 2 步，见 ClientConfig 里同名字段的说明）----
    // 这四个都是"启动前设好、之后只读"，所以不需要同步：set_debug_* 一律要求在
    // create() 之前调用（那时解码线程与 io 回调都还没起来）。
    //
    // stall 为什么必须落在 **io 线程**（on_frame 里）而不是解码线程：
    //   拉屏是"收到一帧才请求下一帧"，而"请求下一帧"是在 on_frame **返回之后**
    //   由 async_client 发出的。所以只有让 on_frame 晚返回，才能推迟下一次请求、
    //   把停顿时长原样加到下一帧的到达间隔上。放在解码线程上动不了请求节奏 ——
    //   它顶多让队列积压（queue_ms 涨），gap 分布纹丝不动。这一条实测确认过。
    std::uint32_t debug_stall_every_n_ = 0; ///< 每 N 帧停顿一次（0 = 关）；仅 io 线程读
    std::uint32_t debug_stall_ms_      = 0; ///< 每次停顿多久 ms；仅 io 线程读
    std::uint32_t debug_stall_seen_    = 0; ///< io 线程自己的帧计数（仅 io 线程读写）
    std::uint32_t debug_decode_stall_every_n_ = 0; ///< 每 N 帧停顿一次（0 = 关）；仅解码线程读
    std::uint32_t debug_decode_stall_ms_      = 0; ///< 每次停顿多久 ms；仅解码线程读
    std::uint32_t debug_decode_stall_seen_    = 0; ///< 解码线程自己的帧计数（仅解码线程读写）

    // ---- 仅 UI 线程访问 ----
    std::optional<std::chrono::steady_clock::time_point> last_move_;

    // ---- 链路状态（UI 线程显示用；由封送消息更新）----
    bool        connected_ = false;
    /// 当前链路状态文案（由 WM_APP_STATE 更新）。
    /// 空 = 无中间态 ⇒ 退化为「重连中…」—— 这个退化**只在"确实会重连"时才成立**：
    /// 终止态会由客户端的 emit_state("stopped") 填成「不会再重连」。
    /// （2026-09-27 之前终止态不填，于是"已经放弃"被显示成"正在重连"。）
    std::string status_label_;
    std::string last_reason_;

    /// 【2B 第三刀 权限模型】服务端下发的角色名（默认 "control"）。
    /// 与 `connected_` 一样只在 UI 线程读写（经 PostMessage 封送）。
    /// 断开时**不清空**：用户刚看到 "[只读]" 就因为网络抖了一下而消失，
    /// 会让"刚才那个只读提示到底是不是真的"变得无从确认。
    /// 它只在下一次握手应答时被覆盖。
    std::string role_label_ = "control";
};

} // namespace rc::client
