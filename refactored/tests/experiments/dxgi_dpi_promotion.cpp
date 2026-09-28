// ============================================================
// 实验：创建 D3D11 设备 / DXGI Desktop Duplication 会不会把**进程**顶成 DPI-aware？
//
// 【为什么必须查清 —— 它决定一条已写进日志的告警是对是错】
//   当前行为（2026-09-23 实测，见 _temp/backend_runs、_temp/srv_dxgi.log）：
//     capture_backend=gdi  + dpi_aware=false → 报告"被抓屏虚拟化"(1707x960)  ✅ 符合预期
//     capture_backend=dxgi + dpi_aware=false → 报告"2560x1440 == 物理尺寸，
//                                              本进程已感知 DPI"            ❓ 说不通
//   配置里明明是 dpi_aware=false，谁把进程变成 aware 的？
//
//   代码顺序上唯一可能的嫌疑人是 **DXGI 抓屏器的构造函数**：它在 AsioServer 构造里被调用，
//   早于 DPI 报告，内部执行 D3D11CreateDevice → EnumOutputs → DuplicateOutput。
//   （GDI+ 已被排除：gdiplus_dpi_probe.cpp 证明 GdiplusStartup 前后上下文不变。）
//
// 【为什么这条结论重要】
//   若 D3D11 真的把进程顶成 aware，那么在 dxgi 后端下：
//     · dpi_aware=false **不是"不感知"** —— 抓屏给物理像素、输入坐标也可能变成物理坐标，
//       §6.13 那条"点击系统性偏移"的告警在此路径下可能**不成立**（= 我写的 WARN 文案有错）；
//     · 且这是"依赖一个未文档化的副作用" —— 正确性挂在副作用上，必须显式化才敢用。
//   另外要问清顶成了**哪一级**：system-aware 与 per-monitor-aware 在多显示器不同缩放下
//   行为不同，不能含糊成"aware 了"。
//
// 用法：直接跑，打印每一步之后的「线程上下文 / 进程 awareness / GetSystemMetrics / 屏幕 DC 尺寸」。
// ============================================================

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#include <Windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <cstdio>

using Microsoft::WRL::ComPtr;

namespace {

const char* ctx_name(DPI_AWARENESS_CONTEXT c) {
    if (c == DPI_AWARENESS_CONTEXT_UNAWARE)              return "unaware";
    if (c == DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)         return "system-aware";
    if (c == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)    return "per-monitor";
    if (c == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) return "per-monitor-v2";
    if (c == DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED)    return "unaware-gdiscaled";
    return "(other)";
}

// 把上下文翻译成 DPI_AWARENESS 级别（PROCESS_DPI_UNAWARE / SYSTEM_DPI_AWARE / PER_MONITOR_DPI_AWARE）
// 注意：本机 SDK(10.0.26100.0) 里这两个函数是**单参数直接返回**，不是老的 "传指针 + 返回 BOOL"。
const char* level_name(DPI_AWARENESS_CONTEXT c) {
    switch (::GetAwarenessFromDpiAwarenessContext(c)) {
        case DPI_AWARENESS_UNAWARE:            return "PROCESS_DPI_UNAWARE";
        case DPI_AWARENESS_SYSTEM_AWARE:       return "SYSTEM_DPI_AWARE";
        case DPI_AWARENESS_PER_MONITOR_AWARE:  return "PER_MONITOR_DPI_AWARE";
        case DPI_AWARENESS_INVALID:            return "DPI_AWARENESS_INVALID";
        default:                               return "(unknown)";
    }
}

// 屏幕 DC 的尺寸 = BitBlt 会抓到的尺寸（与 GetSystemMetrics 互为印证）
void measure_screen_dc(int& w, int& h) {
    HDC dc = ::GetDC(nullptr);
    w = ::GetDeviceCaps(dc, HORZRES);
    h = ::GetDeviceCaps(dc, VERTRES);
    ::ReleaseDC(nullptr, dc);
}

void dump(const char* tag) {
    const DPI_AWARENESS_CONTEXT ctx = ::GetThreadDpiAwarenessContext();
    int dcw = 0, dch = 0;
    measure_screen_dc(dcw, dch);
    std::printf("%-30s 上下文=%-15s 级别=%-21s 有效DPI=%-4u GetSystemMetrics=%dx%d  屏幕DC=%dx%d\n",
                tag, ctx_name(ctx), level_name(ctx),
                ::GetDpiFromDpiAwarenessContext(ctx),
                ::GetSystemMetrics(SM_CXSCREEN), ::GetSystemMetrics(SM_CYSCREEN),
                dcw, dch);
}

/// 输入坐标空间判定：往一个**虚拟空间放不下**的位置设光标，再读回来。
///
/// 【判据为什么是 (2000,700)】
///   物理屏 2560x1440 @150%：
///     · unaware（虚拟空间 1707x960）：SetCursorPos(2000,700) 会被当**虚拟**坐标
///       -> 物理 (3000,1050) 超出屏 -> 夹到 2559 -> 读回 2559/1.5 ≈ 1706
///     · aware  （物理空间 2560x1440）：就是 2000
///   于是"读回值 > 1800"= 物理坐标空间，"≈1706" = 被虚拟化。
///   这与 GetSystemMetrics/屏幕DC 是两个独立手段，两者一致才敢下结论。
///
/// 【纪律】会短暂移动真实光标，读完立刻复原到原位。
struct InputSpace {
    int  readback_x = -1;
    int  readback_y = -1;
    bool physical   = false;
};

InputSpace probe_input_space() {
    InputSpace r;
    POINT save{};
    if (!::GetCursorPos(&save)) {
        return r;
    }
    if (!::SetCursorPos(2000, 700)) {
        return r;
    }
    POINT got{};
    if (::GetCursorPos(&got)) {
        r.readback_x = got.x;
        r.readback_y = got.y;
        r.physical   = (got.x > 1800);
    }
    ::SetCursorPos(save.x, save.y); // 复原，别动用户的光标
    return r;
}

void dump_input(const char* tag) {
    const InputSpace s = probe_input_space();
    std::printf("%-30s SetCursorPos(2000,700) 读回 = (%d,%d) -> 坐标空间 = %s\n",
                tag, s.readback_x, s.readback_y,
                s.physical ? "物理（2560 宽）" : "被虚拟化（1707 宽，已夹到边界）");
}

const char* hr_str(HRESULT hr) {
    static char buf[32];
    std::snprintf(buf, sizeof(buf), "0x%08lX", static_cast<unsigned long>(hr));
    return buf;
}

} // namespace

int main() {
    std::printf("本机：物理屏 2560x1440，缩放 150%%\n"
                "  unaware  -> GetSystemMetrics/屏幕DC 都是 1707x960\n"
                "  aware    -> 都是 2560x1440\n\n");

    dump("1) 进程刚启动");
    dump_input("   输入坐标空间");

    // ---- 2) 复刻 dxgi_capturer.cpp 的设备创建（标志完全相同）----
    const D3D_FEATURE_LEVEL want[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
    ComPtr<ID3D11Device>        device;
    ComPtr<ID3D11DeviceContext> ctx;
    HRESULT hr = ::D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
                                     want, _countof(want), D3D11_SDK_VERSION,
                                     &device, nullptr, &ctx);
    std::printf("\n   D3D11CreateDevice -> %s\n", hr_str(hr));
    dump("2) D3D11CreateDevice 后");

    if (SUCCEEDED(hr)) {
        // ---- 3) 复刻 adapter -> output -> DuplicateOutput ----
        ComPtr<IDXGIDevice> dxgi_dev;
        hr = device.As(&dxgi_dev);
        ComPtr<IDXGIAdapter> adapter;
        if (SUCCEEDED(hr)) hr = dxgi_dev->GetAdapter(&adapter);
        ComPtr<IDXGIOutput> output;
        if (SUCCEEDED(hr)) hr = adapter->EnumOutputs(0, &output);
        ComPtr<IDXGIOutput1> output1;
        if (SUCCEEDED(hr)) hr = output.As(&output1);
        ComPtr<IDXGIOutputDuplication> dup;
        if (SUCCEEDED(hr)) hr = output1->DuplicateOutput(device.Get(), &dup);
        std::printf("   DuplicateOutput   -> %s\n", hr_str(hr));
        dump("3) DuplicateOutput 后");
        // 关键一问：被顶成 aware 之后，**输入**是否也变成物理坐标？
        // 若是，则 §6.13 那条"输入仍被虚拟化 -> 点击系统性偏移"的告警在此路径下**不成立**。
        dump_input("   输入坐标空间");

        // ---- 4) 反证：此刻再想显式改成 per-monitor，会不会被拒？----
        //      若返回 FALSE + ERROR_ACCESS_DENIED，说明进程级 awareness 已被别的东西定死过。
        ::SetLastError(0);
        const BOOL ok = ::SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        std::printf("   SetProcessDpiAwarenessContext(per-monitor-v2) -> %s, GetLastError=%lu\n",
                    ok ? "TRUE" : "FALSE", static_cast<unsigned long>(::GetLastError()));
        dump("4) 尝试显式改 per-monitor 后");

        // ---- 5) 线程级覆盖仍然有效吗？（对比：显式切 unaware / per-monitor）----
        if (::SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_UNAWARE)) {
            dump("5a) 线程显式设 unaware");
            // 反证：线程级 unaware 会把**输入**也按虚拟化算吗？若会，说明输入坐标
            // 完全由调用线程的 DPI 上下文决定 —— 那么"抓屏与输入是否同一空间"就完全
            // 取决于两件事用的是不是同一个上下文。这是后面所有坐标一致性的根据。
            dump_input("   输入坐标空间");
        }
        if (::SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)) {
            dump("5b) 线程显式设 per-monitor-v2");
        }
    }

    std::printf("\n判定要点：\n"
                "  · 第 2/3 步后若 GetSystemMetrics 由 1707 变 2560 -> **D3D11/DXGI 把进程顶成了 aware**；\n"
                "  · 第 4 步若被拒（ACCESS_DENIED）-> 反向印证 awareness 已被该副作用定死；\n"
                "  · 级别字段告诉你顶成的是 SYSTEM_DPI_AWARE 还是 PER_MONITOR_DPI_AWARE。\n");
    return 0;
}
