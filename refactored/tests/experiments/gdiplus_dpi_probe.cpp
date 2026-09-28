// ============================================================
// 实验：GdiplusStartup 会不会改变进程/线程的 DPI 上下文？
//
// 【为什么必须查清 —— 这条链上挂着一个可能推翻既有结论的疑点】
//   改动后实测到一对**互相矛盾**的事实（同一台机器、同一份代码）：
//     · 启动期报告（main 线程，在 AsioServer 构造**之后**）读 GetSystemMetrics → 2560×1440
//     · 抓屏线程实际抓到的帧（同一进程内）                          → 1707×960
//   而改动只是把报告从"构造之前"挪到了"构造之后"，中间多出来的东西是
//   AsioServer 的构造 —— 它里面调用了 GdiplusStartup。
//
//   GDI+（早期版本）在初始化时为了提高高分屏下的渲染正确性，会调用
//   `SetProcessDPIAware()`。如果这条路径在本机真的生效，后果很重：
//     · 服务端的 `dpi_aware=false` 就**不是"不感知"** —— GDI+ 把它顶成了 system-aware；
//     · §6.12 那轮"dpi_aware 关 / 开"的 A/B 就要重新审：若两轮其实都是 aware，
//       那测到的 +69% 就不是"缩放开销"，整条推论链作废。
//   （不过抓屏线程给的是 1707×960，与"进程被顶成 aware"矛盾 —— 所以这里是
//     事实冲突，不是结论，只能靠实验分辨。）
//
// 用法：直接跑，看两行输出。
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
#include <atlimage.h>   // 必须在 gdiplus.h 之前：它带进 OLE 头（IStream/PROPID），
                        // 否则 WIN32_LEAN_AND_MEAN 下 gdiplus 头会成片报错
#include <gdiplus.h>

#include <cstdio>

namespace {

const char* ctx_name(DPI_AWARENESS_CONTEXT c) {
    if (c == DPI_AWARENESS_CONTEXT_UNAWARE)              return "unaware";
    if (c == DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)         return "system-aware";
    if (c == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE)    return "per-monitor";
    if (c == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) return "per-monitor-v2";
    if (c == DPI_AWARENESS_CONTEXT_UNAWARE_GDISCALED)    return "unaware-gdiscaled";
    return "(other)";
}

void dump(const char* tag) {
    // GetThreadDpiAwarenessContext 在"线程未被显式设置过"时返回**进程**的有效上下文，
    // 所以它同时回答了"线程现在按什么算"和"进程默认是什么"。
    std::printf("%-26s 线程/进程上下文 = %-16s GetSystemMetrics = %dx%d\n",
                tag, ctx_name(::GetThreadDpiAwarenessContext()),
                ::GetSystemMetrics(SM_CXSCREEN), ::GetSystemMetrics(SM_CYSCREEN));
}

} // namespace

int main() {
    std::printf("本机物理屏应当由 Display Settings 决定；缩放 150%% 时\n"
                "  unaware 上下文 -> 1707x960（物理画面左上角 1:1 裁剪）\n"
                "  aware   上下文 -> 2560x1440（物理像素）\n\n");

    dump("1) 进程刚启动");

    ULONG_PTR                     token = 0;
    Gdiplus::GdiplusStartupInput  input;
    const Gdiplus::Status         st = Gdiplus::GdiplusStartup(&token, &input, nullptr);
    std::printf("   GdiplusStartup -> %d（0 = Ok）\n", static_cast<int>(st));

    dump("2) GdiplusStartup 之后");

    // 再验证一次"显式设 unaware 是否仍能压回去" —— 若此刻 context 变成 system-aware
    // 且这里能压回 unaware，说明进程默认值确实被改过（显式设置只是临时覆盖）。
    if (::SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_UNAWARE) != nullptr) {
        dump("3) 显式设 unaware 后");
    }

    if (token != 0) {
        Gdiplus::GdiplusShutdown(token);
    }
    dump("4) GdiplusShutdown 之后");
    return 0;
}
