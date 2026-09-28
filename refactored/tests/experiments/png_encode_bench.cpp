// ============================================================
// 实验: GDI+ PNG 编码微基准 —— 量清"理论极限",为是否换实现提供数据。
//
// 在自己进程内构造 5 种合成内容 × 4 种尺寸,每帧走和产品 server/
// delta_capturer.cpp:encode_png 完全一致的链路:
//     GlobalAlloc(GMEM_MOVEABLE, 0)
//   → CreateStreamOnHGlobal(hglobal, /*fDeleteOnRelease=*/TRUE, ...)
//   → CImage::Save(IStream, Gdiplus::ImageFormatPNG)
//   → IStream::Stat  → GlobalLock → memcpy
// (fDeleteOnRelease=TRUE: Release stream 自动释放底层 HGLOBAL)
//
// 每组合: 预热 5 帧 / 测量 30 帧 / 取 min(消 GC / 缓存 / 系统调度噪声)。
// 输出: 标准 stdout 一张 markdown 表格 —— 行 = 尺寸,列 = 内容类型,每格 "ms (KB)"。
//
// 5 种合成内容(都用 32bpp BGRA DIB):
//   1) solid   (全 0,最易压)
//   2) gradient (垂直 4 通道渐变,中等可压)
//   3) chars   (白底黑字 GDI TextOut 1000 个 ASCII,模拟代码 / 文档)
//   4) checker (64×64 棋盘,模拟"有边界的重音")
//   5) noise   (每像素 PRNG 0..255,最不可压,考察编码器最坏情况)
//
// 4 种尺寸: 640×480 / 1280×720 / 1707×960 / 2560×1440(后两个即本机桌面)。
// ============================================================

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif

#include <Windows.h>
#include <atlimage.h>   // 必须先于 gdiplus.h:它带进 OLE 头 (IStream/PROPID)
#include <gdiplus.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <random>
#include <string>
#include <vector>

namespace {

constexpr int kWarmup   = 5;
constexpr int kMeasured = 30;

const char* kSizeNames[] = { "640x480", "1280x720", "1707x960", "2560x1440" };
const int    kWidths[]   = {    640,      1280,      1707,       2560 };
const int    kHeights[]  = {    480,       720,       960,       1440 };

inline std::uint32_t MakeBGRA(std::uint8_t b, std::uint8_t g,
                              std::uint8_t r, std::uint8_t a) {
    return (static_cast<std::uint32_t>(b))
         | (static_cast<std::uint32_t>(g) << 8)
         | (static_cast<std::uint32_t>(r) << 16)
         | (static_cast<std::uint32_t>(a) << 24);
}

// 取 DIB 中第 y 行首像素指针(已支持 CImage 默认的 bottom-up 调整
// —— atlimage.h 把 m_pBits 移到顶行 + m_nPitch 取反,但 GetPixelAddress
// 已经把符号算好了;不要自己用 GetBits()+y*GetPitch() 拼)。
inline std::uint32_t* RowPtr(CImage& img, int y) {
    return reinterpret_cast<std::uint32_t*>(img.GetPixelAddress(0, y));
}

void Fill_Solid(CImage& img) {
    const int w = img.GetWidth();
    const int h = img.GetHeight();
    const std::size_t row_bytes = static_cast<std::size_t>(w) * 4;
    for (int y = 0; y < h; ++y) {
        std::uint32_t* row = RowPtr(img, y);
        std::memset(row, 0, row_bytes);
    }
}

void Fill_Gradient(CImage& img) {
    const int w = img.GetWidth();
    const int h = img.GetHeight();
    const int hmax = std::max(h - 1, 1);
    for (int y = 0; y < h; ++y) {
        const std::uint8_t b = static_cast<std::uint8_t>((y * 255) / hmax);
        const std::uint8_t g = static_cast<std::uint8_t>(((hmax - y) * 255) / hmax);
        const std::uint8_t r = static_cast<std::uint8_t>(((y + h / 2) * 255) / hmax);
        const std::uint8_t a = static_cast<std::uint8_t>((y * 3) % 256);
        const std::uint32_t px = MakeBGRA(b, g, r, a);
        std::uint32_t* row = RowPtr(img, y);
        std::fill(row, row + w, px);
    }
}

void Fill_Checker(CImage& img) {
    const int w = img.GetWidth();
    const int h = img.GetHeight();
    constexpr int kCell = 64;
    for (int y = 0; y < h; ++y) {
        const int cy = (y / kCell) & 1;
        std::uint32_t* row = RowPtr(img, y);
        for (int x = 0; x < w; ++x) {
            const int cx = (x / kCell) & 1;
            row[x] = (cx ^ cy) ? 0xFF000000u : 0xFFFFFFFFu;  // opaque black / white
        }
    }
}

void Fill_Noise(CImage& img, std::mt19937& rng) {
    const int w = img.GetWidth();
    const int h = img.GetHeight();
    std::uniform_int_distribution<int> dist(0, 255);
    for (int y = 0; y < h; ++y) {
        std::uint32_t* row = RowPtr(img, y);
        for (int x = 0; x < w; ++x) {
            row[x] = MakeBGRA(static_cast<std::uint8_t>(dist(rng)),
                              static_cast<std::uint8_t>(dist(rng)),
                              static_cast<std::uint8_t>(dist(rng)),
                              static_cast<std::uint8_t>(255));
        }
    }
}

void Fill_Chars(CImage& img, std::mt19937& rng) {
    // 先涂白
    const int w = img.GetWidth();
    const int h = img.GetHeight();
    for (int y = 0; y < h; ++y) {
        std::fill(RowPtr(img, y), RowPtr(img, y) + w, std::uint32_t{0xFFFFFFFFu});
    }

    // GDI TextOut 1000 个 ASCII 字符,模拟代码 / 文档
    HDC dc = img.GetDC();
    if (dc == nullptr) return;

    ::SetBkMode(dc, TRANSPARENT);
    ::SetTextColor(dc, RGB(0, 0, 0));

    HFONT font = ::CreateFontW(
        14, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        ANSI_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
        ANTIALIASED_QUALITY, FIXED_PITCH | FF_MODERN, L"Consolas");
    if (font == nullptr) { img.ReleaseDC(); return; }
    HGDIOBJ oldf = ::SelectObject(dc, font);

    static const char kAlpha[] =
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789 "
        ".,;:!?(){}[]<>/+-=_'\"\\|";
    constexpr std::size_t kAlphaLen = sizeof(kAlpha) - 1;

    std::uniform_int_distribution<std::size_t>  pick(0, kAlphaLen - 1);
    std::uniform_int_distribution<int>          nlen(1, 8);
    std::uniform_int_distribution<int>          rx(0, std::max(w - 80, 1));
    std::uniform_int_distribution<int>          ry(0, std::max(h - 18, 1));

    std::wstring buf;
    buf.reserve(8);
    for (int i = 0; i < 1000; ++i) {
        const int n = nlen(rng);
        buf.clear();
        for (int j = 0; j < n; ++j) {
            buf.push_back(static_cast<wchar_t>(kAlpha[pick(rng)]));
        }
        ::TextOutW(dc, rx(rng), ry(rng), buf.c_str(),
                   static_cast<int>(buf.size()));
    }

    ::SelectObject(dc, oldf);
    ::DeleteObject(font);
    img.ReleaseDC();
}

bool PrepareImage(CImage& img, int w, int h) {
    img.Destroy();
    const BOOL br = img.Create(w, h, 32, 0);
    if (br != 1) {
        std::fprintf(stderr, "  -> CImage::Create(%d,%d,32,0) returned %ld\n",
                     w, h, static_cast<long>(br));
        std::fflush(stderr);
        return false;
    }
    return true;
}

// 与产品 encode_png 完全相同的链路
bool EncodePng_Product(const CImage& image, std::vector<char>& out) {
    HGLOBAL hglobal = ::GlobalAlloc(GMEM_MOVEABLE, 0);
    if (!hglobal) return false;

    IStream* raw_stream = nullptr;
    if (FAILED(::CreateStreamOnHGlobal(hglobal, /*fDeleteOnRelease=*/TRUE,
                                       &raw_stream))) {
        ::GlobalFree(hglobal);
        return false;
    }
    struct StreamReleaser {
        void operator()(IStream* p) const { if (p) p->Release(); }
    };
    std::unique_ptr<IStream, StreamReleaser> stream(raw_stream);

    // CImage::Save 接受 IStream*;隐式转 Gdiplus::Image *
    if (FAILED(image.Save(stream.get(), Gdiplus::ImageFormatPNG))) return false;

    STATSTG stat{};
    if (FAILED(stream->Stat(&stat, STATFLAG_NONAME))) return false;
    const std::size_t size = static_cast<std::size_t>(stat.cbSize.QuadPart);
    if (size == 0) return false;

    void* mem = ::GlobalLock(hglobal);
    if (!mem) return false;
    out.resize(size);
    std::memcpy(out.data(), mem, size);
    ::GlobalUnlock(hglobal);
    return true;
}

struct Stat {
    double      min_ms        = 1e9;
    std::size_t bytes_at_min  = 0;
};

Stat MeasureOneFrame(const CImage& img, int warmup, int frames) {
    Stat st;
    std::vector<char> buf;
    for (int i = 0; i < warmup; ++i) {
        buf.clear();
        if (!EncodePng_Product(img, buf)) {
            std::fprintf(stderr, "encode failed during warmup\n");
            return st;
        }
    }
    for (int i = 0; i < frames; ++i) {
        buf.clear();
        const auto t0 = std::chrono::steady_clock::now();
        const bool ok = EncodePng_Product(img, buf);
        const auto t1 = std::chrono::steady_clock::now();
        if (!ok) {
            std::fprintf(stderr, "encode failed at measured frame %d\n", i);
            return st;
        }
        const double ms =
            std::chrono::duration<double, std::milli>(t1 - t0).count();
        if (ms < st.min_ms) {
            st.min_ms       = ms;
            st.bytes_at_min = buf.size();
        }
    }
    return st;
}

void PrintSizeRow(const char* size_name,
                  const Stat& a, const Stat& b, const Stat& c,
                  const Stat& d, const Stat& e) {
    auto cell = [](const Stat& s) {
        std::printf("%.2f ms / %zu KB",
                    s.min_ms, s.bytes_at_min / 1024);
    };
    std::printf("| %s | ", size_name); cell(a); std::printf(" | "); cell(b);
    std::printf(" | "); cell(c); std::printf(" | "); cell(d);
    std::printf(" | "); cell(e); std::printf(" |\n");
}

}  // namespace

int main() {
    ULONG_PTR gdiplus_token = 0;
    {
        Gdiplus::GdiplusStartupInput input;
        if (Gdiplus::GdiplusStartup(&gdiplus_token, &input, nullptr)
                != Gdiplus::Ok) {
            std::fprintf(stderr, "GdiplusStartup failed\n");
            return 1;
        }
    }

    std::printf("GDI+ PNG encoder micro-benchmark\n");
    std::printf("  链路 = GlobalAlloc + CreateStreamOnHGlobal(fDeleteOnRelease=TRUE)"
                " + CImage::Save(PNG) + Stat + GlobalLock/Copy\n");
    std::printf("  每组合 = 预热 %d 帧 / 测量 %d 帧取 min(消 GC / 缓存 / 调度噪声)\n",
                kWarmup, kMeasured);
    std::printf("  内容: solid(全 0)/ gradient(4ch 渐变)/ chars(白底黑字 GDI 1000 ASCII)/"
                " checker(64×64)/ noise(每像素 PRNG)\n\n");

    std::printf("| size     | solid       | gradient    | chars       |"
                " checker     | noise       |\n");
    std::printf("|----------|-------------|-------------|-------------|"
                "-------------|-------------|\n");

    constexpr int kNumSizes = 4;
    for (int s = 0; s < kNumSizes; ++s) {
        const int w = kWidths[s];
        const int h = kHeights[s];

        std::vector<CImage> imgs(5);

        if (!PrepareImage(imgs[0], w, h) ||
            !PrepareImage(imgs[1], w, h) ||
            !PrepareImage(imgs[2], w, h) ||
            !PrepareImage(imgs[3], w, h) ||
            !PrepareImage(imgs[4], w, h)) {
            std::fprintf(stderr, "PrepareImage failed at size %s\n",
                         kSizeNames[s]);
            Gdiplus::GdiplusShutdown(gdiplus_token);
            return 1;
        }

        Fill_Solid(imgs[0]);
        Fill_Gradient(imgs[1]);
        Fill_Checker(imgs[2]);

        {
            std::mt19937 r2(0xC0FFEEu);
            Fill_Noise(imgs[3], r2);
        }
        {
            std::mt19937 r3(0xCAFEBABEu);
            Fill_Chars(imgs[4], r3);
        }

        Stat s0 = MeasureOneFrame(imgs[0], kWarmup, kMeasured);
        Stat s1 = MeasureOneFrame(imgs[1], kWarmup, kMeasured);
        Stat s2 = MeasureOneFrame(imgs[2], kWarmup, kMeasured);
        Stat s3 = MeasureOneFrame(imgs[3], kWarmup, kMeasured);
        Stat s4 = MeasureOneFrame(imgs[4], kWarmup, kMeasured);

        PrintSizeRow(kSizeNames[s], s0, s1, s2, s3, s4);
    }

    std::printf("\n(注: 每格 = min(30 帧) 编码毫秒 / 该帧编码后字节数÷1024)\n");

    Gdiplus::GdiplusShutdown(gdiplus_token);
    return 0;
}
