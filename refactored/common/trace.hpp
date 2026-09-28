#pragma once
// ============================================================================
//  trace.hpp —— 排障打点（无锁、每次调用立即落盘）
//
//  【它解决什么问题】
//    排查"进程卡死/整体停摆"时，最大的干扰来自日志设施本身：
//    spdlog 的每个 sink 内部有互斥锁，只要有一个线程卡在写 sink 上，
//    其余线程的日志会全部堵住 —— 外在现象和"业务代码卡住"一模一样，
//    你无法判断究竟是业务停了还是日志停了。
//
//    这个打点器刻意做得"笨"：每次 open/write/close，不共享任何状态、
//    不缓存、不加锁。它的输出因此可以当作可信的执行轨迹：
//      · 某条打点没出现 → 代码确实没执行到那一行（不是日志丢了）；
//      · 打点还在涨而日志停了 → 卡的是日志设施，而不是业务逻辑。
//    它还带线程号，能直接看出"是哪个线程停住了、其他线程是否还活着"。
//
//    这个工具不是凭空的：项目里真的踩到过"服务端能连上但毫无响应"，
//    最后就是靠它把范围从 socket/accept 一路缩到"卡在写日志"上的。
//
//  【默认关闭】
//    每条打点都是一次文件开关，非常慢。所以默认编译为 no-op，
//    只有显式打开开关时才生效：
//        cmake -DRC_ENABLE_TRACE=ON ...       （见 cmake 选项）
//    或者给单个源文件加 /DRC_ENABLE_TRACE。
//
//  【输出位置】logs/trace.log（相对于进程工作目录，追加写）
// ============================================================================

#include <cstdio>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace rc::debug {

namespace detail {

/// 追加打开文件。
///
/// 【为什么要绕这一下】MSVC 把 fopen 列入"不安全 CRT 函数"（C4996）。
/// 关键在于它有两种强度：
///   · 命令行 ninja 构建：只有 /W4  → 仅告警，构建照过；
///   · VS 工程构建：开了 /sdl（<SDLCheck>）→ **同一条告警升级为错误**。
/// 于是同一个文件出现"cmake 能过、VS 里 F7 报错"的假差异 —— 本项目真的
/// 在 VS 里撞到过（trace.hpp 的 fopen 报 2 个 C4996，两个包含它的 TU 各一次）。
///
/// 两种收场方式里选了前者：
///   1) 换成 fopen_s，从根上不调用被禁函数（本实现）；
///   2) 用 _CRT_SECURE_NO_WARNINGS 把告警压掉 —— 那是把问题藏起来，
///      下一个用 fopen 的人会再踩一遍。
/// 非 MSVC 平台没有 fopen_s，仍走标准 fopen。
inline FILE* open_append(const char* path) {
#if defined(_MSC_VER)
    FILE* f = nullptr;
    if (::fopen_s(&f, path, "a") != 0) {
        return nullptr; // 打开失败（目录不存在 / 无权限）：调用方当作 no-op
    }
    return f;
#else
    return std::fopen(path, "a");
#endif
}

} // namespace detail

/// 打一条轨迹。a/b 用来附带上下文（会话号、错误码……）。
inline void trace_point(const char* tag, unsigned long a = 0, unsigned long b = 0) {
    if (FILE* f = detail::open_append("logs/trace.log")) {
        std::fprintf(f, "[tid=%lu] %s a=%lu b=%lu\n",
                     static_cast<unsigned long>(::GetCurrentThreadId()), tag, a, b);
        std::fclose(f); // fclose 触发刷盘，保证进程被强杀也能留下记录
    }
}

} // namespace rc::debug

#ifdef RC_ENABLE_TRACE
#define RC_TRACE(...) ::rc::debug::trace_point(__VA_ARGS__)
#else
/// 关闭时展开成空语句：不产生任何代码，参数也不会被求值
#define RC_TRACE(...) ((void)0)
#endif
