#pragma once
// ============================================================
// 收图线程：请求 -> 接收 -> 回调
//
// 通过 std::function 回调把“网络层”与“UI 层”解耦：
// RemoteWindow 不需要知道数据从哪个线程、哪条链路来，只管渲染。
// 这是第四阶段观察者模式 / 事件总线的前身。
//
// RAII：析构自动 stop()，杜绝旧版“窗口关了线程还在 recv”的悬挂。
// ============================================================

#include "network_client.hpp"
#include "stream_decoder.hpp" // proto::StreamDecoder（收图线程独占使用）

#include <atomic>
#include <functional>
#include <thread>
#include <vector>

namespace rc::client {

class ScreenReceiver {
public:
    using FrameCallback  = std::function<void(std::vector<char>)>; // 收到一帧 PNG 字节
    using ClosedCallback = std::function<void()>;                  // 连接断开

    ScreenReceiver(NetworkClient& net, FrameCallback on_frame, ClosedCallback on_closed);
    ~ScreenReceiver();

    ScreenReceiver(const ScreenReceiver&)            = delete;
    ScreenReceiver& operator=(const ScreenReceiver&) = delete;

    void start();
    void stop(); // 设置停止标志并 join 线程（需在 socket 关闭后调用）

private:
    void loop();

    NetworkClient&  net_;
    FrameCallback   on_frame_;
    ClosedCallback  on_closed_;
    std::atomic<bool> running_{false};
    std::thread     thread_;
    proto::StreamDecoder decoder_; // 仅收图线程访问
};

} // namespace rc::client
