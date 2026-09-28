#pragma once
// ============================================================================
//  frame.hpp —— 协议帧头与切流（v2）
//
//  【替代了什么】
//    旧版：手写的 PacketHeader 结构体 + #pragma pack(1) + 固定长度接收缓冲区
//          + 手工 index/memmove 的粘包处理。
//    新版：显式小端编解码的 8 字节帧头，配 asio::async_read 的"读满 N 字节"语义。
//
//  【为什么不用 #pragma pack 直接 memcpy 结构体】
//    1) #pragma pack(1) 是编译器扩展，跨编译器/跨架构不可移植；一旦有人漏写就会静默错位；
//    2) memcpy 出来的多字节整数是"主机字节序"，x86 与 ARM/网络字节序解释不同；
//    3) 结构体布局随编译器版本可能变化，协议一旦上线就再也改不动。
//    改成逐字节显式读写后，协议字节流在任何平台都是同一个定义，且不会有对齐陷阱。
//
//  【帧格式】
//     0        2        3        4                      8
//     +--------+--------+--------+----------------------+-----------------+
//     | magic  | ver    | flags  | payload_len (LE u32) | FlatBuffers 载荷 |
//     | u16 LE | u8     | u8     |                      |                 |
//     +--------+--------+--------+----------------------+-----------------+
//      magic = 0x4352（字节流里是 0x52 0x43，即 ASCII "RC"）
//      ver   = 协议主版本；不一致时服务端直接拒绝，避免用错 schema 解析
//      flags = 保留位，当前必须为 0（为将来"压缩/加密"标记预留）
//
//  帧头只负责「切流」，业务语义全部交给 FlatBuffers Envelope —— 职责不重叠。
// ============================================================================

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace rc::net {

/// 帧魔数：字节流中为 'R'(0x52) 'C'(0x43)，便于抓包时肉眼辨认与快速丢弃垃圾数据
inline constexpr std::uint16_t kFrameMagic = 0x4352;

/// 协议主版本（破坏性变更时 +1；追加字段不算破坏性变更）
inline constexpr std::uint8_t kProtocolMajor = 2;

/// 帧头固定 8 字节
inline constexpr std::size_t kFrameHeaderSize = 8;

/// 单帧载荷上限（64MB）。防止对端（或攻击者）声明一个巨大长度导致我们把内存吃满。
/// 屏幕帧最大也就几 MB，64MB 已非常宽松。
inline constexpr std::uint32_t kMaxPayloadBytes = 64u * 1024u * 1024u;

struct FrameHeader {
    std::uint16_t magic       = kFrameMagic;
    std::uint8_t  version     = kProtocolMajor;
    std::uint8_t  flags       = 0;
    std::uint32_t payload_len = 0;
};

enum class FrameDecodeError {
    kOk = 0,
    kBadMagic,      ///< 不是本协议的流（错连、端口被别的服务占用、或对端发垃圾）
    kBadVersion,    ///< 主版本不匹配
    kTooLarge,      ///< 声明长度超过上限
    kBadFlags,      ///< 保留位被使用（当前版本不允许）
};

const char* to_string(FrameDecodeError e) noexcept;

struct FrameDecodeResult {
    FrameDecodeError error = FrameDecodeError::kOk;
    FrameHeader      header{};

    bool ok() const noexcept { return error == FrameDecodeError::kOk; }
};

/// 编码帧头为 8 字节（显式小端，与主机字节序无关）
std::array<std::uint8_t, kFrameHeaderSize> encode_header(std::uint32_t payload_len,
                                                         std::uint8_t  flags = 0,
                                                         std::uint8_t  version = kProtocolMajor) noexcept;

/// 从 8 字节缓冲区解出帧头。任何非法情况都返回具体错误，调用方据此关闭连接并记日志。
FrameDecodeResult decode_header(const std::uint8_t* data) noexcept;

/// 把「帧头 + 载荷」拼成一个连续缓冲区。
///
/// 【为什么拼成一个连续包再发，而不是两段 writev】
///   发送侧只做一次 async_write，就不存在"帧头发出去了、载荷还没发"的中间态，
///   也就不需要 send 之间的互斥锁（旧版 send_mutex_ 就是在补这个洞）。
///   代价是一次内存拷贝，第三阶段可用单次分配的缓冲区池消掉。
std::vector<std::uint8_t> pack_frame(const std::uint8_t* payload, std::size_t payload_len);

} // namespace rc::net
