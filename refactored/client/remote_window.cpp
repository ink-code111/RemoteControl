#include "remote_window.hpp"
#include "logger.hpp"
#include "win_raii.hpp"

#include <windowsx.h> // GET_X_LPARAM / GET_Y_LPARAM / GET_WHEEL_DELTA_WPARAM

#include <cstring>
#include <memory>
#include <vector>

#include <algorithm>
#include <cmath>
#include <thread>

namespace rc::client {
namespace {

// 与旧版一致的节流阈值：约 60 次/秒
constexpr auto    kMouseMoveMinInterval = std::chrono::milliseconds(16);
constexpr wchar_t kClassName[]          = L"RcRemoteWindow";

/// "这一帧在队列里等了多久"超过它，才算一次**冻结**（而不是均匀的慢）。
///
/// 为什么需要一个门槛：稳态下队列里偶尔压着一帧是正常的（解码永远比到达慢半拍），
/// 那种一两帧的落后不该记成冻结 —— 否则判据的"正向不许有冻结"就永远不成立。
///
/// 为什么取固定毫秒而不是"几个帧周期"：这里的量是**用户感知**的门槛，不是性能指标。
/// 200 ms 以下的画面停滞，人眼基本感觉不到是"卡"；而真正的 resync 冻结是秒级的
/// （关键帧间隔 60 帧 × 40 ms ≈ 2.5 s），两者差一个数量级，门槛放在哪里都不影响结论。
/// 反过来，用"帧周期倍数"会让门槛随机器负载浮动，而感知门槛本不该浮动。
constexpr double kFreezeFloorMs = 200.0;

/// 从**已排序**的数组里取分位数（q ∈ [0,1]，nearest-rank 法）。
///
/// 用最近秩法、不做线性插值：抖动关心的是"最坏的那几帧到底有多坏"，
/// 插值恰好会把极值向外抹平 —— 而极值就是这里唯一重要的东西。
/// n == 0 返回 0，调用方必须靠样本数去区分"值为 0"和"没测到"。
double percentile_sorted(const double* v, std::size_t n, double q) {
    if (n == 0) {
        return 0.0;
    }
    if (q <= 0.0) {
        return v[0];
    }
    if (q >= 1.0) {
        return v[n - 1];
    }
    const auto rank = static_cast<std::size_t>(std::ceil(q * static_cast<double>(n)));
    const auto idx  = rank == 0 ? std::size_t{0} : rank - 1;
    return v[idx < n ? idx : n - 1];
}

/// 【输入→显示延迟】合理上限：超过它的一律**不计入分布**，也不计入样本溢出。
///
/// 为什么要单列一档而不是直接算进 P95/max：这类值几乎只可能来自"账本身错了"
/// （两端序号错位 —— 例如重连前后各自计数），而不是链路真的慢了几秒。
/// 把它算进分布，P95/max 就成了幻觉；直接丢掉又会让"没测到"这件事被掩盖。
/// 所以另立一个计数器，在 [input-latency] 行里如实报出来："丢弃 N 个"。
constexpr double kInputLatMaxMs = 10000.0;

// ---------------------------------------------------------------------------
// 【按脏区重绘】失效矩形（客户区坐标）怎么从"帧空间的脏区"算出来
// ---------------------------------------------------------------------------
// 光晕 / 默认外扩值 / 反向对照用的负值：见 remote_window.hpp 里
// `invalidate_halo_px_` 的完整说明（实测依据在 docs §6.25）。

/// 帧空间坐标压进 PostMessage 参数时的上界（16 位字段）。合法脏区必然远小于它。
/// 一旦越界就退化成"整窗失效"（见 decode_loop 的打包处）—— **宁可多画，不可漏画**。
constexpr std::int32_t kInvalidateMaxPx = 0x7FFF;

/// 会被转发到远端的本地输入消息。
///
/// 之所以需要一个"是不是本地输入"的判断：`set_input_forwarding(false)` 要拦的是
/// **系统 → 本程序**这一个方向的消息，而不是本程序自己发出的输入。两者走的是完全
/// 不同的路径（前者经窗口过程，后者直接 net_.send_mouse()），所以一张消息名单就够，
/// 不会误伤自动输入源。
bool is_local_input_message(UINT msg) noexcept {
    switch (msg) {
    case WM_MOUSEMOVE:
    case WM_MOUSEWHEEL:
    case WM_LBUTTONDOWN:
    case WM_LBUTTONUP:
    case WM_RBUTTONDOWN:
    case WM_RBUTTONUP:
    case WM_MBUTTONDOWN:
    case WM_MBUTTONUP:
    case WM_LBUTTONDBLCLK:
    case WM_RBUTTONDBLCLK:
    case WM_MBUTTONDBLCLK:
    case WM_KEYDOWN:
    case WM_KEYUP:
    case WM_SYSKEYDOWN:
    case WM_SYSKEYUP:
        return true;
    default:
        return false;
    }
}

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

} // namespace

RemoteWindow::RemoteWindow(AsyncClient& net, std::wstring title)
    : net_(net), base_title_(std::move(title)) {}

RemoteWindow::~RemoteWindow() {
    // 自动输入源先停：它每 interval_ms 会调一次 net_.send_mouse()，而 net_ 由
    // client/main.cpp 持有，要到 message_loop 返回之后才 stop()。停在这里，
    // 保证不存在"窗口对象已析构、输入线程还在发"的窗口期。
    auto_input_stop_.store(true);
    if (auto_input_thread_.joinable()) {
        auto_input_thread_.join();
    }
    // 必须在这里停下解码线程：它持有 this，还在被销毁的对象上碰 image_mutex_。
    // 调用方（client/main.cpp）的时序保证了此刻不会有新的 on_frame：
    //   run_message_loop() 返回 -> client.stop() 已 join io 线程 -> 才轮到本析构。
    stop_decode_thread();
}

bool RemoteWindow::create(int show_cmd) {
    WNDCLASSW wc{};
    wc.lpfnWndProc   = &RemoteWindow::proc_static;
    wc.hInstance     = GetModuleHandleW(nullptr);
    wc.lpszClassName = kClassName;
    wc.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    wc.hCursor       = LoadCursorW(nullptr, IDC_ARROW);
    wc.hIcon         = LoadIconW(nullptr, IDI_APPLICATION);
    // CS_DBLCLKS：让系统把两次快速点击折叠为 WM_xBUTTONDBLCLK
    //（旧版漏配此样式，双击处理分支从未被执行过）
    wc.style = CS_HREDRAW | CS_VREDRAW | CS_DBLCLKS;

    if (!RegisterClassW(&wc)) {
        if (::GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            RC_LOG_ERROR("RegisterClassW failed, GetLastError={}", ::GetLastError());
            return false;
        }
    }

    hwnd_ = CreateWindowExW(0, kClassName, base_title_.c_str(), WS_OVERLAPPEDWINDOW,
                            CW_USEDEFAULT, CW_USEDEFAULT, 1024, 720,
                            nullptr, nullptr, wc.hInstance, this);
    if (hwnd_ == nullptr) {
        RC_LOG_ERROR("CreateWindowExW failed, GetLastError={}", ::GetLastError());
        return false;
    }
    ShowWindow(hwnd_, show_cmd);
    UpdateWindow(hwnd_);
    // 窗口句柄已就绪，解码线程可以放心把 WM_APP_FRAME_READY 投给它了。
    // 放在 create() 里而不是构造函数里：CreateWindowExW 失败时不必起线程。
    start_decode_thread();

    // 自动输入源（诊断）：interval = 0 时**根本不建线程**，生产路径上零开销。
    // 它要等第一帧到达才知道远端分辨率，所以放在解码线程之后起。
    if (auto_input_interval_ms_ > 0) {
        auto_input_thread_ = std::thread([this] { run_auto_input(); });
    }
    return true;
}

int RemoteWindow::run_message_loop() {
    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return static_cast<int>(msg.wParam);
}

void RemoteWindow::set_debug_dump(std::wstring path_prefix, std::uint64_t after_frames) {
    // 只在解码线程启动之前调用（main 里紧跟构造之后），因此不需要加锁
    dump_path_  = std::move(path_prefix);
    dump_after_ = after_frames;
}

void RemoteWindow::set_invalidate_halo(int px) {
    // 同上：必须在 create() 之前调用，所以不加锁
    invalidate_halo_px_ = px;
}

void RemoteWindow::set_paint_dump(std::wstring path_prefix, std::uint32_t after_paints) {
    // 只在 UI 线程创建之前调用（main 里紧跟构造之后），因此不需要加锁
    paint_dump_path_  = std::move(path_prefix);
    paint_dump_after_ = after_paints;
}

void RemoteWindow::set_debug_stall(std::uint32_t every_n, std::uint32_t ms) {
    // 同上：必须在 create() 之前调用，所以不加锁
    debug_stall_every_n_ = every_n;
    debug_stall_ms_      = ms;
}

void RemoteWindow::set_debug_decode_stall(std::uint32_t every_n, std::uint32_t ms) {
    // 同上：必须在 create() 之前调用，所以不加锁
    debug_decode_stall_every_n_ = every_n;
    debug_decode_stall_ms_      = ms;
}

void RemoteWindow::set_keyframe_priority(bool on) {
    // 同上：必须在 create() 之前调用，所以不加锁
    keyframe_priority_ = on;
}

void RemoteWindow::set_stretch_mode(const std::string& mode) {
    // 同上：必须在 create() 之前调用，所以不加锁。
    // 只认两个值，其余一律回落到 halftone 并留下 WARN —— 一个没人认识的字符串
    // 如果静默变成默认行为，日志里就会出现一个**从未被要求过**的模式，
    // 而"看起来正常"的错配置是本项目最忌讳的形态（§8.6 / §8.12 / §8.21 同源）。
    if (mode == "coloroncolor") {
        stretch_mode_      = COLORONCOLOR;
        stretch_mode_name_ = "coloroncolor";
    } else {
        if (mode != "halftone") {
            RC_LOG_WARN("stretch_mode 取值无法识别：\"{}\" —— 回落到 halftone"
                        "（只认 halftone / coloroncolor）",
                        mode);
        }
        stretch_mode_      = HALFTONE;
        stretch_mode_name_ = "halftone";
    }
}

void RemoteWindow::set_partial_repaint(bool on) {
    // 同上：必须在 create() 之前调用，所以不加锁。
    partial_repaint_ = on;
}

void RemoteWindow::set_auto_input(std::uint32_t interval_ms, double x0_frac, double x1_frac,
                                  double y_frac) {
    // 同上：必须在 create() 之前调用（线程在 create() 之后才起），所以不加锁
    auto_input_interval_ms_ = interval_ms;
    auto_input_x0_          = x0_frac;
    auto_input_x1_          = x1_frac;
    auto_input_y_           = y_frac;
}

void RemoteWindow::set_input_forwarding(bool on) {
    input_forwarding_ = on;
}

void RemoteWindow::on_input_sent(std::int64_t epoch, std::chrono::steady_clock::time_point at) {
    // io 线程调用。只做两件事：把时刻写进环形表、推进"已发到第几个"。
    // 时刻表必须能容忍"序号跳跃/回退"：重连会让 epoch 从 0 重新开始，
    // 而旧值自然被新值按取模下标覆盖 —— 环形表天然满足这个语义，不需要清理。
    const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(at.time_since_epoch())
                        .count();
    input_sent_ns_[epoch % kInputRingCap].store(ns, std::memory_order_relaxed);
    // release：保证"时刻已写好"对读到这个序号的读者可见。
    // 若读者先看到序号、后看到时刻，最坏结果是配到一个 0（当作没记录而跳过）。
    input_sent_epoch_.store(epoch, std::memory_order_release);
    if (epoch >= kInputRingCap && (epoch - kInputRingCap) % kInputRingCap == 0) {
        // 走过一整圈：说明这条会话已经发了 4096 次输入，环形表开始覆盖旧时刻。
        // 只在整圈时记一次，避免每帧一条日志。
        RC_LOG_WARN("[input-latency] 输入序号已过 {}，时刻环形表开始覆盖旧值"
                    "（未结算的旧样本会因此丢失，见 [input-latency] 的\"丢弃\"）",
                    epoch);
    }
}

void RemoteWindow::run_auto_input() {
    // 自动输入源（诊断用）：在远端画面的两个横向位置之间来回，每 interval_ms 一次。
    // 坐标按**远端画面的比例**换算，所以在任何分辨率下都落在屏内 ——
    // 落到屏外的坐标会被系统 clamp，于是"读回来的位置"和"请求的位置"不符，
    // 服务端的坐标校验会大面积失败（那是判据的前置不变式之一，见 docs §6.13 的教训）。
    std::int32_t rx = -1;
    std::int32_t ry = -1;
    bool         to_x1 = false;

    while (!auto_input_stop_.load(std::memory_order_relaxed)) {
        // 远端尺寸要等第一帧到达才知道；没有它就没法把比例变成坐标，先等。
        {
            std::lock_guard<std::mutex> lock(image_mutex_);
            if (remote_w_ > 0 && remote_h_ > 0) {
                const double frac = to_x1 ? auto_input_x1_ : auto_input_x0_;
                rx = static_cast<std::int32_t>(static_cast<double>(remote_w_) * frac);
                ry = static_cast<std::int32_t>(static_cast<double>(remote_h_) * auto_input_y_);
            }
        }
        if (rx >= 0 && ry >= 0 && net_.connected()) {
            rc::input::MouseEvent ev;
            ev.action = rc::input::MouseAction::kMove;
            ev.x      = rx;
            ev.y      = ry;
            net_.send_mouse(ev);
            auto_input_sent_.fetch_add(1, std::memory_order_relaxed);
            to_x1 = !to_x1;
        }
        // 用固定步长睡、而不是睡满整个周期再醒来判断停止标志：
        // 后者会让退出最多延迟一个 interval（探针脚本按秒计的超时会因此变得不可预测）。
        const auto deadline = std::chrono::steady_clock::now() +
                              std::chrono::milliseconds(auto_input_interval_ms_);
        while (!auto_input_stop_.load(std::memory_order_relaxed) &&
               std::chrono::steady_clock::now() < deadline) {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }
}

void RemoteWindow::settle_present(std::int64_t visible_epoch) {
    // 终点时刻：StretchBlt 刚刚完成，像素已经写进窗口 DC。
    // 再往后（DWM 合成、显示器扫描输出）软件就测不到了，本项目也不猜 ——
    // 那是光传感器/高速相机的领域，文档里对此有明确的边界声明。
    const auto at       = std::chrono::steady_clock::now();
    const auto sent_max = input_sent_epoch_.load(std::memory_order_acquire);

    // 结算的上界取两者**较小值**，少任何一个都会配出不存在的输入：
    //   · visible_epoch —— 画面里确实已经反映到的位置（服务端给的下界语义）
    //   · sent_max      —— 客户端真正记下过时刻的位置
    // 只看前者：重连后新会话的序号（从 0 起）会去配客户端一路累加下来的旧序号；
    // 只看后者：会把"还没画上去的帧"算成已显示（延迟报小，危险方向）。
    const auto cap = visible_epoch < sent_max ? visible_epoch : sent_max;

    // 服务端换了会话（序号归零）：把结算进度拉回它前面，而不是去补配那些
    // "上一会话发过、当前画面上永远不会再出现"的输入。
    // 少了这一步，cap 会长期小于 presented_epoch_ ⇒ **再也不产生新样本**，
    // 而 [input-latency] 仍旧显示一个漂亮的历史数字 —— 正是那种"看起来正常"的失效。
    if (cap < presented_epoch_) {
        presented_epoch_ = cap;
    }
    if (cap <= presented_epoch_) {
        return; // 稳态：两帧之间没有新的输入被显示
    }

    std::lock_guard<std::mutex> lock(lat_mutex_);
    while (presented_epoch_ < cap) {
        const auto k  = ++presented_epoch_;
        const auto ns = input_sent_ns_[k % kInputRingCap].load(std::memory_order_relaxed);
        if (ns == 0) {
            ++lat_missing_time_; // 这一格没记过时刻（环形表被整圈覆盖过）—— 不计样本
            continue;
        }
        const double ms =
            std::chrono::duration<double, std::milli>(
                at - std::chrono::steady_clock::time_point(std::chrono::nanoseconds(ns)))
                .count();
        if (ms >= 0.0 && ms <= kInputLatMaxMs) {
            lat_present_.add(ms);
        } else {
            ++lat_rejected_;
        }
    }
}

void RemoteWindow::note_paint(std::chrono::steady_clock::time_point t_paint,
                              std::chrono::steady_clock::time_point t_blt0,
                              std::chrono::steady_clock::time_point t_blt1,
                              double clip_frac) {
    // 【UI 绘制分段】见 remote_window.hpp 里 paint_* 的说明。三段各自留样本，
    // 因为它们的修法完全不同：等待（改重绘触发方式）/ 准备（改 API 调用）/ 计算（改缩放）。
    const auto to_ms = [](std::chrono::steady_clock::time_point a,
                          std::chrono::steady_clock::time_point b) {
        return std::chrono::duration<double, std::milli>(b - a).count();
    };
    // 投递时刻先读出来（解码线程写、这里读，取到就是取到了），不要持锁做这步。
    const auto posted = frame_posted_ns_.load(std::memory_order_acquire);

    std::lock_guard<std::mutex> lock(lat_mutex_);
    ++paint_count_;
    if (posted != 0) {
        const double wait_ms =
            to_ms(std::chrono::steady_clock::time_point(std::chrono::nanoseconds(posted)), t_paint);
        // 只收非负样本。负值意味着"这次绘制开始之后才有新的重绘请求投递进来" ——
        // 那一刻的"等待"没有定义。静默按 0 算会拉低均值（"看起来正常"的错数），
        // 直接丢掉又会让"有多少次没配上"这件事消失，所以单独计数。
        if (wait_ms >= 0.0) {
            paint_wait_.add(wait_ms);
            ++paint_paired_;
        } else {
            ++paint_stale_;
        }
    }
    paint_prep_.add(to_ms(t_paint, t_blt0));
    paint_blt_.add(to_ms(t_blt0, t_blt1));
    // 【按脏区重绘的自证】本次实际被画到的客户区占比。它在"关掉开关"时必须回到
    // ≈1、在"开着"时应当明显小 —— 只看 StretchBlt 耗时下降是不够的：
    // 那也可能来自别的原因（比如换了缩放模式）。真正要证明的是"画布只被写了一块"。
    paint_clip_frac_.add(clip_frac);
    if (clip_frac > 0.99) {
        ++paint_clip_full_;
    }
}

// 标题 = 基础名 + 链路状态。三种情形合成，集中在这里渲染，
// 避免"连接中/握手中/重连中"各自 SetWindowText、互相覆盖。
void RemoteWindow::refresh_title() {
    std::wstring title = base_title_;
    if (connected_) {
        title += L"  [已连接]";
        // 【2B 第三刀 权限模型】只读必须显眼：一个看不出权限的会话，会让用户
        // 把"没有写权限"读成"输入功能坏了"，然后去查一条完全正常的链路。
        // 放在 [已连接] 之后而不是替换它 —— 两件事都要看到：连上了、但只是只读。
        if (role_label_ == "view") {
            title += L"  [只读]";
        }
    } else {
        // 有状态文案就显示它；没有（刚断开、还没开始重试）则退化显示"重连中…"。
        // ⚠️ 这个退化只对"会重连"成立 —— **终止态**（凭据被拒/协议不合/次数用尽）
        //    会由 emit_state("stopped") 把 status_label_ 填成「不会再重连」，
        //    不再落进这里。否则用户会看到"正在重连"而永远等不到（2026-09-27 修）。
        title += L"  [";
        title += to_wide(status_label_.empty() ? std::string("重连中…") : status_label_);
        title += L"]";
        if (!last_reason_.empty()) {
            title += L"  -  ";
            title += to_wide(last_reason_);
        }
    }
    ::SetWindowTextW(hwnd_, title.c_str());
}

// ---- 窗口过程绑定到对象实例 ----
LRESULT CALLBACK RemoteWindow::proc_static(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    RemoteWindow* self = nullptr;
    if (msg == WM_NCCREATE) {
        auto* cs = reinterpret_cast<CREATESTRUCTW*>(lp);
        self     = static_cast<RemoteWindow*>(cs->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
        // 关键：WM_NCCREATE 发生在 CreateWindowExW 返回之前，
        // 此刻成员 hwnd_ 还是 nullptr。必须在这里把真实句柄记下来。
        if (self != nullptr) {
            self->hwnd_ = hwnd;
        }
    } else {
        self = reinterpret_cast<RemoteWindow*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    }
    if (self != nullptr) {
        return self->proc(hwnd, msg, wp, lp);
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

// 注意形参 hwnd：窗口过程必须使用系统传入的句柄，而不能用成员 hwnd_。
// 建窗期间的消息（WM_NCCREATE/WM_CREATE/WM_GETMINMAXINFO…）都早于成员赋值，
// 用成员会让 DefWindowProcW 收到 nullptr 并返回 FALSE，
// 导致 CreateWindowExW 直接失败、窗口一个都建不出来。
LRESULT RemoteWindow::proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    // 【诊断】测量"输入→显示延迟"时切断本地输入转发（见 set_input_forwarding）。
    //   同机自测下，服务端 SetCursorPos 移动的正是**本机物理光标**；它一旦落到客户端
    //   窗口上，客户端就会收到 WM_MOUSEMOVE、把它当用户操作回灌给服务端 —— 输入流里
    //   于是混进一路既不可控也不可预期的反馈。两台机器部署时这条回路并不存在，
    //   所以关掉它反而更接近真实拓扑（而不是"绕过了被测链路"）。
    if (!input_forwarding_ && is_local_input_message(msg)) {
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    switch (msg) {
    case WM_PAINT: {
        // 【UI 绘制分段】三个时刻把这一段拆开（见 remote_window.hpp 里 paint_* 的说明）：
        // 入口 / StretchBlt 前 / StretchBlt 后。拆开是因为"等 UI 来画"与"画本身"
        // 混在一起时只能报一个数，而两者的修法完全相反。
        const auto t_paint = std::chrono::steady_clock::now();
        PAINTSTRUCT ps;
        HDC         hdc = BeginPaint(hwnd, &ps);

        // 【按脏区重绘的自证】BeginPaint 已经把这个窗口的**更新区**设成了本次绘制的
        // 裁剪区（GDI 的行为），而 GetClipBox 返回包含当前裁剪区的最小矩形 ⇒
        // 它就是"这次真正会被写到"的范围。开关有没有生效靠它证明，而不是靠
        // "我调用了 InvalidateRect"这个**动作**（§6.12：配置生效 ≠ 机制生效）。
        // 关掉开关时这个比例应当回到 ≈100% —— 判据的反向对照就落在这条上。
        RECT cr{};
        ::GetClientRect(hwnd, &cr);
        const int  cw = cr.right - cr.left;
        const int  ch = cr.bottom - cr.top;
        double     clip_frac = 1.0;
        {
            RECT clip{};
            if (::GetClipBox(hdc, &clip) != 0 && cw > 0 && ch > 0) {
                const auto iw = static_cast<double>(std::max(0L, clip.right - clip.left));
                const auto ih = static_cast<double>(std::max(0L, clip.bottom - clip.top));
                clip_frac     = (iw * ih) / (static_cast<double>(cw) * static_cast<double>(ch));
                if (clip_frac > 1.0) {
                    clip_frac = 1.0; // COMPLEXREGION 给的是**包围盒**，可能比实际大
                }
            }
        }

        // 【按脏区重绘的判据】诊断落盘：把"窗口实际画出来的"与"同一时刻整幅重绘的
        // 参考"成对存下来。两者取自**同一个 image_、同一个临界区** ⇒ 只差"这次的裁剪
        // 区"这一件事，于是"逐像素相等"正是"没留下残影"。时间间隔在构造上被消掉了 ——
        // 绝不能"客户端落盘 + 另一个进程稍后抓一帧"（§8.12 实测：半秒够浏览器滚出 15% 假差异）。
        const bool dump_this = !paint_dump_path_.empty() &&
                               (++paint_seen_) > paint_dump_after_ &&
                               paint_dump_count_ < kMaxDebugDumps;
        bool dump_ref_ok = false;

        // 没有画像时 t_blt0/t_blt1 停在入口时刻 ⇒ 准备与 blt 都记为 0，语义正确。
        auto         t_blt0               = t_paint;
        auto         t_blt1               = t_paint;
        std::int64_t visible_before_paint = 0;
        {
            // lock_guard：即使绘制中途异常/提前 return 也会解锁
            std::lock_guard<std::mutex> lock(image_mutex_);
            if (!image_.IsNull()) {
                const int old_mode = SetStretchBltMode(hdc, stretch_mode_);
                SetBrushOrgEx(hdc, 0, 0, nullptr);
                t_blt0 = std::chrono::steady_clock::now();
                image_.StretchBlt(hdc, 0, 0, cw, ch, 0, 0, remote_w_, remote_h_, SRCCOPY);
                t_blt1 = std::chrono::steady_clock::now();
                SetStretchBltMode(hdc, old_mode);
                // 尺寸随绘制一起记：blt 的代价正比于目标像素数，不记尺寸的数字没法横比。
                paint_src_w_.store(remote_w_, std::memory_order_relaxed);
                paint_src_h_.store(remote_h_, std::memory_order_relaxed);
                paint_dst_w_.store(cw, std::memory_order_relaxed);
                paint_dst_h_.store(ch, std::memory_order_relaxed);

                if (dump_this && cw > 0 && ch > 0) {
                    // 参考 = 用**同一份 image_**再做一次整幅缩放，但**不带本次的裁剪区**
                    // （画到独立 DIB 上，它没有窗口的更新区）。所以参考 == "如果整窗重绘
                    // 会是什么样"。这里 Save 在锁内，与既有 keyframe dump 同一规矩：
                    // 诊断落盘只发生在极少数几帧上（<= kMaxDebugDumps）。
                    CImage ref;
                    ref.Create(cw, ch, 32);
                    HDC rdc = ref.GetDC();
                    if (rdc != nullptr) {
                        const int om = SetStretchBltMode(rdc, stretch_mode_);
                        SetBrushOrgEx(rdc, 0, 0, nullptr);
                        image_.StretchBlt(rdc, 0, 0, cw, ch, 0, 0, remote_w_, remote_h_,
                                          SRCCOPY);
                        SetStretchBltMode(rdc, om);
                        ref.ReleaseDC();
                    }
                    const auto p = paint_dump_path_ + L"_ref_" +
                                   std::to_wstring(paint_dump_count_ + 1) + L".png";
                    dump_ref_ok = SUCCEEDED(ref.Save(p.c_str(), GUID_NULL));
                }
            }
            // 【输入→显示延迟】与"刚才画出去的那张画面"在**同一个临界区**里取值：
            // 这就是"像素已经进了窗口"那一刻、画面上已经反映了哪些输入。
            // 放到锁外读，解码线程可能已经把更新的一帧登记进来了，于是会把还没画上去的
            // 帧算成已显示 —— 延迟报小，而报小是唯一危险的方向。
            visible_before_paint = visible_input_epoch_;
        }
        EndPaint(hwnd, &ps);

        if (dump_this) {
            // 实际 = 窗口客户区当前的内容。放在 EndPaint **之后**：此刻像素已经提交。
            // 读窗口 DC 而不是复用 hdc —— BeginPaint 那个句柄带着裁剪区，读不到整幅。
            const int n = ++paint_dump_count_;
            CImage    act;
            act.Create(cw, ch, 32);
            HDC adc = act.GetDC();
            HDC wdc = ::GetDC(hwnd);
            bool ok_act = false;
            if (adc != nullptr && wdc != nullptr) {
                ok_act = ::BitBlt(adc, 0, 0, cw, ch, wdc, 0, 0, SRCCOPY) != 0;
            }
            if (wdc != nullptr) {
                ::ReleaseDC(hwnd, wdc);
            }
            if (adc != nullptr) {
                act.ReleaseDC();
            }
            const auto p     = paint_dump_path_ + L"_paint_" + std::to_wstring(n) + L".png";
            const bool ok_sv = ok_act && SUCCEEDED(act.Save(p.c_str(), GUID_NULL));
            RC_LOG_INFO("[paint-dump] 第 {} 对（裁剪 {:.0f}%）：实际={} 参考={}", n,
                        100.0 * clip_frac, ok_sv ? "saved" : "FAILED",
                        dump_ref_ok ? "saved" : "FAILED");
        }

        // 绘制分段的记账放在 EndPaint 之后：与下面结算同一时点，口径好对齐。
        note_paint(t_paint, t_blt0, t_blt1, clip_frac);
        // 结算放在 EndPaint **之后**：测量的终点是"像素进了窗口 DC"，
        // 而 EndPaint 是这次绘制的事务性收尾（验证 DC、清空更新区），
        // 它本身不是画面的一部分，不该被算进"用户看到之前"的那一段。
        settle_present(visible_before_paint);
        return 0;
    }

    // ---- 鼠标事件：全部收敛到 send_mouse_action，消除旧版六段复制粘贴 ----
    case WM_MOUSEMOVE:      send_mouse_move_throttled(lp);                        return 0;
    case WM_MOUSEWHEEL:     send_wheel(wp);                                       return 0;
    case WM_LBUTTONDOWN:
        SetCapture(hwnd);
        send_mouse_action(rc::input::MouseAction::kLDown, lp);
        return 0;
    case WM_LBUTTONUP:
        ReleaseCapture();
        send_mouse_action(rc::input::MouseAction::kLUp, lp);
        return 0;
    case WM_RBUTTONDOWN:    send_mouse_action(rc::input::MouseAction::kRDown, lp); return 0;
    case WM_RBUTTONUP:      send_mouse_action(rc::input::MouseAction::kRUp, lp);   return 0;
    case WM_MBUTTONDOWN:    send_mouse_action(rc::input::MouseAction::kMDown, lp); return 0;
    case WM_MBUTTONUP:      send_mouse_action(rc::input::MouseAction::kMUp, lp);   return 0;
    case WM_LBUTTONDBLCLK:  send_mouse_action(rc::input::MouseAction::kLDClick, lp); return 0;
    case WM_RBUTTONDBLCLK:  send_mouse_action(rc::input::MouseAction::kRDClick, lp); return 0;
    case WM_MBUTTONDBLCLK:  send_mouse_action(rc::input::MouseAction::kMDClick, lp); return 0;

    // ---- 键盘事件（行为与旧版一致：WM_SYSKEY* 覆盖 Alt 组合键） ----
    case WM_KEYDOWN:
    case WM_SYSKEYDOWN:
        send_key(static_cast<int>(wp), false);
        return 0;
    case WM_KEYUP:
    case WM_SYSKEYUP:
        send_key(static_cast<int>(wp), true);
        return 0;

    case WM_ERASEBKGND:
        return 1; // 抑制背景擦除闪烁：整幅画面由 WM_PAINT 全量重绘

    // ---- 来自 io 线程的链路状态通知 ----
    case WM_APP_CONNECTED:
        connected_ = true;
        status_label_.clear(); // 中间态文案作废，避免下次断开时残留
        last_reason_.clear();
        refresh_title();
        return 0;

    case WM_APP_DISCONNECTED: {
        // 接手 io 线程投递过来的原因字符串，用完必须释放（否则每断线一次泄漏一次）
        std::unique_ptr<std::string> reason(reinterpret_cast<std::string*>(lp));
        connected_ = false;
        status_label_.clear(); // 先显示"重连中… + 原因"，等下次尝试开始再换成"连接中…"
        last_reason_ = reason ? *reason : std::string("连接断开，正在重连");
        // 不再弹框、不再关窗口：客户端会自动重连，窗口保持打开并在标题提示状态
        refresh_title();
        return 0;
    }

    case WM_APP_STATE: {
        // 中间态（连接中/握手中/重连中）：有了它，"握手迟迟不返回"才看得见
        std::unique_ptr<std::string> label(reinterpret_cast<std::string*>(lp));
        if (label == nullptr || connected_) {
            return 0; // 已连接时以连接状态为准，忽略队列里残留的中间态
        }
        status_label_ = *label;
        refresh_title();
        return 0;
    }

    case WM_APP_ROLE: {
        // 【2B 第三刀 权限模型】接手 io 线程投递过来的角色字符串（用完必须释放）。
        // 刻意**不判 connected_**：角色就是握手成功那一刻下发的，此时 connected_ 是
        // true；上面 WM_APP_STATE 那个判据是为了丢"握手中"的陈旧中间态，不适用这里。
        std::unique_ptr<std::string> role(reinterpret_cast<std::string*>(lp));
        if (role == nullptr) {
            return 0;
        }
        role_label_ = *role;
        refresh_title();
        return 0;
    }

    case WM_APP_FRAME_READY: {
        // 解码线程已经把新图像换进 image_（锁只在这期间被短暂持有）。
        // 这里只标记重绘，真正的贴图留给 WM_PAINT —— 消息处理里不该做重活。
        //
        // 【按脏区重绘】wp/lp 携带"这一帧改了远端画面的**哪一块**"（帧空间坐标）。
        // 只把对应的客户区矩形标记成待重绘 ⇒ WM_PAINT 里 BeginPaint 返回的 HDC
        // **自带这个矩形的裁剪区**，于是那一次 StretchBlt 的落笔范围就只有它，
        // GDI 会按裁剪跳过其余整条带（实测代价 ∝ 裁剪面积，§6.25）。
        // ⇒ **WM_PAINT 里的代码一个字都不用改**，这也让"画的还是同一次缩放"
        // 成为构造上的事实，而不是靠自觉。
        // 哨兵 (0,0)：区域未知 / 越界 / 开关关掉 ⇒ 整窗失效（= 引入本机制前的行为）。
        RECT dst{};
        if (wp == 0 && lp == 0) {
            ::InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        }
        const auto fx0 = static_cast<std::int32_t>((static_cast<std::uint64_t>(wp) >> 16) & 0xFFFF);
        const auto fy0 = static_cast<std::int32_t>(static_cast<std::uint64_t>(wp) & 0xFFFF);
        const auto fw  = static_cast<std::int32_t>((static_cast<std::uint64_t>(lp) >> 16) & 0xFFFF);
        const auto fh  = static_cast<std::int32_t>(static_cast<std::uint64_t>(lp) & 0xFFFF);

        RECT cli{};
        std::int32_t rw = 0;
        std::int32_t rh = 0;
        {
            std::lock_guard<std::mutex> lock(image_mutex_);
            rw = remote_w_;
            rh = remote_h_;
        }
        if (::GetClientRect(hwnd, &cli) == 0 || rw <= 0 || rh <= 0 ||
            cli.right <= 0 || cli.bottom <= 0) {
            ::InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        }

        // 帧空间 → 客户区，**向外取整**（起点 floor / 终点 ceil）：
        // 向内取整会让最外那一行/列永远不重绘 —— 那是**永久残影**，不是少画一点。
        // 再加 kInvalidateHaloPx 兜住插值光晕（见文件顶部的实测说明）。
        const auto fl = [](std::int32_t v, std::int32_t s, std::int32_t d) {
            return static_cast<std::int32_t>(static_cast<long long>(v) * d / s);
        };
        const auto ce = [](std::int32_t v, std::int32_t s, std::int32_t d) {
            return static_cast<std::int32_t>((static_cast<long long>(v) * d + s - 1) / s);
        };
        dst.left   = std::max(0L, static_cast<LONG>(fl(fx0, rw, cli.right) - invalidate_halo_px_));
        dst.top    = std::max(0L, static_cast<LONG>(fl(fy0, rh, cli.bottom) - invalidate_halo_px_));
        dst.right  = std::min(static_cast<LONG>(cli.right),
                              static_cast<LONG>(ce(fx0 + fw, rw, cli.right) + invalidate_halo_px_));
        dst.bottom = std::min(static_cast<LONG>(cli.bottom),
                              static_cast<LONG>(ce(fy0 + fh, rh, cli.bottom) + invalidate_halo_px_));
        if (dst.right <= dst.left || dst.bottom <= dst.top) {
            // 退化（理论上到不了这里）：宁可整窗重画，不可什么都不画。
            ::InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        }
        ::InvalidateRect(hwnd, &dst, FALSE);
        return 0;
    }

    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;

    default:
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
}

bool RemoteWindow::map_to_remote(LPARAM lp, int& out_x, int& out_y) {
    RECT rc{};
    if (!GetClientRect(hwnd_, &rc)) {
        return false;
    }
    const int cw = rc.right - rc.left;
    const int ch = rc.bottom - rc.top;

    // 锁内只读共享尺寸、不做任何网络/阻塞操作
    int rw = 0;
    int rh = 0;
    {
        std::lock_guard<std::mutex> lock(image_mutex_);
        rw = remote_w_;
        rh = remote_h_;
    }
    if (rw <= 0 || rh <= 0 || cw <= 0 || ch <= 0) {
        return false; // 还没收到过画面，无法映射坐标
    }
    // 与旧版相同的线性映射；GET_X_LPARAM 正确处理多屏负坐标
    out_x = GET_X_LPARAM(lp) * rw / cw;
    out_y = GET_Y_LPARAM(lp) * rh / ch;
    return true;
}

void RemoteWindow::send_mouse_action(rc::input::MouseAction action, LPARAM lp) {
    int rx = 0;
    int ry = 0;
    if (!map_to_remote(lp, rx, ry)) {
        return;
    }
    rc::input::MouseEvent ev;
    ev.action = action;
    ev.x      = rx;
    ev.y      = ry;
    net_.send_mouse(ev);
}

void RemoteWindow::send_mouse_move_throttled(LPARAM lp) {
    // steady_clock 是单调时钟：不受用户改系统时间/NTP 校时影响（优于旧版 GetTickCount）
    const auto now = std::chrono::steady_clock::now();
    if (last_move_.has_value() && (now - *last_move_) < kMouseMoveMinInterval) {
        return;
    }
    last_move_ = now;
    send_mouse_action(rc::input::MouseAction::kMove, lp);
}

void RemoteWindow::send_wheel(WPARAM wp) {
    // 滚轮消息的坐标是屏幕坐标，需要先转成客户区坐标
    POINT pt{GET_X_LPARAM(static_cast<LPARAM>(wp)), GET_Y_LPARAM(static_cast<LPARAM>(wp))};
    ::ScreenToClient(hwnd_, &pt);

    int rx = 0;
    int ry = 0;
    if (!map_to_remote(MAKELPARAM(pt.x, pt.y), rx, ry)) {
        return;
    }

    rc::input::MouseEvent ev;
    ev.action      = rc::input::MouseAction::kWheel;
    ev.x           = rx;
    ev.y           = ry;
    ev.wheel_delta = GET_WHEEL_DELTA_WPARAM(wp);
    net_.send_mouse(ev);
}

void RemoteWindow::send_key(int vk, bool up) {
    rc::input::KeyboardEvent ev;
    ev.vk = vk;
    ev.up = up;
    net_.send_keyboard(ev);
}

void RemoteWindow::start_decode_thread() {
    decode_thread_ = std::thread([this] { decode_loop(); });
}

void RemoteWindow::stop_decode_thread() {
    if (!decode_thread_.joinable()) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(frame_mutex_);
        decode_stop_ = true;
    }
    frame_cv_.notify_all();
    decode_thread_.join();
}

// io 线程：只搬字节，不解码。
//
// 为什么不能在这里解码（这正是本次改造要修的问题）：
//   1) GDI+ 解一张 1707×960 的 PNG 是十几到几十毫秒的重活。放在 io 线程上，读循环、
//      心跳定时器、写完成回调全被按在它后面排队 —— 链路层的实时性被画面解码绑架；
//   2) 旧实现还要在解码期间持有 image_mutex_，而 UI 线程的 WM_PAINT 要抢同一把锁，
//      于是"绘制等解码、解码等绘制"互相堵。
// 现在这里只剩一次 memcpy（ScreenFrameView 是网络缓冲区视图，只在本次回调内有效，
// 解码要换线程就必须把字节搬出来），然后把帧丢进队列交给解码线程。
void RemoteWindow::on_frame(const ScreenFrameView& frame) {
    // 【诊断】每 N 帧让 io 线程停顿 X ms —— 制造抖动。
    // 位置在"到达统计之前"是刻意的：它推迟的是**下一次请求**（拉屏是收到一帧才请求
    // 下一帧，而请求由本函数返回后发出），所以停顿时长会原样出现在下一帧的到达间隔里。
    // 见 hpp 里 debug_stall_* 的注释：这条路径非放 io 线程不可。
    if (debug_stall_every_n_ > 0 && ++debug_stall_seen_ % debug_stall_every_n_ == 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(debug_stall_ms_));
    }

    const auto recv_at = std::chrono::steady_clock::now();

    // 到达间隔：相邻两帧之间的实际周期，也就是端到端帧率的倒数（21.2 fps ↔ 47.2 ms）。
    // 必须放在函数最顶部量，且在**所有**分支之前 —— "无变化"的空增量帧也会立刻
    // return，但它们同样是完整走了一趟请求-应答，漏掉它们统计就会偏高。
    double gap_ms = 0.0;
    {
        std::lock_guard<std::mutex> lock(frame_mutex_);
        if (last_frame_recv_at_.time_since_epoch().count() != 0) {
            gap_ms = std::chrono::duration<double, std::milli>(recv_at - last_frame_recv_at_).count();
            gap_ms_sum_ += gap_ms;
            ++gap_count_;
            gap_all_.add(gap_ms); // 分布样本（均值看不出抖动，见 hpp 里 GapSamples 的注释）
        }
        last_frame_recv_at_ = recv_at;
    }

    // 空载荷要分两种：整帧却是空的 → 服务端出了问题，忽略；
    // 增量帧却是空的 → 服务端在说"这一帧没有任何变化"。
    // 后者不必重绘，但**必须**照常计数：async_client 在回调返回后就会请求下一帧，
    // 在这里 return 只是跳过重绘，不会打断拉屏节奏。
    //
    // 这个分支曾经**永远不成立**：服务端把"无变化"编码成空 dirty_rects，而空数组
    // 在协议里是"整帧"的意思，于是 frame.delta 恒为 false，这一帧既不被计数、
    // 也认不出是增量链的一环——服务端统计发出 1638 帧、客户端只认到 141 帧。
    // 修在服务端：增量帧一律带矩形，0×0 即"无变化"（见 proto_codec.cpp 与
    // session.cpp:on_capture_done 的注释）。
    if (frame.data == nullptr || frame.size == 0) {
        if (frame.size == 0 && frame.delta) {
            std::lock_guard<std::mutex> lock(frame_mutex_);
            // 空增量帧也是"收到了一帧"，必须计入收帧数——否则客户端的到达帧率
            // 会低于服务端的发送帧率，看起来像丢包，其实是口径不一致把统计搞错了。
            ++frames_received_;
            ++delta_frames_;
            ++idle_frames_;
        }
        return;
    }

    {
        std::lock_guard<std::mutex> lock(frame_mutex_);
        if (decode_stop_) {
            return; // 窗口已进入收尾流程，不必再投递
        }

        PendingFrame pf;
        pf.bytes.assign(frame.data, frame.data + frame.size);
        pf.delta = frame.delta;
        pf.rx    = frame.rect_x;
        pf.ry    = frame.rect_y;
        pf.seq   = ++frame_seq_next_;
        // 归因用的时刻跟着帧一起进队列：本帧何时到达、从请求发出算走了多久、
        // 距上一帧到达隔了多久。放在帧上而不是成员变量上，是因为队列里可能同时
        // 压着好几帧，用成员变量会把"上一帧的时刻"错记到"这一帧"头上。
        // 而 gap_ms 必须跟着帧走还有一个原因：只有它是同口径的（见 PendingFrame 注释）。
        pf.recv_at = recv_at;
        pf.wire_ms = frame.wire_ms;
        // 归因口径的自证字段：跟着帧走队列，理由与上面几个字段相同
        pf.requested = frame.requested;
        pf.gap_ms  = gap_ms;
        // 输入→显示延迟的配对键，跟着帧一起进队列（理由同上：队列里可能压着多帧）
        pf.input_epoch = frame.input_epoch;

        if (!pf.delta && keyframe_priority_) {
            // ---- 整帧走优先通道：不排进增量队列，于是溢出清空时清不到它 ----
            // 旧整帧被直接顶掉，但那**不是丢弃**：新整帧完全取代旧整帧所携带的信息
            // （两者都是完整画面，后者更新）。所以只计数、不报警。
            if (pending_key_.has_value()) {
                ++key_frames_superseded_;
            }
            pending_key_ = std::move(pf);
        } else {
            // 队列满 = 解码线程已经落后 8 帧。差异帧下不能"只留最新一帧"（链会断），
            // 所以整段丢掉并交给序号校验去触发失步恢复：后续增量帧会被忽略，
            // 直到服务端的周期性整帧到达为止。
            //
            // ⚠️ 这个"整段清空"正是 docs §8.17 的病根：它清掉的**不只是**增量帧 ——
            // 关掉优先通道时整帧也在里面，于是"越需要整帧的时候它越容易被一起丢掉"，
            // resync 可以长期收敛不了（实测最长冻结 3795 ms，且理论上没有上界）。
            // 开了优先通道后，走到这个分支的就只剩增量帧了。
            if (pending_frames_.size() >= kMaxPendingFrames) {
                const auto dropped = static_cast<std::uint64_t>(pending_frames_.size());
                frames_dropped_ += dropped;
                // 单独把被清掉的**整帧**数出来：这是"整帧被吞"的直接证据，
                // 也是优先通道那条判据的落点（开着它必须恒为 0）。
                for (const auto& f : pending_frames_) {
                    if (!f.delta) {
                        ++key_frames_discarded_;
                        // 必须**另打一条**，不能只靠解码线程的 5 秒汇总：
                        // 解码线程一旦卡在 resync 里就再也不会打汇总行，那样这条最关键的
                        // 证据反而会消失 —— 判据只能报"没测到"，把故障藏起来。
                        // （本项目三次栽在"证据本身依赖于被测对象还活着"上，见 docs §8.14。）
                        RC_LOG_WARN("[keyframe] 整帧被队列溢出丢弃 (seq {}) —— "
                                    "resync 只能再等下一个整帧，这就是冻结迟迟不收敛的病根",
                                    f.seq);
                    }
                }
                pending_frames_.clear();
                RC_LOG_WARN("decode queue overflow: dropped {} frame(s), waiting for next keyframe",
                            dropped);
            }
            pending_frames_.push_back(std::move(pf));
        }

        ++frames_received_;
        bytes_sum_ += frame.size;
        if (frame.delta) {
            ++delta_frames_;
        } else {
            ++full_frames_;
        }
    }
    frame_cv_.notify_one();
}

void RemoteWindow::decode_loop() {
    // 序号连续性：差异帧是接力式的，中间缺一帧后面就全错位。
    // 一旦发现缺口就进入 resync —— 丢弃所有增量帧，直到下一个整帧把画面重建出来。
    // 这是"不需要新增协议字段"的恢复手段：整帧由服务端周期性发（每 60 帧）。
    std::uint64_t expected_seq = 0;
    bool          resync       = true;

    // 定时汇总（每 5 秒一条）：这是判断"解码是不是瓶颈"的客观依据
    auto          last_report   = std::chrono::steady_clock::now();
    std::uint64_t decoded_cnt   = 0; ///< 真正作用到画面上的帧数（整帧 + 成功贴上的增量）
    std::uint64_t skipped_cnt   = 0; ///< 因失步被主动忽略的增量帧
    std::uint64_t resync_cnt    = 0;
    std::uint64_t recv_last     = 0;
    std::uint64_t drop_last     = 0;
    std::uint64_t idle_last     = 0;
    std::uint64_t bytes_last    = 0;
    // 整帧优先通道的窗口基线（四个计数都是"累计值"，本段用差值报）
    std::uint64_t key_disc_last = 0;
    std::uint64_t key_sup_last  = 0;
    std::uint64_t key_prom_last = 0;
    std::uint64_t key_appl_last = 0;
    // 输入→显示延迟的窗口基线：自动源发出总数是累计值，本段用差值报
    //（它与"客户端已发总数"的差就是"另有别人在发输入"——同机自测时典型来源是回灌）。
    std::int64_t  auto_last     = 0;
    double        decode_ms_sum = 0.0;
    // 归因：把这三种等待单独累计。判据是
    //   到达间隔 ≈ wire + 队列 + 解码 + 贴图 + 客户端限流等待(收到帧后硬等到 next_frame_at_)
    // 若左边明显大于右边之和，说明还有一段没被观测到——那就该继续往下拆，而不是
    // 拿"应该差不多"糊过去（这个项目里凡是"应该差不多"的地方最后都藏着 bug）。
    double        wire_ms_sum  = 0.0; ///< 请求发出 → 帧到达（唯一跨进程的一段；仅有在途请求的帧）
    std::uint64_t wire_n       = 0;   ///< 上面那个和的样本数（分母必须与它同集合，别用 attr_count）
    std::uint64_t attr_no_request = 0; ///< 本段"到达时无在途请求"的变化帧数（往返对它无定义，已排除）
    double        queue_ms_sum = 0.0; ///< 帧到达 → 解码线程取走（队列积压）
    double        apply_ms_sum = 0.0; ///< 解码完成 → 贴图/替换完成
    double        gap_chg_sum  = 0.0; ///< 与上面三个**同口径**的到达间隔（只统计变化帧）
    std::uint64_t attr_count   = 0;   ///< 上面四个和的样本数

    // ---- 冻结时长（resync 的真正代价，第 2 步）----
    //
    // resync 期间画面**完全不动**：客户端把增量帧全丢掉，一直等到服务端的下一个整帧。
    // 整帧每 60 帧一次（keyframe_interval），41 ms 帧周期下最坏能冻 2.5 秒。
    // 这在平均值上完全看不见（20 秒里只发生一次），体感却是"画面卡死两秒"——
    // 差异帧留了一个从没测过的最坏情况。
    //
    // 口径：**从上一次画面真正更新，量到下一次画面真正更新**。
    // 不用"从检测到 seq 缺口算起"—— 那会漏掉"缺口帧到达之前"的那一段，
    // 而用户正是在那一段里盯着一个不动的画面。
    std::chrono::steady_clock::time_point last_applied_at{};
    std::chrono::steady_clock::time_point freeze_since{};
    double        freeze_sum = 0.0; ///< 本段累计冻结时长
    double        freeze_max = 0.0; ///< 本段最长一次
    std::uint64_t freeze_n   = 0;   ///< 本段冻结次数

    /// 进入 resync：把冻结起点钉在"上一次画面真正更新"这一刻。
    ///
    /// last_applied_at 为零 = 启动至今还没出过图，那是"首帧延迟"而不是 resync 冻结 ——
    /// 混进来会让每次连接都凭空多出一条两秒的"冻结"（正是判据要量的东西，更不能污染）。
    /// 只在 freeze_since 还是零时写入：resync 期间会反复走到这里（每一帧都缺口），
    /// 但冻结起点只能有一个。
    const auto enter_resync = [&] {
        resync = true;
        if (freeze_since.time_since_epoch().count() == 0) {
            freeze_since = last_applied_at;
        }
    };

    /// 画面真正更新了一次（整帧替换 或 增量贴图成功）：结算冻结、推进 last_applied_at，
    /// 并把这一帧携带的输入序号登记为"可见"。
    ///
    /// 参数必须来自**这一帧自己**（跟着帧走队列，见 PendingFrame::input_epoch），
    /// 不能用成员变量：队列里可能压着多帧，用成员会把上一帧的序号记到这一帧头上。
    const auto mark_applied = [&](std::int64_t input_epoch) {
        const auto now_applied = std::chrono::steady_clock::now();
        if (freeze_since.time_since_epoch().count() != 0) {
            const double frozen = std::chrono::duration<double, std::milli>(
                                      now_applied - freeze_since)
                                      .count();
            freeze_sum += frozen;
            if (frozen > freeze_max) {
                freeze_max = frozen;
            }
            ++freeze_n;
            // 独立一行（不进 [decode]）：每次冻结都是一个"用户刚才盯了两秒不动画面"的
            // 事件，值得单独留证；而 [decode] 是 5 秒一次的汇总，会把它平均掉。
            RC_LOG_WARN("[resync] 冻结 {:.0f} ms 后恢复（等到整帧；关键帧间隔 60 帧）", frozen);
            freeze_since = {};
        }
        last_applied_at = now_applied;

        // ---- 输入→显示延迟（口径一：输入 → 贴图）----
        // 先把"此刻画面上已经反映了哪些输入"登记下来。加锁是因为它必须与 image_ 的
        // 内容一致：上面那次贴图/换指针刚完成，所以现在写进去的序号与画面恰好对应。
        // （不加锁的话，UI 线程可能在两次操作之间读到，把还没画上去的帧算成已显示。）
        //
        // 取 max 而不是直接赋值：序号是单调的，但 resync 期间可能先应用了一个序号更大
        // 的整帧、再收到序号更小的陈旧增量帧 —— 取 max 保证"可见序号"永不倒退。
        {
            std::lock_guard<std::mutex> lock(image_mutex_);
            if (input_epoch > visible_input_epoch_) {
                visible_input_epoch_ = input_epoch;
            }
        }

        // 结算：所有"还没有配对过的、且序号不超过本帧"的输入，都以本帧为首次可见帧。
        // 上半界用 input_sent_epoch_（客户端真正记过时刻的位置）——
        // 服务端在重连后序号归零，不封顶就会去配一串客户端从没发过的序号。
        const auto sent_max = input_sent_epoch_.load(std::memory_order_relaxed);
        // 服务端换了会话：把配对进度拉回来，否则这里会长期不产生新样本，
        // 而汇总行仍旧打着一个漂亮的老数字（"看起来正常"的典型失效）。
        if (input_epoch < paired_composite_) {
            paired_composite_ = input_epoch;
        }
        const auto cap = input_epoch < sent_max ? input_epoch : sent_max;
        while (paired_composite_ < cap) {
            const auto k  = ++paired_composite_;
            const auto ns = input_sent_ns_[k % kInputRingCap].load(std::memory_order_relaxed);
            if (ns == 0) {
                lat_missing_time_.fetch_add(1, std::memory_order_relaxed);
                continue;
            }
            const double ms =
                std::chrono::duration<double, std::milli>(
                    now_applied - std::chrono::steady_clock::time_point(std::chrono::nanoseconds(ns)))
                    .count();
            if (ms >= 0.0 && ms <= kInputLatMaxMs) {
                lat_composite_.add(ms); // 写和读都在解码线程 ⇒ 无需锁
            } else {
                lat_rejected_.fetch_add(1, std::memory_order_relaxed);
            }
        }
    };

    for (;;) {
        PendingFrame pf;
        bool         from_key_slot = false; ///< 这一帧是从优先通道取的整帧
        std::size_t  superseded    = 0;     ///< 它跳过（=取代）了多少个排队的增量帧
        {
            std::unique_lock<std::mutex> lock(frame_mutex_);
            frame_cv_.wait(lock, [this] {
                return decode_stop_ || !pending_frames_.empty() || pending_key_.has_value();
            });
            if (decode_stop_) {
                return;
            }
            // **只要有整帧在等就先应用它**（理由见 hpp 里 pending_key_ 的注释）：
            //   · 排在它**前面**的增量帧全被它取代 —— 贴完那些小块再贴这张整图，
            //     结果就是这张整图本身，所以把它们摘掉不丢任何信息；
            //   · 排在它**后面**的增量帧是相对它的，必须先有它才能贴。
            // 两种情况指向同一个动作，于是这个分支不需要任何条件判断。
            if (pending_key_.has_value()) {
                pf = std::move(*pending_key_);
                pending_key_.reset();
                from_key_slot = true;
                // 把被它取代的那些增量帧从队列里摘掉（序号严格小于它的必然排在它前面，
                // 因为队列是按到达序 push 的）。**不计入 frames_dropped_** ——
                // 它们不是丢了，是被整帧整体取代了，记成"丢弃"会把两个完全不同的
                // 事件混成一个数，判据就再也分不清"整帧被吞"和"增量帧被取代"。
                while (!pending_frames_.empty() && pending_frames_.front().seq < pf.seq) {
                    pending_frames_.pop_front();
                    ++superseded;
                }
                if (superseded > 0) {
                    ++key_frames_promoted_;
                }
            } else {
                pf = std::move(pending_frames_.front());
                pending_frames_.pop_front();
            }
        }
        // 队列等待：帧早就到了、但解码线程还在忙前面那一帧的时长。
        // 稳态下它应该≈0；持续偏大说明解码跟不上，画面延迟会越积越多。
        const double queue_ms =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - pf.recv_at)
                .count();

        if (from_key_slot) {
            // 整帧是**自洽的基准**：它不依赖前面任何一帧，所以这里不是"缺口"，
            // 而是"我们主动跳过了若干被它取代的帧"。
            //
            // 但**跳过本身就是一次冻结**：那些排队的增量帧是"桌面真的在变"的铁证
            //（空增量帧在 on_frame 里就 return 了、根本进不了队列），它们没被按时应用，
            // 说明画面在这段时间里是冻住的 —— 用户看的就是那一段。所以这里照样进 resync，
            // 把冻结起点钉在"上一次画面真正更新"的那一刻。
            //
            // 门槛用"这一帧等了多久"（queue_ms）而不是"跳过了几帧"，有两个原因：
            //   · 慢机器上稳态也可能偶尔压着一两帧，一跳就报会把噪声记成冻结；
            //   · queue_ms 正好是"这帧在队列里等了多久"——稳态≈0，真卡住时是秒级，
            //     而"跳过了几帧"在帧周期不同的机器上根本不可比。
            // 也不能用 "now - last_applied_at"：桌面静止时整帧照样每 60 帧来一张
            //（增量帧是空的、不改变画面），那种间隔不是冻结，用它会把静止桌面判成一直在冻。
            if (!resync && queue_ms > kFreezeFloorMs) {
                ++resync_cnt;
                RC_LOG_WARN("frame gap — 整帧优先通道跳过 {} 帧、等待 {:.0f} ms 后直接应用整帧",
                            superseded, queue_ms);
                enter_resync();
            }
            expected_seq = pf.seq + 1;
        } else {
            // 缺口检测：丢帧时队列是被整段清掉的，所以这里必然看到 seq 跳变
            if (pf.seq != expected_seq) {
                if (!resync) {
                    ++resync_cnt;
                    RC_LOG_WARN("frame gap (expected seq {}, got {}) — 暂停应用增量帧，等整帧",
                                expected_seq, pf.seq);
                }
                enter_resync();
            }
            expected_seq = pf.seq + 1;
        }

        // 【诊断】每 N 帧让**解码线程**停顿 X ms —— 制造队列溢出（从而触发 resync）。
        // 位置讲究：① 在 queue_ms 之后，否则这段等待会被算成"帧在队列里排队"；
        // ② 在缺口检测之后，否则会推迟失步的发现；③ 在解码之前 —— 停顿期间 io 线程
        //    照常投递，pending_frames_ 攒到 8 帧上限，on_frame 就会整段丢掉它，
        //    于是"投递过又被清掉"的序号缺口真的出现了。这正是 resync 的唯一真实入口。
        // 500 ms @ 37 ms 帧周期 ⇒ 停顿期间约到达 13 帧 > 容量 8，必定溢出。
        if (debug_decode_stall_every_n_ > 0 &&
            ++debug_decode_stall_seen_ % debug_decode_stall_every_n_ == 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(debug_decode_stall_ms_));
        }

        // ---- 以下全程在解码线程上跑，io 线程与 UI 线程都不必等 ----
        HGLOBAL hglobal = ::GlobalAlloc(GMEM_MOVEABLE, pf.bytes.size());
        if (hglobal == nullptr) {
            RC_LOG_ERROR("GlobalAlloc failed ({} bytes)", pf.bytes.size());
            continue;
        }

        void* mem = ::GlobalLock(hglobal);
        if (mem == nullptr) {
            ::GlobalFree(hglobal);
            continue;
        }
        std::memcpy(mem, pf.bytes.data(), pf.bytes.size());
        ::GlobalUnlock(hglobal);

        IStream* raw_stream = nullptr;
        if (FAILED(::CreateStreamOnHGlobal(hglobal, TRUE /*fDeleteOnRelease*/, &raw_stream))) {
            ::GlobalFree(hglobal);
            continue;
        }
        // fDeleteOnRelease=TRUE：流 Release() 时自动释放 hglobal，不会双重释放
        std::unique_ptr<IStream, rc::win::IStreamReleaser> stream(raw_stream);

        // 先解到**临时** CImage 上：全程不碰 image_mutex_。
        // 这样 WM_PAINT 等锁的时间与解码耗时无关，只剩下面那次换指针/贴图的量级。
        const auto t0 = std::chrono::steady_clock::now();
        CImage     decoded;
        const bool decoded_ok = SUCCEEDED(decoded.Load(stream.get()));
        const double decode_ms =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
        const auto   t_decoded = std::chrono::steady_clock::now();
        if (!decoded_ok) {
            RC_LOG_WARN("decode frame failed ({} bytes)", pf.bytes.size());
            continue; // 单帧解不出来不该拖垮整条链路：丢掉，继续等下一帧
        }

        // 【按脏区重绘】这一帧改了**画面里的哪一块**（远端帧空间坐标）。
        // 它决定 UI 线程要失效多少客户区。初值 = 整帧：任何一条分支只要没明确设过，
        // 就退化成整窗失效 —— **宁可多画，不可漏画**（漏画 = 永久残影，且很难被发现）。
        std::int32_t chg_x0 = 0;
        std::int32_t chg_y0 = 0;
        std::int32_t chg_x1 = 0;
        std::int32_t chg_y1 = 0;

        if (pf.delta) {
            if (resync) {
                // 链已断或从没拿到过基准画面：这一帧增量无法安放，直接丢掉。
                // 不报错——这是设计内的恢复路径，下面等整帧即可。
                ++skipped_cnt;
                continue;
            }
            const int dw = static_cast<int>(decoded.GetWidth());
            const int dh = static_cast<int>(decoded.GetHeight());
            if (dw <= 0 || dh <= 0) {
                continue;
            }
            // 贴图到累积画面。持锁范围只是一次 1:1 BitBlt（脏区域通常几 KB～几 MB），
            // 与"解码整帧"完全不是一个量级，但绝不能把解码也放进来。
            bool composited = false;
            {
                std::lock_guard<std::mutex> lock(image_mutex_);
                if (!image_.IsNull()) {
                    HDC dc = image_.GetDC();
                    if (dc != nullptr) {
                        HDC src_dc = decoded.GetDC();
                        if (src_dc != nullptr) {
                            // 1:1 贴图，不做任何缩放：脏矩形的坐标是**远端原始像素**坐标，
                            // 缩放留给 WM_PAINT 那一次 StretchBlt 统一做。
                            composited =
                                ::BitBlt(dc, pf.rx, pf.ry, dw, dh, src_dc, 0, 0, SRCCOPY) != 0;
                            decoded.ReleaseDC();
                        }
                        image_.ReleaseDC();
                    }
                }
            }
            if (!composited) {
                // 贴不上（没有累积画面，或 BitBlt 失败）：这一帧丢了，画面已经不可信，
                // 同样进入"等整帧"。
                enter_resync();
                ++skipped_cnt;
                continue;
            }
            mark_applied(pf.input_epoch);
            ++dump_applied_;
            // 增量帧改变的范围 = 刚贴上去的那一块（帧空间）。
            // 注意"改变"指的是**画面**变了哪块，而不是"服务端发了哪块" ——
            // 两者在这里恰好一致：解码出来的补丁被 1:1 贴在 (rx,ry)，其余像素没动。
            chg_x0 = pf.rx;
            chg_y0 = pf.ry;
            chg_x1 = pf.rx + dw;
            chg_y1 = pf.ry + dh;
        } else {
            // ---- 诊断：在"替换画面"之前把两份图一起落盘 ----
            // 时机是这个函数里唯一正确的时机：此刻 image_ 还是"由前面若干增量帧拼出来的
            // 累积画面"，而 decoded 是服务端刚发来的整帧 —— 两者只差一个帧周期（约 40 ms），
            // 桌面在这段时间里几乎不会变，于是逐像素对照才有意义。
            //（换成"客户端落盘 + 另一个进程稍后抓一整帧"就不行：半秒的间隔里浏览器
            //  滚动一下就能造出 15% 的假差异，实测踩过。）
            //
            // 门槛用"收到的帧数"而不是"作用到画面的帧数"：桌面静止时服务端发来的
            // 全是"无变化"帧，它们不进这个分支、也不改变画面——若按后者计数，
            // 桌面越安静越攒不够，落盘会被无限推迟（实测踩过：整轮一对图都没落）。
            std::uint64_t seen = 0;
            {
                std::lock_guard<std::mutex> lock(frame_mutex_);
                seen = frames_received_;
            }
            if (!dump_path_.empty() && seen >= dump_after_ && dump_count_ < kMaxDebugDumps) {
                const int  n       = ++dump_count_;
                const auto suffix  = L"_" + std::to_wstring(n) + L".png";
                bool       ok_accum = false;
                {
                    std::lock_guard<std::mutex> lock(image_mutex_);
                    if (!image_.IsNull()) {
                        // 第二参数 GUID_NULL：由扩展名推断编码器，不必引 <gdiplus.h>
                        ok_accum = SUCCEEDED(
                            image_.Save((dump_path_ + L"_accum" + suffix).c_str(), GUID_NULL));
                    }
                }
                const bool ok_key =
                    SUCCEEDED(decoded.Save((dump_path_ + L"_key" + suffix).c_str(), GUID_NULL));
                RC_LOG_INFO("[dump] pair {} after {} applied / {} received frames: "
                            "accumulated={}, keyframe={}",
                            n, dump_applied_, seen, ok_accum ? "saved" : "FAILED",
                            ok_key ? "saved" : "FAILED");
            }

            {
                std::lock_guard<std::mutex> lock(image_mutex_);
                if (!image_.IsNull()) {
                    image_.Destroy(); // Attach 要求当前未挂接任何位图
                }
                image_.Attach(decoded.Detach()); // 句柄移交，O(1)，没有像素拷贝
                // 每帧刷新远端尺寸：正确处理远端分辨率变化
                remote_w_ = static_cast<int>(image_.GetWidth());
                remote_h_ = static_cast<int>(image_.GetHeight());
            }
            mark_applied(pf.input_epoch);
            ++dump_applied_;
            ++key_frames_applied_; // 整帧真的进了画面（"优先通道在起作用"的直接证据）
            resync = false; // 拿到整帧 = 画面已重建，恢复接收增量
            // 整帧替换 ⇒ 整幅画面都是"新的"（远端分辨率也可能刚变过）⇒ 整窗失效。
            chg_x1 = remote_w_;
            chg_y1 = remote_h_;
        }

        if (hwnd_ != nullptr) {
            // 【UI 绘制分段】先记时刻再投递：UI 线程在 WM_PAINT 入口拿它做差，
            // 得到"消息投递 + 重绘调度"那一段（它是**等待**，不是本进程在算）。
            // 顺序不能反 —— 先投递再记时刻会把这一段的起点往后挪，量出来的等待偏小。
            frame_posted_ns_.store(
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    std::chrono::steady_clock::now().time_since_epoch())
                    .count(),
                std::memory_order_release);
            // 【按脏区重绘】把"这一帧改了哪一块"随消息一起带过去。
            // 用消息参数而不是共享成员：天然没有"解码线程写 / UI 线程读"的竞态，
            // 也不需要"消费一次就清零"的协议 —— 多个消息排队时各自的参数互不干扰，
            // 而 `InvalidateRect` 本身就会把多次调用的矩形并进同一个更新区。
            //
            // 帧空间坐标压进两个 16 位字段。任何一项越界（理论上到不了，但这条路径
            // 一旦错了就是**永久残影**）都退化成哨兵 (0,0) = 整窗失效。
            WPARAM wp_msg = 0;
            LPARAM lp_msg = 0;
            if (partial_repaint_ && chg_x1 > chg_x0 && chg_y1 > chg_y0 && chg_x0 >= 0 &&
                chg_y0 >= 0 && chg_x1 <= kInvalidateMaxPx && chg_y1 <= kInvalidateMaxPx) {
                wp_msg = (static_cast<WPARAM>(chg_x0) << 16) | static_cast<WPARAM>(chg_y0);
                lp_msg = (static_cast<LPARAM>(chg_x1 - chg_x0) << 16) |
                         static_cast<LPARAM>(chg_y1 - chg_y0);
            }
            ::PostMessageW(hwnd_, WM_APP_FRAME_READY, wp_msg, lp_msg);
        }

        // 贴图耗时：解码完成 → 画面已更新（含抢 image_mutex_ 的等待）。
        // 单独看它是因为 WM_PAINT 也要这把锁：如果这里偏高，那问题不在解码而在锁竞争，
        // 两者的修法完全不同（前者减字节量，后者缩临界区）。
        //
        // ⚠️ 往返只累计**有在途请求**的帧：服务端主动推的帧（输入优先抓屏）到达时，
        // "上一个请求发出"那一刻与它的采集无关，算出来的数看着正常却是错的。
        // 被排除的数量单独计数并打出来 —— 排除本身必须可见（§8.22）。
        if (pf.requested) {
            wire_ms_sum += pf.wire_ms;
            ++wire_n;
        } else {
            ++attr_no_request;
        }
        queue_ms_sum += queue_ms;
        gap_chg_sum += pf.gap_ms;
        gap_chg_.add(pf.gap_ms); // 分布样本；只由解码线程读写，不需要锁
        apply_ms_sum += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() -
                                                                  t_decoded)
                            .count();
        ++attr_count;

        // ---- 每 5 秒汇总一次：出图帧率 / 收到 / 丢弃 / 单帧解码耗时 ----
        ++decoded_cnt;
        decode_ms_sum += decode_ms;
        const auto   now  = std::chrono::steady_clock::now();
        const double secs = std::chrono::duration<double>(now - last_report).count();
        if (secs >= 5.0) {
            std::uint64_t recv  = 0;
            std::uint64_t drop  = 0;
            std::uint64_t bytes = 0;
            std::uint64_t full  = 0;
            std::uint64_t delta = 0;
            std::uint64_t idle  = 0;
            double        gap_sum = 0.0;
            std::uint64_t gap_n   = 0;
            {
                std::lock_guard<std::mutex> lock(frame_mutex_);
                recv  = frames_received_;
                drop  = frames_dropped_;
                bytes = bytes_sum_;
                full  = full_frames_;
                delta = delta_frames_;
                idle  = idle_frames_;
                // 到达间隔是 io 线程累计的，在这里取走并清零（同一把锁）
                gap_sum       = gap_ms_sum_;
                gap_n         = gap_count_;
                gap_ms_sum_   = 0.0;
                gap_count_    = 0;
            }
            const auto recv_delta = recv - recv_last;
            const auto idle_delta = idle - idle_last;
            const auto avg_attr   = [&](double sum) {
                return attr_count > 0 ? sum / static_cast<double>(attr_count) : 0.0;
            };
            // 往返均值必须用**它自己的**分母：无在途请求的帧不在样本里（见 wire_n）。
            // 拿 attr_count 当分母会把"被排除的帧"当成"值为 0 的帧"平均进去 ——
            // 那正是本项目最想避免的那种"看似正常的错数"。
            const auto avg_wire = [&]() {
                return wire_n > 0 ? wire_ms_sum / static_cast<double>(wire_n) : 0.0;
            };
            const auto avg_gap = gap_n > 0 ? gap_sum / static_cast<double>(gap_n) : 0.0;
            // 【为什么在行尾追加"本段 收到 = 有像素 + 空 + 丢弃 + 失步"】
            // 因为开头那个 "N fps 出图" 的**名字骗人**：它的分子 decoded_cnt 是
            // "真正作用到画面上的帧数"，**不含**"无变化的空增量帧"（那些在 on_frame 里
            // 就 return 了，根本进不到这里）。于是它恒等于 (到达 − 空) / 秒 ——
            // 一个**由画面内容支配**的量：桌面越安静、或抓屏越密（相邻两帧之间桌面没变），
            // 它就越低，而与"UI 画得慢不慢"毫无关系。
            // 本轮（2026-09-24）曾把它在 on/off 两轮之间的差记成"输入优先抓屏的代价"，
            // 实为**空帧漂移**：六组窗口里两轮 off 的空帧比例是 0% / 0.9% 与 13.8% / 20.7%
            // ——比 on 轮（12.6% / 15.6%）还高，而出图 fps 也是 on 轮最高。
            // 把这条等式直接打进日志，是为了让下一个人**不必自己推**就能看出
            // "该不该拿它做 A/B 判据"（本项目规矩：分类必须由观测自己给出）。
            RC_LOG_INFO("[decode] {:.1f} fps 出图 | 收到 {} 帧(+{}) 丢弃 {} 帧(+{}) | "
                        "单帧解码 avg={:.1f} ms | 整帧 {} 增量 {}(空 {} 本段+{}) 失步 {} | "
                        "平均 {:.1f} KB/帧 | 归因 周期 {:.1f}(全部帧) | 变化帧 {:.1f} = 往返 "
                        "{:.1f} + 队列 {:.1f} + 解码 {:.1f} + 贴图 {:.1f} ms"
                        " | 往返样本 {} 帧(另有 {} 帧到达时无在途请求，不计入)"
                        " | 本段 收到 +{} = 有像素 {} + 空 +{} + 丢弃 +{} + 失步 +{}",
                        static_cast<double>(decoded_cnt) / secs, recv, recv_delta, drop,
                        drop - drop_last, decode_ms_sum / static_cast<double>(decoded_cnt), full,
                        delta, idle, idle_delta, skipped_cnt + resync_cnt,
                        recv_delta > 0
                            ? static_cast<double>(bytes - bytes_last) / static_cast<double>(recv_delta) /
                                  1024.0
                            : 0.0,
                        avg_gap, avg_attr(gap_chg_sum), avg_wire(),
                        avg_attr(queue_ms_sum), decode_ms_sum / static_cast<double>(decoded_cnt),
                        avg_attr(apply_ms_sum), wire_n, attr_no_request,
                        recv_delta, decoded_cnt, idle_delta, drop - drop_last,
                        skipped_cnt + resync_cnt);

            // ---- 延迟维度（第 2 步）：到达间隔的分布 + resync 冻结 ----
            // **单独一行**，不往 [decode] 上挂：那行的字段位置被 RE_* 按位置依赖，
            // 而且末尾已经挂着归因段，再堆下去两种读者（人和正则）都难受。
            // 项目里 [capture] / [capture-x] 就是这么分的，先例在同一份代码里。
            {
                GapSamples all_s;
                GapSamples chg_s;
                {
                    std::lock_guard<std::mutex> lock(frame_mutex_);
                    all_s = gap_all_; // 结构体拷贝（~8 KB 栈），5 秒一次，可忽略
                    gap_all_.clear();
                }
                chg_s = gap_chg_; // 解码线程自己写的，无需加锁
                gap_chg_.clear();

                std::sort(all_s.v, all_s.v + all_s.n); // 排一次，取三个分位
                std::sort(chg_s.v, chg_s.v + chg_s.n);

                RC_LOG_INFO(
                    "[latency] 到达间隔 全部帧 n={} P50 {:.1f} / P95 {:.1f} / max {:.1f} ms"
                    " | 变化帧 n={} P50 {:.1f} / P95 {:.1f} / max {:.1f} ms"
                    " | 冻结 {} 次 共 {:.0f} ms 最长 {:.0f} ms | 队列丢弃 {} 帧(累计)",
                    all_s.n, percentile_sorted(all_s.v, all_s.n, 0.50),
                    percentile_sorted(all_s.v, all_s.n, 0.95), percentile_sorted(all_s.v, all_s.n, 1.0),
                    chg_s.n, percentile_sorted(chg_s.v, chg_s.n, 0.50),
                    percentile_sorted(chg_s.v, chg_s.n, 0.95), percentile_sorted(chg_s.v, chg_s.n, 1.0),
                    freeze_n, freeze_sum, freeze_max, drop);
                if (all_s.overflow > 0 || chg_s.overflow > 0) {
                    // 溢出说明"这 5 秒里的帧数超过了容量"——分位数只覆盖了前一部分样本，
                    // 而最坏的那一帧很可能就在被丢掉的那部分里。必须报出来，不能静默。
                    RC_LOG_WARN("[latency] 间隔样本溢出（全部帧 {} / 变化帧 {} 个被丢弃，容量 {}）"
                                " —— 本段分位数不完整",
                                all_s.overflow, chg_s.overflow, GapSamples::kCap);
                }

                freeze_sum = 0.0;
                freeze_max = 0.0;
                freeze_n   = 0;
            }

            // ---- 整帧优先通道（第 2 步修订）：单独一行 ----
            // 为什么单独一行、且必须能自证：这一改动要证明的是"整帧没有被吞掉"，
            // 而不是"配置项写着 true"。判据读的是后三个计数 —— 只有它们能区分
            // "通道开着且真在起作用"和"通道开着但压根没走到"。
            {
                std::uint64_t kd = 0, ks = 0, kp = 0, ka = 0;
                {
                    std::lock_guard<std::mutex> lock(frame_mutex_);
                    kd = key_frames_discarded_;  // io 线程写
                    ks = key_frames_superseded_; // io 线程写
                    kp = key_frames_promoted_;   // 解码线程写（自己读自己，顺手在同一把锁下取）
                    ka = key_frames_applied_;    // 解码线程写
                }
                RC_LOG_INFO("[keyframe] 整帧优先通道 {} | 本段 应用 {} / 被顶替 {} / 被丢弃 {} "
                            "/ 跳帧直用 {}",
                            keyframe_priority_ ? "on" : "**OFF**(旧路径：整帧与增量帧同队列)",
                            ka - key_appl_last, ks - key_sup_last, kd - key_disc_last,
                            kp - key_prom_last);
                if (kd != key_disc_last) {
                    // 被丢弃就必须吵：这说明整帧还在跟增量帧挤同一条队列，
                    // resync 随时可能收敛不了。开着通道而它非零 = 这个改动失效了。
                    RC_LOG_WARN("[keyframe] 本段有 {} 个整帧被队列溢出丢弃 —— 优先通道{}",
                                kd - key_disc_last,
                                keyframe_priority_ ? "**失效**（应为 0，请查是否被旁路）"
                                                   : "未启用（这是旧路径的预期行为）");
                }
                key_sup_last  = ks;
                key_disc_last = kd;
                key_prom_last = kp;
                key_appl_last = ka;
            }

            // ---- 输入→显示延迟（第 2 步 2b）：单独一行 ----
            // 两个口径一起报，因为它们的**差**正是"UI 消息调度"那一段 ——
            // 只报总数会把这一段藏起来，而那恰恰是 UI 线程忙起来时用户能感觉到的东西。
            //
            // 同时必须报"本段自动源发了几次 / 已发总数 / 丢弃 / 无时刻记录"：
            // 这张表的可信度完全取决于配对率，而"没有样本"和"有样本"在只打分位数时
            // 读起来一模一样 —— 2a 的 §8.18 就是这么栽的（判据变绿而其实什么都没测到）。
            {
                GapSamples pres;
                {
                    std::lock_guard<std::mutex> lock(lat_mutex_);
                    pres = lat_present_; // 结构体拷贝（~8 KB 栈），5 秒一次，可忽略
                    lat_present_.clear();
                }
                std::sort(pres.v, pres.v + pres.n);
                // lat_composite_ 的写和读都在解码线程 ⇒ 这里不需要锁
                std::sort(lat_composite_.v, lat_composite_.v + lat_composite_.n);

                const auto sent_epoch = input_sent_epoch_.load(std::memory_order_relaxed);
                const auto missing    = lat_missing_time_.load(std::memory_order_relaxed);
                const auto rejected   = lat_rejected_.load(std::memory_order_relaxed);
                const auto auto_n     = auto_input_sent_.load(std::memory_order_relaxed);

                RC_LOG_INFO(
                    "[input-latency] 输入→显示 n={} P50 {:.1f} / P95 {:.1f} / max {:.1f} ms"
                    " | 输入→贴图 n={} P50 {:.1f} / P95 {:.1f} / max {:.1f} ms"
                    " | 本段自动源发 {} / 已发总数 {} | 丢弃(超上限) {} 无时刻 {}",
                    pres.n, percentile_sorted(pres.v, pres.n, 0.50),
                    percentile_sorted(pres.v, pres.n, 0.95),
                    percentile_sorted(pres.v, pres.n, 1.0),
                    lat_composite_.n,
                    percentile_sorted(lat_composite_.v, lat_composite_.n, 0.50),
                    percentile_sorted(lat_composite_.v, lat_composite_.n, 0.95),
                    percentile_sorted(lat_composite_.v, lat_composite_.n, 1.0),
                    auto_n - auto_last, sent_epoch, rejected, missing);
                auto_last = auto_n;

                if (pres.overflow > 0) {
                    RC_LOG_WARN("[input-latency] 输入→显示 样本溢出（{} 个被丢弃，容量 {}）"
                                " —— 本段分位数不完整", pres.overflow, GapSamples::kCap);
                }
                lat_composite_.clear();
            }

            // ---- UI 绘制分段（2026-09-24）：单独一行 ----
            // 它解释的正是上面那两行的**差**（输入→显示 − 输入→贴图）。拆成三段是因为
            // "等 UI 来画"与"画本身"的修法完全相反，只报一个合计数会让人优化错的那一半。
            // 数据由 UI 线程写（每次 WM_PAINT 一次），这里取走并清零。
            {
                GapSamples   pw, pp, pb, pc;
                std::int64_t n = 0, paired = 0, stale = 0, clip_full = 0;
                {
                    std::lock_guard<std::mutex> lock(lat_mutex_);
                    pw        = paint_wait_;
                    pp        = paint_prep_;
                    pb        = paint_blt_;
                    pc        = paint_clip_frac_;
                    n         = paint_count_;
                    paired    = paint_paired_;
                    stale     = paint_stale_;
                    clip_full = paint_clip_full_;
                    paint_wait_.clear();
                    paint_prep_.clear();
                    paint_blt_.clear();
                    paint_clip_frac_.clear();
                    paint_count_     = 0;
                    paint_paired_    = 0;
                    paint_stale_     = 0;
                    paint_clip_full_ = 0;
                }
                std::sort(pw.v, pw.v + pw.n);
                std::sort(pp.v, pp.v + pp.n);
                std::sort(pb.v, pb.v + pb.n);
                std::sort(pc.v, pc.v + pc.n);

                RC_LOG_INFO(
                    "[paint] 本段 {} 次绘制（配上投递 {} / 无定义 {}）"
                    " | 投递+调度 P50 {:.1f} / P95 {:.1f} / max {:.1f} ms"
                    " | 准备 P50 {:.2f} ms"
                    " | StretchBlt P50 {:.1f} / P95 {:.1f} / max {:.1f} ms"
                    " | 模式 {} | {}x{} → {}x{}",
                    n, paired, stale, percentile_sorted(pw.v, pw.n, 0.50),
                    percentile_sorted(pw.v, pw.n, 0.95), percentile_sorted(pw.v, pw.n, 1.0),
                    percentile_sorted(pp.v, pp.n, 0.50), percentile_sorted(pb.v, pb.n, 0.50),
                    percentile_sorted(pb.v, pb.n, 0.95), percentile_sorted(pb.v, pb.n, 1.0),
                    stretch_mode_name_,
                    paint_src_w_.load(std::memory_order_relaxed),
                    paint_src_h_.load(std::memory_order_relaxed),
                    paint_dst_w_.load(std::memory_order_relaxed),
                    paint_dst_h_.load(std::memory_order_relaxed));

            // ---- 按脏区重绘的自证（2026-09-25，§6.25）：**另起一行** ----
            // ⛔ 这些字段**不能塞进上面那行**：`[paint]` 是**按位置整体解析**的
            //    （`run_input_latency_check.RE_PAINT` 与 `stretch_sweep.py` 都是整块正则，
            //    末尾跟着 `| 模式 X | WxH → WxH`）。在行中间插字段会让它们**静默失配**，
            //    后果是第 11 项报"没有 [paint] 行 —— 没测到"（**已踩过，见 §6.25(9)**）。
            //    老日志行格式不可动 —— 新数据另起一行。
            RC_LOG_INFO(
                "[paint-clip] 实画面积 P50 {:.0f}% / P95 {:.0f}%（整窗 {} 次）"
                " | 光晕 {}px | 按脏区重绘 {}",
                100.0 * percentile_sorted(pc.v, pc.n, 0.50),
                100.0 * percentile_sorted(pc.v, pc.n, 0.95), clip_full,
                invalidate_halo_px_, partial_repaint_ ? "on" : "off");
            }

            last_report   = now;
            decoded_cnt   = 0;
            skipped_cnt   = 0;
            resync_cnt    = 0;
            wire_ms_sum   = 0.0;
            wire_n        = 0;
            attr_no_request = 0;
            queue_ms_sum  = 0.0;
            apply_ms_sum  = 0.0;
            gap_chg_sum   = 0.0;
            attr_count    = 0;
            recv_last     = recv;
            drop_last     = drop;
            idle_last     = idle;
            bytes_last    = bytes;
            decode_ms_sum = 0.0;
        }
    }
}

void RemoteWindow::on_connected() {
    // io 线程 -> UI 线程：只能 PostMessage，绝不能直接碰 UI
    if (hwnd_ != nullptr) {
        PostMessageW(hwnd_, WM_APP_CONNECTED, 0, 0);
    }
}

void RemoteWindow::on_disconnected(const std::string& reason) {
    // 把原因通过堆内存传过去（PostMessage 的 lParam 是整数，装不下 std::string）
    auto* payload = new std::string(reason);
    if (hwnd_ != nullptr && PostMessageW(hwnd_, WM_APP_DISCONNECTED, 0,
                                         reinterpret_cast<LPARAM>(payload))) {
        return;
    }
    delete payload; // 投递失败（窗口已销毁）则自行释放，避免泄漏
}

void RemoteWindow::on_state(const char* state) {
    if (state == nullptr) {
        return;
    }
    // 网络层只说英文状态名（state_name()），中文界面文案只在 UI 这一层出现 —— 保持分层。
    const std::string s(state);
    std::string       label;
    if (s == "resolving" || s == "connecting") {
        label = "连接中…";
    } else if (s == "handshaking") {
        label = "握手中…";
    } else if (s == "reconnecting") {
        label = "重连中…";
    } else if (s == "stopped") {
        // 【2026-09-27】**终止态**：客户端不会再重连了（凭据被拒 / 协议不合 / 重试次数用尽）。
        //
        // 为什么必须单独一条、而不能让它落进 refresh_title 的 fallback：
        //   那条 fallback 在 status_label_ 为空时显示「重连中…」，于是"已经彻底放弃"
        //   会被显示成"正在重连" —— 用户等一件永远不会发生的事。
        //   这与「只读会话必须在标题上看得见」是同一条道理（§6.31）：**状态必须说真话**，
        //   否则用户会去查一条完全正常的链路。
        // 与上一条同样的分层：网络层只说英文状态名，中文文案只在这层出现。
        label = "不会再重连";
    } else {
        return; // idle / ready：这两端由 on_connected / on_disconnected 表达
    }

    auto* payload = new std::string(std::move(label));
    if (hwnd_ != nullptr && PostMessageW(hwnd_, WM_APP_STATE, 0,
                                         reinterpret_cast<LPARAM>(payload))) {
        return;
    }
    delete payload; // 投递失败（窗口已销毁）则自行释放，避免泄漏
}

void RemoteWindow::on_role(const char* role) {
    if (role == nullptr) {
        return;
    }
    // 与 on_state 同一套封送方式（new + PostMessage + 失败即释放）。
    // 传的是**协议侧的英文角色名**（"control"/"view"），中文文案在 refresh_title 里拼 ——
    // 分层与 on_state 保持一致：网络层不懂界面语言。
    auto* payload = new std::string(role);
    if (hwnd_ != nullptr &&
        PostMessageW(hwnd_, WM_APP_ROLE, 0, reinterpret_cast<LPARAM>(payload))) {
        return;
    }
    delete payload;
}

} // namespace rc::client
