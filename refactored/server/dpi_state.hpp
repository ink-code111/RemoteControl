#pragma once
// ============================================================
// DPI 状态探测：把"这台机器在缩放、而服务端抓屏被虚拟化了"变成可读的数字
//
// 【为什么需要它】
//   docs §6.13 定死了两条**功能缺陷**（不是"糊一点"）：服务端不感知 DPI 时，
//   150% 缩放下 BitBlt 抓到的 1707×960 是**物理画面左上角 1:1 的裁剪**，于是
//     ① 远端只看得到桌面的左上 44%（右侧 33%、下侧 33% 永不入帧，任务栏也不在帧里）；
//     ② 输入侧（SetCursorPos / GetCursorInfo）仍按 1.5 倍虚拟化，
//        与 1:1 的抓屏**不在同一坐标空间** → 点击系统性偏移 1.5 倍。
//   两者都只在"缩放 ≠ 100%"的机器上成立，而配置里只有一个 `dpi_aware: false`。
//   使用者没有任何途径知道自己正踩在上面 —— 这和"改了配置没人知道生效没有"
//   是同一个病的两面：**那一面是配置假装生效，这一面是缺陷假装不存在。**
//   所以启动时必须把机器的缩放状态量化后打进日志。
//
// 【为什么判据不能是"缩放比例等于 1.5"】
//   缩放可能是 125% / 150% / 175% / 自定义，不存在可硬编码的常量。
//   可靠的判据是**同一个 API 在两个 DPI 上下文下读出不同的值** ——
//   差值本身就是"被虚拟化"的量化结果，比值就是缩放比：
//     GetSystemMetrics(SM_CXSCREEN) 在 unaware 上下文 → 虚拟化尺寸（= BitBlt 会抓到的）
//                                   在 aware   上下文 → 物理尺寸
//   再加一路**独立来源** EnumDisplaySettings（显示模式，不随调用方 DPI 上下文变化）
//   做交叉验证：两个互不相关的手段给出同一个物理尺寸，才敢把结论写进日志。
//   （同款交叉验证在本项目用过两次：§6.13 的逐像素比对、§6.14 的
//     GetSystemMetrics vs DXGI_OUTDUPL_DESC —— 都靠"两个来源一致"排除仪器故障。）
// ============================================================

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
// SetThreadDpiAwarenessContext / DPI_AWARENESS_CONTEXT_* 需要 0x0A00；
// 与 common/asio_common.hpp 保持一致，但这里自带 guard 以免被单独 include 时失效。
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#include <Windows.h>

namespace rc::server {

/// 一次 DPI 探测的结果。**每个字段都是实测值**，不含任何推算常量。
struct ScreenGeometry {
    /// 在**当前**进程/线程 DPI 上下文下，BitBlt 实际会抓到的尺寸。
    /// 这是最要紧的一个数：它就是远端能看到的那块画面。
    int frame_w = 0;
    int frame_h = 0;

    /// 物理像素尺寸（临时切到 aware 上下文读）。
    int phys_w = 0;
    int phys_h = 0;

    /// 显示模式里的尺寸（EnumDisplaySettings，独立于调用方 DPI 上下文的第二个来源）
    int mode_w = 0;
    int mode_h = 0;

    /// 线程级 DPI 上下文切换在这台机器上是否真的能区分上下文
    /// （判据：两个上下文下 GetSystemMetrics 读出不同值）。false 表示"测不出缩放"，
    /// 但**分不清**是"真的 100%"还是"切换被拒" —— 日志里要如实说，不能含糊成前者。
    bool context_switch_effective = false;

    /// 显示模式那一路是否读到了（读到才能做交叉验证）
    bool mode_ok = false;

    /// 抓屏是否被系统虚拟化。
    ///
    /// 判据刻意写成"实抓 < 物理"，而不是"实抓 != 某个常数"：
    /// 缩放比例因机器而异，把 1707 写死成判据，换台 125% 的机器就会漏判。
    bool virtualized() const noexcept {
        return frame_w > 0 && phys_w > 0 && (frame_w < phys_w || frame_h < phys_h);
    }

    /// 缩放比例（%）。仅在 virtualized() 为真时有意义。
    int scaling_percent() const noexcept {
        if (!virtualized() || frame_w <= 0) {
            return 100;
        }
        return static_cast<int>(static_cast<double>(phys_w) * 100.0 / static_cast<double>(frame_w) + 0.5);
    }

    /// 远端可见的桌面比例（宽 / 高 / 面积）
    double visible_x() const noexcept {
        return phys_w > 0 ? static_cast<double>(frame_w) / static_cast<double>(phys_w) : 1.0;
    }
    double visible_y() const noexcept {
        return phys_h > 0 ? static_cast<double>(frame_h) / static_cast<double>(phys_h) : 1.0;
    }
    double visible_area() const noexcept { return visible_x() * visible_y(); }

    /// 物理尺寸是否被两个独立来源共同证实（aware 读数 == 显示模式）。
    /// 只在这里为真时，日志里那句"物理 WxH"才算有证据。
    bool phys_confirmed() const noexcept {
        return mode_ok && phys_w == mode_w && phys_h == mode_h;
    }
};

/// DPI_AWARENESS_CONTEXT 的可读名（日志用）
inline const char* dpi_context_name(DPI_AWARENESS_CONTEXT ctx) noexcept {
    if (ctx == DPI_AWARENESS_CONTEXT_UNAWARE)              return "unaware";
    if (ctx == DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)         return "system-aware";
    if (ctx == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)    return "per-monitor-aware";
    if (ctx == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) return "per-monitor-aware-v2";
    if (ctx == DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED)    return "unaware-gdiscaled";
    return "unknown";
}

/// 当前调用线程生效的 DPI 感知**级别**名（PROCESS_DPI_UNAWARE / SYSTEM_DPI_AWARE /
/// PER_MONITOR_DPI_AWARE）。
///
/// 【为什么单独要级别，而不只看 frame 尺寸】
///   2026-09-23 实测：`capture_backend=gdi` 抓 1707x960 与 `=dxgi` 抓 2560x1440 时，
///   两者的 `context_switch_effective` 都是 true —— 光看尺寸分不清"机器在缩放"与
///   "进程被顶成 aware"。级别是**第三种**独立读数：它直接说进程处在哪一档，
///   与"尺寸"、"显示模式"三路互证。三路一致才敢在日志里下结论。
inline const char* dpi_awareness_name() noexcept {
    // 注意：本机 SDK(10.0.26100.0) 里该函数是**单参数直接返回**，不是老的"传指针+返回 BOOL"
    switch (::GetAwarenessFromDpiAwarenessContext(::GetThreadDpiAwarenessContext())) {
        case DPI_AWARENESS_UNAWARE:           return "PROCESS_DPI_UNAWARE";
        case DPI_AWARENESS_SYSTEM_AWARE:      return "SYSTEM_DPI_AWARE";
        case DPI_AWARENESS_PER_MONITOR_AWARE: return "PER_MONITOR_DPI_AWARE";
        default:                              return "DPI_AWARENESS_INVALID";
    }
}

/// 探测当前屏幕几何与 DPI 状态。
///
/// 【副作用】会让**调用线程**临时切换 DPI 上下文，返回前复原到进入时的值。
/// 不改动进程级设置，因此可以在启动期任意位置调用。
///
/// 【为什么先读"当前上下文"再切】
///   frame_w 必须是"抓屏会得到的尺寸"，所以它要在**没被本函数改动过**的上下文下读。
///   顺序必须是：① 读当前 → ② 切 aware 读物理 → ③ 复原。
///   反过来先切再读，frame_w 就变成了别的东西，整个判据失去意义。
inline ScreenGeometry measure_screen_geometry() noexcept {
    ScreenGeometry g;

    // ① 当前上下文下的读数 = BitBlt 真正会抓到的尺寸
    g.frame_w = ::GetSystemMetrics(SM_CXSCREEN);
    g.frame_h = ::GetSystemMetrics(SM_CYSCREEN);

    // ② 临时切到 aware 读物理尺寸
    DPI_AWARENESS_CONTEXT old = ::SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    if (old != nullptr) {
        g.phys_w = ::GetSystemMetrics(SM_CXSCREEN);
        g.phys_h = ::GetSystemMetrics(SM_CYSCREEN);

        // ③ 自证：切到 unaware 再读一次。两个上下文读出**不同**值，才说明这台机器上
        //    线程级切换确实能区分上下文（等价于"存在缩放"）。
        //    读不出差异有两种可能，本函数不猜是哪一种 —— 交给调用方按 context_switch_effective 措辞。
        ::SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_UNAWARE);
        const int unaware_w = ::GetSystemMetrics(SM_CXSCREEN);
        g.context_switch_effective = (unaware_w != g.phys_w);

        ::SetThreadDpiAwarenessContext(old); // 复原
    } else {
        // 线程级切换不可用：物理尺寸这条路径测不出来。
        // 这里退化成与 frame 相同，于是 virtualized() 必然为 false ——
        // 但调用方会看到 context_switch_effective == false 且 mode_ok 可能为真，
        // 从而知道"未检出"不等于"没有缺陷"。
        g.phys_w = g.frame_w;
        g.phys_h = g.frame_h;
    }

    // ④ 独立来源交叉验证：显示模式与调用方 DPI 上下文无关，是纯粹的第二意见
    DEVMODEW dm{};
    dm.dmSize = sizeof(dm);
    if (::EnumDisplaySettingsW(nullptr, ENUM_CURRENT_SETTINGS, &dm) != FALSE && dm.dmPelsWidth > 0) {
        g.mode_w  = static_cast<int>(dm.dmPelsWidth);
        g.mode_h  = static_cast<int>(dm.dmPelsHeight);
        g.mode_ok = true;
    }
    return g;
}

} // namespace rc::server
