#pragma once
// ============================================================================
//  asio_common.hpp —— Asio 统一入口与错误处理助手
//
//  【为什么用 standalone Asio 而不是 Boost.Asio】
//    1) Boost 是巨型依赖（源码包上百 MB），而 standalone Asio 只有几 MB 头文件，
//       且功能上与 Boost.Asio 是同一份代码的镜像 —— 拿到的是同样的异步模型；
//    2) 不引入 Boost 意味着不用维护 Boost 构建，工程依赖面小一个数量级；
//    3) 头文件依赖方式在 CMake 与 vcxproj 里都只需加一个 include 路径。
//    若将来项目里已经有 Boost，把 ASIO_STANDALONE 换成 #include <boost/asio.hpp>
//    加上 namespace 别名即可，业务代码基本不用改。
//
//  【必须在任何 asio 头之前定义 ASIO_STANDALONE】
//    否则 Asio 会去 include Boost 的东西，直接编译失败。
// ============================================================================

#ifndef ASIO_STANDALONE
#define ASIO_STANDALONE
#endif

// Asio 在 Windows 上需要的最低系统版本（0x0601=Win7，0x0A00=Win10）。
// 不定义的话某些 asio 版本会报 winsock2 相关的编译错误。
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif

#include <asio.hpp>

// 2B 第二刀 TLS：is_disconnect() 要能认出"TLS 对端没发 close_notify 就断了"这一类。
// 该头只依赖 asio 自身的错误码定义（虽然内部会带上 openssl 的类型头），
// 不会改变本文件"Asio 统一入口"的定位。
#include <asio/ssl/error.hpp>

#include "clock.hpp"

#include <chrono>
#include <cstdint>
#include <string>
#include <system_error>

namespace rc::net {

using tcp          = asio::ip::tcp;
using error_code   = std::error_code;          // standalone Asio 默认即 std::error_code
using io_context   = asio::io_context;
using steady_timer = asio::steady_timer;

/// 连接/收发是否属于"对端正常或异常关闭"这一类。
/// 这类错误不需要报 error 级别日志 —— 客户端关窗口、网络抖动都会走到这里。
inline bool is_disconnect(const error_code& ec) noexcept {
    return ec == asio::error::eof
        || ec == asio::error::connection_reset
        || ec == asio::error::connection_aborted
        || ec == asio::error::operation_aborted
        || ec == asio::error::broken_pipe
        || ec == asio::error::not_connected
        // 【2B 第二刀 TLS】对端在没发 close_notify 的情况下断了（进程被强杀、
        // 网线拔掉、或者对端只是 close 了 TCP 而不走 TLS 的优雅关闭）。
        // 语义上就是"对端关闭"，**不是**故障 —— 不认它的话，每一次正常的客户端
        // 关窗口都会在服务端日志里留下一行 WARN，而且会污染按行解析日志的判据。
        // ⚠️ 但要注意反向风险：把"真截断攻击"也当成正常关闭。本工具不在此处设防 ——
        //    TLS 层已保证内容完整性（截断只能丢掉尾部，改不了已收到的字节）。
        || ec == asio::ssl::error::stream_truncated;
}

/// 给日志用的可读描述：把 asio 的错误码转成 "名字(值): 描述"
inline std::string describe(const error_code& ec) {
    if (!ec) return "ok";
    return ec.message() + " [asio category value=" + std::to_string(ec.value()) + "]";
}

// now_ms() / wall_ms() / steady_now() 见 clock.hpp —— 纯 chrono 工具单独成头，
// 协议层不必为拿一个时间戳而把整个 Asio 拖进编译单元。

} // namespace rc::net

// ---------------------------------------------------------------------------
//  把两个最常用的网络类型别名提升到 rc 命名空间。
//
//  业务代码在 rc::server / rc::client 里写 tcp::socket、error_code 即可：
//  C++ 的名字查找会先找本命名空间，再依次找外层（rc::server -> rc），
//  于是在 rc 里声明的别名正好被它们看见，不必到处写 net:: 前缀。
//
//  为什么不用 using namespace rc::net：
//    那会把 asio 的名字（post、bind_executor、async_write...）全撒进整个 rc，
//    任何一处重名或误用都难排查。这里只提升 2 个类型别名，范围最小、意图明确。
// ---------------------------------------------------------------------------
namespace rc {

using net::error_code;
using net::tcp;

} // namespace rc
