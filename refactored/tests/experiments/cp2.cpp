// 实验 2：在生产同款路径上验证光标合成。
//
// 实验 1 用的是纯色位图，底色 alpha 本身就是 0，导致"DrawIconEx 没写 alpha"
// 和"DrawIconEx 把 alpha 写成 0"两种情况无法区分。这一版换成真实屏幕抓图：
//
//   1) 真实桌面 BitBlt 出来的 alpha 到底是 0x00 还是 0xFF？
//      （若是 0x00，PNG 会整张透明；用户能看见画面说明是 0xFF，但要有数据）
//   2) 画完光标后，alpha 有没有从 0xFF 变成别的值？
//      （光标周围的透明衬垫若把 alpha 写坏，客户端会看到透明/黑方块）
//   3) 光标的实际落点与 GetCursorInfo 报的位置差多少（hotspot 修正对不对）？
//   4) 存成 PNG 再解回来，GDI+ 有没有在保存环节改动 alpha？
#include <Windows.h>
#include <atlimage.h>
#include <gdiplus.h>

#include <cstdio>
#include <vector>

namespace {

struct Rgba {
    unsigned char r = 0, g = 0, b = 0, a = 0;
};

Rgba sample(CImage& img, int x, int y) {
    Rgba        out;
    const auto* p = static_cast<const unsigned char*>(img.GetPixelAddress(x, y));
    if (p != nullptr) {
        out.b = p[0];
        out.g = p[1];
        out.r = p[2];
        out.a = p[3];
    }
    return out;
}

} // namespace

int main() {
    // 关键：服务端进程是 per-monitor-v2 DPI 感知的（client/main.cpp 与 server/main.cpp 都设了），
    // 所以这里的实验也必须设，否则拿到的是被系统缩放过的"逻辑坐标"，
    // 与生产路径根本不是同一套坐标系（上一版就栽在这上面）。
    ::SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    Gdiplus::GdiplusStartupInput input;
    ULONG_PTR                   token = 0;
    if (Gdiplus::GdiplusStartup(&token, &input, nullptr) != Gdiplus::Ok) {
        std::printf("GdiplusStartup failed\n");
        return 1;
    }

    const int W = ::GetSystemMetrics(SM_CXSCREEN);
    const int H = ::GetSystemMetrics(SM_CYSCREEN);
    std::printf("主屏 %dx%d\n", W, H);

    CImage img;
    if (img.Create(W, H, 32, 0) != 1) {
        std::printf("CImage::Create failed\n");
        return 1;
    }

    HDC screen_dc = ::GetDC(nullptr);
    HDC image_dc  = img.GetDC();
    const BOOL blt = ::BitBlt(image_dc, 0, 0, W, H, screen_dc, 0, 0, SRCCOPY);
    ::ReleaseDC(nullptr, screen_dc);
    std::printf("BitBlt -> %d\n", static_cast<int>(blt));

    // ---- 1) 真实桌面 alpha 抽样：应该是清一色 0xFF ----
    long long ff = 0, zero = 0, other = 0;
    for (int y = 5; y < H; y += 97) {
        for (int x = 5; x < W; x += 89) {
            const Rgba c = sample(img, x, y);
            if (c.a == 0xFF)      ++ff;
            else if (c.a == 0x00) ++zero;
            else                  ++other;
        }
    }
    std::printf("抓屏后 alpha 抽样: FF=%lld  00=%lld  其它=%lld\n", ff, zero, other);

    // ---- 2) 光标信息 ----
    CURSORINFO ci{};
    ci.cbSize = sizeof(ci);
    if (!::GetCursorInfo(&ci)) {
        std::printf("GetCursorInfo 失败\n");
        return 1;
    }
    std::printf("光标 flags=0x%08lX pos=(%ld,%ld)\n",
                static_cast<unsigned long>(ci.flags), ci.ptScreenPos.x, ci.ptScreenPos.y);

    // 把光标挪到一个已知位置，避免依赖用户当前鼠标在哪
    const long orig_x = ci.ptScreenPos.x;
    const long orig_y = ci.ptScreenPos.y;
    const int  hot_x  = 300;
    const int  hot_y  = 200;

    ::SetCursorPos(hot_x, hot_y);

    // 连读三次：如果位置在漂移，说明是用户正在真的移动鼠标（物理鼠标事件
    // 会覆盖 SetCursorPos 的结果），而不是坐标换算问题。这个判断很关键，
    // 否则会把"用户碰了鼠标"误诊成"定位算错了"。
    for (int i = 0; i < 3; ++i) {
        ::Sleep(120);
        CURSORINFO probe{};
        probe.cbSize = sizeof(probe);
        ::GetCursorInfo(&probe);
        std::printf("第 %d 次读取（SetCursorPos 目标 %d,%d）: pos=(%ld,%ld)\n",
                    i + 1, hot_x, hot_y, probe.ptScreenPos.x, probe.ptScreenPos.y);
    }

    CURSORINFO ci2{};
    ci2.cbSize = sizeof(ci2);
    ::GetCursorInfo(&ci2);
    std::printf("继续用 pos=(%ld,%ld) 做命中判断\n", ci2.ptScreenPos.x, ci2.ptScreenPos.y);

    ICONINFO ii{};
    const bool has_ii = ::GetIconInfo(ci2.hCursor, &ii) != FALSE;
    std::printf("GetIconInfo ok=%d hotspot=(%lu,%lu)\n",
                static_cast<int>(has_ii), ii.xHotspot, ii.yHotspot);

    // 画之前先把这一块拍下来，画完好逐像素比对
    const int box_x = static_cast<int>(ci2.ptScreenPos.x) - 36;
    const int box_y = static_cast<int>(ci2.ptScreenPos.y) - 36;
    const int box_w = 72;
    const int box_h = 72;
    std::vector<Rgba> before(static_cast<std::size_t>(box_w) * box_h);
    for (int y = 0; y < box_h; ++y) {
        for (int x = 0; x < box_w; ++x) {
            before[static_cast<std::size_t>(y) * box_w + x] = sample(img, box_x + x, box_y + y);
        }
    }

    // ---- 3) 与生产代码同款：减去 hotspot 偏移后 DrawIconEx ----
    const int dx = static_cast<int>(ci2.ptScreenPos.x)
                   - (has_ii ? static_cast<int>(ii.xHotspot) : 0);
    const int dy = static_cast<int>(ci2.ptScreenPos.y)
                   - (has_ii ? static_cast<int>(ii.yHotspot) : 0);
    const BOOL drew = ::DrawIconEx(image_dc, dx, dy, ci2.hCursor, 0, 0, 0, nullptr, DI_NORMAL);
    std::printf("DrawIconEx(左上角 %d,%d) -> %d\n", dx, dy, static_cast<int>(drew));

    // ---- 4) 逐像素比对：改了多少、alpha 有没有被写坏 ----
    int changed = 0, alpha_broken = 0, alpha_kept = 0;
    int minx = W, miny = H, maxx = -1, maxy = -1;
    for (int y = 0; y < box_h; ++y) {
        for (int x = 0; x < box_w; ++x) {
            const Rgba now = sample(img, box_x + x, box_y + y);
            const Rgba old = before[static_cast<std::size_t>(y) * box_w + x];
            if (now.r == old.r && now.g == old.g && now.b == old.b && now.a == old.a) {
                continue;
            }
            ++changed;
            if (now.a == 0xFF) {
                ++alpha_kept;
            } else {
                ++alpha_broken;
            }
            if (box_x + x < minx) minx = box_x + x;
            if (box_y + y < miny) miny = box_y + y;
            if (box_x + x > maxx) maxx = box_x + x;
            if (box_y + y > maxy) maxy = box_y + y;
        }
    }
    std::printf("\n光标覆盖像素=%d  alpha 保持FF=%d  alpha 被破坏=%d\n",
                changed, alpha_kept, alpha_broken);
    if (maxx >= 0) {
        std::printf("改动像素包围盒 = (%d,%d)-(%d,%d)  尺寸 %dx%d\n",
                    minx, miny, maxx, maxy, maxx - minx + 1, maxy - miny + 1);
        std::printf("GetCursorInfo 报的位置 (%ld,%ld) 在包围盒内: %s\n",
                    ci2.ptScreenPos.x, ci2.ptScreenPos.y,
                    (ci2.ptScreenPos.x >= minx && ci2.ptScreenPos.x <= maxx
                     && ci2.ptScreenPos.y >= miny && ci2.ptScreenPos.y <= maxy)
                        ? "是"
                        : "否");
    } else {
        std::printf("没有任何像素被改动 —— DrawIconEx 没画上任何东西\n");
    }

    img.ReleaseDC();

    // 把热点周围一小块导出成文本，供人工核对图形形状
    const int hx = static_cast<int>(ci2.ptScreenPos.x);
    const int hy = static_cast<int>(ci2.ptScreenPos.y);
    std::printf("\n--- 光标位置周围 24x20（R,G,B,A 十六进制），锚点 (%d,%d) ---\n", hx, hy);
    for (int y = hy - 4; y < hy + 16; ++y) {
        std::printf("y=%4d |", y);
        for (int x = hx - 4; x < hx + 20; ++x) {
            const Rgba c = sample(img, x, y);
            std::printf(" %02X%02X%02X%02X", c.r, c.g, c.b, c.a);
        }
        std::printf("\n");
    }

    const HRESULT hr = img.Save("screen_cursor.png", Gdiplus::ImageFormatPNG);
    std::printf("\nSave PNG -> hr=0x%08lX\n", static_cast<unsigned long>(hr));

    // 把光标还给用户：测试期间挪动过它，用完必须放回去
    ::SetCursorPos(orig_x, orig_y);
    std::printf("光标已还原到 (%ld,%ld)\n", orig_x, orig_y);

    if (has_ii) {
        if (ii.hbmMask != nullptr) ::DeleteObject(ii.hbmMask);
        if (ii.hbmColor != nullptr) ::DeleteObject(ii.hbmColor);
    }
    Gdiplus::GdiplusShutdown(token);
    return 0;
}
