// ============================================================
// DXGI Desktop Duplication 读回带宽 spike
// （第三阶段第 1 步的前置实验，见 docs/02 §6.14）
//
// 【为什么要先写这个，而不是先写骨架】
//   §6.12 反推出 BitBlt 是"≈12 ms 固定 + 边际 0.47 GB/s"，卡在 GDI 自己的读回路径上。
//   于是"DXGI 换成 D3D11 staging copy 就能吃到那 26 ms"成了第 1 步的全部指望 ——
//   但 DXGI 的读回带宽**从未实测**。它可能是 2 ms，也可能因为 Map 要等 GPU、
//   要走 PCIe 而变成 40 ms。若是后者，整个第 1 步的前提就是错的。
//   所以第一个动作是把这一个数**量出来**（本项目方法论 #1：先量后改）。
//
// 【为什么要在同一个进程、同一个循环里配一条 GDI 对照臂】
//   BitBlt 对机器负载敏感（同一份代码在本机测到过 20.1 / 26 / 30 ms，见 docs §6.12）。
//   若 DXGI 这轮和"历史上的 GDI 数字"比，差异无法归因给实现（方法论 #4）。
//   这里让两条臂**逐次配对采样**（第 i 轮先 DXGI 后 GDI，同一时刻、同一负载），
//   配对数就是样本数，负载漂移对两条臂等量作用。
//   注意顺序固定为"先 DXGI 后 GDI"：AcquireNextFrame 是事件驱动的，
//   若在它前面插一个 40 ms 的 BitBlt，会把它等的那一帧等过去，测到的等待时间就假了。
//
// 【受控变化源有两个职责，缺一不可】
//   1. 制造变化 —— AcquireNextFrame 只在屏幕真的变化时才返回帧，屏幕静止会超时；
//   2. 当位置标记 —— 它是一块纯橙色窗口，在被抓到的帧里直接搜这个颜色就能量出
//      "这块窗口落在帧的哪里"。默认行程 1600..2048 是**故意**的：
//      GDI 在 DPI-unaware 下只抓得到物理画面左上 1707×960（§6.13），
//      于是这块窗口在 GDI 帧里应当**完全不可见**、在 DXGI 帧里应当**完整可见**。
//      这一条不需要任何坐标系推理，直接就是"DXGI 能不能拿回那 33% 桌面"的判决。
//
// 【为什么不做成"抓一帧"而是跑一段循环】
//   Map 的耗时是首次 CPU 触碰时才真正发生的（DMA 读回），单帧会被冷启动污染。
//
// 用法（在 refactored 目录下执行）：
//   build-ninja\tests\rc_dxgi_spike.exe --seconds 8
//   build-ninja\tests\rc_dxgi_spike.exe --seconds 8 --unaware     # 复现服务端当前 DPI 上下文
//   build-ninja\tests\rc_dxgi_spike.exe --seconds 8 --mode dirty  # 只拷脏区框
//   build-ninja\tests\rc_dxgi_spike.exe --seconds 5 --no-motion   # 屏幕静止：看超时语义
//
// 退出码：0 = 正常取到数据；2 = 环境不可用（如 DuplicateOutput 失败、一个样本都没有）。
//   —— 沿用本项目的约定：2 = "没测到"，绝不与 0/1 混起来当结论。
// ============================================================

#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <Windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

// ---------------------------------------------------------------- 计时

/// 用 QPC 而不是 steady_clock：本 spike 要分辨的差值在 1 ms 量级，
/// 而 system_clock 会受到 NTP 调整影响（本机时钟与沙箱时钟实测差一天，见 §6.13）。
long long qpc_freq() {
    static LARGE_INTEGER f{};
    if (f.QuadPart == 0) ::QueryPerformanceFrequency(&f);
    return f.QuadPart;
}
double now_ms() {
    LARGE_INTEGER c;
    ::QueryPerformanceCounter(&c);
    return static_cast<double>(c.QuadPart) * 1000.0 / static_cast<double>(qpc_freq());
}

struct Stats {
    std::vector<double> v;
    void add(double x) { v.push_back(x); }
    double mean() const {
        if (v.empty()) return 0.0;
        double s = 0.0;
        for (double x : v) s += x;
        return s / static_cast<double>(v.size());
    }
    double pct(double p) const {
        if (v.empty()) return 0.0;
        std::vector<double> c = v;
        std::sort(c.begin(), c.end());
        const std::size_t i = static_cast<std::size_t>(
            std::min<double>(static_cast<double>(c.size() - 1), p * static_cast<double>(c.size() - 1) + 0.5));
        return c[i];
    }
    double maxv() const {
        double m = 0.0;
        for (double x : v) if (x > m) m = x;
        return m;
    }
    std::size_t n() const { return v.size(); }
};

const char* hr_name(HRESULT hr) {
    switch (hr) {
    case S_OK:                                return "S_OK";
    case DXGI_ERROR_WAIT_TIMEOUT:             return "DXGI_ERROR_WAIT_TIMEOUT";
    case DXGI_ERROR_ACCESS_LOST:              return "DXGI_ERROR_ACCESS_LOST";
    case DXGI_ERROR_INVALID_CALL:             return "DXGI_ERROR_INVALID_CALL";
    case DXGI_ERROR_UNSUPPORTED:              return "DXGI_ERROR_UNSUPPORTED";
    case DXGI_ERROR_NOT_CURRENTLY_AVAILABLE:  return "DXGI_ERROR_NOT_CURRENTLY_AVAILABLE";
    case DXGI_ERROR_SESSION_DISCONNECTED:     return "DXGI_ERROR_SESSION_DISCONNECTED";
    case E_ACCESSDENIED:                      return "E_ACCESSDENIED";
    case E_INVALIDARG:                        return "E_INVALIDARG";
    default:                                  return "(未命名)";
    }
}

const char* fmt_name(DXGI_FORMAT f) {
    switch (f) {
    case DXGI_FORMAT_R8G8B8A8_UNORM: return "R8G8B8A8_UNORM";
    case DXGI_FORMAT_B8G8R8A8_UNORM: return "B8G8R8A8_UNORM";
    case DXGI_FORMAT_R10G10B10A2_UNORM: return "R10G10B10A2_UNORM";
    default: return "(其它)";
    }
}

const char* awareness_name(DPI_AWARENESS a) {
    switch (a) {
    case DPI_AWARENESS_UNAWARE:           return "UNAWARE";
    case DPI_AWARENESS_SYSTEM_AWARE:      return "SYSTEM_AWARE";
    case DPI_AWARENESS_PER_MONITOR_AWARE: return "PER_MONITOR_AWARE";
    default:                              return "(UNAVAILABLE)";
    }
}

/// 本线程当前生效的 DPI 上下文。
///
/// 【为什么必须把它读出来，而不是"相信自己设过"】
///   本机实测：进程级 `SetProcessDpiAwarenessContext(UNAWARE)` **会返回 FAILED(err=5
///   ERROR_ACCESS_DENIED)**，而 exe 里并没有 DPI 清单（连跑 6 次全失败，另有一次成功）。
///   也就是说同一个二进制的进程级设置**不可靠**。若只打印结果、不校验生效值，
///   就会在"自称 unaware、实际 aware"的状态下照跑不误 —— 正是本项目最忌讳的静默失效
///   （docs/03 方法论 #5）。所以下面既设线程级上下文，又读回生效值，不匹配就退出 2。
DPI_AWARENESS current_awareness() {
    return ::GetAwarenessFromDpiAwarenessContext(::GetThreadDpiAwarenessContext());
}

// ---------------------------------------------------------------- 受控变化源

constexpr COLORREF kOrange = RGB(255, 192, 0); // 与 Python 探针一致：内存里 BGRA = FF C0 00 xx

struct MotionWindow {
    int x0 = 0, y0 = 0, w = 0, h = 0, x1 = 0, step = 24, period_ms = 40;
    /// 让窗口线程单独提升 DPI 感知，从而用**物理坐标**建窗。
    ///
    /// 为什么需要这个：本 spike 有一条臂要把整个进程设成 DPI-unaware（复现服务端），
    /// 这时若窗口也用 unaware 坐标建，它会按 1.5 倍被虚拟化放大到物理 2400..2820 ——
    /// 直接跑出物理屏外，"受控变化源在场"这条前提就没了。
    /// 用 SetThreadDpiAwarenessContext 只提升建窗那个线程，抓屏仍在 unaware 的主线程里做，
    /// 于是"受控源在物理 (1600,570)"与"抓屏方 unaware"两个前提可以同时成立。
    bool thread_aware = false;
    int got_rect[4] = {0, 0, 0, 0};   ///< 建窗后窗口线程自己读到的 rect（物理）
    HWND hwnd = nullptr;
    HANDLE created = nullptr;
    std::thread pump_th, mover_th;
    std::atomic<bool> stop{false};

    static LRESULT CALLBACK wnd_proc(HWND h, UINT m, WPARAM wp, LPARAM lp) {
        switch (m) {
        case WM_CLOSE:   ::DestroyWindow(h); return 0;
        case WM_DESTROY: ::PostQuitMessage(0); return 0;
        default:         return ::DefWindowProcW(h, m, wp, lp);
        }
    }

    bool start() {
        created = ::CreateEventW(nullptr, TRUE, FALSE, nullptr);
        pump_th = std::thread([this] { pump(); });
        if (::WaitForSingleObject(created, 8000) != WAIT_OBJECT_0 || hwnd == nullptr) return false;
        mover_th = std::thread([this] { move(); });
        return true;
    }

    void stop_and_join() {
        stop = true;
        if (mover_th.joinable()) mover_th.join();
        if (hwnd) ::PostMessageW(hwnd, WM_CLOSE, 0, 0);
        if (pump_th.joinable()) pump_th.join();
        if (created) ::CloseHandle(created);
    }

    /// 把窗口挪到指定位置（测量间隙用，避免"停在哪算哪"的随机性）。
    void place(int x) const {
        if (hwnd) ::SetWindowPos(hwnd, HWND_TOPMOST, x, y0, w, h, SWP_NOACTIVATE);
    }

private:
    void pump() {
        // 只提升线程的 DPI 感知（不影响主线程的抓屏路径），让窗口坐标 = 物理坐标。
        if (thread_aware) {
            ::SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        }

        // 类名带 pid：同一进程内重复注册同名类会失败（Python 夹具踩过这个坑，§8.12），
        // 这里多一层保险。
        static wchar_t cls[64];
        ::swprintf_s(cls, L"RcDxgiSpikeProbe_%lu", ::GetCurrentProcessId());

        WNDCLASSEXW wc{};
        wc.cbSize = sizeof(wc);
        wc.lpfnWndProc = &MotionWindow::wnd_proc;
        wc.hInstance = ::GetModuleHandleW(nullptr);
        wc.hbrBackground = ::CreateSolidBrush(kOrange);
        wc.lpszClassName = cls;
        if (!::RegisterClassExW(&wc)) {
            ::SetEvent(created);
            return;
        }

        hwnd = ::CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                 cls, L"spike", WS_POPUP,
                                 x0, y0, w, h, nullptr, nullptr, wc.hInstance, nullptr);
        ::SetEvent(created);
        if (!hwnd) return;

        // 窗口可见、且贴到最顶层。之后由 mover 线程平移。
        ::ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        ::SetWindowPos(hwnd, HWND_TOPMOST, x0, y0, w, h, SWP_NOACTIVATE);

        // 由**建窗线程**读回的 rect：若 thread_aware 生效，这就是真实物理位置。
        {
            RECT r{};
            if (::GetWindowRect(hwnd, &r)) {
                got_rect[0] = r.left; got_rect[1] = r.top;
                got_rect[2] = r.right - r.left; got_rect[3] = r.bottom - r.top;
            }
        }

        MSG msg;
        while (::GetMessageW(&msg, nullptr, 0, 0) > 0) {
            ::TranslateMessage(&msg);
            ::DispatchMessageW(&msg);
        }
    }

    void move() {
        // 【必须也在这里设一遍】只给建窗线程设是不够的：平移线程每 40 ms 用 SetWindowPos
        // 把窗口挪一次，若它是 unaware，就会按 1.5 倍把窗口推到物理 2400 去 ——
        // 实测踩过：DXGI 帧里的橙色跑到 (2400,855) 只有 160 px 宽（被物理屏右边裁掉）。
        if (thread_aware) {
            ::SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        }
        int x = x0, d = step;
        while (!stop.load()) {
            ::SetWindowPos(hwnd, HWND_TOPMOST, x, y0, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
            x += d;
            if (x >= x1) { x = x1; d = -step; }
            else if (x <= x0) { x = x0; d = step; }
            ::Sleep(static_cast<DWORD>(period_ms));
        }
    }
};

// ---------------------------------------------------------------- 找橙色

struct Orange {
    std::uint64_t n = 0;
    int x0 = 0, y0 = 0, x1 = -1, y1 = -1; // x1/y1 为闭区间最大坐标；-1 = 未找到
    int w() const { return (x1 < x0) ? 0 : (x1 - x0 + 1); }
    int h() const { return (y1 < y0) ? 0 : (y1 - y0 + 1); }
};

/// 逐像素找纯橙色。@param off_b/off_g/off_r 是三个通道在 4 字节像素内的字节偏移，
/// 由像素格式决定（BGRA 是 0/1/2，RGBA 也是 0/1/2 但语义相反）——
/// 传错不会报错，只会静默地什么都找不到，所以调用方必须先把格式打印出来。
Orange find_orange(const std::uint8_t* base, int stride, int w, int h,
                   int off_b, int off_g, int off_r) {
    Orange r;
    for (int y = 0; y < h; ++y) {
        const std::uint8_t* row = base + static_cast<std::size_t>(y) * stride;
        for (int x = 0; x < w; ++x) {
            const std::uint8_t* p = row + static_cast<std::size_t>(x) * 4;
            if (p[off_b] == 0x00 && p[off_g] == 0xC0 && p[off_r] == 0xFF) {
                ++r.n;
                if (r.n == 1) { r.x0 = r.x1 = x; r.y0 = r.y1 = y; }
                else {
                    if (x < r.x0) r.x0 = x;
                    if (x > r.x1) r.x1 = x;
                    if (y < r.y0) r.y0 = y;
                    if (y > r.y1) r.y1 = y;
                }
            }
        }
    }
    return r;
}

// ---------------------------------------------------------------- CLI

struct Args {
    double seconds = 8.0;
    std::string mode = "full";     // full | dirty
    bool unaware = false;
    bool window_aware = false;     ///< 窗口线程单独提升 DPI 感知（unaware 轮必须开）
    bool no_motion = false;
    bool no_gdi = false;
    bool allow_mismatch = false;   ///< 允许"想要的 DPI 上下文没生效"也继续跑（默认拒绝）
    std::string order = "dxgi";    // dxgi | gdi —— 每轮先做哪条臂（顺序对照）
    int wx = 0, wy = 570, ww = 420, wh = 300;
    int x0 = 1600, x1 = 2048;
    int period_ms = 40;
    UINT acquire_ms = 200;
    std::string label = "-";
};

Args parse(int argc, char** argv) {
    Args a;
    for (int i = 1; i < argc; ++i) {
        const std::string s = argv[i];
        auto next = [&](const char* what) -> std::string {
            if (i + 1 >= argc) { std::printf("[spike] %s 缺少取值\n", what); std::exit(2); }
            return argv[++i];
        };
        if (s == "--seconds")            a.seconds = std::atof(next("--seconds").c_str());
        else if (s == "--mode")          a.mode = next("--mode");
        else if (s == "--unaware")       a.unaware = true;
        else if (s == "--window-aware")  a.window_aware = true;
        else if (s == "--no-motion")     a.no_motion = true;
        else if (s == "--no-gdi")        a.no_gdi = true;
        else if (s == "--allow-mismatch") a.allow_mismatch = true;
        else if (s == "--order")         a.order = next("--order");
        else if (s == "--wx")            a.wx = std::atoi(next("--wx").c_str());
        else if (s == "--wy")            a.wy = std::atoi(next("--wy").c_str());
        else if (s == "--ww")            a.ww = std::atoi(next("--ww").c_str());
        else if (s == "--wh")            a.wh = std::atoi(next("--wh").c_str());
        else if (s == "--x0")            a.x0 = std::atoi(next("--x0").c_str());
        else if (s == "--x1")            a.x1 = std::atoi(next("--x1").c_str());
        else if (s == "--period-ms")     a.period_ms = std::atoi(next("--period-ms").c_str());
        else if (s == "--acquire-ms")    a.acquire_ms = static_cast<UINT>(std::atoi(next("--acquire-ms").c_str()));
        else if (s == "--label")         a.label = next("--label");
        else { std::printf("[spike] 未知参数 %s\n", s.c_str()); std::exit(2); }
    }
    if (a.wx == 0) a.wx = a.x0; // 默认与行程左端对齐
    if (a.mode != "full" && a.mode != "dirty") { std::printf("[spike] --mode 只能是 full/dirty\n"); std::exit(2); }
    if (a.order != "dxgi" && a.order != "gdi") { std::printf("[spike] --order 只能是 dxgi/gdi\n"); std::exit(2); }
    return a;
}

// ---------------------------------------------------------------- 主流程

struct GdiArm {
    HDC     screen_dc = nullptr;
    HDC     mem_dc = nullptr;
    HBITMAP bmp = nullptr;
    HGDIOBJ old_bmp = nullptr;
    void*   bits = nullptr;
    int     w = 0, h = 0;
    int     stride = 0;

    bool init(int iw, int ih) {
        w = iw; h = ih; stride = w * 4;
        screen_dc = ::GetDC(nullptr);
        if (!screen_dc) return false;
        BITMAPINFO bmi{};
        bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bmi.bmiHeader.biWidth = w;
        bmi.bmiHeader.biHeight = -h;       // 负 = top-down，与服务端 CImage 的行为一致
        bmi.bmiHeader.biPlanes = 1;
        bmi.bmiHeader.biBitCount = 32;
        bmi.bmiHeader.biCompression = BI_RGB;
        bmp = ::CreateDIBSection(screen_dc, &bmi, DIB_RGB_COLORS, &bits, nullptr, 0);
        if (!bmp) return false;
        mem_dc = ::CreateCompatibleDC(screen_dc);
        old_bmp = ::SelectObject(mem_dc, bmp);
        return mem_dc != nullptr;
    }
    ~GdiArm() {
        if (mem_dc) { if (old_bmp) ::SelectObject(mem_dc, old_bmp); ::DeleteDC(mem_dc); }
        if (bmp) ::DeleteObject(bmp);
        if (screen_dc) ::ReleaseDC(nullptr, screen_dc);
    }
};

int run(const Args& a) {
    std::printf("[spike] label=%s mode=%s dpi=%s motion=%s gdi_arm=%s order=%s\n",
                a.label.c_str(), a.mode.c_str(), a.unaware ? "unaware" : "aware",
                a.no_motion ? "off" : "on", a.no_gdi ? "off" : "on", a.order.c_str());

    // ---- 受控变化源（除非 --no-motion；那种情况下让它静态停在画面外也行，
    //      但为了"要么不动、要么在可见处不动"，静态时摆在行程左端 x0）
    //
    // 注意它排在 DPI 设置**之前**：窗口建在自己的线程上（可单独设 DPI 感知），
    // 与主线程的抓屏上下文互不干扰，所以先后无所谓；
    // 而下面那条"DPI 不匹配即退出"需要能清理它，所以先把它建出来。
    MotionWindow mw;
    mw.x0 = a.x0; mw.x1 = a.no_motion ? a.x0 : a.x1;
    mw.y0 = a.wy; mw.w = a.ww; mw.h = a.wh;
    mw.period_ms = a.period_ms;
    mw.thread_aware = a.window_aware;
    if (!mw.start()) { std::printf("[spike] 受控变化源创建失败\n"); return 2; }

    // ---- DPI 上下文：进程级 + 线程级都设，然后**读回生效值**校验 ----
    //
    // 抓屏走的是本线程的上下文（`GetDC`/`BitBlt`/`GetSystemMetrics` 都按调用线程算），
    // 所以线程级设置才是有决定性的那一个；进程级只作为"顺手也设一下"。
    // 实测进程级会 ACCESS_DENIED（见 current_awareness 的注释），所以绝不能只靠它。
    {
        const DPI_AWARENESS_CONTEXT want_ctx = a.unaware ? DPI_AWARENESS_CONTEXT_UNAWARE
                                                        : DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2;
        const DPI_AWARENESS before = current_awareness();
        const BOOL p_ok = ::SetProcessDpiAwarenessContext(want_ctx);
        std::printf("[spike] SetProcessDpiAwarenessContext(%s) -> %s (err=%lu)  [进程级，可能被拒绝]\n",
                    a.unaware ? "UNAWARE" : "PER_MONITOR_AWARE_V2",
                    p_ok ? "ok" : "FAILED", p_ok ? 0UL : ::GetLastError());
        const DPI_AWARENESS_CONTEXT old = ::SetThreadDpiAwarenessContext(want_ctx);
        const DPI_AWARENESS after = current_awareness();
        std::printf("[spike] 本线程 DPI 上下文：%s -> %s（SetThreadDpiAwarenessContext %s）\n",
                    awareness_name(before), awareness_name(after), old ? "ok" : "FAILED");
    }
    const int sys_w = ::GetSystemMetrics(SM_CXSCREEN);
    const int sys_h = ::GetSystemMetrics(SM_CYSCREEN);
    std::printf("[spike] GetSystemMetrics(SM_CXSCREEN/SM_CYSCREEN) = %dx%d\n", sys_w, sys_h);

    // 【硬校验】想要的上下文没生效就直接退出 2，绝不混进正常数据。
    // 理由：一个"自称 unaware 其实 aware"的进程，会让 GDI 对照臂悄悄变成 2560×1440，
    // 于是 B 组（unaware）与 A 组测的其实是同一件事 —— 而结论看起来完全正常。
    {
        const bool is_unaware = (current_awareness() == DPI_AWARENESS_UNAWARE);
        if (is_unaware != a.unaware) {
            std::printf("[spike] **DPI 上下文与请求不符**：想要 %s，实际 %s\n",
                        a.unaware ? "UNAWARE" : "aware", awareness_name(current_awareness()));
            if (!a.allow_mismatch) {
                mw.stop_and_join();
                std::printf("[spike] 拒绝在本配置下出数据（加 --allow-mismatch 可强行继续）\n");
                return 2;
            }
            std::printf("[spike] 已按 --allow-mismatch 继续，本轮数据必须按实际上下文解读\n");
        }
    }

    std::printf("[spike] 受控变化源 %dx%d @ (x %d..%d, y %d)，周期 %d ms，建窗/平移线程 Aware=%d\n",
                a.ww, a.wh, mw.x0, mw.x1, a.wy, a.period_ms, a.window_aware ? 1 : 0);
    ::Sleep(500); // 等窗口真的贴上屏，别把冷启动算进去
                  // （也让建窗线程有机会把 got_rect 写出来 —— 读得太早会拿到 0x0 的竞态值）

    std::printf("[spike]   建窗线程读到的窗口 rect = (%d,%d) %dx%d\n",
                mw.got_rect[0], mw.got_rect[1], mw.got_rect[2], mw.got_rect[3]);
    {
        // 同一时刻、同一个窗口，由**主线程**（= 抓屏所在的 DPI 上下文）再读一次。
        // 读出来不同就说明窗口 rect 这一族 API 也会按 DPI 虚拟化；
        // 读出来相同则说明它返回的始终是物理坐标 —— 两种结果都有用，别预设哪一种。
        // （§6.13 已实测 `SetCursorPos` 在不感知进程里会被放大 ×1.5。）
        RECT r{};
        if (::GetWindowRect(mw.hwnd, &r)) {
            std::printf("[spike]   主线程（%s）读同一窗口 rect = (%ld,%ld) %ldx%ld\n",
                        awareness_name(current_awareness()),
                        r.left, r.top, r.right - r.left, r.bottom - r.top);
        }
    }

    // ---- D3D11 + Desktop Duplication
    ComPtr<IDXGIFactory1> factory;
    HRESULT hr = ::CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(factory.GetAddressOf()));
    if (FAILED(hr)) { std::printf("[spike] CreateDXGIFactory1 失败 %s\n", hr_name(hr)); mw.stop_and_join(); return 2; }

    ComPtr<IDXGIAdapter1> adapter;
    ComPtr<IDXGIOutput1>  output;
    bool found = false;
    for (UINT ai = 0; !found; ++ai) {
        ComPtr<IDXGIAdapter1> ad;
        if (factory->EnumAdapters1(ai, ad.GetAddressOf()) == DXGI_ERROR_NOT_FOUND) break;
        for (UINT oi = 0; ; ++oi) {
            ComPtr<IDXGIOutput> out;
            if (ad->EnumOutputs(oi, out.GetAddressOf()) == DXGI_ERROR_NOT_FOUND) break;
            DXGI_OUTPUT_DESC od{};
            out->GetDesc(&od);
            std::printf("[spike]   发现输出 adapter=%u output=%u 「%ls」 %ldx%ld @ (%ld,%ld) primary=%d\n",
                        ai, oi, od.DeviceName,
                        od.DesktopCoordinates.right - od.DesktopCoordinates.left,
                        od.DesktopCoordinates.bottom - od.DesktopCoordinates.top,
                        od.DesktopCoordinates.left, od.DesktopCoordinates.top,
                        od.AttachedToDesktop ? 1 : 0);
            // 主屏 = 桌面坐标含 (0,0)。这与服务端"抓主屏"的语义一致。
            if (od.DesktopCoordinates.left == 0 && od.DesktopCoordinates.top == 0) {
                adapter = ad;
                if (FAILED(out.As(&output))) {
                    std::printf("[spike] IDXGIOutput -> IDXGIOutput1 失败\n");
                    mw.stop_and_join();
                    return 2;
                }
                found = true;
                break;
            }
        }
    }
    if (!found) { std::printf("[spike] 没找到主输出\n"); mw.stop_and_join(); return 2; }

    DXGI_ADAPTER_DESC1 adesc{};
    adapter->GetDesc1(&adesc);
    std::printf("[spike] 适配器：%ls（显存 %llu MB, 软件=%d）\n", adesc.Description,
                static_cast<unsigned long long>(adesc.DedicatedVideoMemory / (1024 * 1024)),
                (adesc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) ? 1 : 0);

    D3D_FEATURE_LEVEL level{};
    ComPtr<ID3D11Device>        device;
    ComPtr<ID3D11DeviceContext> ctx;
    hr = ::D3D11CreateDevice(adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, nullptr, 0,
                             D3D11_SDK_VERSION, device.GetAddressOf(), &level, ctx.GetAddressOf());
    std::printf("[spike] D3D11CreateDevice(hardware) -> %s feature_level=0x%x\n", hr_name(hr),
                static_cast<unsigned>(level));
    if (FAILED(hr)) {
        hr = ::D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, 0, nullptr, 0,
                                 D3D11_SDK_VERSION, device.GetAddressOf(), &level, ctx.GetAddressOf());
        std::printf("[spike] 退回 WARP -> %s\n", hr_name(hr));
        if (FAILED(hr)) { mw.stop_and_join(); return 2; }
    }

    const double t_dup0 = now_ms();
    ComPtr<IDXGIOutputDuplication> dup;
    hr = output->DuplicateOutput(device.Get(), dup.GetAddressOf());
    const double t_dup1 = now_ms();
    std::printf("[spike] DuplicateOutput -> %s（耗时 %.2f ms）\n", hr_name(hr), t_dup1 - t_dup0);
    if (FAILED(hr)) {
        // 最常见的两种：已经有别的进程在复制这个输出（本机自测时服务端若开了 DXGI 就会撞上），
        // 或本会话不支持（安全桌面/RDP）。
        std::printf("[spike] 无法复制输出，环境不可用 —— 这不是性能结论。\n");
        mw.stop_and_join();
        return 2;
    }

    DXGI_OUTDUPL_DESC dd{};
    dup->GetDesc(&dd);
    const int W = static_cast<int>(dd.ModeDesc.Width);
    const int H = static_cast<int>(dd.ModeDesc.Height);
    std::printf("[spike] DXGI_OUTDUPL_DESC: %dx%d fmt=%s 旋转=%d\n", W, H,
                fmt_name(dd.ModeDesc.Format), static_cast<int>(dd.Rotation));
    if (W != sys_w || H != sys_h) {
        std::printf("[spike] **注意**：DXGI 尺寸 %dx%d 与 GetSystemMetrics %dx%d 不一致 "
                    "→ 这正是「抓屏被 DPI 虚拟化」的判据\n", W, H, sys_w, sys_h);
    }

    // 通道偏移：CImage 的 DIB 是 BGRA；DXGI 模式格式决定另一条臂。
    const bool dxgi_is_bgra = (dd.ModeDesc.Format == DXGI_FORMAT_B8G8R8A8_UNORM);
    const int d_off_b = dxgi_is_bgra ? 0 : 2;
    const int d_off_g = 1;
    const int d_off_r = dxgi_is_bgra ? 2 : 0;

    // ---- staging 纹理（CPU 可读）
    D3D11_TEXTURE2D_DESC sd{};
    sd.Width = static_cast<UINT>(W);
    sd.Height = static_cast<UINT>(H);
    sd.MipLevels = 1;
    sd.ArraySize = 1;
    sd.Format = dd.ModeDesc.Format;
    sd.SampleDesc.Count = 1;
    sd.Usage = D3D11_USAGE_STAGING;
    sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;
    hr = device->CreateTexture2D(&sd, nullptr, staging.GetAddressOf());
    if (FAILED(hr)) { std::printf("[spike] CreateTexture2D(staging) 失败 %s\n", hr_name(hr)); mw.stop_and_join(); return 2; }

    // ---- GDI 对照臂：与当前服务端同一条路径（BitBlt 进 DIB section）
    GdiArm gdi;
    if (!a.no_gdi && !gdi.init(sys_w, sys_h)) {
        std::printf("[spike] GDI 对照臂初始化失败，跳过\n");
    }
    const bool gdi_on = gdi.mem_dc != nullptr;
    if (gdi_on) {
        std::printf("[spike] GDI 对照臂：BitBlt %dx%d（= GetSystemMetrics 尺寸，即服务端当前抓屏尺寸）\n",
                    gdi.w, gdi.h);
    }

    std::vector<std::uint8_t> cpu_buf(static_cast<std::size_t>(W) * H * 4);

    Stats st_wait, st_copy, st_map, st_memcpy, st_scan, st_release, st_total;
    Stats st_gdi_blit, st_gdi_memcpy;
    Stats st_dirty_frac;
    std::uint64_t n_timeout = 0, n_frames = 0, n_dirty_rts_ok = 0, n_accum_gt1 = 0;
    std::uint64_t n_dirty_empty = 0;      ///< 取到帧、但脏区是 0 条 —— 语义可疑，要显式数
    HRESULT dirty_hr = S_OK;
    HRESULT last_fail = S_OK;
    Orange dx_orange_union, gd_orange_union;
    int row_pitch_reported = 0;
    int dirty_box_w_max = 0, dirty_box_h_max = 0;
    long long present_time_first = 0, present_time_last = 0;

    // GDI 对照臂做成可调用体，是为了能**换顺序**再跑一轮：
    // BitBlt 对机器负载极其敏感（同一份代码历史测到 20.1 / 26 / 30 ms），
    // 而"紧跟在一次 DXGI 抓取之后"可能让 BitBlt 去等还没落地的 GPU/GDI 工作，
    // 从而**虚高**。跑成 A(先 DXGI)→A2(先 GDI)，两轮的 GDI 数一致才说明顺序无影响
    // —— 这就是本项目方法论 #4（两轮 A/B 要带回照）在这里的用法。
    auto gdi_arm = [&]() {
        if (!gdi_on) return;
        const double g0 = now_ms();
        ::BitBlt(gdi.mem_dc, 0, 0, gdi.w, gdi.h, gdi.screen_dc, 0, 0, SRCCOPY);
        const double g1 = now_ms();
        const int gs = gdi.w * 4;
        for (int y = 0; y < gdi.h; ++y) {
            std::memcpy(cpu_buf.data() + static_cast<std::size_t>(y) * gs,
                        static_cast<const std::uint8_t*>(gdi.bits) + static_cast<std::size_t>(y) * gs,
                        static_cast<std::size_t>(gs));
        }
        const double g2 = now_ms();
        st_gdi_blit.add(g1 - g0);
        st_gdi_memcpy.add(g2 - g1);

        const Orange go = find_orange(cpu_buf.data(), gs, gdi.w, gdi.h, 0, 1, 2);
        if (go.n) {
            if (gd_orange_union.n == 0) gd_orange_union = go;
            else {
                gd_orange_union.n += go.n;
                gd_orange_union.x0 = std::min(gd_orange_union.x0, go.x0);
                gd_orange_union.x1 = std::max(gd_orange_union.x1, go.x1);
                gd_orange_union.y0 = std::min(gd_orange_union.y0, go.y0);
                gd_orange_union.y1 = std::max(gd_orange_union.y1, go.y1);
            }
        }
    };

    const double t_start = now_ms();
    const double t_end = t_start + a.seconds * 1000.0;

    while (now_ms() < t_end) {
        // --order gdi：GDI 臂排在 AcquireNextFrame **之前**。
        // 代价是 AcquireNextFrame 的等待时间不再可比（它要等 BitBlt 跑完），
        // 本模式只用来看 GDI 自己的数。
        if (a.order == "gdi") gdi_arm();

        DXGI_OUTDUPL_FRAME_INFO info{};
        ComPtr<IDXGIResource> res;
        const double t0 = now_ms();
        hr = dup->AcquireNextFrame(a.acquire_ms, &info, res.GetAddressOf());
        const double t1 = now_ms();

        if (hr == DXGI_ERROR_WAIT_TIMEOUT) { st_wait.add(t1 - t0); ++n_timeout; continue; }
        if (FAILED(hr)) { last_fail = hr; break; }
        st_wait.add(t1 - t0);
        if (info.AccumulatedFrames > 1) ++n_accum_gt1;
        if (present_time_first == 0) present_time_first = info.LastPresentTime.QuadPart;
        present_time_last = info.LastPresentTime.QuadPart;

        ComPtr<ID3D11Texture2D> tex;
        if (FAILED(res.As(&tex))) { dup->ReleaseFrame(); last_fail = E_NOINTERFACE; break; }

        // 脏矩形（DXGI 原生）：既是"能不能用它省掉比对"的依据，
        // 也是 dirty 模式要拷的那块框。注意它给的是**全局共享**的脏区，
        // 与我们的 per-consumer 脏区语义不同（docs/03 第 1 步①）。
        //
        // 【坑】不要用"传 NULL 探长度"的写法：实测它返回 0 且不给长度，
        // 于是这一整块逻辑静默变成"脏矩形永远是 0 条"——一个会骗人的观测。
        // 改成先给一块足够大的缓冲，只用 DXGI_ERROR_MORE_DATA 这一条正路扩容。
        std::vector<RECT> rects;
        {
            UINT cap = 1024;
            rects.resize(cap);
            UINT got = 0;
            HRESULT hrd = dup->GetFrameDirtyRects(cap * sizeof(RECT), rects.data(), &got);
            if (hrd == DXGI_ERROR_MORE_DATA) {
                rects.resize(got / sizeof(RECT));
                UINT cap2 = static_cast<UINT>(rects.size());
                hrd = dup->GetFrameDirtyRects(cap2 * sizeof(RECT), rects.data(), &got);
            }
            if (hrd == S_OK) {
                rects.resize(got / sizeof(RECT));
                ++n_dirty_rts_ok;
            } else {
                if (dirty_hr == S_OK) dirty_hr = hrd;   // 只在第一次记，避免刷屏
                rects.clear();
            }
        }
        if (rects.empty()) ++n_dirty_empty;
        int bx0 = INT_MAX, by0 = INT_MAX, bx1 = -1, by1 = -1;
        std::uint64_t dirty_px = 0;
        for (const RECT& r : rects) {
            bx0 = std::min<int>(bx0, r.left);
            by0 = std::min<int>(by0, r.top);
            bx1 = std::max<int>(bx1, r.right);
            by1 = std::max<int>(by1, r.bottom);
            dirty_px += static_cast<std::uint64_t>(r.right - r.left) * static_cast<std::uint64_t>(r.bottom - r.top);
        }
        if (bx1 >= bx0 && by1 >= by0) {
            bx0 = std::max(0, bx0); by0 = std::max(0, by0);
            bx1 = std::min(W, bx1); by1 = std::min(H, by1);
            st_dirty_frac.add(100.0 * static_cast<double>(dirty_px) / (static_cast<double>(W) * H));
            dirty_box_w_max = std::max(dirty_box_w_max, bx1 - bx0);
            dirty_box_h_max = std::max(dirty_box_h_max, by1 - by0);
        }

        // ---- DXGI 臂
        const bool use_dirty = (a.mode == "dirty") && (bx1 > bx0) && (by1 > by0);
        const int copy_w = use_dirty ? (bx1 - bx0) : W;
        const int copy_h = use_dirty ? (by1 - by0) : H;

        const double t2a = now_ms();
        if (use_dirty) {
            D3D11_BOX box{};
            box.left = static_cast<UINT>(bx0);
            box.top = static_cast<UINT>(by0);
            box.front = 0;
            box.right = static_cast<UINT>(bx1);
            box.bottom = static_cast<UINT>(by1);
            box.back = 1;
            ctx->CopySubresourceRegion(staging.Get(), 0, 0, 0, 0, tex.Get(), 0, &box);
        } else {
            ctx->CopyResource(staging.Get(), tex.Get());
        }
        const double t2 = now_ms();

        D3D11_MAPPED_SUBRESOURCE m{};
        hr = ctx->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &m);
        const double t3 = now_ms();
        if (FAILED(hr)) { dup->ReleaseFrame(); last_fail = hr; break; }
        if (row_pitch_reported == 0) {
            row_pitch_reported = static_cast<int>(m.RowPitch);
            std::printf("[spike] staging RowPitch = %d（%dx4 = %d，%s）\n", row_pitch_reported,
                        W, W * 4, row_pitch_reported == W * 4 ? "无填充" : "**有填充，真实实现必须按行拷**");
        }

        // memcpy 出到连续缓冲：真实实现必须交给比对/编码链，这一段不能不计。
        const int dst_stride = copy_w * 4;
        for (int y = 0; y < copy_h; ++y) {
            std::memcpy(cpu_buf.data() + static_cast<std::size_t>(y) * dst_stride,
                        static_cast<const std::uint8_t*>(m.pData) + static_cast<std::size_t>(y) * m.RowPitch,
                        static_cast<std::size_t>(dst_stride));
        }
        const double t4 = now_ms();

        // 全屏扫描（我们的比对就是这个量级：逐像素看有没有变）。只量一次，两条臂共用。
        Orange o;
        if (!use_dirty) {
            o = find_orange(cpu_buf.data(), dst_stride, copy_w, copy_h, d_off_b, d_off_g, d_off_r);
            if (o.n) {
                if (dx_orange_union.n == 0) dx_orange_union = o;
                else {
                    dx_orange_union.n += o.n;
                    dx_orange_union.x0 = std::min(dx_orange_union.x0, o.x0);
                    dx_orange_union.x1 = std::max(dx_orange_union.x1, o.x1);
                    dx_orange_union.y0 = std::min(dx_orange_union.y0, o.y0);
                    dx_orange_union.y1 = std::max(dx_orange_union.y1, o.y1);
                }
            }
        }
        const double t5 = now_ms();

        ctx->Unmap(staging.Get(), 0);
        res.Reset();
        dup->ReleaseFrame();
        const double t6 = now_ms();

        st_copy.add(t2 - t2a);
        st_map.add(t3 - t2);
        st_memcpy.add(t4 - t3);
        st_scan.add(t5 - t4);
        st_release.add(t6 - t5);
        st_total.add(t6 - t0);
        ++n_frames;

        // ---- GDI 臂（配对采样）
        if (a.order == "dxgi") gdi_arm();
    }

    mw.stop_and_join();

    // ---------------------------------------------------------- 报告
    const double dx_grab = st_copy.mean() + st_map.mean() + st_memcpy.mean();
    std::printf("\n[spike] ===== DXGI 臂（%llu 帧 / 超时 %llu 次）=====\n",
                static_cast<unsigned long long>(n_frames), static_cast<unsigned long long>(n_timeout));
    auto line = [](const char* what, const Stats& s, double px) {
        const double mp = (s.mean() > 0) ? px / s.mean() : 0.0;
        std::printf("[spike]   %-14s 均值 %7.3f  p50 %7.3f  p95 %7.3f  最大 %7.3f ms", what,
                    s.mean(), s.pct(0.5), s.pct(0.95), s.maxv());
        if (px > 0) std::printf("   （%.2f GB/s）", mp / 1e6);
        std::printf("\n");
    };
    const double dx_px = static_cast<double>(W) * H * 4;
    line("AcquireNextFrame", st_wait, 0);
    line("CopyResource", st_copy, dx_px);
    line("Map", st_map, dx_px);
    line("memcpy 出", st_memcpy, dx_px);
    std::printf("[spike]   %-14s 均值 %7.3f  p50 %7.3f  p95 %7.3f  最大 %7.3f ms\n", "扫描(比对)",
                st_scan.mean(), st_scan.pct(0.5), st_scan.pct(0.95), st_scan.maxv());
    std::printf("[spike]   %-14s 均值 %7.3f  p95 %7.3f  最大 %7.3f ms\n", "Unmap+Release",
                st_release.mean(), st_release.pct(0.95), st_release.maxv());

    std::printf("[spike] DXGI 抓一帧合计（拷贝+Map+memcpy）= %.3f ms  [%.3f GB/s]\n",
                dx_grab, dx_grab > 0 ? dx_px / dx_grab / 1e6 : 0.0);
    std::printf("[spike] DXGI 端到端一轮（含等待）= %.3f ms\n", st_total.mean());
    if (gdi_on) {
        std::printf("[spike] GDI 臂：BitBlt %.3f ms + memcpy %.3f ms = %.3f ms  （%dx%d = %.2f M 像素）\n",
                    st_gdi_blit.mean(), st_gdi_memcpy.mean(), st_gdi_blit.mean() + st_gdi_memcpy.mean(),
                    gdi.w, gdi.h, static_cast<double>(gdi.w) * gdi.h / 1e6);
        std::printf("[spike] → 同刻配对：DXGI 比 GDI 省 %.3f ms（%.2fx）\n",
                    (st_gdi_blit.mean() + st_gdi_memcpy.mean()) - dx_grab,
                    dx_grab > 0 ? (st_gdi_blit.mean() + st_gdi_memcpy.mean()) / dx_grab : 0.0);
    }
    std::printf("[spike] 脏矩形：取成功 %llu 次（其中 0 条 %llu 次），首个失败 hr=%s\n",
                static_cast<unsigned long long>(n_dirty_rts_ok),
                static_cast<unsigned long long>(n_dirty_empty), hr_name(dirty_hr));
    std::printf("[spike] 脏矩形平均面积占 %.2f%%；并集框最大 %dx%d\n",
                st_dirty_frac.mean(), dirty_box_w_max, dirty_box_h_max);
    std::printf("[spike] AccumulatedFrames>1 的次数 = %llu（>0 说明我们处理得比屏幕刷新慢）\n",
                static_cast<unsigned long long>(n_accum_gt1));
    if (n_frames > 1) {
        const double span_ms = static_cast<double>(present_time_last - present_time_first) / 10000.0;
        std::printf("[spike] LastPresentTime 跨度 = %.1f ms / %llu 帧 → 屏幕侧实际 %.2f fps\n",
                    span_ms, static_cast<unsigned long long>(n_frames - 1),
                    span_ms > 0 ? (n_frames - 1) * 1000.0 / span_ms : 0.0);
    }

    auto bbox_str = [](const Orange& o, char* buf, std::size_t n) {
        if (o.n == 0) { std::snprintf(buf, n, "无"); }
        else { std::snprintf(buf, n, "(%d,%d)-(%d,%d) %dx%d n=%llu", o.x0, o.y0, o.x1, o.y1, o.w(), o.h(),
                             static_cast<unsigned long long>(o.n)); }
    };
    char b1[160], b2[160];
    bbox_str(dx_orange_union, b1, sizeof(b1));
    bbox_str(gd_orange_union, b2, sizeof(b2));
    std::printf("\n[spike] ===== 覆盖判决：受控变化源（橙色 %dx%d）在各臂帧里的位置 =====\n", a.ww, a.wh);
    std::printf("[spike]   DXGI 帧（%dx%d）里：%s\n", W, H, b1);
    std::printf("[spike]   GDI  帧（%dx%d）里：%s\n", gdi_on ? gdi.w : 0, gdi_on ? gdi.h : 0, b2);
    std::printf("[spike]   行程右端 x1=%d；GDI 帧宽 %d → %s\n", a.x1, gdi_on ? gdi.w : sys_w,
                (gdi_on && a.x1 >= gdi.w) ? "x1 本来就在 GDI 帧外" : "x1 在 GDI 帧内");

    // 机器可读的一行，给 Python 驱动/归档用
    std::printf("\nSPIKE_JSON {\"label\":\"%s\",\"mode\":\"%s\",\"dpi\":\"%s\",\"order\":\"%s\","
                "\"dxgi_w\":%d,\"dxgi_h\":%d,\"sys_w\":%d,\"sys_h\":%d,\"row_pitch\":%d,"
                "\"frames\":%llu,\"timeouts\":%llu,\"dirty_ok\":%llu,\"dirty_empty\":%llu,"
                "\"dx_copy_ms\":%.4f,\"dx_map_ms\":%.4f,\"dx_memcpy_ms\":%.4f,\"dx_scan_ms\":%.4f,"
                "\"dx_wait_ms\":%.4f,\"dx_grab_ms\":%.4f,\"dx_total_ms\":%.4f,"
                "\"gdi_blit_ms\":%.4f,\"gdi_memcpy_ms\":%.4f,\"gdi_total_ms\":%.4f,"
                "\"dirty_pct\":%.3f,\"dx_orange_n\":%llu,\"gd_orange_n\":%llu,"
                "\"dx_orange_x0\":%d,\"dx_orange_x1\":%d,\"gd_orange_x0\":%d,\"gd_orange_x1\":%d}\n",
                a.label.c_str(), a.mode.c_str(), a.unaware ? "unaware" : "aware", a.order.c_str(),
                W, H, sys_w, sys_h, row_pitch_reported,
                static_cast<unsigned long long>(n_frames), static_cast<unsigned long long>(n_timeout),
                static_cast<unsigned long long>(n_dirty_rts_ok), static_cast<unsigned long long>(n_dirty_empty),
                st_copy.mean(), st_map.mean(), st_memcpy.mean(), st_scan.mean(),
                st_wait.mean(), dx_grab, st_total.mean(),
                st_gdi_blit.mean(), st_gdi_memcpy.mean(), st_gdi_blit.mean() + st_gdi_memcpy.mean(),
                st_dirty_frac.mean(),
                static_cast<unsigned long long>(dx_orange_union.n),
                static_cast<unsigned long long>(gd_orange_union.n),
                dx_orange_union.x0, dx_orange_union.x1, gd_orange_union.x0, gd_orange_union.x1);

    if (n_frames == 0) {
        std::printf("[spike] **没有任何一帧** —— 这不是性能结论，是没测到（last_fail=%s）\n", hr_name(last_fail));
        return 2;
    }
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    const Args a = parse(argc, argv);
    const int rc = run(a);
    std::printf("[spike] exit=%d\n", rc);
    return rc;
}
