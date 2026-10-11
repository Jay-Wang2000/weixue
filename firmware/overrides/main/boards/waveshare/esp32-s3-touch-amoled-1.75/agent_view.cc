#include "agent_view.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>

LV_FONT_DECLARE(font_noto_sans_basic_20_4);

namespace {

using namespace agent_ui;

constexpr int32_t kCx = 233;
constexpr uint32_t kSlideMs = 220;
constexpr uint32_t kEnterDelayMs = 120;
constexpr uint32_t kEnterMs = 180;
constexpr int32_t kEnterRise = 12;
constexpr int64_t kSpinStepMs = 2000;
constexpr int kOptionTextMax = 136;

const lv_font_t* Small() { return &font_noto_sans_basic_20_4; }

void Plain(lv_obj_t* obj) {
    lv_obj_remove_style_all(obj);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_CLICKABLE);
}

lv_point_precise_t P(float x, float y) {
    return {static_cast<lv_value_precise_t>(x), static_cast<lv_value_precise_t>(y)};
}

void DrawLine(lv_layer_t* layer, float x1, float y1, float x2, float y2, int32_t width, uint32_t color) {
    lv_draw_line_dsc_t d;
    lv_draw_line_dsc_init(&d);
    d.p1 = P(x1, y1);
    d.p2 = P(x2, y2);
    d.width = width;
    d.color = lv_color_hex(color);
    d.opa = LV_OPA_COVER;
    d.round_start = 1;
    d.round_end = 1;
    lv_draw_line(layer, &d);
}

// Arc whose centreline has radius r; clock degrees (0 = 12 o'clock, clockwise).
void ClockArc(lv_layer_t* layer, int32_t cx, int32_t cy, float r, int32_t width, float start, float span,
              uint32_t color, lv_opa_t opa, bool rounded) {
    lv_draw_arc_dsc_t a;
    lv_draw_arc_dsc_init(&a);
    a.center = {cx, cy};
    a.radius = static_cast<uint16_t>(lroundf(r + width / 2.0f));
    a.width = width;
    if (span >= 360) {
        a.start_angle = 0;
        a.end_angle = 360;
    } else {
        float lv = fmodf(start - 90 + 720, 360);
        a.start_angle = lv;
        a.end_angle = fmodf(lv + span, 360);
    }
    a.color = lv_color_hex(color);
    a.opa = opa;
    a.rounded = rounded ? 1 : 0;
    lv_draw_arc(layer, &a);
}

void Disc(lv_layer_t* layer, float cx, float cy, float r, uint32_t color) {
    lv_draw_fill_dsc_t f;
    lv_draw_fill_dsc_init(&f);
    f.color = lv_color_hex(color);
    f.opa = LV_OPA_COVER;
    f.radius = LV_RADIUS_CIRCLE;
    lv_area_t a = {static_cast<int32_t>(lroundf(cx - r)), static_cast<int32_t>(lroundf(cy - r)),
                   static_cast<int32_t>(lroundf(cx + r)) - 1, static_cast<int32_t>(lroundf(cy + r)) - 1};
    lv_draw_fill(layer, &f, &a);
}

std::string Hex(uint32_t color) {
    char buf[8];
    snprintf(buf, sizeof(buf), "%06X", static_cast<unsigned>(color & 0xFFFFFF));
    return buf;
}

// Sets a label colour only when it changes (a style write invalidates the object).
void SetColor(lv_obj_t* label, uint32_t color) {
    uint32_t now = lv_color_to_u32(lv_obj_get_style_text_color(label, LV_PART_MAIN)) & 0xFFFFFF;
    if (now != (color & 0xFFFFFF)) {
        lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    }
}

int32_t TextWidth(const char* text, const lv_font_t* font) {
    lv_point_t size;
    lv_text_get_size(&size, text, font, 0, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    return size.x;
}

}  // namespace

AgentView::AgentView(lv_obj_t* face_parent, lv_obj_t* screen, lv_obj_t* page_parent, int32_t width,
                     int32_t height, Callbacks callbacks)
    : width_(width), height_(height), cb_(std::move(callbacks)) {
    BuildFace(face_parent);
    BuildCard(screen);
    BuildPage(page_parent);
    SetZhFont(nullptr);
}

AgentView::~AgentView() { lv_anim_delete(this, nullptr); }

// Building blocks

lv_obj_t* AgentView::Box(lv_obj_t* parent, int32_t x, int32_t y, int32_t w, int32_t h) {
    lv_obj_t* obj = lv_obj_create(parent);
    Plain(obj);
    lv_obj_set_pos(obj, x, y);
    lv_obj_set_size(obj, w, h);
    return obj;
}

lv_obj_t* AgentView::Text(lv_obj_t* parent, const lv_font_t* font, uint32_t color) {
    lv_obj_t* label = lv_label_create(parent);
    lv_obj_remove_flag(label, LV_OBJ_FLAG_CLICKABLE);
    lv_label_set_text(label, "");
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    if (font != nullptr) {
        lv_obj_set_style_text_font(label, font, 0);
    }
    lv_label_set_recolor(label, true);
    lv_label_set_long_mode(label, LV_LABEL_LONG_MODE_DOTS);
    return label;
}

lv_obj_t* AgentView::Glyph(lv_obj_t* parent, int32_t cx, int32_t cy, int radius, int stroke) {
    int32_t box = 2 * (radius + stroke) + 4;
    lv_obj_t* obj = Box(parent, cx - box / 2, cy - box / 2, box, box);
    auto spec = std::make_unique<GlyphSpec>();
    spec->radius = radius;
    spec->stroke = stroke;
    lv_obj_set_user_data(obj, spec.get());
    specs_.push_back(std::move(spec));
    lv_obj_add_event_cb(obj, GlyphDrawCb, LV_EVENT_DRAW_MAIN, this);
    lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
    return obj;
}

lv_obj_t* AgentView::Chevron(lv_obj_t* parent, int32_t cx, int32_t cy, bool up, uint32_t color) {
    lv_obj_t* obj = Box(parent, cx - 8, cy - 6, 16, 12);
    lv_obj_set_user_data(obj, reinterpret_cast<void*>(static_cast<uintptr_t>((color << 1) | (up ? 1 : 0))));
    lv_obj_add_event_cb(obj, ChevronDrawCb, LV_EVENT_DRAW_MAIN, this);
    return obj;
}

lv_obj_t* AgentView::Hit(lv_obj_t* parent, int32_t x, int32_t y, int32_t w, int32_t h, lv_event_cb_t cb) {
    lv_obj_t* obj = Box(parent, x, y, w, h);
    lv_obj_add_flag(obj, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(obj, PressCb, LV_EVENT_PRESSED, this);
    lv_obj_add_event_cb(obj, cb, LV_EVENT_CLICKED, this);
    return obj;
}

// x is the left edge (LEFT), the centre (CENTER) or the right edge (RIGHT); baseline from the
// parent's top.
void AgentView::Place(lv_obj_t* label, int32_t x, int32_t baseline, int32_t width, lv_text_align_t align) {
    placements_.push_back({label, x, baseline, width, align});
    ApplyPlacement(placements_.back());
}

void AgentView::ApplyPlacement(const Placement& p) {
    const lv_font_t* font = lv_obj_get_style_text_font(p.label, LV_PART_MAIN);
    int32_t ascent = font ? font->line_height - font->base_line : 30;
    int32_t left = p.align == LV_TEXT_ALIGN_CENTER ? p.x - p.width / 2
                   : p.align == LV_TEXT_ALIGN_RIGHT ? p.x - p.width
                                                    : p.x;
    lv_obj_set_width(p.label, p.width);
    lv_obj_set_style_text_align(p.label, p.align, 0);
    lv_obj_set_pos(p.label, left, p.baseline - ascent);
    if (font != nullptr) {
        lv_obj_set_height(p.label, p.label == card_body_ ? 2 * font->line_height : font->line_height);
    }
}

void AgentView::Relayout() {
    for (const auto& p : placements_) {
        ApplyPlacement(p);
    }
}

void AgentView::SetText(lv_obj_t* label, const std::string& text) {
    if (strcmp(lv_label_get_text(label), text.c_str()) != 0) {
        lv_label_set_text(label, text.c_str());
    }
}

// Recolour markup makes LVGL's own dots cut early, so the line is fitted to the label first.
void AgentView::SetLine(lv_obj_t* label, const agent_ui::Line& line) {
    int32_t max_width = lv_obj_get_width(label);
    for (const auto& p : placements_) {
        if (p.label == label) max_width = p.width;
    }
    const lv_font_t* font = lv_obj_get_style_text_font(label, LV_PART_MAIN);
    int32_t space = lv_obj_get_style_text_letter_space(label, LV_PART_MAIN);
    auto width = [font, space](const std::string& plain) {
        lv_point_t size;
        lv_text_get_size(&size, plain.c_str(), font, space, 0, LV_COORD_MAX, LV_TEXT_FLAG_NONE);
        return size.x;
    };
    std::string text;
    for (const auto& span : agent_ui::FitLine(line, max_width - 2, width)) {
        if (span.text.empty()) continue;
        text += "#" + Hex(span.color) + " " + span.text + "#";
    }
    SetText(label, text);
}

void AgentView::Show(lv_obj_t* obj, bool show) {
    if (show == lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) {
        if (show) {
            lv_obj_remove_flag(obj, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
        }
    }
}

void AgentView::SetZhLabel(lv_obj_t* label, const char* text) {
    lv_label_set_text(label, text);
    zh_labels_.push_back(label);
}

void AgentView::SetZhFont(const lv_font_t* font) {
    zh_font_ = font;
    if (font != nullptr) {
        small_cjk_ = *Small();
        small_cjk_.fallback = font;
    }
    const lv_font_t* state_font = font != nullptr ? &small_cjk_ : Small();
    lv_obj_set_style_text_font(cx_status_, state_font, 0);
    for (auto& slot : task_slots_) {
        lv_obj_set_style_text_font(slot.status, state_font, 0);
    }
    for (lv_obj_t* label : zh_labels_) {
        if (font != nullptr) {
            lv_obj_set_style_text_font(label, font, 0);
        }
        Show(label, font != nullptr);
    }
    // Without the CJK font the separators go too.
    card_kind_ = CardKind::kNone;
    header_key_.clear();
    SetText(header_chats_, chats_open_ ? (font ? "Chats ·" : "Chats") : "Chats");
    SetText(tasks_title_, font ? "Tasks ·" : "Tasks");
    SetText(home_text_, font ? "Home ·" : "Home");
    Relayout();
}

void AgentView::SetShift(int32_t x, int32_t y) {
    for (lv_obj_t* obj : {cx_root_, card_content_, page_root_}) {
        lv_obj_set_style_translate_x(obj, x, 0);
        lv_obj_set_style_translate_y(obj, y, 0);
    }
}

// Touch guard: a press that woke the display never clicks.

void AgentView::PressCb(lv_event_t* e) {
    auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
    self->press_waking_ = self->cb_.wake_press && self->cb_.wake_press();
}

bool AgentView::Clickable(lv_event_t* e) {
    (void)e;
    if (press_waking_) {
        press_waking_ = false;
        return false;
    }
    lv_indev_t* indev = lv_indev_active();
    return indev == nullptr || lv_indev_get_gesture_dir(indev) == LV_DIR_NONE;
}

// Glyphs

void AgentView::SetGlyph(lv_obj_t* obj, agent_ui::Glyph glyph, float progress) {
    auto* spec = static_cast<GlyphSpec*>(lv_obj_get_user_data(obj));
    bool spin = glyph == Glyph::kWorking && progress < 0;
    auto it = std::find(spin_glyphs_.begin(), spin_glyphs_.end(), obj);
    if (spin && it == spin_glyphs_.end()) spin_glyphs_.push_back(obj);
    if (!spin && it != spin_glyphs_.end()) spin_glyphs_.erase(it);
    Show(obj, glyph != Glyph::kNone);
    if (spec->glyph != glyph || std::fabs(spec->progress - progress) > 0.001f) {
        spec->glyph = glyph;
        spec->progress = progress;
        lv_obj_invalidate(obj);
    }
}

void AgentView::GlyphDrawCb(lv_event_t* e) {
    lv_obj_t* obj = lv_event_get_target_obj(e);
    auto* spec = static_cast<GlyphSpec*>(lv_obj_get_user_data(obj));
    lv_layer_t* layer = lv_event_get_layer(e);
    lv_area_t a;
    lv_obj_get_coords(obj, &a);
    const int32_t cx = (a.x1 + a.x2 + 1) / 2, cy = (a.y1 + a.y2 + 1) / 2;
    const float r = spec->radius;
    const float k = r / 17.0f;
    switch (spec->glyph) {
        case Glyph::kNone:
            break;
        case Glyph::kWorking: {
            ClockArc(layer, cx, cy, r, spec->stroke, 0, 360, color::kTrack, LV_OPA_COVER, false);
            float start = 0, span = 360 * spec->progress;
            if (spec->progress < 0) {
                start = 45.0f * spec->spin;
                span = 90;
            }
            if (span >= 1) ClockArc(layer, cx, cy, r, spec->stroke, start, span, color::kEye, LV_OPA_COVER, true);
            break;
        }
        case Glyph::kQuestion: {
            ClockArc(layer, cx, cy, r, spec->stroke, 0, 360, color::kAttention, LV_OPA_COVER, false);
            const lv_font_t* font = r >= 14 ? lv_obj_get_style_text_font(obj, LV_PART_MAIN) : Small();
            lv_draw_label_dsc_t l;
            lv_draw_label_dsc_init(&l);
            l.font = font;
            l.color = lv_color_hex(color::kAttention);
            l.text = "?";
            l.align = LV_TEXT_ALIGN_CENTER;
            int32_t h = font->line_height;
            lv_area_t t = {cx - static_cast<int32_t>(r), cy - h / 2, cx + static_cast<int32_t>(r), cy + h / 2};
            lv_draw_label(layer, &l, &t);
            break;
        }
        case Glyph::kApproval: {
            float s = r / 18.0f;
            Disc(layer, cx, cy, r, color::kAttention);
            DrawLine(layer, cx, cy - 9 * s, cx, cy + 2 * s, std::max(2, static_cast<int>(lroundf(4.5f * s))), 0x000000);
            Disc(layer, cx, cy + 9 * s, std::max(1.5f, 2.6f * s), 0x000000);
            break;
        }
        case Glyph::kDone:
            ClockArc(layer, cx, cy, r, spec->stroke, 0, 360, color::kDone, LV_OPA_COVER, false);
            DrawLine(layer, cx - 8 * k, cy + 0.5f * k, cx - 2.5f * k, cy + 6 * k, spec->stroke, color::kDone);
            DrawLine(layer, cx - 2.5f * k, cy + 6 * k, cx + 8.5f * k, cy - 6 * k, spec->stroke, color::kDone);
            break;
        case Glyph::kFailed:
            ClockArc(layer, cx, cy, r, spec->stroke, 0, 360, color::kFail, LV_OPA_COVER, false);
            DrawLine(layer, cx - 6 * k, cy - 6 * k, cx + 6 * k, cy + 6 * k, spec->stroke, color::kFail);
            DrawLine(layer, cx - 6 * k, cy + 6 * k, cx + 6 * k, cy - 6 * k, spec->stroke, color::kFail);
            break;
    }
}

void AgentView::ChevronDrawCb(lv_event_t* e) {
    lv_obj_t* obj = lv_event_get_target_obj(e);
    auto bits = reinterpret_cast<uintptr_t>(lv_obj_get_user_data(obj));
    bool up = bits & 1;
    uint32_t col = static_cast<uint32_t>(bits >> 1);
    lv_area_t a;
    lv_obj_get_coords(obj, &a);
    float cx = (a.x1 + a.x2 + 1) / 2.0f, cy = (a.y1 + a.y2 + 1) / 2.0f;
    float tip = up ? cy - 4 : cy + 4, base = up ? cy + 4 : cy - 4;
    lv_layer_t* layer = lv_event_get_layer(e);
    DrawLine(layer, cx - 6, base, cx, tip, 2, col);
    DrawLine(layer, cx, tip, cx + 6, base, 2, col);
}

void AgentView::ScrollDrawCb(lv_event_t* e) {
    auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
    lv_layer_t* layer = lv_event_get_layer(e);
    lv_area_t parent;
    lv_obj_get_coords(lv_obj_get_parent(lv_event_get_target_obj(e)), &parent);
    int32_t cx = parent.x1 + kCx, cy = parent.y1 + kCx;
    ClockArc(layer, cx, cy, 212, 4, 66, 48, color::kScrollTrack, LV_OPA_COVER, true);
    if (self->scroll_total_ > 1) {
        float thumb = std::max(8.0f, 48.0f / self->scroll_total_);
        float start = 66 + (48 - thumb) * self->scroll_focus_ / std::max(1, self->scroll_total_ - 1);
        ClockArc(layer, cx, cy, 212, 4, start, thumb, color::kValue, LV_OPA_COVER, true);
    }
}

void AgentView::Animate(int64_t now_ms) {
    if (spin_glyphs_.empty() || now_ms - last_spin_ms_ < kSpinStepMs) {
        return;
    }
    last_spin_ms_ = now_ms;
    for (lv_obj_t* obj : spin_glyphs_) {
        auto* spec = static_cast<GlyphSpec*>(lv_obj_get_user_data(obj));
        spec->spin = (spec->spin + 1) % 8;
        if (!lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) {
            lv_obj_invalidate(obj);
        }
    }
}

// Face complication (§4.1-4.3) and transient line (§4.8)

void AgentView::BuildFace(lv_obj_t* parent) {
    cx_root_ = Box(parent, 0, 0, width_, height_);
    lv_obj_add_flag(cx_root_, LV_OBJ_FLAG_HIDDEN);
    cx_hit_ = Hit(cx_root_, kCx - 170, 188, 340, 160, [](lv_event_t* e) {
        auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
        if (self->Clickable(e) && self->cb_.complication_tap) self->cb_.complication_tap();
    });
    cx_glyph_ = Glyph(cx_root_, kCx, 212, 17, 3);
    cx_main_ = Text(cx_root_, nullptr, color::kText);
    Place(cx_main_, kCx, 262, 330, LV_TEXT_ALIGN_CENTER);
    cx_status_ = Text(cx_root_, Small(), color::kLabel);
    Place(cx_status_, kCx, 294, 330, LV_TEXT_ALIGN_CENTER);
    cx_more_ = Box(cx_root_, 209, 312, 48, 30);
    lv_obj_set_style_radius(cx_more_, 15, 0);
    lv_obj_set_style_border_width(cx_more_, 1, 0);
    lv_obj_set_style_border_color(cx_more_, lv_color_hex(color::kBorder), 0);
    cx_more_text_ = Text(cx_root_, Small(), color::kValue);
    Place(cx_more_text_, kCx, 334, 80, LV_TEXT_ALIGN_CENTER);
    cx_hint_ = Text(cx_root_, Small(), color::kSub);
    Place(cx_hint_, kCx, 334, 330, LV_TEXT_ALIGN_CENTER);

    tr_title_ = Text(parent, nullptr, color::kText);
    Place(tr_title_, kCx, 330, 330, LV_TEXT_ALIGN_CENTER);
    tr_summary_ = Text(parent, Small(), color::kValue);
    Place(tr_summary_, kCx, 366, 320, LV_TEXT_ALIGN_CENTER);
    lv_obj_add_flag(tr_title_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(tr_summary_, LV_OBJ_FLAG_HIDDEN);

    bedside_dot_ = Box(parent, kCx - 5, 0, 10, 10);
    lv_obj_set_style_radius(bedside_dot_, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(bedside_dot_, lv_color_hex(0x801400), 0);
    lv_obj_set_style_bg_opa(bedside_dot_, LV_OPA_COVER, 0);
    lv_obj_add_flag(bedside_dot_, LV_OBJ_FLAG_HIDDEN);
}

void AgentView::MainEnterCb(void* var, int32_t value) {
    auto* label = static_cast<lv_obj_t*>(var);
    lv_obj_set_style_translate_y(label, kEnterRise * (255 - value) / 255, 0);
    lv_obj_set_style_text_opa(label, static_cast<lv_opa_t>(value), 0);
}

void AgentView::SetComplication(const Complication& c, bool visible, int64_t now_ms) {
    (void)now_ms;
    bool show = visible && c.show;
    if (show != cx_visible_) {
        cx_visible_ = show;
        Show(cx_root_, show);
        if (show) {
            lv_anim_delete(cx_main_, MainEnterCb);
            MainEnterCb(cx_main_, 0);
            lv_anim_t a;
            lv_anim_init(&a);
            lv_anim_set_var(&a, cx_main_);
            lv_anim_set_exec_cb(&a, MainEnterCb);
            lv_anim_set_values(&a, 0, 255);
            lv_anim_set_delay(&a, kEnterDelayMs);
            lv_anim_set_duration(&a, kEnterMs);
            lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
            lv_anim_start(&a);
        }
    }
    if (!c.show) {
        return;
    }
    cx_task_ = c.task_id;
    cx_glyph_kind_ = c.glyph;
    auto* spec = static_cast<GlyphSpec*>(lv_obj_get_user_data(cx_glyph_));
    int radius = c.glyph == Glyph::kApproval ? 18 : c.glyph == Glyph::kWorking ? 16 : 17;
    int stroke = c.glyph == Glyph::kWorking ? 4 : 3;
    if (spec->radius != radius || spec->stroke != stroke) {
        spec->radius = radius;
        spec->stroke = stroke;
        lv_obj_invalidate(cx_glyph_);
    }
    SetGlyph(cx_glyph_, c.glyph, c.progress);
    SetText(cx_main_, c.main);
    SetColor(cx_main_, c.main_color);
    SetLine(cx_status_, c.status);
    char more[12];
    snprintf(more, sizeof(more), "+%d", c.more);
    SetText(cx_more_text_, more);
    bool hint = !c.hint.empty();
    Show(cx_more_, c.more > 0 && !hint);
    Show(cx_more_text_, c.more > 0 && !hint);
    SetText(cx_hint_, c.hint);
    Show(cx_hint_, hint);
}

void AgentView::SetTransient(const std::string& title, const std::string& summary, uint32_t summary_color) {
    bool show = !title.empty();
    Show(tr_title_, show);
    Show(tr_summary_, show && !summary.empty());
    SetText(tr_title_, agent_ui::Plain(title));
    SetText(tr_summary_, agent_ui::Plain(summary));
    SetColor(tr_summary_, summary_color);
}

void AgentView::SetBedsideDot(bool on, int32_t y) {
    if (on) {
        lv_obj_set_y(bedside_dot_, y - 5);
    }
    Show(bedside_dot_, on);
}

// Card (§4.4, §4.5)

void AgentView::BuildCard(lv_obj_t* screen) {
    card_root_ = lv_obj_create(screen);
    Plain(card_root_);
    lv_obj_set_size(card_root_, width_, height_);
    lv_obj_set_style_bg_color(card_root_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(card_root_, LV_OPA_COVER, 0);
    lv_obj_set_pos(card_root_, 0, height_);
    lv_obj_add_flag(card_root_, LV_OBJ_FLAG_HIDDEN);
    card_content_ = Box(card_root_, 0, 0, width_, height_);

    for (int i = 0; i < 2; i++) {
        card_eyes_[i] = Box(card_content_, i == 0 ? 201 : 241, 44, 24, 28);
        lv_obj_set_style_bg_color(card_eyes_[i], lv_color_hex(color::kEye), 0);
        lv_obj_set_style_bg_opa(card_eyes_[i], LV_OPA_COVER, 0);
        lv_obj_set_style_radius(card_eyes_[i], 3, 0);
    }
    card_header_ = Text(card_content_, Small(), color::kAttentionText);
    card_header_zh_ = Text(card_content_, Small(), color::kAttentionText);
    card_body_ = Text(card_content_, nullptr, color::kText);
    lv_label_set_long_mode(card_body_, LV_LABEL_LONG_MODE_DOTS);
    Place(card_body_, kCx, 152, 360, LV_TEXT_ALIGN_CENTER);
    card_context_ = Text(card_content_, Small(), color::kValue);
    Place(card_context_, kCx, 230, 360, LV_TEXT_ALIGN_CENTER);

    static const uint32_t kButtonColor[2] = {color::kApprove, color::kDeny};
    static const char* const kVerb[2] = {"Approve", "Deny"};
    static const char* const kZh[2] = {"批准", "拒绝"};
    for (int i = 0; i < 2; i++) {
        int32_t x = i == 0 ? 75 : 239;
        card_buttons_[i] = Hit(card_content_, x, 262, 152, 80, [](lv_event_t* e) {
            auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
            int index = lv_event_get_target_obj(e) == self->card_buttons_[0] ? 0 : 1;
            if (self->Clickable(e) && self->cb_.card_tap) self->cb_.card_tap(index);
        });
        lv_obj_t* b = card_buttons_[i];
        lv_obj_set_style_radius(b, 24, 0);
        lv_obj_set_style_border_width(b, 2, 0);
        lv_obj_set_style_border_color(b, lv_color_hex(kButtonColor[i]), 0);
        lv_obj_set_style_bg_color(b, lv_color_hex(kButtonColor[i]), 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_opa(b, LV_OPA_40, LV_STATE_PRESSED);
        lv_obj_set_style_bg_opa(b, LV_OPA_40, LV_STATE_CHECKED);
        card_button_text_[i] = Text(card_content_, nullptr, kButtonColor[i]);
        lv_label_set_text(card_button_text_[i], kVerb[i]);
        Place(card_button_text_[i], x + 76, 301, 148, LV_TEXT_ALIGN_CENTER);
        card_button_zh_[i] = Text(card_content_, Small(), kButtonColor[i]);
        lv_obj_set_style_text_opa(card_button_zh_[i], 191, 0);
        SetZhLabel(card_button_zh_[i], kZh[i]);
        Place(card_button_zh_[i], x + 76, 329, 148, LV_TEXT_ALIGN_CENTER);
    }
    for (int i = 0; i < 4; i++) {
        card_options_[i] = Hit(card_content_, 0, 0, 152, 64, [](lv_event_t* e) {
            auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
            lv_obj_t* target = lv_event_get_target_obj(e);
            for (int k = 0; k < 4; k++) {
                if (target == self->card_options_[k] && self->Clickable(e) && self->cb_.card_tap) {
                    self->cb_.card_tap(k);
                }
            }
        });
        lv_obj_t* o = card_options_[i];
        lv_obj_set_style_radius(o, 22, 0);
        lv_obj_set_style_border_width(o, 2, 0);
        lv_obj_set_style_border_color(o, lv_color_hex(color::kBorder), 0);
        lv_obj_set_style_bg_color(o, lv_color_hex(color::kBorder), 0);
        lv_obj_set_style_bg_opa(o, LV_OPA_TRANSP, 0);
        lv_obj_set_style_bg_opa(o, LV_OPA_40, LV_STATE_PRESSED);
        lv_obj_set_style_bg_opa(o, LV_OPA_40, LV_STATE_CHECKED);
        card_option_text_[i] = Text(card_content_, nullptr, color::kText);
        card_badges_[i] = Box(card_content_, 0, 0, 22, 22);
        lv_obj_set_style_radius(card_badges_[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_border_width(card_badges_[i], 1, 0);
        lv_obj_set_style_border_color(card_badges_[i], lv_color_hex(color::kSub), 0);
        card_badge_text_[i] = Text(card_content_, Small(), color::kLabel);
        char n[2] = {static_cast<char>('1' + i), 0};
        lv_label_set_text(card_badge_text_[i], n);
    }
    card_later_ = Hit(card_content_, 157, 285, 152, 64, [](lv_event_t* e) {
        auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
        if (self->Clickable(e) && self->cb_.card_tap) self->cb_.card_tap(-1);
    });
    lv_obj_set_style_radius(card_later_, 22, 0);
    lv_obj_set_style_border_width(card_later_, 2, 0);
    lv_obj_set_style_border_color(card_later_, lv_color_hex(color::kBorder), 0);
    lv_obj_set_style_bg_color(card_later_, lv_color_hex(color::kBorder), 0);
    lv_obj_set_style_bg_opa(card_later_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_bg_opa(card_later_, LV_OPA_40, LV_STATE_PRESSED);
    card_later_text_ = Text(card_content_, nullptr, color::kValue);
    lv_label_set_text(card_later_text_, "Later");
    Place(card_later_text_, kCx, 327, 148, LV_TEXT_ALIGN_CENTER);

    card_footer_ = Text(card_content_, Small(), color::kLabel);
    Place(card_footer_, kCx, 384, 330, LV_TEXT_ALIGN_CENTER);
    card_outcome_glyph_ = Glyph(card_content_, kCx, 200, 17, 3);
    card_outcome_ = Text(card_content_, nullptr, color::kText);
    Place(card_outcome_, kCx, 262, 330, LV_TEXT_ALIGN_CENTER);
    lv_obj_add_flag(card_outcome_, LV_OBJ_FLAG_HIDDEN);
}

void AgentView::CardSlideCb(void* var, int32_t value) {
    lv_obj_set_y(static_cast<AgentView*>(var)->card_root_, value);
}

void AgentView::CardSlideDoneCb(lv_anim_t* a) {
    auto* self = static_cast<AgentView*>(a->var);
    if (!self->card_shown_) {
        lv_obj_add_flag(self->card_root_, LV_OBJ_FLAG_HIDDEN);
    }
}

void AgentView::ShowCard(bool show) {
    if (show == card_shown_) {
        return;
    }
    card_shown_ = show;
    lv_anim_delete(this, CardSlideCb);
    if (show) {
        lv_obj_remove_flag(card_root_, LV_OBJ_FLAG_HIDDEN);
    }
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, this);
    lv_anim_set_exec_cb(&a, CardSlideCb);
    lv_anim_set_values(&a, lv_obj_get_y(card_root_), show ? 0 : height_);
    lv_anim_set_duration(&a, kSlideMs);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_set_completed_cb(&a, CardSlideDoneCb);
    lv_anim_start(&a);
}

// Option boxes: 2 in one row, 3 as two over one centred, 4 as a grid; badges outside the columns.
void AgentView::LayoutOptions(int n, bool small) {
    static const int32_t kGrid[4][2] = {{75, 250}, {239, 250}, {75, 326}, {239, 326}};
    for (int i = 0; i < 4; i++) {
        bool show = i < n;
        Show(card_options_[i], show);
        Show(card_option_text_[i], show);
        Show(card_badges_[i], show);
        Show(card_badge_text_[i], show);
        if (!show) continue;
        int32_t x = kGrid[i][0], y = kGrid[i][1];
        if (n == 2) y = 285;
        if (n == 3 && i == 2) x = 157;
        bool left = x < kCx - 76 || (n == 3 && i == 2);
        lv_obj_set_pos(card_options_[i], x, y);
        int32_t bx = left ? x - 15 : x + 167;
        lv_obj_set_pos(card_badges_[i], bx - 11, y + 32 - 11);
        if (small) {
            lv_obj_set_style_text_font(card_option_text_[i], Small(), 0);
        } else {
            lv_obj_remove_local_style_prop(card_option_text_[i], LV_STYLE_TEXT_FONT, 0);
        }
        const lv_font_t* font = lv_obj_get_style_text_font(card_option_text_[i], LV_PART_MAIN);
        int32_t baseline = y + 32 + (font->line_height - font->base_line) / 2 - 2;
        Placement p = {card_option_text_[i], x + 76, baseline, 140, LV_TEXT_ALIGN_CENTER};
        ApplyPlacement(p);
        p = {card_badge_text_[i], bx, y + 32 + 7, 22, LV_TEXT_ALIGN_CENTER};
        ApplyPlacement(p);
    }
}

void AgentView::SetCard(const CardView& v) {
    bool approval = v.kind == CardKind::kApproval;
    bool question = v.kind == CardKind::kQuestion;
    bool outcome = v.phase == CardPhase::kOutcome;
    if (v.kind != card_kind_) {
        card_kind_ = v.kind;
        // Eye pose: attentive square eyes on approvals, confused on questions.
        int32_t y[2] = {44, 44}, h[2] = {28, 28}, r = 3;
        if (question) {
            y[0] = 50;
            h[0] = 20;
            y[1] = 38;
            h[1] = 32;
            r = 6;
        }
        for (int i = 0; i < 2; i++) {
            card_eye_h_[i] = h[i];
            lv_obj_set_style_radius(card_eyes_[i], r, 0);
            lv_obj_set_y(card_eyes_[i], y[i]);
        }
        SetBlink(blink_);
        SetText(card_header_, approval ? (zh_font_ ? "Needs your OK ·" : "Needs your OK")
                                       : (zh_font_ ? "Question ·" : "Question"));
        lv_label_set_text(card_header_zh_, approval ? "待批准" : "问题");
        if (std::find(zh_labels_.begin(), zh_labels_.end(), card_header_zh_) == zh_labels_.end()) {
            zh_labels_.push_back(card_header_zh_);
            if (zh_font_) lv_obj_set_style_text_font(card_header_zh_, zh_font_, 0);
        }
        Show(card_header_zh_, zh_font_ != nullptr);
        // Header: English and Chinese halves centred together on baseline 108.
        int32_t en = TextWidth(lv_label_get_text(card_header_), Small());
        int32_t zh = zh_font_ ? TextWidth(lv_label_get_text(card_header_zh_), zh_font_) + 6 : 0;
        int32_t left = kCx - (en + zh) / 2;
        Placement p = {card_header_, left, 108, en + 2, LV_TEXT_ALIGN_LEFT};
        ApplyPlacement(p);
        p = {card_header_zh_, left + en + 6, 108, zh + 2, LV_TEXT_ALIGN_LEFT};
        ApplyPlacement(p);
        Placement ctx = {card_context_, kCx, approval ? 230 : 228, 360, LV_TEXT_ALIGN_CENTER};
        ApplyPlacement(ctx);
        Placement foot = {card_footer_, kCx, approval ? 384 : 428, 330, LV_TEXT_ALIGN_CENTER};
        ApplyPlacement(foot);
    }
    for (lv_obj_t* obj : {card_header_, card_body_, card_context_, card_footer_}) {
        Show(obj, !outcome);
    }
    Show(card_header_zh_, !outcome && zh_font_ != nullptr);
    SetText(card_body_, v.body);
    SetLine(card_context_, v.context);
    SetLine(card_footer_, v.footer);

    for (int i = 0; i < 2; i++) {
        bool show = approval && !outcome;
        Show(card_buttons_[i], show);
        Show(card_button_text_[i], show);
        Show(card_button_zh_[i], show && zh_font_ != nullptr);
        if (v.pressed == i) {
            lv_obj_add_state(card_buttons_[i], LV_STATE_CHECKED);
        } else {
            lv_obj_remove_state(card_buttons_[i], LV_STATE_CHECKED);
        }
    }
    int n = question && !outcome ? static_cast<int>(std::min<size_t>(v.options.size(), 4)) : 0;
    bool small = false;
    for (int i = 0; i < n; i++) {
        SetText(card_option_text_[i], v.options[i].label);
        const lv_font_t* big = lv_obj_get_style_text_font(card_content_, LV_PART_MAIN);
        if (TextWidth(v.options[i].label.c_str(), big) > kOptionTextMax) small = true;
        if (v.pressed == i) {
            lv_obj_add_state(card_options_[i], LV_STATE_CHECKED);
        } else {
            lv_obj_remove_state(card_options_[i], LV_STATE_CHECKED);
        }
    }
    LayoutOptions(n, small);
    bool later = question && !outcome && v.options.empty();
    Show(card_later_, later);
    Show(card_later_text_, later);

    Show(card_outcome_, outcome);
    SetGlyph(card_outcome_glyph_, outcome ? v.outcome_glyph : Glyph::kNone, -1);
    if (outcome) {
        SetText(card_outcome_, v.outcome);
        SetColor(card_outcome_, v.outcome_color);
    }
}

bool AgentView::CardControlCenter(int control, int32_t* x, int32_t* y) {
    lv_obj_t* obj = nullptr;
    if (control == -1) {
        obj = card_later_;
    } else if (card_kind_ == CardKind::kApproval && control >= 0 && control < 2) {
        obj = card_buttons_[control];
    } else if (card_kind_ == CardKind::kQuestion && control >= 0 && control < 4) {
        obj = card_options_[control];
    }
    if (obj == nullptr || !CardCovering() || lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) {
        return false;
    }
    lv_obj_update_layout(card_root_);
    lv_area_t a;
    lv_obj_get_coords(obj, &a);
    *x = (a.x1 + a.x2 + 1) / 2;
    *y = (a.y1 + a.y2 + 1) / 2;
    return true;
}

void AgentView::SetBlink(float blink) {
    blink_ = blink;
    for (int i = 0; i < 2; i++) {
        int32_t full = card_eye_h_[i];
        int32_t h = std::max<int32_t>(3, lroundf(full * (1.0f - blink)));
        if (lv_obj_get_height(card_eyes_[i]) != h) {
            lv_obj_set_height(card_eyes_[i], h);
            lv_obj_set_style_translate_y(card_eyes_[i], (full - h) / 2, 0);
        }
        int32_t ph = std::max<int32_t>(3, lroundf(28 * (1.0f - blink)));
        if (lv_obj_get_height(page_eyes_[i]) != ph) {
            lv_obj_set_height(page_eyes_[i], ph);
            lv_obj_set_style_translate_y(page_eyes_[i], (28 - ph) / 2, 0);
        }
    }
}

// Management page 1 (§4.9)

void AgentView::BuildPage(lv_obj_t* parent) {
    page_root_ = Box(parent, 0, 0, width_, height_);
    for (int i = 0; i < 2; i++) {
        page_eyes_[i] = Box(page_root_, i == 0 ? 201 : 241, 28, 24, 28);
        lv_obj_set_style_bg_color(page_eyes_[i], lv_color_hex(color::kEye), 0);
        lv_obj_set_style_bg_opa(page_eyes_[i], LV_OPA_COVER, 0);
        lv_obj_set_style_radius(page_eyes_[i], 6, 0);
    }
    header_ = Hit(page_root_, 88, 70, 290, 56, [](lv_event_t* e) {
        auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
        if (self->Clickable(e) && self->cb_.chats_toggle) self->cb_.chats_toggle();
    });
    lv_obj_set_style_radius(header_, 28, 0);
    lv_obj_set_style_border_width(header_, 2, 0);
    lv_obj_set_style_border_color(header_, lv_color_hex(color::kBorder), 0);
    lv_obj_set_style_bg_color(header_, lv_color_hex(color::kBorder), 0);
    lv_obj_set_style_bg_opa(header_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_bg_opa(header_, LV_OPA_40, LV_STATE_PRESSED);
    header_dot_ = Box(page_root_, 109, 93, 10, 10);
    lv_obj_set_style_radius(header_dot_, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(header_dot_, lv_color_hex(color::kEye), 0);
    lv_obj_set_style_bg_opa(header_dot_, LV_OPA_COVER, 0);
    header_title_ = Text(page_root_, nullptr, color::kText);
    Place(header_title_, 128, 108, 150, LV_TEXT_ALIGN_LEFT);
    header_chats_ = Text(page_root_, Small(), color::kValue);
    lv_label_set_text(header_chats_, "Chats");
    Place(header_chats_, 344, 105, 64, LV_TEXT_ALIGN_RIGHT);
    header_chats_zh_ = Text(page_root_, Small(), color::kValue);
    SetZhLabel(header_chats_zh_, "对话");
    Place(header_chats_zh_, 252, 105, 50, LV_TEXT_ALIGN_LEFT);
    header_chevron_ = Chevron(page_root_, 358, 98, false, color::kValue);

    tasks_title_ = Text(page_root_, Small(), color::kLabel);
    Place(tasks_title_, kCx - 26, 156, 80, LV_TEXT_ALIGN_CENTER);
    tasks_title_zh_ = Text(page_root_, Small(), color::kLabel);
    SetZhLabel(tasks_title_zh_, "任务");
    Place(tasks_title_zh_, kCx + 16, 156, 50, LV_TEXT_ALIGN_LEFT);
    empty_ = Text(page_root_, Small(), color::kSub);
    lv_label_set_text(empty_, "Nothing running");
    Place(empty_, kCx, 230, 300, LV_TEXT_ALIGN_CENTER);

    static const int32_t kRowY[3] = {188, 260, 332};
    for (int i = 0; i < 3; i++) {
        TaskSlot& s = task_slots_[i];
        int32_t c = kRowY[i];
        s.hit = Hit(page_root_, 64, c - 32, 340, 70, [](lv_event_t* e) {
            auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
            for (auto& slot : self->task_slots_) {
                if (slot.hit == lv_event_get_target_obj(e) && self->Clickable(e) && self->cb_.row_tap) {
                    self->cb_.row_tap(slot.task_id);
                }
            }
        });
        s.glyph = Glyph(page_root_, 93, c, 17, 3);
        s.title = Text(page_root_, nullptr, color::kText);
        Place(s.title, 124, c + 2, 270, LV_TEXT_ALIGN_LEFT);
        s.status = Text(page_root_, Small(), color::kLabel);
        Place(s.status, 124, c + 30, 270, LV_TEXT_ALIGN_LEFT);
    }
    more_ = Box(page_root_, 185, 376, 96, 30);
    lv_obj_set_style_radius(more_, 15, 0);
    lv_obj_set_style_border_width(more_, 1, 0);
    lv_obj_set_style_border_color(more_, lv_color_hex(color::kBorder), 0);
    more_text_ = Text(page_root_, Small(), color::kValue);
    Place(more_text_, kCx, 398, 92, LV_TEXT_ALIGN_CENTER);

    home_hit_ = Hit(page_root_, 153, 406, 160, 48, [](lv_event_t* e) {
        auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
        if (self->Clickable(e) && self->cb_.home_tap) self->cb_.home_tap();
    });
    home_chevron_ = Chevron(page_root_, kCx, 417, false, color::kSub);
    home_text_ = Text(page_root_, Small(), color::kSub);
    Place(home_text_, kCx - 16, 442, 80, LV_TEXT_ALIGN_CENTER);
    home_zh_ = Text(page_root_, Small(), color::kSub);
    SetZhLabel(home_zh_, "家");
    Place(home_zh_, kCx + 20, 442, 30, LV_TEXT_ALIGN_LEFT);

    // Chats (expanded header)
    new_chat_hit_ = Hit(page_root_, 150, 134, 166, 38, [](lv_event_t* e) {
        auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
        if (self->Clickable(e) && self->cb_.chat_tap) self->cb_.chat_tap("");
    });
    new_chat_glyph_ = Box(page_root_, 178, 140, 24, 24);
    lv_obj_set_style_radius(new_chat_glyph_, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(new_chat_glyph_, 2, 0);
    lv_obj_set_style_border_color(new_chat_glyph_, lv_color_hex(color::kEye), 0);
    lv_obj_t* plus = Text(new_chat_glyph_, Small(), color::kEye);
    lv_label_set_text(plus, "+");
    lv_obj_center(plus);
    new_chat_text_ = Text(page_root_, Small(), color::kValue);
    lv_label_set_text(new_chat_text_, "New chat");
    Place(new_chat_text_, 210, 159, 110, LV_TEXT_ALIGN_LEFT);

    auto chat_hit = [](lv_event_t* e) {
        auto* self = static_cast<AgentView*>(lv_event_get_user_data(e));
        for (auto& slot : self->chat_slots_) {
            if (slot.hit == lv_event_get_target_obj(e) && self->Clickable(e) && self->cb_.chat_tap &&
                !slot.chat_id.empty()) {
                self->cb_.chat_tap(slot.chat_id);
            }
        }
    };
    // Slot 0: the focus row; 1-3 below at falling contrast.
    ChatSlot& f = chat_slots_[0];
    f.hit = Hit(page_root_, 53, 180, 360, 72, chat_hit);
    f.frame = f.hit;
    lv_obj_set_style_radius(f.frame, 36, 0);
    lv_obj_set_style_border_width(f.frame, 3, 0);
    lv_obj_set_style_border_color(f.frame, lv_color_hex(color::kEye), 0);
    f.dot = Box(page_root_, 77, 211, 10, 10);
    lv_obj_set_style_radius(f.dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(f.dot, lv_color_hex(color::kEye), 0);
    lv_obj_set_style_bg_opa(f.dot, LV_OPA_COVER, 0);
    f.title = Text(page_root_, nullptr, color::kText);
    Place(f.title, 98, 226, 158, LV_TEXT_ALIGN_LEFT);
    f.glyph = Glyph(page_root_, 266, 216, 7, 3);
    f.state = Text(page_root_, Small(), color::kValue);
    Place(f.state, 280, 223, 124, LV_TEXT_ALIGN_LEFT);
    static const int32_t kSlotY[3] = {284, 342, 404};
    static const uint32_t kSlotTitle[3] = {color::kValue, color::kLabel, color::kSub};
    for (int i = 1; i < 4; i++) {
        ChatSlot& s = chat_slots_[i];
        int32_t c = kSlotY[i - 1];
        s.hit = Hit(page_root_, 70, c - 28, 330, 56, chat_hit);
        if (i == 1) s.glyph = Glyph(page_root_, 84, c, 9, 2);
        bool big = i < 3;
        s.title = Text(page_root_, big ? nullptr : Small(), kSlotTitle[i - 1]);
        s.state = Text(page_root_, Small(), i == 1 ? color::kValue : color::kSub);
        if (i == 1) {
            Place(s.title, 102, c + 10, 230, LV_TEXT_ALIGN_LEFT);
            Place(s.state, 410, c + 8, 140, LV_TEXT_ALIGN_RIGHT);
        } else if (i == 2) {
            Place(s.title, 102, c + 10, 200, LV_TEXT_ALIGN_LEFT);
            Place(s.state, 310, c + 8, 90, LV_TEXT_ALIGN_LEFT);
        } else {
            Place(s.title, kCx, c, 300, LV_TEXT_ALIGN_CENTER);
            lv_obj_add_flag(s.state, LV_OBJ_FLAG_HIDDEN);
        }
    }
    scroll_ = Box(page_root_, 410, 130, 50, 206);
    lv_obj_add_event_cb(scroll_, ScrollDrawCb, LV_EVENT_DRAW_MAIN, this);
    lv_obj_set_style_bg_opa(scroll_, LV_OPA_TRANSP, 0);
    SetHeader("New chat", false);
    SetRows({}, 0);
}

void AgentView::SetHeader(const std::string& title, bool chats_open) {
    std::string key = (chats_open ? "1" : "0") + title;
    if (key == header_key_) {
        return;
    }
    header_key_ = key;
    chats_open_ = chats_open;
    SetText(header_title_, agent_ui::Plain(title));
    Show(header_dot_, !chats_open);
    Show(header_title_, !chats_open);
    if (chats_open) {
        SetText(header_chats_, zh_font_ ? "Chats ·" : "Chats");
        Placement p = {header_chats_, zh_font_ ? 248 : 270, 105, 90, LV_TEXT_ALIGN_RIGHT};
        ApplyPlacement(p);
    } else {
        SetText(header_chats_, "Chats");
        Placement p = {header_chats_, 344, 105, 64, LV_TEXT_ALIGN_RIGHT};
        ApplyPlacement(p);
    }
    Show(header_chats_zh_, chats_open && zh_font_ != nullptr);
    lv_obj_set_user_data(header_chevron_,
                         reinterpret_cast<void*>(static_cast<uintptr_t>((color::kValue << 1) | (chats_open ? 1 : 0))));
    lv_obj_set_x(header_chevron_, (chats_open ? 306 : 358) - 8);
    lv_obj_invalidate(header_chevron_);

    bool tasks = !chats_open;
    for (lv_obj_t* obj : {tasks_title_, home_hit_, home_chevron_, home_text_}) {
        Show(obj, tasks);
    }
    Show(tasks_title_zh_, tasks && zh_font_ != nullptr);
    Show(home_zh_, tasks && zh_font_ != nullptr);
    if (!tasks) {
        Show(empty_, false);
        for (auto& s : task_slots_) {
            Show(s.hit, false);
            Show(s.glyph, false);
            Show(s.title, false);
            Show(s.status, false);
        }
        Show(more_, false);
        Show(more_text_, false);
    }
    for (lv_obj_t* obj : {new_chat_hit_, new_chat_glyph_, new_chat_text_, scroll_}) {
        Show(obj, chats_open);
    }
    if (!chats_open) {
        for (auto& s : chat_slots_) {
            for (lv_obj_t* obj : {s.hit, s.dot, s.glyph, s.title, s.state}) {
                if (obj != nullptr) Show(obj, false);
            }
        }
    }
}

void AgentView::SetRows(const std::vector<Row>& rows, int more) {
    if (chats_open_) {
        return;
    }
    Show(empty_, rows.empty());
    for (size_t i = 0; i < 3; i++) {
        TaskSlot& s = task_slots_[i];
        bool show = i < rows.size();
        Show(s.hit, show);
        Show(s.title, show);
        Show(s.status, show);
        if (!show) {
            SetGlyph(s.glyph, Glyph::kNone, -1);
            s.task_id.clear();
            continue;
        }
        const Row& r = rows[i];
        s.task_id = r.task_id;
        auto* spec = static_cast<GlyphSpec*>(lv_obj_get_user_data(s.glyph));
        int radius = r.glyph == Glyph::kApproval ? 18 : r.glyph == Glyph::kWorking ? 16 : 17;
        int stroke = r.glyph == Glyph::kWorking ? 4 : 3;
        if (spec->radius != radius || spec->stroke != stroke) {
            spec->radius = radius;
            spec->stroke = stroke;
            lv_obj_invalidate(s.glyph);
        }
        SetGlyph(s.glyph, r.glyph, r.progress);
        SetText(s.title, r.title);
        SetLine(s.status, r.status);
    }
    char buf[16];
    snprintf(buf, sizeof(buf), "+%d more", more);
    SetText(more_text_, buf);
    Show(more_, more > 0);
    Show(more_text_, more > 0);
}

void AgentView::SetChats(const std::vector<ChatRow>& chats, int focus) {
    if (!chats_open_) {
        return;
    }
    scroll_total_ = static_cast<int>(chats.size());
    scroll_focus_ = focus;
    lv_obj_invalidate(scroll_);
    for (int i = 0; i < 4; i++) {
        ChatSlot& s = chat_slots_[i];
        int index = focus + i;
        bool show = index >= 0 && index < static_cast<int>(chats.size());
        Show(s.hit, show);
        Show(s.title, show);
        if (s.dot != nullptr) Show(s.dot, false);
        if (!show) {
            if (s.glyph != nullptr) SetGlyph(s.glyph, Glyph::kNone, -1);
            Show(s.state, false);
            s.chat_id.clear();
            continue;
        }
        const ChatRow& c = chats[index];
        s.chat_id = c.chat_id;
        if (i == 0) {
            if (lv_obj_get_style_border_width(s.frame, LV_PART_MAIN) != (c.current ? 3 : 2)) {
                lv_obj_set_style_border_width(s.frame, c.current ? 3 : 2, 0);
                lv_obj_set_style_border_color(s.frame, lv_color_hex(c.current ? color::kEye : color::kBorder), 0);
            }
            Show(s.dot, c.current);
            SetText(s.title, c.title);
            SetGlyph(s.glyph, c.chip.empty() ? Glyph::kNone : c.glyph, c.progress);
            std::string state = c.chip.empty() ? "· " + c.age : c.chip;
            SetText(s.state, state);
            SetColor(s.state, c.chip.empty() ? color::kLabel : c.chip_color);
            Show(s.state, true);
            // State chip right-aligned in the row; the title takes the rest.
            constexpr int32_t kRight = 396;
            int32_t w = std::min<int32_t>(TextWidth(state.c_str(), Small()) + 2, 170);
            int32_t chip_x = kRight - w;
            Placement p = {s.state, chip_x, 223, w, LV_TEXT_ALIGN_LEFT};
            ApplyPlacement(p);
            lv_obj_set_x(s.glyph, chip_x - 18 - lv_obj_get_width(s.glyph) / 2);
            int32_t title_w = chip_x - (c.chip.empty() ? 12 : 34) - 98;
            p = {s.title, 98, 226, std::max<int32_t>(60, title_w), LV_TEXT_ALIGN_LEFT};
            ApplyPlacement(p);
        } else if (i == 1) {
            SetText(s.title, c.title);
            SetGlyph(s.glyph, c.chip.empty() ? Glyph::kNone : c.glyph, c.progress);
            SetText(s.state, c.chip.empty() ? "· " + c.age : c.chip);
            SetColor(s.state, c.chip.empty() ? color::kSub : c.chip_color);
            Show(s.state, true);
        } else if (i == 2) {
            SetText(s.title, c.title);
            SetText(s.state, "· " + c.age);
            Show(s.state, true);
        } else {
            SetText(s.title, c.title + " · " + c.age);
        }
    }
}
