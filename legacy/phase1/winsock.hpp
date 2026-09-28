#pragma once
// ============================================================
// Winsock RAII 封装
//
// 修复的旧代码缺陷：
//   1) WSAStartup 失败后照样继续创建 socket；
//   2) socket 关闭逻辑散布在各 return 路径，异常即泄漏；
//   3) send() 只判断返回值 >0，忽略“部分发送”——屏幕帧动辄几百 KB，
//      阻塞 socket 单次 send 也可能只发出一部分，旧代码实际会
//      把大帧截断，后续字节流整体错乱（这是“有时花屏/有时崩”的根源）；
//   4) 多线程对同一 socket 并发 send，TCP 字节流交错损坏（由各使用
//      方加 send_mutex_ 串行化，见 Session / NetworkClient）。
// ============================================================

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <WinSock2.h>
#include <WS2tcpip.h>

#include <cstdio>
#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>

namespace rc::net {

// RAII：构造即 WSAStartup，析构自动 WSACleanup，失败抛异常快速失败
class WinsockGuard {
public:
    WinsockGuard() {
        WSADATA data{};
        if (::WSAStartup(MAKEWORD(2, 2), &data) != 0) {
            throw std::runtime_error("WSAStartup failed");
        }
    }
    ~WinsockGuard() { ::WSACleanup(); }
    WinsockGuard(const WinsockGuard&)            = delete;
    WinsockGuard& operator=(const WinsockGuard&) = delete;
};

// RAII Socket：析构自动 closesocket；只可移动不可拷贝，
// 所有权关系在类型系统层面表达清楚，杜绝“两个地方以为都拥有它”。
class TcpSocket {
public:
    TcpSocket() = default;
    explicit TcpSocket(SOCKET s) : sock_(s) {}
    ~TcpSocket() { reset(); }

    TcpSocket(TcpSocket&& other) noexcept
        : sock_(std::exchange(other.sock_, INVALID_SOCKET)) {}
    TcpSocket& operator=(TcpSocket&& other) noexcept {
        if (this != &other) {
            reset();
            sock_ = std::exchange(other.sock_, INVALID_SOCKET);
        }
        return *this;
    }

    bool   valid() const { return sock_ != INVALID_SOCKET; }
    SOCKET get()   const { return sock_; }

    void reset() {
        if (valid()) {
            ::closesocket(sock_);
            sock_ = INVALID_SOCKET;
        }
    }

    // 阻塞接收一段数据。返回 0 = 对端正常关闭；SOCKET_ERROR = 出错。
    int recv_some(char* data, int len) {
        return ::recv(sock_, data, len, 0);
    }

    // 循环发送直到全部发出，修复旧版“部分发送截断大帧”的缺陷。
    // 返回 false 表示连接已断（调用方应结束会话）。
    bool send_all(const char* data, int len) {
        int sent = 0;
        while (sent < len) {
            const int n = ::send(sock_, data + sent, len - sent, 0);
            if (n == SOCKET_ERROR || n == 0) {
                return false;
            }
            sent += n;
        }
        return true;
    }

    // 阻塞 accept 一个连接（第二阶段将替换为 Asio async_accept）
    std::optional<TcpSocket> accept_one() const {
        sockaddr_storage addr{};
        int              len = static_cast<int>(sizeof(addr));
        SOCKET s = ::accept(sock_, reinterpret_cast<sockaddr*>(&addr), &len);
        if (s == INVALID_SOCKET) {
            return std::nullopt;
        }
        return TcpSocket(s);
    }

private:
    SOCKET sock_ = INVALID_SOCKET;
};

namespace detail {

inline std::string endpoint_text(const std::string& host, const std::string& port) {
    return host + ":" + port;
}

} // namespace detail

// 解析 host:port 并建立监听。失败抛异常（带上下文），替代旧版
// “bind 失败返回 -2，main 里只打印一句‘启动服务失败’”的黑盒行为。
inline TcpSocket make_listener(const std::string& host, std::uint16_t port, int backlog) {
    addrinfo hints{};
    hints.ai_family   = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    hints.ai_flags    = AI_PASSIVE;

    char port_str[16]{};
    std::snprintf(port_str, sizeof(port_str), "%u", static_cast<unsigned>(port));

    addrinfo* raw = nullptr;
    if (::getaddrinfo(host.c_str(), port_str, &hints, &raw) != 0 || raw == nullptr) {
        throw std::runtime_error("getaddrinfo failed: " + detail::endpoint_text(host, port_str));
    }
    // RAII 管理 getaddrinfo 结果，异常路径同样不泄漏
    std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> guard(raw, &::freeaddrinfo);

    for (addrinfo* p = raw; p != nullptr; p = p->ai_next) {
        TcpSocket s(::socket(p->ai_family, p->ai_socktype, p->ai_protocol));
        if (!s.valid()) {
            continue;
        }
        if (::bind(s.get(), p->ai_addr, static_cast<int>(p->ai_addrlen)) == 0 &&
            ::listen(s.get(), backlog) == 0) {
            return s;
        }
    }
    throw std::runtime_error("bind/listen failed: " + detail::endpoint_text(host, port_str));
}

// 连接到服务器（阻塞式；第二阶段将替换为 Asio 异步连接 + 心跳 + 自动重连）
inline TcpSocket connect_to(const std::string& host, std::uint16_t port) {
    addrinfo hints{};
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;

    char port_str[16]{};
    std::snprintf(port_str, sizeof(port_str), "%u", static_cast<unsigned>(port));

    addrinfo* raw = nullptr;
    if (::getaddrinfo(host.c_str(), port_str, &hints, &raw) != 0 || raw == nullptr) {
        throw std::runtime_error("getaddrinfo failed: " + detail::endpoint_text(host, port_str));
    }
    std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> guard(raw, &::freeaddrinfo);

    for (addrinfo* p = raw; p != nullptr; p = p->ai_next) {
        TcpSocket s(::socket(p->ai_family, p->ai_socktype, p->ai_protocol));
        if (!s.valid()) {
            continue;
        }
        if (::connect(s.get(), p->ai_addr, static_cast<int>(p->ai_addrlen)) == 0) {
            return s;
        }
    }
    throw std::runtime_error("connect failed: " + detail::endpoint_text(host, port_str));
}

} // namespace rc::net
