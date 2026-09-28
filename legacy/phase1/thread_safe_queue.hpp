#pragma once
// ============================================================
// 线程安全队列：替代 PostThreadMessage 的线程间通信原语
//
// 为什么不再用 PostThreadMessage：
//   1) 只能传两个指针宽度的参数，类型不安全，所有权只能靠约定；
//   2) 消息队列有系统上限（默认 10000），屏幕帧洪峰时 PostMessage
//      会静默失败，包直接丢失且无人知晓；
//   3) 线程没有消息循环就收不到消息，旧代码为此给每个工作线程
//      强行 GetMessage —— 把“线程”当“窗口”用，语义混乱。
// std::mutex + condition_variable 版本：类型安全、无上限、
// 支持优雅关闭（close() 唤醒所有等待者），杜绝“线程卡死收不到退出通知”。
// 第二阶段网络层切换 Asio 后，此队列继续作为 IO 线程 -> 业务线程的通道。
// ============================================================

#include <condition_variable>
#include <deque>
#include <mutex>
#include <optional>
#include <utility>

namespace rc {

template <typename T>
class ThreadSafeQueue {
public:
    void push(T value) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            queue_.push_back(std::move(value));
        }
        cv_.notify_one();
    }

    // 阻塞取出。队列已 close 且为空时返回 nullopt —— 工作线程据此退出。
    std::optional<T> pop() {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this] { return closed_ || !queue_.empty(); });
        if (queue_.empty()) {
            return std::nullopt;
        }
        T value = std::move(queue_.front());
        queue_.pop_front();
        return value;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            closed_ = true;
        }
        cv_.notify_all();
    }

private:
    std::mutex              mutex_;
    std::condition_variable cv_;
    std::deque<T>           queue_;
    bool                    closed_ = false;
};

} // namespace rc
