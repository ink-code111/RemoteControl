#include "frame.hpp"

#include <cstring>

namespace rc::net {
namespace {

// 显式小端读写：不依赖主机字节序，也不用 memcpy 结构体（避免对齐/填充陷阱）
inline void put_u16_le(std::uint8_t* p, std::uint16_t v) noexcept {
    p[0] = static_cast<std::uint8_t>(v & 0xFFu);
    p[1] = static_cast<std::uint8_t>((v >> 8) & 0xFFu);
}

inline void put_u32_le(std::uint8_t* p, std::uint32_t v) noexcept {
    p[0] = static_cast<std::uint8_t>(v & 0xFFu);
    p[1] = static_cast<std::uint8_t>((v >> 8) & 0xFFu);
    p[2] = static_cast<std::uint8_t>((v >> 16) & 0xFFu);
    p[3] = static_cast<std::uint8_t>((v >> 24) & 0xFFu);
}

inline std::uint16_t get_u16_le(const std::uint8_t* p) noexcept {
    return static_cast<std::uint16_t>(static_cast<std::uint16_t>(p[0]) |
                                      (static_cast<std::uint16_t>(p[1]) << 8));
}

inline std::uint32_t get_u32_le(const std::uint8_t* p) noexcept {
    return static_cast<std::uint32_t>(p[0]) |
           (static_cast<std::uint32_t>(p[1]) << 8) |
           (static_cast<std::uint32_t>(p[2]) << 16) |
           (static_cast<std::uint32_t>(p[3]) << 24);
}

} // namespace

const char* to_string(FrameDecodeError e) noexcept {
    switch (e) {
    case FrameDecodeError::kOk:         return "ok";
    case FrameDecodeError::kBadMagic:   return "bad magic (not an RC frame)";
    case FrameDecodeError::kBadVersion: return "unsupported protocol major version";
    case FrameDecodeError::kTooLarge:   return "declared payload exceeds limit";
    case FrameDecodeError::kBadFlags:   return "reserved flags must be zero";
    }
    return "unknown";
}

std::array<std::uint8_t, kFrameHeaderSize> encode_header(std::uint32_t payload_len,
                                                         std::uint8_t  flags,
                                                         std::uint8_t  version) noexcept {
    std::array<std::uint8_t, kFrameHeaderSize> out{};
    put_u16_le(out.data() + 0, kFrameMagic);
    out[2] = version;
    out[3] = flags;
    put_u32_le(out.data() + 4, payload_len);
    return out;
}

FrameDecodeResult decode_header(const std::uint8_t* data) noexcept {
    FrameDecodeResult r{};
    r.header.magic   = get_u16_le(data + 0);
    r.header.version = data[2];
    r.header.flags   = data[3];
    r.header.payload_len = get_u32_le(data + 4);

    // 顺序有讲究：先验魔数（能立刻识别"连错了服务"这类问题），再验版本，最后才谈长度
    if (r.header.magic != kFrameMagic) {
        r.error = FrameDecodeError::kBadMagic;
        return r;
    }
    if (r.header.version != kProtocolMajor) {
        r.error = FrameDecodeError::kBadVersion;
        return r;
    }
    if (r.header.flags != 0) {
        r.error = FrameDecodeError::kBadFlags;
        return r;
    }
    if (r.header.payload_len > kMaxPayloadBytes) {
        r.error = FrameDecodeError::kTooLarge;
        return r;
    }
    return r;
}

std::vector<std::uint8_t> pack_frame(const std::uint8_t* payload, std::size_t payload_len) {
    std::vector<std::uint8_t> out(kFrameHeaderSize + payload_len);
    const auto hdr = encode_header(static_cast<std::uint32_t>(payload_len));
    std::memcpy(out.data(), hdr.data(), kFrameHeaderSize);
    if (payload_len > 0 && payload != nullptr) {
        std::memcpy(out.data() + kFrameHeaderSize, payload, payload_len);
    }
    return out;
}

} // namespace rc::net
