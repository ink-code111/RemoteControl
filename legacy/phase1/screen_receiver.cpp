#include "screen_receiver.hpp"
#include "logger.hpp"

namespace rc::client {

ScreenReceiver::ScreenReceiver(NetworkClient& net, FrameCallback on_frame, ClosedCallback on_closed)
    : net_(net), on_frame_(std::move(on_frame)), on_closed_(std::move(on_closed)) {}

ScreenReceiver::~ScreenReceiver() {
    stop(); // RAII：即使上层忘记 stop，析构也保证线程不悬挂
}

void ScreenReceiver::start() {
    if (running_.exchange(true)) {
        return;
    }
    thread_ = std::thread(&ScreenReceiver::loop, this);
}

void ScreenReceiver::stop() {
    running_ = false;
    if (thread_.joinable()) {
        thread_.join();
    }
}

void ScreenReceiver::loop() {
    // 64KB 读块 + StreamDecoder 增量拼包，替代旧版一次性 malloc 10MB
    std::vector<char> buf(64 * 1024);

    while (running_) {
        // 1) 发送屏幕请求（业务流程与旧版一致：请求-应答式拉流）
        const proto::Packet request{proto::Cmd::Screen, {}};
        if (!net_.send_packet(request)) {
            RC_LOG_WARN("send screen request failed");
            break;
        }

        // 2) 阻塞接收，直到凑出一个完整的 Screen 帧（StreamDecoder
        //    天然处理粘包/半包；旧版直接把一次 recv 当一个完整包，
        //    大帧到达时必然解析失败）
        bool got_frame = false;
        while (running_ && !got_frame) {
            const int n = net_.recv_some(buf.data(), static_cast<int>(buf.size()));
            if (n <= 0) {
                RC_LOG_INFO("connection closed while receiving frame");
                running_ = false;
                break;
            }
            decoder_.feed(buf.data(), static_cast<std::size_t>(n));
            while (auto pkt = decoder_.next()) {
                if (pkt->cmd == proto::Cmd::Screen) {
                    on_frame_(std::move(pkt->body)); // 回调 UI 层渲染
                    got_frame = true;
                    break;
                }
                // 其他类型的包（如心跳应答）在此忽略
            }
        }
    }

    if (on_closed_) {
        on_closed_();
    }
}

} // namespace rc::client
