// 一次性实验：验证 DrawIconEx 在 32bpp CImage 上的真实表现。
//
// 要回答两个问题（不靠推测）：
//   1) hotspot 偏移对不对 —— DrawIconEx 的 x/y 是"图标左上角"，
//      而光标坐标是"热点"，两者差一个 (xHotspot, yHotspot)。
//   2) 32bpp 位图的 alpha 通道会不会被写坏 —— 若 DrawIconEx 把光标周围的
//      衬垫区 alpha 写成 0，存成 PNG 后客户端会看到透明方块（最经典的坑）。
//
// 做法：造一张纯色 32bpp 位图（模拟 BitBlt 之后的桌面位图），直接读回像素，
// 不经过任何图像库。这样 alpha 是不是有问题，一眼就能看到。
#include <Windows.h>
#include <atlimage.h>
#include <gdiplus.h>

#include <cstdio>

int main() {
    Gdiplus::GdiplusStartupInput input;
    ULONG_PTR                   token = 0;
    if (Gdiplus::GdiplusStartup(&token, &input, nullptr) != Gdiplus::Ok) {
        std::printf("GdiplusStartup failed\n");
        return 1;
    }

    const int W = 200;
    const int H = 120;

    CImage img;
    if (img.Create(W, H, 32, 0) != 1) {
        std::printf("CImage::Create failed\n");
        return 1;
    }

    HDC dc = img.GetDC();
    RECT rc{0, 0, W, H};
    HBRUSH br = ::CreateSolidBrush(RGB(30, 90, 200)); // 不透明底色
    ::FillRect(dc, &rc, br);
    ::DeleteObject(br);

    // ---- 取光标句柄：优先用当前系统光标，拿不到就用标准箭头兜底 ----
    CURSORINFO ci{};
    ci.cbSize = sizeof(ci); // 忘了赋值 cbSize 是 GetCursorInfo 最常见的用法错误
    const bool got = ::GetCursorInfo(&ci) != FALSE;
    std::printf("GetCursorInfo ok=%d flags=0x%08lX hCursor=%p pos=(%ld,%ld)\n",
                static_cast<int>(got), static_cast<unsigned long>(ci.flags),
                static_cast<void*>(ci.hCursor), ci.ptScreenPos.x, ci.ptScreenPos.y);

    HCURSOR cur = (got && ci.hCursor != nullptr) ? ci.hCursor
                                                 : ::LoadCursor(nullptr, IDC_ARROW);
    std::printf("使用光标句柄 %p\n", static_cast<void*>(cur));

    ICONINFO ii{};
    const bool has_ii = ::GetIconInfo(cur, &ii) != FALSE;
    std::printf("GetIconInfo ok=%d hotspot=(%lu,%lu) hbmMask=%p hbmColor=%p\n",
                static_cast<int>(has_ii), ii.xHotspot, ii.yHotspot,
                static_cast<void*>(ii.hbmMask), static_cast<void*>(ii.hbmColor));

    // 热点放在 (50,40)：按 hotspot 偏移反推左上角
    const int hot_x = 50;
    const int hot_y = 40;
    const int dx    = hot_x - (has_ii ? static_cast<int>(ii.xHotspot) : 0);
    const int dy    = hot_y - (has_ii ? static_cast<int>(ii.yHotspot) : 0);

    const BOOL drew = ::DrawIconEx(dc, dx, dy, cur, 0, 0, 0, nullptr, DI_NORMAL);
    std::printf("DrawIconEx -> %d  (左上角画在 %d,%d)\n", static_cast<int>(drew), dx, dy);

    // ---- 读回像素：先看绘制区 ----
    std::printf("\n--- 绘制区 16x16，格式 BGRA 的十六进制（A=alpha）---\n");
    for (int y = dy; y < dy + 16; ++y) {
        std::printf("y=%3d |", y);
        for (int x = dx; x < dx + 16; ++x) {
            const auto* p = static_cast<const unsigned char*>(img.GetPixelAddress(x, y));
            if (p != nullptr) {
                std::printf(" %02X%02X%02X%02X", p[2], p[1], p[0], p[3]);
            } else {
                std::printf(" ????????");
            }
        }
        std::printf("\n");
    }

    // ---- 再看光标右下方外侧：这里应保持纯底色且 A=FF ----
    std::printf("\n--- 光标右下外侧 12x6（应仍是纯底色 A=FF）---\n");
    bool outside_broken = false;
    for (int y = dy + 22; y < dy + 28; ++y) {
        std::printf("y=%3d |", y);
        for (int x = dx + 22; x < dx + 34; ++x) {
            const auto* p = static_cast<const unsigned char*>(img.GetPixelAddress(x, y));
            if (p != nullptr) {
                std::printf(" %02X%02X%02X%02X", p[2], p[1], p[0], p[3]);
                if (p[3] != 0xFF) outside_broken = true;
            }
        }
        std::printf("\n");
    }
    std::printf("外侧 alpha 异常: %s\n", outside_broken ? "是（会出透明方块）" : "否");

    // ---- 统计整张图有多少像素 alpha != FF ----
    long long bad_alpha = 0;
    long long cursor_px = 0;
    for (int y = 0; y < H; ++y) {
        for (int x = 0; x < W; ++x) {
            const auto* p = static_cast<const unsigned char*>(img.GetPixelAddress(x, y));
            if (p == nullptr) continue;
            if (p[3] != 0xFF) ++bad_alpha;
            // 与底色不同的算"光标像素"
            if (p[0] != 200 || p[1] != 90 || p[2] != 30) ++cursor_px;
        }
    }
    std::printf("\n整图 alpha != FF 的像素数 = %lld / %d\n", bad_alpha, W * H);
    std::printf("整图与底色不同的像素数   = %lld（光标实际覆盖量）\n", cursor_px);

    img.ReleaseDC();
    const HRESULT hr = img.Save("cursor_test.png", Gdiplus::ImageFormatPNG);
    std::printf("\nSave PNG -> hr=0x%08lX\n", static_cast<unsigned long>(hr));

    if (has_ii) {
        if (ii.hbmMask != nullptr) ::DeleteObject(ii.hbmMask);
        if (ii.hbmColor != nullptr) ::DeleteObject(ii.hbmColor);
    }
    Gdiplus::GdiplusShutdown(token);
    return 0;
}
