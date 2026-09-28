#pragma once
// ============================================================
// 增量式流解码器：彻底解决 TCP 粘包 / 半包
//
// 旧代码在 recv 循环里手工维护 index + memmove 缓冲区，逻辑分散、
// 极易出错（原始版本就因此出过越界）。
// 这里把“积累字节流 -> 提取完整帧”封装为独立组件：
//   - 任何 IO 模型都能复用（第二阶段接入 Asio 异步读时直接复用本类）；
//   - 内置失步重同步与超长帧防御，被污染的字节流不会拖垮服务。
// 注：vector 头部 erase 是 O(n)，第一阶段帧频下可接受；
//     第三阶段高帧率场景将替换为环形缓冲区。
// ============================================================

#include "packet.hpp"

#include <optional>

namespace rc::proto {

class StreamDecoder {
public:
    // 送入新收到的字节（来自 recv / async_read 的任意长度数据块）
    void feed(const char* data, std::size_t len) {
        buf_.insert(buf_.end(), data, data + len);
    }

    // 尝试取出一个完整包；数据不完整时返回 nullopt（半包，等待更多数据）
    std::optional<Packet> next() {
        for (;;) {
            if (buf_.size() < kHeaderSize) {
                return std::nullopt;
            }
            if (read_u32_le(buf_.data()) != kMagic) {
                // 字节流失步（残留脏数据/被篡改）：滑动一个字节重新搜索帧头
                buf_.erase(buf_.begin());
                continue;
            }
            const std::uint32_t body_len = read_u32_le(buf_.data() + 8);
            if (body_len > kMaxBodySize) {
                // 非法长度：丢弃这个伪帧头，向后重新同步
                buf_.erase(buf_.begin(), buf_.begin() + 4);
                continue;
            }
            if (buf_.size() < kHeaderSize + body_len) {
                return std::nullopt; // 半包
            }

            Packet pkt;
            pkt.cmd = static_cast<Cmd>(read_u32_le(buf_.data() + 4));
            pkt.body.assign(buf_.begin() + static_cast<std::ptrdiff_t>(kHeaderSize),
                            buf_.begin() + static_cast<std::ptrdiff_t>(kHeaderSize + body_len));
            buf_.erase(buf_.begin(),
                       buf_.begin() + static_cast<std::ptrdiff_t>(kHeaderSize + body_len));
            return pkt;
        }
    }

private:
    std::vector<char> buf_;
};

} // namespace rc::proto
