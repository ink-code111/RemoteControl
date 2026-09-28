#include "delta_capturer.hpp"

#include "capture_internal.hpp"
#include "logger.hpp"
#include "win_raii.hpp"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#include <atlimage.h>
#include <gdiplus.h>

#include <algorithm>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <type_traits>

namespace rc::server {

// 编译期保证 token 类型与 GDI+ 要求的 ULONG_PTR 完全一致，
// 防止将来有人把它改回 unsigned long 又引入 32 位截断
static_assert(std::is_same<std::uintptr_t, ULONG_PTR>::value,
              "gdiplus_token_ 必须与 ULONG_PTR 同宽");

namespace {

/// 差异检测的横向"段宽"。取 64 像素而不是 8/16：
/// 段越小，一次光标移动命中的段数越多（每段都是一次函数调用开销）；
/// 段越大，大范围变化时的横向定位越粗（会把少许未变像素也圈进脏矩形）。
/// 64 在两者之间，且按 64 对齐——1707/64 ≈ 27 段/行。
constexpr int kTile = 64;

/// 脏面积超过整屏这个比例就干脆发整帧。
/// 为什么不是"脏就值得发增量"：增量帧要额外付"客户端合成一次 BitBlt"的成本，
/// 而且累积式画面一旦链断就要等关键帧恢复；接近整屏时整帧反而更简单也更稳。
constexpr double kFullFrameRatio = 0.85;

/// 最多同时维护多少个消费者的基准帧。
/// 一份基准帧在 2560×1440 下 = 14.7 MB，不设上限的话 32 个客户端就是 470 MB。
/// 超出上限时按 LRU 淘汰——被淘汰的消费者不会出错，只是下一帧退化成整帧。
constexpr std::size_t kMaxConsumers = 8;

/// 【多块脏矩形上限】—— 复用 diff_bbox 已有那次 64px 瓦片扫描，把"命中瓦片"记下来。
/// diff_bbox 没多扫一个字节，所以"加观测会改变被测对象"不成立；观测本身（2D 图 +
/// 4-连通 BFS）的耗时单独记到 diff_tile_ms_sum_ 里，**自证**代价可忽略。
///
/// 【为什么"命中瓦片面积"不等于 Σ(命中瓦片 64×64)】
///   diff_bbox 的包围盒本身就是**纵向精确到行、横向 64px 瓦片粒度**的（min_x 用 x0、
///   max_x 用 xe），所以口径对得上的"命中面积"必须也是纵向行精确、横向瓦片粒度：
///     hit_row_px = Σ_over_脏行 (该行命中瓦片的实际宽度)
///   如果直接把命中瓦片按 64×64 累加，纵向会被放大到 64 行对齐 —— 一个 5 行高的变化
///   会被算成 64 行，比值反而 >1（看起来像"多块能省成负数"），所以那个量只作对照用。
///
/// 【为什么只统计增量帧】
///   与 dirty_px_sum_ / delta_px_sum_ 同口径 —— 整帧的脏区恒为整屏，混进来会把
///   "能省多少"抬到 100% 与"应该发整帧"的语义自相矛盾。
struct TileProbe {
    int tx = 0;  ///< 瓦片列数（自证粒度）
    int ty = 0;  ///< 瓦片行数
    int w  = 0;  ///< 屏幕宽（finish 时用来算边界瓦片的实际宽度）
    int h  = 0;  ///< 屏幕高
    std::vector<std::uint8_t> hit;   ///< 2D 命中图（tx*ty）

    std::size_t hit_row_px  = 0;     ///< 主指标：行精确 × 横向瓦片
    std::size_t hit_tile_px = 0;     ///< 对照：2D 瓦片粒度的实际面积
    int         blocks      = 0;     ///< 4-连通块数（≈ 多块方案至少要传几块）

    /// 每帧入口由 finish() 调用前调一次（保证 tx/ty 与 diff_bbox 的瓦片扫描一致）。
    void begin(int W, int H) {
        w = W; h = H;
        tx = (W + kTile - 1) / kTile;
        ty = (H + kTile - 1) / kTile;
        hit.assign(static_cast<std::size_t>(tx) * static_cast<std::size_t>(ty), 0);
        hit_row_px = 0;
        hit_tile_px = 0;
        blocks = 0;
    }

    /// 扫描结束后算：2D 实际面积 + 4-连通块数。**就地清零** hit，
    /// 这样下次 begin() 的 assign 不会保留旧数据。
    void finish() {
        if (hit.empty()) return;
        std::vector<int> stk;
        stk.reserve(64);
        for (int tyi = 0; tyi < ty; ++tyi) {
            const int py = std::min(kTile, h - tyi * kTile);
            const auto row_base = static_cast<std::size_t>(tyi) * tx;
            for (int txi = 0; txi < tx; ++txi) {
                const auto i = row_base + txi;
                if (hit[i] == 0) continue;
                const int px = std::min(kTile, w - txi * kTile);
                hit_tile_px += static_cast<std::size_t>(px) * py;
                // 4-连通 BFS（显式栈：920 格量级，递归会爆栈）
                ++blocks;
                hit[i] = 0;
                stk.push_back(static_cast<int>(i));
                while (!stk.empty()) {
                    const int cur = stk.back(); stk.pop_back();
                    const int cx = cur % tx;
                    const int cy = cur / tx;
                    if (cx > 0) {
                        const int ni = cur - 1;
                        if (hit[ni]) { hit[ni] = 0; stk.push_back(ni); }
                    }
                    if (cx + 1 < tx) {
                        const int ni = cur + 1;
                        if (hit[ni]) { hit[ni] = 0; stk.push_back(ni); }
                    }
                    if (cy > 0) {
                        const int ni = cur - tx;
                        if (hit[ni]) { hit[ni] = 0; stk.push_back(ni); }
                    }
                    if (cy + 1 < ty) {
                        const int ni = cur + tx;
                        if (hit[ni]) { hit[ni] = 0; stk.push_back(ni); }
                    }
                }
            }
        }
        hit.clear();  // 让下次 begin() 不会复用旧数据（begin 里有 assign，但容量收掉更稳）
    }
};

/// diff_bbox 用来顺手写命中图的实例。**复用**而非每帧新建：920 字节 × 25 fps 也才
/// 23 KB/s 的堆抖动，但项目在意观测对运行时的影响（GDI 历史上有过对象泄漏教训）。
TileProbe g_tile_probe;

// ImageDc（CImage 的 DC RAII）与 detail::composite_system_cursor(HDC) 都定义在
// capture_internal.hpp 里 —— GDI 后端也要用它们，放在这里就变成两份会各自漂移的实现。

/// 逐行比对两帧，求变化区域的**并集包围盒**（整屏坐标系，原点左上）。
/// @return false 表示两帧完全相同（没有任何脏区）。
///
/// 【算法选择】为什么不直接铺 64×64 瓦片网格：
///   瓦片网格会把"光标移动 5 像素"这种变化放大成 1~4 个整瓦片的脏区；
///   而逐行先筛一遍只需付一次整屏大小的 memcmp（内存带宽约 0.5 ms），
///   之后只对**脏行**做横向定位（光标只有 ~18 行高 → 18×40 次小 memcmp），
///   既更准也更快。整屏都在变的最坏情况也只是退化成一次全量扫描，与瓦片网格同级。
///
/// 【为什么用 GetPixelAddress 而不是 GetBits()+手算 pitch】
///   CImage 的 DIB 是自下而上还是自上而下取决于创建参数，手算 pitch 极易把 Y 轴搞反
///   （现象是差异矩形上下颠倒，而且画面上看不出"错"、只觉得偶尔花屏）。
///   GetPixelAddress 直接给出**逻辑坐标**下的像素地址，方向问题根本不存在。
///
/// @param probe 可选：顺手把"命中瓦片"写进 `g_tile_probe`（多块脏矩形上限的量法）。
///               不传则与改造前完全等价——这条路径上**没有一个字节被多扫**。
bool diff_bbox(ATL::CImage& cur, ATL::CImage& prev, int w, int h,
               int& bx, int& by, int& bw, int& bh, TileProbe* probe = nullptr) {
    BYTE* cur0  = static_cast<BYTE*>(cur.GetPixelAddress(0, 0));
    BYTE* prev0 = static_cast<BYTE*>(prev.GetPixelAddress(0, 0));
    if (cur0 == nullptr || prev0 == nullptr) {
        return true; // 拿不到像素地址（非 DIB 段）：保守当作整屏都变了
    }
    // 用相邻两行的地址差求 pitch，而不是假设正负号
    std::ptrdiff_t pitch = static_cast<std::ptrdiff_t>(w) * 4;
    if (h >= 2) {
        BYTE* cur1 = static_cast<BYTE*>(cur.GetPixelAddress(0, 1));
        if (cur1 != nullptr) {
            pitch = cur1 - cur0;
        }
    }

    const std::size_t row_bytes = static_cast<std::size_t>(w) * 4;
    const int         tiles     = (w + kTile - 1) / kTile;

    int min_x = w;
    int max_x = -1;
    int min_y = h;
    int max_y = -1;

    for (int y = 0; y < h; ++y) {
        const BYTE* a = cur0 + static_cast<std::ptrdiff_t>(y) * pitch;
        const BYTE* b = prev0 + static_cast<std::ptrdiff_t>(y) * pitch;
        if (std::memcmp(a, b, row_bytes) == 0) {
            continue; // 整行相同：跳过横向定位
        }
        if (y < min_y) {
            min_y = y;
        }
        max_y = y;

        for (int t = 0; t < tiles; ++t) {
            const int         x0  = t * kTile;
            const int         px  = std::min(kTile, w - x0);
            const std::size_t len = static_cast<std::size_t>(px) * 4;
            const auto        off = static_cast<std::ptrdiff_t>(x0) * 4;
            if (std::memcmp(a + off, b + off, len) != 0) {
                if (x0 < min_x) {
                    min_x = x0;
                }
                const int xe = std::min(w, x0 + kTile);
                if (xe > max_x) {
                    max_x = xe;
                }
                if (probe != nullptr) {
                    // 用 hit[idx] 兼当"本帧是否已记录"标记：同一瓦片高度 = kTile 行，
                    // 所以 (ty=tile_row, tx=t) 会被本瓦片内多个扫描行重复命中；
                    // 不去重的话 hit_row_px 会被重复累加 64 倍。
                    const auto idx = static_cast<std::size_t>(y / kTile) * probe->tx + t;
                    if (probe->hit[idx] == 0) {
                        probe->hit[idx] = 1;
                        probe->hit_row_px += static_cast<std::size_t>(xe - x0);
                    }
                }
            }
        }
    }

    if (max_x < 0) {
        return false; // 所有行都相同
    }
    bx = min_x;
    by = min_y;
    bw = max_x - min_x;
    bh = max_y - min_y + 1;
    return true;
}

/// PNG 编码到内存流。抽成独立函数是因为整帧与裁剪帧都要走这条路径，
/// 而第二阶段那段"GlobalAlloc → CreateStreamOnHGlobal → Save → Stat → 取字节"
/// 的所有权移交细节（fDeleteOnRelease 与 guard 的配合）只该写对一次。
bool encode_png(ATL::CImage& image, std::vector<char>& out) {
    HGLOBAL hglobal = ::GlobalAlloc(GMEM_MOVEABLE, 0);
    if (hglobal == nullptr) {
        RC_LOG_ERROR("GlobalAlloc failed");
        return false;
    }
    // RAII 兜底：若后续 CreateStreamOnHGlobal 失败，由此 guard 释放
    win::GlobalMemPtr hglobal_guard(hglobal);

    IStream* raw_stream = nullptr;
    if (FAILED(::CreateStreamOnHGlobal(hglobal, TRUE /*fDeleteOnRelease*/, &raw_stream))) {
        return false; // hglobal 由上面的 guard 释放
    }
    // fDeleteOnRelease=TRUE：流 Release() 时自动释放底层内存。
    // 所有权已移交给流，guard 必须 release() 放行，避免双重释放
    //（旧版代码正是在这里 Release 之后又 GlobalFree，属于双重释放）。
    hglobal_guard.release();
    std::unique_ptr<IStream, win::IStreamReleaser> stream(raw_stream);

    if (FAILED(image.Save(stream.get(), Gdiplus::ImageFormatPNG))) {
        RC_LOG_ERROR("CImage::Save(PNG) failed");
        return false;
    }

    STATSTG stat{};
    if (FAILED(stream->Stat(&stat, STATFLAG_NONAME))) {
        RC_LOG_ERROR("IStream::Stat failed");
        return false;
    }
    const std::size_t size = static_cast<std::size_t>(stat.cbSize.QuadPart);
    if (size == 0) {
        return false;
    }

    void* mem = ::GlobalLock(hglobal);
    if (mem == nullptr) {
        RC_LOG_ERROR("GlobalLock failed");
        return false;
    }
    out.resize(size);
    std::memcpy(out.data(), mem, size);
    ::GlobalUnlock(hglobal);
    return true;
}

/// 单个消费者（会话）的差异帧基准。
struct ConsumerBase {
    CImage        img;               ///< 该消费者手上已有的那张图（含光标合成结果）
    bool          valid          = false;
    std::uint32_t since_keyframe = 0; ///< 距上次整帧过了多少帧（到点强制整帧）
    std::uint64_t last_use       = 0; ///< LRU 时钟
};

} // namespace

/// 基准帧表。放在 .cpp 里定义，头文件只留一个前向声明的 unique_ptr。
struct DeltaCapturerBase::BaseStore {
    std::mutex                            mu;
    std::map<std::uint32_t, ConsumerBase> by_consumer;
    std::uint64_t                         tick = 0;
};

DeltaCapturerBase::DeltaCapturerBase(bool enable_delta, std::uint32_t keyframe_interval)
    : enable_delta_(enable_delta),
      keyframe_interval_(keyframe_interval == 0 ? 1 : keyframe_interval),
      bases_(std::make_unique<BaseStore>()) {
    // GDI+ 生命周期在基类：PNG 编码发生在 finish()，两个后端都要用
    Gdiplus::GdiplusStartupInput input;
    if (Gdiplus::GdiplusStartup(&gdiplus_token_, &input, nullptr) != Gdiplus::Ok) {
        gdiplus_token_ = 0;
        RC_LOG_WARN("GdiplusStartup failed, PNG encoding may not work");
    }
}

DeltaCapturerBase::~DeltaCapturerBase() {
    if (gdiplus_token_ != 0) {
        Gdiplus::GdiplusShutdown(gdiplus_token_);
        gdiplus_token_ = 0;
    }
}

void DeltaCapturerBase::composite_cursor(ATL::CImage& image) const {
    if (!capture_cursor_ || image.IsNull()) {
        return;
    }
    detail::ImageDc dc(image);
    if (dc.get() == nullptr) {
        return;
    }
    detail::composite_system_cursor(dc.get());
}

void DeltaCapturerBase::release_consumer(std::uint32_t consumer_id) noexcept {
    // 会话关闭时调用。基准帧是 MB 级的位图，不释放的话每个连过的客户端
    // 都会在服务端留一份，长时间运行就是稳定增长的内存泄漏。
    if (bases_ == nullptr) {
        return;
    }
    std::lock_guard<std::mutex> lock(bases_->mu);
    bases_->by_consumer.erase(consumer_id);
}

bool DeltaCapturerBase::capture(CapturedFrame& out, std::uint32_t consumer_id) {
    out.data.clear();
    out.width  = 0;
    out.height = 0;
    out.delta  = false;
    out.rect_x = 0;
    out.rect_y = 0;
    out.rect_w = 0;
    out.rect_h = 0;

    // 空档：请求到达 → 抓屏开始。默认构造（0）表示调用方没给时刻，不计入。
    const auto t_grab_begin = std::chrono::steady_clock::now();
    double     wait_ms      = 0.0;
    if (out.request_at.time_since_epoch().count() != 0) {
        wait_ms = std::chrono::duration<double, std::milli>(t_grab_begin - out.request_at).count();
    }

    // ---- 1) 抓屏（唯一由后端决定的一段）----
    CImage    cur;
    GrabTiming t{};
    if (!grab(cur, t)) {
        return false;
    }
    if (cur.IsNull() || cur.GetWidth() <= 0 || cur.GetHeight() <= 0) {
        // 后端契约违约：grab() 必须给出一张有效位图。显式报出来，别让它在后面
        // 变成一张 0x0 的图悄悄发出去（客户端会拿到"画面全黑"而服务端毫无提示）。
        RC_LOG_ERROR("[{}] grab() 返回了无效位图 {}x{}", backend(), cur.GetWidth(), cur.GetHeight());
        return false;
    }

    return finish(cur, t, wait_ms, out, consumer_id);
}

bool DeltaCapturerBase::finish(ATL::CImage& cur, const GrabTiming& t, double wait_ms,
                               CapturedFrame& out, std::uint32_t consumer_id) {
    // 【诊断，默认关】反向对照用：故意每帧泄漏 N 个 GDI 对象。
    // `debug_leak_gdi_per_frame_ == 0`（默认）时这段一行都不执行，与被测行为**逐字等价**。
    // 泄漏的是内存 DC（最典型的 GDI 对象），量级与历史上真出过的那个 bug
    // （`GetIconInfo` 的掩码 + 彩色位图没归还 = 每帧 2 个）一致 —— 详见 config.hpp 字段注释。
    for (std::uint32_t i = 0; i < debug_leak_gdi_per_frame_; ++i) {
        (void)::CreateCompatibleDC(nullptr); // 故意的：不 DeleteDC
    }

    const int width  = cur.GetWidth();
    const int height = cur.GetHeight();

    bool   need_full = !enable_delta_; // 关了差异帧：每帧都是整帧（第二阶段行为）
    bool   changed   = true;           // 只在"增量"路径下有意义
    int    bx = 0, by = 0, bw = 0, bh = 0;
    CImage crop;                       // 需要裁剪时才是有效位图
    double diff_ms      = 0.0;
    double diff_scan_ms = 0.0;
    double diff_crop_ms = 0.0;
    double encode_ms    = 0.0;

    // ---- 2) 差异检测：相对"这个消费者手上已有的那帧" ----
    if (enable_delta_) {
        const auto t_diff_begin = std::chrono::steady_clock::now();
        {
            std::lock_guard<std::mutex> lock(bases_->mu);
            auto& base = bases_->by_consumer[consumer_id]; // 首次访问自动建条目
            base.last_use = ++bases_->tick;

            // LRU 淘汰：只在超限且本次是"新消费者"时才动手
            if (bases_->by_consumer.size() > kMaxConsumers) {
                auto victim = bases_->by_consumer.begin();
                for (auto it = bases_->by_consumer.begin(); it != bases_->by_consumer.end(); ++it) {
                    if (it->first != consumer_id && it->second.last_use < victim->second.last_use) {
                        victim = it;
                    }
                }
                if (victim->first != consumer_id) {
                    RC_LOG_WARN("delta base evicted: consumer {} (LRU, {} bases kept)", victim->first,
                                kMaxConsumers);
                    bases_->by_consumer.erase(victim);
                }
            }

            if (!base.valid || base.img.IsNull() || base.img.GetWidth() != width ||
                base.img.GetHeight() != height || base.since_keyframe >= keyframe_interval_) {
                // 没有基准 / 分辨率变了 / 到关键帧点：这次发整帧
                //
                // 【分辨率变了也要发整帧 —— 这一条在换后端时会真的用上】
                // DXGI 给的是物理 2560×1440，而 GDI 在 unaware 下只给 1707×960。
                // 后端一换，帧尺寸就变，旧基准必须作废：否则客户端会把新尺寸的
                // 增量贴到旧尺寸的累积画面上，画面从此永久错乱（而且不报任何错）。
                need_full = true;
            } else {
                // 【瓦片探测】多块脏矩形上限的量法。diff_bbox 不多扫一个字节；
                // 这套 begin/finish（含 2D 命中图 + 4-连通 BFS）的代价在下面
                // 单独计到 diff_tile_ms_sum_ 里、自证可忽略。
                g_tile_probe.begin(width, height);
                const auto t_tile_begin = std::chrono::steady_clock::now();
                changed = diff_bbox(cur, base.img, width, height, bx, by, bw, bh, &g_tile_probe);
                g_tile_probe.finish();
                const auto t_tile_done = std::chrono::steady_clock::now();
                const double tile_stat_ms =
                    std::chrono::duration<double, std::milli>(t_tile_done - t_tile_begin).count();
                if (changed) {
                    const auto dirty = static_cast<double>(bw) * static_cast<double>(bh);
                    const auto total = static_cast<double>(width) * static_cast<double>(height);
                    if (dirty > kFullFrameRatio * total) {
                        need_full = true; // 几乎整屏都在变，增量不划算
                    } else {
                        // 只有"决定发增量"的帧才进这块统计（与 dirty_px_sum_ 同一口径）：
                        // 整帧的脏区恒为整屏，混进来会把"能省多少"抬到 100% 与
                        // "应该发整帧"的语义自相矛盾。
                        tile_hit_row_px_sum_  += g_tile_probe.hit_row_px;
                        tile_hit_tile_px_sum_ += g_tile_probe.hit_tile_px;
                        tile_blocks_sum_      += static_cast<std::uint64_t>(g_tile_probe.blocks);
                        tile_blocks_max_      = std::max(tile_blocks_max_,
                                                          static_cast<std::uint64_t>(g_tile_probe.blocks));
                        ++tile_delta_count_;
                        const auto bbox_px = static_cast<std::size_t>(bw) * static_cast<std::size_t>(bh);
                        tile_bbox_px_sum_ += bbox_px;
                        tile_ratio_.push_back(bbox_px > 0
                                                  ? static_cast<double>(g_tile_probe.hit_row_px) / bbox_px
                                                  : 1.0);
                        tile_blocks_.push_back(g_tile_probe.blocks);
                        diff_tile_ms_sum_ += tile_stat_ms;
                    }
                }
            }
        }

        const auto t_scan_done = std::chrono::steady_clock::now();

        // 裁剪：从 cur 直接拷出脏区域。放在锁外——它是内存拷贝，
        // 而锁内还有 release_consumer() 在路上（会话关闭时），不该被它等。
        if (!need_full && changed) {
            if (crop.Create(bw, bh, 32, 0) != 1) {
                RC_LOG_WARN("crop Create({}x{}) failed, falling back to full frame", bw, bh);
                need_full = true;
            } else {
                // 需要 cur 的 DC —— 抓屏阶段的 DC 已经释放了，这里重新取一次。
                // 这次取放**计入"裁剪"**而不是"抓屏"，因为它是裁剪这件事的成本。
                // 之所以不改成纯内存拷贝：那会改动一条已通过回归的链路，
                // 而项目规矩是"改链路必须配 A/B"。这里保留原行为，代价是几十微秒。
                detail::ImageDc     crop_dc(crop);
                detail::ImageDc     cur_dc(cur);
                if (crop_dc.get() == nullptr || cur_dc.get() == nullptr ||
                    !::BitBlt(crop_dc.get(), 0, 0, bw, bh, cur_dc.get(), bx, by, SRCCOPY)) {
                    RC_LOG_WARN("crop BitBlt failed, falling back to full frame");
                    need_full = true;
                }
            }
        }

        // 比对（含裁剪拷贝）单独计时。差异帧的经济账就写在这里：
        // 只有"省下的编码时间 > 付出的比对成本"，这改造才真的赚。
        // 再拆成"扫描"与"裁剪"两段：扫描是纯 CPU 的逐行比对（DXGI 省不掉，
        // 因为服务端仍要自己判断有没有变化），而裁剪是一次像素拷贝 ——
        // 换成 DXGI 的原生脏矩形后这段就可以整段消失，值不值有数可依。
        const auto t_crop_done = std::chrono::steady_clock::now();
        diff_scan_ms = std::chrono::duration<double, std::milli>(t_scan_done - t_diff_begin).count();
        diff_crop_ms = std::chrono::duration<double, std::milli>(t_crop_done - t_scan_done).count();
        diff_ms      = std::chrono::duration<double, std::milli>(t_crop_done - t_diff_begin).count();
    }

    // ---- 3) 编码：整帧编 cur，增量编 crop，无变化则一个字节都不编 ----
    std::size_t payload_bytes = 0;
    // 初值必须是 0 而不是整屏：无变化的空增量帧实际编码了 0 个像素，
    // 若按整屏计，"脏区占比"会被空帧抬到 90% 以上，与"编码只要 1 ms"自相矛盾。
    std::size_t dirty_px      = 0;

    const bool idle_frame     = (!need_full && !changed);
    const auto t_encode_begin = std::chrono::steady_clock::now();
    if (!idle_frame) {
        ATL::CImage& src = need_full ? cur : crop;
        if (!encode_png(src, out.data)) {
            return false;
        }
        payload_bytes = out.data.size();
        dirty_px      = need_full ? static_cast<std::size_t>(width) * static_cast<std::size_t>(height)
                                  : static_cast<std::size_t>(bw) * static_cast<std::size_t>(bh);
    }
    encode_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() -
                                                         t_encode_begin)
                    .count();

    // 整屏尺寸永远带上（差异帧的 data 只是其中一块）
    out.width  = width;
    out.height = height;
    out.delta  = !need_full;
    if (out.delta && changed) {
        out.rect_x = bx;
        out.rect_y = by;
        out.rect_w = bw;
        out.rect_h = bh;
    }

    // ---- 4) 更新该消费者的基准帧 ----
    // 用 find() 而不是 operator[]：会话可能在本帧抓取期间关闭并调用了
    // release_consumer()，此时再 operator[] 就会凭空重建一条永不回收的条目。
    if (enable_delta_) {
        std::lock_guard<std::mutex> lock(bases_->mu);
        auto                        it = bases_->by_consumer.find(consumer_id);
        if (it != bases_->by_consumer.end()) {
            ConsumerBase& base = it->second;
            if (!base.img.IsNull()) {
                base.img.Destroy(); // Attach 要求当前未挂接任何位图
            }
            base.img.Attach(cur.Detach()); // 句柄平移，O(1)，没有整屏的像素拷贝
            base.valid          = true;
            base.since_keyframe = need_full ? 0 : base.since_keyframe + 1;
            base.last_use       = ++bases_->tick;
        }
    }

    // ---- 5) 分段耗时汇总（每 5 秒一条）----
    grab_ms_sum_        += t.total_ms;
    grab_dc_ms_sum_     += t.prep_ms;
    grab_blit_ms_sum_   += t.move_ms;
    grab_cursor_ms_sum_ += t.cursor_ms;
    grab_rel_ms_sum_    += t.release_ms;
    diff_ms_sum_        += diff_ms;
    diff_scan_ms_sum_   += diff_scan_ms;
    diff_crop_ms_sum_   += diff_crop_ms;
    encode_ms_sum_      += encode_ms;
    wait_ms_sum_        += wait_ms;
    if (!idle_frame) {
        // 同口径样本：客户端那边"往返/解码/贴图"只在有变化的帧上有值，
        // 所以服务端也要单独留一份"有变化帧"的平均，两边才能相减（见 hpp 注释）
        ++chg_count_;
        chg_grab_ms_sum_   += t.total_ms;
        chg_diff_ms_sum_   += diff_ms;
        chg_encode_ms_sum_ += encode_ms;
    }
    if (wait_ms > 10.0) {
        ++wait_gt10_count_;
    }

    ++capture_count_;
    if (need_full) {
        ++keyframe_count_;
    } else {
        ++delta_count_;
    }
    if (idle_frame) {
        ++idle_count_;
    }
    bytes_sum_    += payload_bytes;
    dirty_px_sum_ += dirty_px;
    if (!idle_frame && !need_full) {
        // 增量帧（真正编了一块 crop）单独一套样本。**整帧不进这一套** —— 见 hpp 注释：
        // 整帧的脏区恒为整屏，混进来会把"脏区占比"和"ms/Mpx"两个量一起带偏。
        delta_px_sum_        += dirty_px;
        delta_encode_ms_sum_ += encode_ms;
    }

    maybe_report(width, height);
    return true;
}

void DeltaCapturerBase::maybe_report(int width, int height) {
    const auto now = std::chrono::steady_clock::now();
    if (last_report_.time_since_epoch().count() == 0) {
        last_report_ = now; // 首次抓屏只用来起表，不产出一行"半截"统计
        return;
    }
    const double secs = std::chrono::duration<double>(now - last_report_).count();
    if (secs < 5.0) {
        return;
    }

    // 基准帧表的大小要在锁里读：release_consumer() 会从会话线程改它
    std::size_t consumers = 0;
    if (enable_delta_) {
        std::lock_guard<std::mutex> lock(bases_->mu);
        consumers = bases_->by_consumer.size();
    }
    const double n = static_cast<double>(capture_count_);

    // 【这两条日志的格式不能动】
    // 上面那条被 tests/run_frame_rate_probe.py 的 RE_CAP 正则按字段位置解析，
    // 而且旧版二进制打同样的行 —— 改了格式，"改造前 vs 改造后"就再也对不上。
    // 要加字段就追加到行尾（`帧 WxH` 就是这么加的，见下）。
    RC_LOG_INFO("[capture] {:.1f} fps | 抓屏 {:.1f} + 比对 {:.1f} + 编码 {:.1f} = {:.1f} ms/帧 | "
                "{:.2f} MB/帧 | 脏区 {:.1f}% | 整帧 {} 增量 {}(空 {}) | {} 消费者",
                n / secs, grab_ms_sum_ / n, diff_ms_sum_ / n, encode_ms_sum_ / n,
                (grab_ms_sum_ + diff_ms_sum_ + encode_ms_sum_) / n,
                static_cast<double>(bytes_sum_) / n / (1024.0 * 1024.0),
                100.0 * static_cast<double>(dirty_px_sum_) / n /
                    (static_cast<double>(width) * static_cast<double>(height)),
                keyframe_count_, delta_count_, idle_count_, consumers);

    // 同一批数据再打一条拆解行。**不并进上面那条**（理由见上）。
    //
    // 末尾的 "帧 WxH" 是**判定 dpi_aware / 抓屏后端是否真的生效**的硬证据：
    //   · GDI + unaware → 1707x960；GDI + dpi_aware=on → 2560x1440（§6.12）；
    //   · DXGI          → 2560x1440，且**与 DPI 上下文无关**（§6.14）。
    // 没有这一个数，"改了配置但没生效"会让 A/B 两轮其实测的是同一个东西，
    // 而结论看起来完全正常 —— 这类静默失效正是本项目吃过亏的地方。
    // 另外注意 1707x960 不是"桌面缩小版"，而是物理画面**左上角 1:1 的裁剪**（§6.13）。
    RC_LOG_INFO("[capture-x] 抓屏拆解 | DC {:.2f} + BitBlt {:.2f} + 光标 {:.2f} + 释放 {:.2f} "
                "= {:.2f} ms | 比对 扫描 {:.2f} + 裁剪 {:.2f} = {:.2f} ms | 编码 {:.2f} ms | "
                "空档(请求→抓屏) {:.2f} ms，>10ms {} 帧 | 变化帧净工作 {:.2f} ms"
                "(抓屏 {:.2f} + 比对 {:.2f} + 编码 {:.2f}，{} 帧) | 帧 {}x{} | 后端 {}",
                grab_dc_ms_sum_ / n, grab_blit_ms_sum_ / n, grab_cursor_ms_sum_ / n,
                grab_rel_ms_sum_ / n, grab_ms_sum_ / n, diff_scan_ms_sum_ / n,
                diff_crop_ms_sum_ / n, diff_ms_sum_ / n, encode_ms_sum_ / n,
                wait_ms_sum_ / n, wait_gt10_count_,
                chg_count_ > 0
                    ? (chg_grab_ms_sum_ + chg_diff_ms_sum_ + chg_encode_ms_sum_) /
                          static_cast<double>(chg_count_)
                    : 0.0,
                chg_count_ > 0 ? chg_grab_ms_sum_ / static_cast<double>(chg_count_) : 0.0,
                chg_count_ > 0 ? chg_diff_ms_sum_ / static_cast<double>(chg_count_) : 0.0,
                chg_count_ > 0 ? chg_encode_ms_sum_ / static_cast<double>(chg_count_) : 0.0,
                chg_count_, width, height, backend());

    // 【为什么单起一行】上面两行的字段被脚本按**位置**正则解析、且旧版二进制打同样的行，
    // 所以这两个新量**另起一行**（不追加到行尾，免得动到按位置解析的消费方）。
    //
    // 它回答两个在别处问不出来的问题：
    //   · **增量帧脏区占比** = 客户端若改成"按脏区重绘"能省多少。窗口与整帧覆盖同一块
    //     内容、只差一个固定缩放比，所以"帧内脏区占比"就等于"窗口内需要重绘的占比"。
    //     用的是**包围盒**（`dirty_px = bw*bh`）⇒ 比真实并集**偏大**，对"能省多少"偏保守。
    //   · **归一化编码 ms/Mpx** = 编码耗时里"内容量"和"别的"各占多少。原始编码耗时随脏区
    //     大小变（实测两轮同配置差 3.5 倍），要拿它比"两轮可不可重复"，**必须先除以像素数**。
    if (delta_count_ > idle_count_ && width > 0 && height > 0) {
        const auto   dframes = static_cast<double>(delta_count_ - idle_count_);
        const double dmpx    = static_cast<double>(delta_px_sum_) / 1e6;
        const double fmpx    = static_cast<double>(width) * static_cast<double>(height) / 1e6;
        RC_LOG_INFO("[capture-dirty] 增量 {} 帧（整帧 {} / 空 {}）| 脏区均值 {:.2f} Mpx"
                    "（占整帧 {:.1f}%）| 编码均值 {:.2f} ms | 归一化编码 {:.3f} ms/Mpx"
                    " | 全部变化帧归一化 {:.3f} ms/Mpx",
                    delta_count_ - idle_count_, keyframe_count_, idle_count_, dmpx / dframes,
                    100.0 * dmpx / dframes / fmpx, delta_encode_ms_sum_ / dframes,
                    delta_px_sum_ > 0 ? delta_encode_ms_sum_ / dmpx : 0.0,
                    dirty_px_sum_ > 0
                        ? chg_encode_ms_sum_ / (static_cast<double>(dirty_px_sum_) / 1e6)
                        : 0.0);
    }

    // 【多块脏矩形上限】—— §"客户端按脏区重绘" 之后的下一个候选方向。
    // 主指标 `命中/包围盒` 的口径与现有包围盒**完全一致**（纵向行精确、横向 64px 瓦片），
    // 所以可以直接拿这个比值与现在的"脏区占比"对照 —— 多块能省多少 = 1 - 这个比值。
    //
    // 4-连通块数给"协议要预留几块"一个量级感：实测若普遍在 1~2 ⇒ 多块基本没戏；
    // 若常见到 4~8 ⇒ 才有动手的工程价值。
    //
    // **代价自证**：统计耗时单独计到 diff_tile_ms_sum_ 里 —— 若哪天这个数逼近
    // 比对本身的耗时（diff_ms_sum_），就说明该把 begin/finish 拆成降采样了。
    if (tile_delta_count_ > 0 && width > 0 && height > 0 && !tile_ratio_.empty()) {
        std::sort(tile_ratio_.begin(), tile_ratio_.end());
        std::sort(tile_blocks_.begin(), tile_blocks_.end());
        const auto at_q = [](const std::vector<double>& v, double q) {
            const std::size_t i = std::min(
                v.size() - 1,
                static_cast<std::size_t>(q * static_cast<double>(v.size() - 1)));
            return v[i];
        };
        const auto at_qi = [](const std::vector<int>& v, double q) {
            const std::size_t i = std::min(
                v.size() - 1,
                static_cast<std::size_t>(q * static_cast<double>(v.size() - 1)));
            return v[i];
        };
        RC_LOG_INFO("[capture-tiles] 瓦片 {}px（{}x{} 网格）| 增量 {} 帧 | "
                    "命中/包围盒 P50 {:.1f}% / P95 {:.1f}% | 平均 {:.1f}% | "
                    "4-连通块数 P50 {} / P95 {} / max {} | "
                    "命中面积均值 {:.2f} Mpx vs 包围盒均值 {:.2f} Mpx | "
                    "统计耗时 {:.2f} ms",
                    kTile, g_tile_probe.tx, g_tile_probe.ty, tile_delta_count_,
                    100.0 * at_q(tile_ratio_, 0.50), 100.0 * at_q(tile_ratio_, 0.95),
                    100.0 * static_cast<double>(tile_hit_row_px_sum_) /
                        static_cast<double>(tile_bbox_px_sum_),
                    at_qi(tile_blocks_, 0.50), at_qi(tile_blocks_, 0.95), tile_blocks_max_,
                    static_cast<double>(tile_hit_row_px_sum_) / 1e6 / tile_delta_count_,
                    static_cast<double>(tile_bbox_px_sum_) / 1e6 / tile_delta_count_,
                    diff_tile_ms_sum_);
    }

    last_report_        = now;
    capture_count_      = 0;
    keyframe_count_     = 0;
    delta_count_        = 0;
    idle_count_         = 0;
    grab_ms_sum_        = 0.0;
    grab_dc_ms_sum_     = 0.0;
    grab_blit_ms_sum_   = 0.0;
    grab_cursor_ms_sum_ = 0.0;
    grab_rel_ms_sum_    = 0.0;
    diff_ms_sum_        = 0.0;
    diff_scan_ms_sum_   = 0.0;
    diff_crop_ms_sum_   = 0.0;
    encode_ms_sum_      = 0.0;
    wait_ms_sum_        = 0.0;
    wait_gt10_count_    = 0;
    chg_count_          = 0;
    chg_grab_ms_sum_    = 0.0;
    chg_diff_ms_sum_    = 0.0;
    chg_encode_ms_sum_  = 0.0;
    bytes_sum_          = 0;
    dirty_px_sum_       = 0;
    delta_px_sum_        = 0;
    delta_encode_ms_sum_ = 0.0;
    tile_hit_row_px_sum_  = 0;
    tile_hit_tile_px_sum_ = 0;
    tile_blocks_sum_      = 0;
    tile_blocks_max_      = 0;
    tile_delta_count_     = 0;
    tile_bbox_px_sum_     = 0;
    tile_ratio_.clear();
    tile_blocks_.clear();
    diff_tile_ms_sum_     = 0.0;
}

} // namespace rc::server
