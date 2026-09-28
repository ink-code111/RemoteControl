#pragma once
// ============================================================================
//  proto_codec.hpp —— 协议编解码层（FlatBuffers Envelope 的构造与安全解析）
//
//  这一层是「线上字节」与「领域概念」的唯一交界点：
//    发送：领域对象 ──make_xxx()──► DetachedBuffer ──► OutgoingMessage ──► socket
//    接收：socket ──► 载荷字节 ──parse_envelope()──► 由 Session 翻译成领域对象
//
//  【安全要点】任何来自网络的载荷都必须先过 parse_envelope()，
//  它内部会跑 flatbuffers::Verifier —— 不可信输入里的偏移量在被使用前
//  必须整包校验，否则一个精心构造的 offset 就能让进程越界读甚至崩掉。
//
//  【零拷贝】ParsedEnvelope 里只存一个指向原缓冲区的指针，不做任何反序列化拷贝。
//  屏幕帧的像素/编码字节可以用 GetData() 直接读，全程不复制。
// ============================================================================

#include "frame.hpp"
#include "input_types.hpp"

#include <flatbuffers/flatbuffers.h>

#include <cstdint>
#include <string>
#include <string_view>

#include "rc_protocol_generated.h"

// 注意：这里不再写 `namespace v2 = rc::proto::v2;` 这类"自引用别名"。
// 生成的 rc_protocol_generated.h 已经在 rc::proto 下开了 namespace v2，
// 本文件又处在 rc::proto 内，加同名别名等于让名字 v2 在同一作用域里
// 既指命名空间又指别名 —— 编译器直接报 C2386 "当前范围内已存在具有该名称的符号"。
// 实际上也不需要别名：rc::proto 内的 v2:: 会自然查到嵌套命名空间 rc::proto::v2。
namespace rc::proto {

// ---------------------------------------------------------------------------
// 构造（全部返回 DetachedBuffer：它自己管理内存，可直接作为 asio 的写缓冲）
// ---------------------------------------------------------------------------

/// @param auth_token 【2B 认证】共享密钥；空 = 不提供凭据。
///        给了默认值是有意的：既有的调用点（探针 / 各种测试）本来就与
///        "未启用认证的服务端"对话，传空是正确的语义，不必逐个改动。
///        ⚠️ 它不会让"忘了配 token"静默通过 —— 服务端会明确拒绝并给出理由。
flatbuffers::DetachedBuffer make_hello(std::string_view client_name,
                                       std::string_view client_version,
                                       std::uint16_t    protocol_version,
                                       std::string_view auth_token = {});

/// @param role 【2B 第三刀 权限模型】服务端**授权**给这个会话的角色。
///        追加字段（id 最大），默认 `Control` —— 这个默认值方向是刻意的：
///        既有的调用点（探针 / 各种测试）本来就与"未启用权限模型的服务端"对话，
///        它们拿到 Control 才是正确的语义，不必逐个改动。
///        ⚠️ 只有 accepted=true 时它才有意义（被拒的会话没有权限可言）。
flatbuffers::DetachedBuffer make_hello_ack(std::uint32_t       session_id,
                                           bool                accepted,
                                           std::string_view    reject_reason,
                                           std::uint16_t       protocol_version,
                                           v2::Role            role = v2::Role::Control);

flatbuffers::DetachedBuffer make_ping(std::uint32_t seq, std::int64_t client_time_ms);

flatbuffers::DetachedBuffer make_pong(std::uint32_t seq,
                                      std::int64_t  client_time_ms,
                                      std::int64_t  server_time_ms);

flatbuffers::DetachedBuffer make_mouse(const rc::input::MouseEvent& ev);

flatbuffers::DetachedBuffer make_keyboard(const rc::input::KeyboardEvent& ev);

flatbuffers::DetachedBuffer make_screen_request(std::int32_t max_width, std::int32_t quality);

/// 差异帧的脏矩形（整屏坐标系，原点左上）。
/// 传 nullptr 或 w/h 为 0 = "data 是完整一帧"，客户端整体替换。
struct DirtyRect {
    std::int32_t x = 0;
    std::int32_t y = 0;
    std::int32_t w = 0;
    std::int32_t h = 0;
};

/// 注意：屏幕图像字节会被拷进 FlatBuffers 的 buffer（这是 schema 内联 vector 的必然）。
/// 真正省掉的拷贝在别处：发送时不再整包再拷一遍，接收时不需要"反序列化出对象"。
/// @param dirty 差异帧的脏矩形；nullptr = 整帧。见 DirtyRect 的说明。
/// @param input_epoch 【输入→显示延迟】本帧像素**开始采集之前**服务端已应用过的输入事件
///        累计数（下界语义）。0 = 不携带观测（老调用方/测试替身），客户端也不会配出样本。
///        为什么不直接传时间戳：跨进程比较绝对时刻需要时钟同步，而序号不需要 ——
///        客户端只要知道"我什么时候发的"和"这一帧什么时候被画上去"，两者都在它自己的时钟里。
flatbuffers::DetachedBuffer make_screen_frame(std::int32_t     width,
                                              std::int32_t     height,
                                              std::int64_t     timestamp_ms,
                                              v2::ImageFormat  format,
                                              const std::uint8_t* data,
                                              std::size_t      data_len,
                                              const DirtyRect* dirty = nullptr,
                                              std::int64_t     input_epoch = 0);

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

/// 已校验通过的载荷视图（零拷贝）。
struct ParsedEnvelope {
    const v2::Envelope* env = nullptr;

    bool ok() const noexcept { return env != nullptr; }

    v2::Body body_type() const noexcept {
        return env != nullptr ? env->body_type() : v2::Body::NONE;
    }

    /// 便捷取具体类型（调用方应先确认 body_type()）
    const v2::Hello*         as_hello() const noexcept { return env ? env->body_as_Hello() : nullptr; }
    const v2::HelloAck*      as_hello_ack() const noexcept { return env ? env->body_as_HelloAck() : nullptr; }
    const v2::Ping*          as_ping() const noexcept { return env ? env->body_as_Ping() : nullptr; }
    const v2::Pong*          as_pong() const noexcept { return env ? env->body_as_Pong() : nullptr; }
    const v2::MouseEvent*    as_mouse() const noexcept { return env ? env->body_as_MouseEvent() : nullptr; }
    const v2::KeyboardEvent* as_keyboard() const noexcept { return env ? env->body_as_KeyboardEvent() : nullptr; }
    const v2::ScreenRequest* as_screen_request() const noexcept { return env ? env->body_as_ScreenRequest() : nullptr; }
    const v2::ScreenFrame*   as_screen_frame() const noexcept { return env ? env->body_as_ScreenFrame() : nullptr; }
};

/// 安全解析一条载荷。
/// @return true 表示校验通过，out.env 可用；false 表示载荷非法（why 里带原因）。
bool parse_envelope(const std::uint8_t* data, std::size_t len, ParsedEnvelope& out, std::string& why);

// ---------------------------------------------------------------------------
// 线上枚举 <-> 领域模型 的映射（唯一的耦合点，集中在这里便于演进）
// ---------------------------------------------------------------------------

rc::input::MouseEvent    mouse_from_wire(const v2::MouseEvent& m) noexcept;
rc::input::KeyboardEvent keyboard_from_wire(const v2::KeyboardEvent& k) noexcept;

/// 供日志使用
const char* to_string(v2::Body body) noexcept;

/// 【2B 第三刀 权限模型】角色名（供日志/审计用）。未知值返回 "unknown"，
/// **不返回 nullptr** —— 审计行里出现 `role=unknown` 是可以查的，
/// 而一个 nullptr 会让调用点的格式化直接崩掉（本项目纪律：审计输出不能因为
/// 一个意外枚举值就消失）。
const char* to_string(v2::Role role) noexcept;

} // namespace rc::proto
