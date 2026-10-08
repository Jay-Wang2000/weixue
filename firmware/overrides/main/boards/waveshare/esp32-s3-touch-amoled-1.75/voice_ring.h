#ifndef VOICE_RING_H
#define VOICE_RING_H

#include <cmath>
#include <cstdint>

// One arc of a kArcs ring: start and span in degrees clockwise from 3 o'clock.
struct RingArc {
    int16_t start = 0;
    int16_t span = 0;
    uint32_t color = 0;
    uint8_t opa = 0;
    bool rounded = false;

    bool operator==(const RingArc& o) const {
        return start == o.start && span == o.span && color == o.color && opa == o.opa &&
               rounded == o.rounded;
    }
    bool operator!=(const RingArc& o) const { return !(*this == o); }
};

// What the ring shows. Angles are degrees clockwise from 3 o'clock. kSolid is the whole ring in
// one colour; kSpin is a kSpinSpanDeg arc ending at head_deg whose opacity and colour fade from
// the head (color) to the tail (tail_color) in kSpinSteps steps; kArcs is up to kMaxArcs separate
// arcs (agent cues), drawn in order.
struct RingState {
    enum class Style : uint8_t { kOff, kSolid, kSpin, kArcs };

    static constexpr int kTiles = 20;
    static constexpr int kTileDeg = 360 / kTiles;
    static constexpr int kSpinSpanDeg = 120;
    static constexpr int kSpinSteps = 12;
    static constexpr int kMaxArcs = 5;
    // A rounded cap reaches about half the band width past the arc's end.
    static constexpr int kCapDeg = 2;

    Style style = Style::kOff;
    uint8_t opa = 0;
    uint8_t width = 0;
    int16_t head_deg = 0;
    uint32_t color = 0;
    uint32_t tail_color = 0;
    uint8_t n_arcs = 0;
    RingArc arcs[kMaxArcs];

    bool operator==(const RingState& o) const {
        if (style != o.style || opa != o.opa || width != o.width || head_deg != o.head_deg ||
            color != o.color || tail_color != o.tail_color || n_arcs != o.n_arcs) {
            return false;
        }
        for (int i = 0; i < n_arcs; i++) {
            if (arcs[i] != o.arcs[i]) return false;
        }
        return true;
    }

    void AddArc(const RingArc& a) {
        if (n_arcs < kMaxArcs && a.span > 0 && a.opa > 0) arcs[n_arcs++] = a;
    }
    bool operator!=(const RingState& o) const { return !(*this == o); }

    static int Wrap(int deg) { return ((deg % 360) + 360) % 360; }

    // Whether [a0, a0 + len) and [b0, b0 + blen) overlap on the circle.
    static bool ArcsOverlap(int a0, int len, int b0, int blen) {
        int d = Wrap(b0 - a0);
        return d < len || Wrap(a0 - b0) < blen;
    }

    // The part of this state drawn inside one tile; a tile whose view is unchanged needs no redraw.
    RingState TileView(int tile) const {
        if (style == Style::kArcs) {
            RingState view;
            view.style = Style::kArcs;
            view.width = width;
            for (int i = 0; i < n_arcs; i++) {
                const RingArc& a = arcs[i];
                int pad = a.rounded ? kCapDeg : 0;
                if (ArcsOverlap(tile * kTileDeg, kTileDeg, a.start - pad, a.span + 2 * pad)) {
                    view.AddArc(a);
                }
            }
            return view.n_arcs == 0 ? RingState() : view;
        }
        if (style == Style::kOff || opa == 0 ||
            (style == Style::kSpin &&
             !ArcsOverlap(tile * kTileDeg, kTileDeg, head_deg - kSpinSpanDeg, kSpinSpanDeg))) {
            return RingState();
        }
        return *this;
    }

    struct Box {
        int32_t x1, y1, x2, y2;
    };

    // Bounding box of the annular sector [a0, a0 + len) between r_in and r_out around (cx, cy),
    // grown by pad on every side.
    static Box SectorBox(int cx, int cy, int r_out, int r_in, int a0, int len, int pad) {
        float x1 = 1e9f, y1 = 1e9f, x2 = -1e9f, y2 = -1e9f;
        auto add = [&](float deg, float r) {
            float rad = deg * 3.14159265f / 180.0f;
            float x = cx + r * cosf(rad), y = cy + r * sinf(rad);
            x1 = fminf(x1, x);
            y1 = fminf(y1, y);
            x2 = fmaxf(x2, x);
            y2 = fmaxf(y2, y);
        };
        add(a0, r_out);
        add(a0, r_in);
        add(a0 + len, r_out);
        add(a0 + len, r_in);
        for (int axis = 0; axis < 360; axis += 90) {
            if (Wrap(axis - a0) < len) {
                add(axis, r_out);
            }
        }
        return {static_cast<int32_t>(floorf(x1)) - pad, static_cast<int32_t>(floorf(y1)) - pad,
                static_cast<int32_t>(ceilf(x2)) + pad, static_cast<int32_t>(ceilf(y2)) + pad};
    }

    // Step i of a spin (0 = head): start angle, opacity and the head-to-tail colour mix (0-255).
    void SpinStep(int i, int& start_deg, uint8_t& step_opa, uint8_t& mix) const {
        constexpr int step_deg = kSpinSpanDeg / kSpinSteps;
        start_deg = Wrap(head_deg - (i + 1) * step_deg);
        float f = 1.0f - static_cast<float>(i) / kSpinSteps;
        step_opa = static_cast<uint8_t>(lroundf(opa * f * f));
        mix = static_cast<uint8_t>(255 * i / (kSpinSteps - 1));
    }
};

#endif  // VOICE_RING_H
