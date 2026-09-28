#include "proto_codec.hpp"
#include "clock.hpp"   // rc::net::now_ms()：协议层只需要 chrono，不引入 asio
#include "logger.hpp"

namespace rc::proto {
namespace {

/// 收尾：把 Envelope 封口并**写入 schema 声明的 file_identifier("RCEN")**。
///
/// 【必须用生成的 FinishEnvelopeBuffer，不能用裸 fbb.Finish(root)】
///   FlatBuffers 的 file_identifier 不会自动写 —— 它要求调用方在 Finish 时
///   显式传入标识串：fbb.Finish(root, "RCEN")。用裸 Finish(root) 编出来的包
///   结构完全合法，但 buffer 的第 4~7 字节是 0 而不是 "RCEN"。
///   于是接收侧的两道检查会双双失败：
///       EnvelopeBufferHasIdentifier() -> false（"buffer lacks RCEN..."）
///       VerifyEnvelopeBuffer()        -> false（它内部同样按标识串校验）
///   而失败表现只是"连接被对方关掉"，排查时非常费解（这个坑已经被
///   端到端探针踩到过一次）。所以这里一律走生成代码提供的封装，
///   标识串与 schema 永远对得上，不给人写裸 Finish 的机会。
flatbuffers::DetachedBuffer wrap(flatbuffers::FlatBufferBuilder& fbb,
                                 v2::Body                        type,
                                 flatbuffers::Offset<void>       body) {
    const auto env = v2::CreateEnvelope(fbb, type, body);
    v2::FinishEnvelopeBuffer(fbb, env);
    return fbb.Release();
}

} // namespace

// ---------------------------------------------------------------------------
// 构造
// ---------------------------------------------------------------------------

flatbuffers::DetachedBuffer make_hello(std::string_view  client_name,
                                       std::string_view  client_version,
                                       std::uint16_t     protocol_version,
                                       std::string_view  auth_token) {
    flatbuffers::FlatBufferBuilder fbb(256);
    const auto name = fbb.CreateString(client_name.data(), client_name.size());
    const auto ver  = fbb.CreateString(client_version.data(), client_version.size());
    // 空凭据**不写这个字段**（而不是写一个空串）：线上少 4 字节，而且"未提供"与
    // "提供了空串"塌成同一种表示 —— 服务端两侧都读成空串，不必区分。
    const auto tok = auth_token.empty()
                         ? flatbuffers::Offset<flatbuffers::String>()
                         : fbb.CreateString(auth_token.data(), auth_token.size());
    const auto body = v2::CreateHello(fbb, protocol_version, name, ver, tok);
    return wrap(fbb, v2::Body::Hello, body.Union());
}

flatbuffers::DetachedBuffer make_hello_ack(std::uint32_t    session_id,
                                           bool             accepted,
                                           std::string_view reject_reason,
                                           std::uint16_t    protocol_version,
                                           v2::Role         role) {
    flatbuffers::FlatBufferBuilder fbb(256);
    const auto reason = fbb.CreateString(reject_reason.data(), reject_reason.size());
    const auto body   = v2::CreateHelloAck(fbb, protocol_version, session_id,
                                           rc::net::now_ms(), accepted, reason, role);
    return wrap(fbb, v2::Body::HelloAck, body.Union());
}

flatbuffers::DetachedBuffer make_ping(std::uint32_t seq, std::int64_t client_time_ms) {
    flatbuffers::FlatBufferBuilder fbb(64);
    const auto body = v2::CreatePing(fbb, seq, client_time_ms);
    return wrap(fbb, v2::Body::Ping, body.Union());
}

flatbuffers::DetachedBuffer make_pong(std::uint32_t seq,
                                      std::int64_t  client_time_ms,
                                      std::int64_t  server_time_ms) {
    flatbuffers::FlatBufferBuilder fbb(64);
    const auto body = v2::CreatePong(fbb, seq, client_time_ms, server_time_ms);
    return wrap(fbb, v2::Body::Pong, body.Union());
}

flatbuffers::DetachedBuffer make_mouse(const rc::input::MouseEvent& ev) {
    flatbuffers::FlatBufferBuilder fbb(64);
    const auto body = v2::CreateMouseEvent(fbb,
                                           static_cast<v2::MouseAction>(ev.action),
                                           ev.x,
                                           ev.y,
                                           ev.wheel_delta);
    return wrap(fbb, v2::Body::MouseEvent, body.Union());
}

flatbuffers::DetachedBuffer make_keyboard(const rc::input::KeyboardEvent& ev) {
    flatbuffers::FlatBufferBuilder fbb(64);
    const auto body = v2::CreateKeyboardEvent(fbb, ev.vk, ev.up);
    return wrap(fbb, v2::Body::KeyboardEvent, body.Union());
}

flatbuffers::DetachedBuffer make_screen_request(std::int32_t max_width, std::int32_t quality) {
    flatbuffers::FlatBufferBuilder fbb(64);
    const auto body = v2::CreateScreenRequest(fbb, max_width, quality);
    return wrap(fbb, v2::Body::ScreenRequest, body.Union());
}

flatbuffers::DetachedBuffer make_screen_frame(std::int32_t        width,
                                              std::int32_t        height,
                                              std::int64_t        timestamp_ms,
                                              v2::ImageFormat     format,
                                              const std::uint8_t* data,
                                              std::size_t         data_len,
                                              const DirtyRect*    dirty,
                                              std::int64_t        input_epoch) {
    // 预分配足够的空间：屏幕帧通常几百 KB～几 MB，
    // 让 builder 一次性把空间要到，避免内部反复翻倍扩容（每次扩容都要整块搬移）。
    flatbuffers::FlatBufferBuilder fbb(static_cast<std::size_t>(data_len) + 256);
    const auto payload = (data != nullptr && data_len > 0)
                             ? fbb.CreateVector(data, data_len)
                             : flatbuffers::Offset<flatbuffers::Vector<std::uint8_t>>();
    // 脏矩形：空数组 = "data 是完整一帧"（第二阶段的语义，也是向后兼容的默认值）；
    // 非空 = "data 是增量帧，这只是那块矩形的内容，客户端要自行合成到累积画面上"。
    // 当前只用 1 个矩形（变化区域的并集包围盒）—— 用数组而不是单值，
    // 是为了将来拆成多块瓦片时不必再动 schema。
    //
    // 0×0 的矩形是**合法的**，而且是必需的：它是增量帧里"本帧没有任何变化"的表达。
    // 这一点踩过坑——原先这里要求 w>0 && h>0，于是"无变化"被编码成"空数组"，
    // 而空数组在协议里是"整帧"的意思。客户端因此把无变化帧当成了空整帧：
    // 既不计入收帧数（到达帧率凭空少了 90%），也认不出这是差异帧链的一环。
    // 判据要能区分三种帧，就必须让增量帧**总是带矩形**：
    //     空数组 + 有载荷 = 整帧
    //     1 个 w/h>0 的矩形 + 载荷 = 脏区域增量
    //     1 个 0×0 的矩形 + 空载荷  = 无变化
    auto dirty_offset = flatbuffers::Offset<flatbuffers::Vector<const v2::Rect*>>();
    if (dirty != nullptr) {
        const v2::Rect rect(dirty->x, dirty->y, dirty->w, dirty->h);
        dirty_offset = fbb.CreateVectorOfStructs(&rect, 1);
    }
    const auto body = v2::CreateScreenFrame(fbb, width, height, timestamp_ms, format, payload,
                                            dirty_offset, input_epoch);
    return wrap(fbb, v2::Body::ScreenFrame, body.Union());
}

// ---------------------------------------------------------------------------
// 解析
// ---------------------------------------------------------------------------

bool parse_envelope(const std::uint8_t* data,
                    std::size_t         len,
                    ParsedEnvelope&     out,
                    std::string&        why) {
    out.env = nullptr;

    // 最小长度 = 4 字节 root offset + 4 字节 file_identifier
    if (data == nullptr || len < 8) {
        why = "payload too small to be a valid envelope";
        return false;
    }

    // 第一步：标识符校验。两字节都不对就没必要再往下走（同时能快速识别"连错服务"）
    if (!v2::EnvelopeBufferHasIdentifier(data)) {
        why = "buffer lacks RCEN file identifier";
        return false;
    }

    // 第二步：跑 Verifier。这是防"恶意/损坏的偏移量"的关键 ——
    // FlatBuffers 是零拷贝读取，字段访问就是按偏移取指针，
    // 不先校验就等于把野指针交到业务代码手里。
    // 显式给出深度/表数量上限，避免对端用超大嵌套结构强制我们做海量校验（DoS）。
    flatbuffers::Verifier::Options opts;
    opts.max_depth  = 64;
    opts.max_tables = 1'000'000;
    flatbuffers::Verifier verifier(data, len, opts);
    if (!v2::VerifyEnvelopeBuffer(verifier)) {
        why = "FlatBuffers verifier rejected the buffer";
        return false;
    }

    const auto* env = v2::GetEnvelope(data);
    if (env == nullptr) {
        why = "envelope root is null";
        return false;
    }
    if (env->body_type() == v2::Body::NONE) {
        why = "envelope has no body";
        return false;
    }

    out.env = env;
    return true;
}

// ---------------------------------------------------------------------------
// 映射
// ---------------------------------------------------------------------------

rc::input::MouseEvent mouse_from_wire(const v2::MouseEvent& m) noexcept {
    rc::input::MouseEvent ev;
    ev.x           = m.x();
    ev.y           = m.y();
    ev.wheel_delta = m.wheel_delta();

    switch (m.action()) {
    case v2::MouseAction::Move:    ev.action = rc::input::MouseAction::kMove;    break;
    case v2::MouseAction::LDown:   ev.action = rc::input::MouseAction::kLDown;   break;
    case v2::MouseAction::LUp:     ev.action = rc::input::MouseAction::kLUp;     break;
    case v2::MouseAction::RDown:   ev.action = rc::input::MouseAction::kRDown;   break;
    case v2::MouseAction::RUp:     ev.action = rc::input::MouseAction::kRUp;     break;
    case v2::MouseAction::MDown:   ev.action = rc::input::MouseAction::kMDown;   break;
    case v2::MouseAction::MUp:     ev.action = rc::input::MouseAction::kMUp;     break;
    case v2::MouseAction::LDClick: ev.action = rc::input::MouseAction::kLDClick; break;
    case v2::MouseAction::RDClick: ev.action = rc::input::MouseAction::kRDClick; break;
    case v2::MouseAction::MDClick: ev.action = rc::input::MouseAction::kMDClick; break;
    case v2::MouseAction::Wheel:   ev.action = rc::input::MouseAction::kWheel;   break;
    default:
        // 新版本客户端可能发来我们还不认识的枚举值：退化为"无操作"而不是崩掉
        ev.action = rc::input::MouseAction::kMove;
        break;
    }
    return ev;
}

rc::input::KeyboardEvent keyboard_from_wire(const v2::KeyboardEvent& k) noexcept {
    rc::input::KeyboardEvent ev;
    ev.vk = k.vk();
    ev.up = k.up();
    return ev;
}

const char* to_string(v2::Body body) noexcept {
    switch (body) {
    case v2::Body::NONE:          return "NONE";
    case v2::Body::Hello:         return "Hello";
    case v2::Body::HelloAck:      return "HelloAck";
    case v2::Body::Ping:          return "Ping";
    case v2::Body::Pong:          return "Pong";
    case v2::Body::MouseEvent:    return "MouseEvent";
    case v2::Body::KeyboardEvent: return "KeyboardEvent";
    case v2::Body::ScreenRequest: return "ScreenRequest";
    case v2::Body::ScreenFrame:   return "ScreenFrame";
    }
    return "unknown";
}

const char* to_string(v2::Role role) noexcept {
    switch (role) {
    case v2::Role::Control: return "control";
    case v2::Role::View:    return "view";
    }
    // 未知枚举值必须走 default 语义而不是断言失败（schema 的向后兼容条款：
    // 「枚举值只能追加，读到未知值要走 default 分支」）。审计行里写 unknown
    // 比让进程崩掉、或让审计输出整个消失要好得多。
    return "unknown";
}

} // namespace rc::proto
