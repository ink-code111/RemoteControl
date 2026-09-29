#include "tls.hpp"

#include "logger.hpp"

#include <openssl/crypto.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/opensslv.h>
#include <openssl/pem.h>
#include <openssl/rand.h>
#include <openssl/rsa.h>   // EVP_RSA_gen()
#include <openssl/ssl.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>

#include <cstdio>
#include <cctype>
#include <cstdint>
#include <filesystem>
#include <mutex>
#include <vector>

namespace rc::tls {
namespace {

// ---- OpenSSL 对象的 RAII ------------------------------------------------------------------
// 这些类型没有 C++ 析构，裸用就会在每一条 return 路径上漏内存（而启动期代码恰恰
// 有一堆提前返回的错误分支）。用 unique_ptr + 自定义 deleter 统一收口。
template <class T, void (*FreeFn)(T*)>
using ossl_ptr = std::unique_ptr<T, decltype(FreeFn)>;

using x509_ptr      = ossl_ptr<X509, ::X509_free>;
using pkey_ptr      = ossl_ptr<EVP_PKEY, ::EVP_PKEY_free>;
using x509_name_ptr = ossl_ptr<X509_NAME, ::X509_NAME_free>;
using ext_ptr       = ossl_ptr<X509_EXTENSION, ::X509_EXTENSION_free>;
using bio_ptr       = ossl_ptr<BIO, ::BIO_free_all>;
using asn1_int_ptr  = ossl_ptr<ASN1_INTEGER, ::ASN1_INTEGER_free>;

/// 把 OpenSSL 错误栈顶那条转成可读字符串。**必须调用**：OpenSSL 的错误栈是线程局部的，
/// 不读走就会一直积在那儿，下一个不相关的失败会被它污染（诊断时非常坑）。
std::string last_error() {
    const unsigned long e = ::ERR_get_error();
    if (e == 0) {
        return "no OpenSSL error";
    }
    char buf[256];
    ::ERR_error_string_n(e, buf, sizeof(buf));
    return std::string(buf);
}

std::string hex_upper(const unsigned char* p, std::size_t n) {
    static const char* kHex = "0123456789ABCDEF";
    std::string        out;
    out.reserve(n * 2);
    for (std::size_t i = 0; i < n; ++i) {
        out.push_back(kHex[p[i] >> 4]);
        out.push_back(kHex[p[i] & 0x0F]);
    }
    return out;
}

/// 把**已经算好的摘要字节**格式化成 "AA:BB:CC:…"（大写、冒号分隔）。**不做任何哈希。**
///
/// 【为什么"算"与"格式化"必须是两个函数 —— 2026-09-25 实测踩到】
///   最初两者合成一个 `fingerprint_sha256(bytes, len)`，而 pin 校验回调手里拿到的
///   已经是 `X509_digest` 算好的摘要，于是它**又哈希了一遍**。产出的仍是一串合法的
///   64 位十六进制，与正确值长得完全一样，只是**永远不可能相等** ⇒
///   pin 校验退化成"永远拒绝"（而"指纹错就该拒"那一轮照样通过 —— 错的实现
///   在一半的判据上看起来是对的，这正是必须有**正向对照轮**的理由）。
///   分开命名后，"我手上这个到底是不是摘要"变成写代码时必须回答的一个问题。
std::string format_digest(const unsigned char* digest, std::size_t len) {
    const std::string hex = hex_upper(digest, len);
    // 输出成 "AA:BB:…"：与用户在别处（openssl x509 -fingerprint -sha256）看到的一致，
    // 便于人工核对。比较时用的是 normalize 之后的形态，分隔符随便写都行。
    std::string out;
    out.reserve(hex.size() + hex.size() / 2);
    for (std::size_t i = 0; i < hex.size(); i += 2) {
        if (i != 0) {
            out.push_back(':');
        }
        out.append(hex, i, 2);
    }
    return out;
}

} // namespace

// ============================================================================
//  版本
// ============================================================================

std::string runtime_version() {
    const char* s = ::OpenSSL_version(OPENSSL_VERSION);
    return s != nullptr ? std::string(s) : std::string("<unknown>");
}

std::string build_version() {
    return std::string(OPENSSL_VERSION_TEXT);
}

// ============================================================================
//  指纹
// ============================================================================

std::string fingerprint_of_pem_file(const std::string& cert_file, std::string* error) {
    bio_ptr bio(::BIO_new_file(cert_file.c_str(), "rb"), ::BIO_free_all);
    if (!bio) {
        if (error) {
            *error = "无法打开证书文件: " + cert_file + " (" + last_error() + ")";
        }
        return std::string();
    }
    x509_ptr cert(::PEM_read_bio_X509(bio.get(), nullptr, nullptr, nullptr), ::X509_free);
    if (!cert) {
        if (error) {
            *error = "解析 PEM 证书失败: " + cert_file + " (" + last_error() + ")";
        }
        return std::string();
    }

    // 用 X509_digest 而不是"先 i2d 再 EVP_Digest"——它算的就是 DER 的摘要，
    // 且不需要自己管分配。注意算的是 **DER**（不是 PEM 文本），
    // 所以与 `openssl x509 -fingerprint -sha256` 的输出一致。
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned int  md_len = 0;
    if (::X509_digest(cert.get(), ::EVP_sha256(), md, &md_len) != 1) {
        if (error) {
            *error = "计算证书指纹失败: " + cert_file + " (" + last_error() + ")";
        }
        return std::string();
    }
    return format_digest(md, md_len);
}

std::string normalize_fingerprint(const std::string& fp) {
    std::string out;
    out.reserve(fp.size());
    for (char ch : fp) {
        const unsigned char c = static_cast<unsigned char>(ch);
        // 常见的前缀写法 "sha256:" 里的字母属于十六进制字符集，会被保留；
        // 但它不会与真实指纹的前 6 位**同时**成立（真实值是 64 个十六进制位），
        // 所以这里不专门剥前缀 —— 只剥非十六进制字符即可，剩下的交给长度检查。
        if (std::isxdigit(c) != 0) {
            out.push_back(static_cast<char>(std::toupper(c)));
        }
    }
    return out;
}

bool fingerprint_matches(const std::string& configured, const std::string& actual) {
    const std::string a = normalize_fingerprint(configured);
    const std::string b = normalize_fingerprint(actual);
    // 规范化后必须是 64 个十六进制位（SHA-256）。长度不对说明配置写错了 ——
    // 返回 false 让上层把它当成"不匹配"报出来，而不是悄悄通过。
    if (a.size() != 64 || b.size() != 64) {
        return false;
    }
    return a == b;
}

// ============================================================================
//  自签证书
// ============================================================================

bool ensure_self_signed_cert(const std::string& cert_file, const std::string& key_file,
                             std::string* error) {
    const bool have_cert = std::filesystem::exists(cert_file);
    const bool have_key  = std::filesystem::exists(key_file);

    if (have_cert && have_key) {
        return true; // 已存在：**绝不覆盖**（覆盖会让所有已 pin 的客户端失效）
    }
    if (have_cert != have_key) {
        // 只存在一半：这是一种自相矛盾的状态，不能"补一个" —— 补出来的那个
        // 与已有的那个不配对，握手会在运行期以一条难懂的 SSL 错误失败。
        if (error) {
            *error = "自签证书不完整：cert 与 key 只存在其中一个（" + cert_file + " / " +
                     key_file + "）。请把两个都删掉后重启以重新生成。";
        }
        return false;
    }

    // ---- 生成密钥 ----
    // RSA-2048：兼容性最好（TLS 1.2 的经典套件都支持），生成耗时在本机 ~0.1 s 量级，
    // 只在**首次启动**付一次。ECDSA 更快更省，但"要能和任何客户端握手"这件事上
    // RSA 更保险 —— 这里是自用工具，稳妥优先。
    pkey_ptr pkey(::EVP_RSA_gen(2048), ::EVP_PKEY_free);
    if (!pkey) {
        if (error) {
            *error = "生成 RSA 密钥失败: " + last_error();
        }
        return false;
    }

    x509_ptr cert(::X509_new(), ::X509_free);
    if (!cert) {
        if (error) {
            *error = "X509_new 失败: " + last_error();
        }
        return false;
    }

    if (::X509_set_version(cert.get(), 2) != 1) { // 2 = X509 v3（扩展项需要 v3）
        if (error) {
            *error = "设置 X509 版本失败: " + last_error();
        }
        return false;
    }

    // 序列号：随机 64 位（固定值会让"换了证书"在指纹之外还多一处可比对的特征，
    // 也可能与别的自签证书撞号）。
    unsigned char serial_raw[8];
    if (::RAND_bytes(serial_raw, sizeof(serial_raw)) != 1) {
        if (error) {
            *error = "生成序列号失败: " + last_error();
        }
        return false;
    }
    serial_raw[0] &= 0x7F; // 保证是正整数
    // 【2026-09-29 修】累加必须用 64 位无符号：Windows 是 LLP64，`long` 只有 32 位，
    // 8 字节随机数的高 4 字节会被静默移出（`&= 0x7F` 的符号位处理恰好落在被丢掉的
    // 字节上），实际只剩低 4 字节参与 —— 约 50% 概率产出**负数**序列号（违反 RFC 5280
    // 要求序列号为正），且有符号左移溢出本身是 UB。这份代码只在 LP64 平台碰巧是对的，
    // 而本项目 Windows-only。配套用 ASN1_INTEGER_set_uint64（它才收 64 位；
    // 老的 ASN1_INTEGER_set 收 long，在 Windows 上同样是 32 位）。
    uint64_t serial = 0;
    for (unsigned char b : serial_raw) {
        serial = (serial << 8) | b;
    }
    if (serial == 0) {
        serial = 1;
    }
    asn1_int_ptr serial_asn1(::ASN1_INTEGER_new(), ::ASN1_INTEGER_free);
    if (!serial_asn1 || ::ASN1_INTEGER_set_uint64(serial_asn1.get(), serial) != 1 ||
        ::X509_set_serialNumber(cert.get(), serial_asn1.get()) != 1) {
        if (error) {
            *error = "设置序列号失败: " + last_error();
        }
        return false;
    }

    if (::X509_gmtime_adj(::X509_getm_notBefore(cert.get()), 0) == nullptr ||
        ::X509_gmtime_adj(::X509_getm_notAfter(cert.get()),
                          static_cast<long>(10) * 365 * 24 * 60 * 60) == nullptr) {
        if (error) {
            *error = "设置有效期失败: " + last_error();
        }
        return false;
    }
    // 回退 1 分钟再签发：本机时钟与对端若差几十秒，"还没生效"会让握手失败，
    // 而错误信息（certificate is not yet valid）看起来像配置问题。
    ::X509_gmtime_adj(::X509_getm_notBefore(cert.get()), -60);

    if (::X509_set_pubkey(cert.get(), pkey.get()) != 1) {
        if (error) {
            *error = "设置公钥失败: " + last_error();
        }
        return false;
    }

    X509_NAME* name = ::X509_get_subject_name(cert.get());
    if (name == nullptr ||
        ::X509_NAME_add_entry_by_txt(name, "CN", MBSTRING_ASC,
                                     reinterpret_cast<const unsigned char*>("RemoteControl-Server"),
                                     -1, -1, 0) != 1) {
        if (error) {
            *error = "设置证书主题失败: " + last_error();
        }
        return false;
    }
    if (::X509_set_issuer_name(cert.get(), name) != 1) { // 自签：issuer == subject
        if (error) {
            *error = "设置签发者失败: " + last_error();
        }
        return false;
    }

    // ---- 扩展 ----
    // 【为什么必须有扩展】没有扩展项的 v3 证书在部分栈里会被当作"缺少约束"而拒绝。
    // 这里加最小且正确的三条：CA:FALSE（它不是 CA）、serverAuth（只用于服务端）、
    // SAN（把它绑到 localhost/127.0.0.1，便于将来做主机名校验）。
    // 注意：客户端的身份验证走的是**指纹 pin**，不看 CN/SAN —— SAN 是留给
    // "将来可能引入主机名校验"的，不影响当前逻辑。
    struct ExtSpec {
        int         nid;
        const char* value;
    };
    const ExtSpec exts[] = {
        {NID_basic_constraints,      "critical,CA:FALSE"},
        {NID_key_usage,              "critical,digitalSignature,keyEncipherment"},
        {NID_ext_key_usage,          "serverAuth"},
        {NID_subject_alt_name,       "DNS:localhost,IP:127.0.0.1"},
    };
    for (const auto& e : exts) {
        ext_ptr ext(::X509V3_EXT_conf_nid(nullptr, nullptr, e.nid,
                                          const_cast<char*>(e.value)),
                    ::X509_EXTENSION_free);
        if (!ext) {
            if (error) {
                *error = std::string("构造扩展失败( nid=") + std::to_string(e.nid) + " ): " +
                         last_error();
            }
            return false;
        }
        if (::X509_add_ext(cert.get(), ext.get(), -1) != 1) {
            if (error) {
                *error = std::string("添加扩展失败( nid=") + std::to_string(e.nid) + " ): " +
                         last_error();
            }
            return false;
        }
    }

    if (::X509_sign(cert.get(), pkey.get(), ::EVP_sha256()) == 0) {
        if (error) {
            *error = "自签失败: " + last_error();
        }
        return false;
    }

    // ---- 落盘 ----
    std::error_code ec;
    const auto cert_dir = std::filesystem::path(cert_file).parent_path();
    const auto key_dir  = std::filesystem::path(key_file).parent_path();
    if (!cert_dir.empty()) {
        std::filesystem::create_directories(cert_dir, ec);
    }
    if (!key_dir.empty()) {
        std::filesystem::create_directories(key_dir, ec);
    }

    {
        bio_ptr out(::BIO_new_file(cert_file.c_str(), "wb"), ::BIO_free_all);
        if (!out || ::PEM_write_bio_X509(out.get(), cert.get()) != 1) {
            if (error) {
                *error = "写证书文件失败: " + cert_file + " (" + last_error() + ")";
            }
            return false;
        }
    }
    {
        bio_ptr out(::BIO_new_file(key_file.c_str(), "wb"), ::BIO_free_all);
        // 第三个参数为 nullptr = 私钥**不加密**。这是刻意的：本工具没有"口令管理"
        // 这一层，加密私钥就要把口令再存一个地方，等于把问题推给下一个文件。
        // 私钥的访问控制交给文件系统权限（服务端本就是拿到桌面控制权的一方）。
        if (!out || ::PEM_write_bio_PrivateKey(out.get(), pkey.get(), nullptr, nullptr, 0,
                                              nullptr, nullptr) != 1) {
            if (error) {
                *error = "写私钥文件失败: " + key_file + " (" + last_error() + ")";
            }
            return false;
        }
    }

    RC_LOG_INFO("已生成自签证书：{} + {}（RSA-2048，有效期 10 年）", cert_file, key_file);
    return true;
}

// ============================================================================
//  上下文
// ============================================================================

std::shared_ptr<asio::ssl::context>
make_server_context(const std::string& cert_file, const std::string& key_file,
                    std::string* error) {
    auto ctx = std::make_shared<asio::ssl::context>(asio::ssl::context::tls_server);
    error_code ec;

    // 最低 TLS 1.2：1.0/1.1 已被所有主流栈弃用，留着只是给降级攻击留门。
    if (::SSL_CTX_set_min_proto_version(ctx->native_handle(), TLS1_2_VERSION) != 1) {
        if (error) {
            *error = "设置最低 TLS 版本失败: " + last_error();
        }
        return nullptr;
    }

    ctx->use_certificate_chain_file(cert_file, ec);
    if (ec) {
        if (error) {
            *error = "加载证书链失败 " + cert_file + ": " + ec.message();
        }
        return nullptr;
    }
    ctx->use_private_key_file(key_file, asio::ssl::context::pem, ec);
    if (ec) {
        if (error) {
            *error = "加载私钥失败 " + key_file + ": " + ec.message();
        }
        return nullptr;
    }

    // 服务端不校验客户端证书（客户端的身份由 auth_token 那一层负责）。
    // 这里刻意显式写出来：TLS 只做"通道加密 + 服务端身份"，**不等于**双向认证。
    ctx->set_verify_mode(asio::ssl::verify_none, ec);
    if (ec) {
        if (error) {
            *error = "设置服务端校验模式失败: " + ec.message();
        }
        return nullptr;
    }

    return ctx;
}

std::shared_ptr<asio::ssl::context>
make_client_context(const std::string& expected_fingerprint, std::string* error) {
    auto ctx = std::make_shared<asio::ssl::context>(asio::ssl::context::tls_client);
    error_code ec;

    if (::SSL_CTX_set_min_proto_version(ctx->native_handle(), TLS1_2_VERSION) != 1) {
        if (error) {
            *error = "设置最低 TLS 版本失败: " + last_error();
        }
        return nullptr;
    }

    const std::string norm_pin = normalize_fingerprint(expected_fingerprint);
    if (norm_pin.empty()) {
        // 只加密、不认证。这是**危险**的（中间人可以解密/篡改），所以调用方
        // 必须在启动日志里大声告警；这里不静默放行任何东西 —— 只是不做校验。
        ctx->set_verify_mode(asio::ssl::verify_none, ec);
        if (ec) {
            if (error) {
                *error = "设置客户端校验模式失败: " + ec.message();
            }
            return nullptr;
        }
        return ctx;
    }

    if (norm_pin.size() != 64) {
        if (error) {
            *error = "tls_pin_sha256 不是合法的 SHA-256 指纹（规范化后应有 64 个十六进制位，"
                     "实际 " + std::to_string(norm_pin.size()) + " 位）";
        }
        return nullptr;
    }

    // 校验回调：**不看信任链**，只看指纹。自签证书在系统 CA 下永远 preverified=false，
    // 若同时要求 preverified 就永远握不上手 —— 那会把"pin 模式"这个设计直接废掉。
    ctx->set_verify_mode(asio::ssl::verify_peer, ec);
    if (ec) {
        if (error) {
            *error = "设置客户端校验模式失败: " + ec.message();
        }
        return nullptr;
    }
    ctx->set_verify_callback(
        [norm_pin](bool preverified, asio::ssl::verify_context& vc) -> bool {
            // 【为什么拒绝时要把现场打出来】2026-09-25 首次跑 TLS 判据时，**指纹抄对了**
            // 的那一轮也报了 `certificate verify failed`，而判据只看得到"握手失败"。
            // 但"回调拒绝"至少有四种互不相同的成因：深度过滤命中、取不到证书、
            // 摘要算不出来、指纹确实不等 —— 处置完全不同（前三种是我们自己的 bug，
            // 第四种才是"真的有人冒充服务端"）。所以拒绝路径一律带上现场。
            // ⚠️ 判定逻辑**一字未改**：补现场 ≠ 放宽判据。
            const int depth = ::X509_STORE_CTX_get_error_depth(vc.native_handle());
            const int verr  = ::X509_STORE_CTX_get_error(vc.native_handle());
            // 回调会为链上每一张证书各调一次；只看深度 0（叶子 = 服务端自己那张）。
            if (depth != 0) {
                RC_LOG_WARN("TLS pin: 跳过深度 {} 的证书（只看叶子）", depth);
                return false;
            }
            X509* cert = ::X509_STORE_CTX_get_current_cert(vc.native_handle());
            if (cert == nullptr) {
                RC_LOG_WARN("TLS pin: 取不到对端证书（X509_STORE_CTX_get_current_cert 返回空）");
                return false;
            }
            unsigned char md[EVP_MAX_MD_SIZE];
            unsigned int  md_len = 0;
            if (::X509_digest(cert, ::EVP_sha256(), md, &md_len) != 1) {
                RC_LOG_WARN("TLS pin: 证书 SHA-256 摘要计算失败：{}", last_error());
                return false;
            }
            // ⚠️ 这里拿到的 `md` 是**已经算好的摘要**，所以只能用 format_digest()
            //    （只格式化）。用"会再算一次哈希"的助手就会得到一个永远匹配不上的值
            //    —— 这正是本函数名与签名被重写过一次的原因，见 format_digest() 的注释。
            const std::string got = normalize_fingerprint(format_digest(md, md_len));
            if (got == norm_pin) {
                return true;
            }
            RC_LOG_WARN("TLS pin: **指纹不匹配**，不信任该服务端 —— 收到 {} / 期望 {}"
                        "（X509 校验错误 {}: {}，preverified={}）",
                        got, norm_pin, verr, ::X509_verify_cert_error_string(verr),
                        preverified ? "true" : "false");
            return false;
        });

    return ctx;
}

} // namespace rc::tls
