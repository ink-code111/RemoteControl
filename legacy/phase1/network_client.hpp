#pragma once
// ============================================================
// 客户端网络门面：封装“发送一个协议包”
//
// - 发送全程加锁：UI 线程（鼠标/键盘）与收图线程可能并发发送，
//   不加锁则 TCP 字节流交错损坏（旧版正是如此）；
// - send_all 处理部分发送，修复旧版大包被截断的隐患；
// - 第二阶段此类将演进为基于 Boost.Asio 的异步客户端
//  （心跳 / 自动重连 / TLS），调用方接口保持不变。
// ============================================================

#include "packet.hpp"
#include "winsock.hpp"

#include <mutex>
#include <vector>

namespace rc::client {

class NetworkClient {
public:
    explicit NetworkClient(net::TcpSocket socket);

    bool send_packet(const proto::Packet& pkt);
    bool send_packet(proto::Cmd cmd, const std::vector<char>& body);

    // 阻塞接收一段数据（第一阶段仅供收图线程使用）
    int recv_some(char* data, int len);

    void close();           // 关闭连接（幂等；会解除阻塞中的 recv）
    bool is_open() const;

private:
    net::TcpSocket socket_;
    std::mutex     send_mutex_;
};

} // namespace rc::client
