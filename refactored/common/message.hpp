#pragma once
// ============================================================================
//  message.hpp —— 一个"待发送消息"的完整表示：帧头 + FlatBuffers 载荷
//
//  【为什么要单独抽这一层】
//    frame.hpp 只知道字节，proto_codec.hpp 只知道 FlatBuffers，
//    而"发出去"这件事需要把两者拼起来，并且必须解决一个生命周期问题：
//      async_write 是异步的 —— 函数返回后数据还必须活着。
//    于是把帧头和载荷放进一个可以 shared_ptr 持有的对象里，
//    由写队列负责保活；回调里再释放。
//
//  【零拷贝发送】
//    FlatBuffers 的 FlatBufferBuilder 释放出来的 DetachedBuffer 本身就是
//    一段连续、对齐、由它自己管理的内存。我们不再把整包拷进新 vector，
//    而是把它和 8 字节帧头组成两段 const_buffer，用 asio 的
//    gather-write（scatter/gather I/O）一次系统调用发出去。
//    → 大屏幕帧少一次整包 memcpy（几 MB 级别的拷贝，收益很直接）。
// ============================================================================

#include "frame.hpp"

#include <flatbuffers/flatbuffers.h>

#include <asio/buffer.hpp>

#include <array>
#include <cstdint>
#include <memory>

namespace rc::net {

class OutgoingMessage {
public:
    /// 接管一段已经构建好的 FlatBuffers 载荷（零拷贝），并生成对应帧头。
    explicit OutgoingMessage(flatbuffers::DetachedBuffer body)
        : body_(std::move(body)) {
        header_ = encode_header(static_cast<std::uint32_t>(body_.size()));
    }

    OutgoingMessage(const OutgoingMessage&)            = delete;
    OutgoingMessage& operator=(const OutgoingMessage&) = delete;
    OutgoingMessage(OutgoingMessage&&)                 = default;
    OutgoingMessage& operator=(OutgoingMessage&&)      = default;
    ~OutgoingMessage()                                 = default;

    /// 两段缓冲区：帧头 + 载荷。asio 会按顺序把它们全部写完，
    /// 因此不会出现"帧头发出去了、载荷还没发"的中间态。
    std::array<asio::const_buffer, 2> buffers() const noexcept {
        return { asio::buffer(header_.data(), header_.size()),
                 asio::buffer(body_.data(), body_.size()) };
    }

    std::size_t total_bytes() const noexcept { return header_.size() + body_.size(); }

private:
    std::array<std::uint8_t, kFrameHeaderSize> header_{};
    flatbuffers::DetachedBuffer                body_;
};

using OutgoingMessagePtr = std::shared_ptr<OutgoingMessage>;

} // namespace rc::net
