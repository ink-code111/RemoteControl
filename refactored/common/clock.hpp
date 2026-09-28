#pragma once
// ============================================================================
//  clock.hpp —— 时间戳工具（不依赖 asio，任何层都可安全包含）
//
//  【为什么要有单独一个头】
//    时间戳是协议层（proto_codec 要填 server_time_ms）、会话层（心跳/帧间隔）、
//    网络层（asio_common 的 RTT 计算）三处都要用的东西。
//    如果把它塞在 asio_common.hpp 里，proto 层为了拿一个 now_ms() 就得把整个
//    Asio（几千行模板）拖进来编，既拖慢编译又把「协议」和「IO」耦死。
//    抽成独立头后依赖方向是干净的：proto / session / client 都只依赖这里，
//    没有谁需要为了一个时间戳去依赖别人。
//
//  【两种时间的区别，别混用】
//    now_ms()  = steady_clock，单调递增，不受系统时间调整（NTP 校时、用户改表）影响。
//                用于计算「时间差」：RTT、帧间隔、超时判定。
//    wall_ms() = system_clock，Unix 纪元毫秒，可跨进程/跨机器比较。
//                用于要「对齐真实时间」的场合：日志时间轴、服务端时间展示。
//    跨进程传时间戳时必须用 wall_ms()：两个进程的 steady_clock 基准点完全不同，
//    拿 A 的 steady 值去和 B 的 steady 值相减是没有意义的。
// ============================================================================

#include <chrono>
#include <cstdint>

namespace rc::net {

/// 单调时钟毫秒（只用来做「差值」，不要跨进程比较绝对值）
inline std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

/// Unix 纪元毫秒（可跨进程/跨机器比较，用于协议里的 wall-clock 时间戳）
inline std::int64_t wall_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

/// 等价的 time_point，便于 asio steady_timer 计算到期时刻
using steady_time_point = std::chrono::steady_clock::time_point;

inline steady_time_point steady_now() {
    return std::chrono::steady_clock::now();
}

} // namespace rc::net
