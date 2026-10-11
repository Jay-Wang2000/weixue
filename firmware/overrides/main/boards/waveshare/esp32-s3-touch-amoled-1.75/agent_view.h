#ifndef WEIXUE_AGENT_VIEW_H
#define WEIXUE_AGENT_VIEW_H

#include <lvgl.h>

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "agent_ui.h"

// LVGL widgets of the agent UI (weixue-agent-ui-v1 §4): the face complication and transient
// line, the full-screen approval / clarification card, and page 1 of the management view (current
// chat, tasks, chats list). Content comes from agent_ui; no application state is read here. All
// methods run with the display lock held. Coordinates are screen pixels of the 466 px panel.
class AgentView {
public:
    struct Callbacks {
        std::function<void()> complication_tap;
        std::function<void(int)> card_tap;               // control index, -1 = Later
        std::function<void(const std::string&)> row_tap;  // task id
        std::function<void()> chats_toggle;
        std::function<void(const std::string&)> chat_tap;  // chat id, "" = New chat
        std::function<void()> home_tap;
        // True when this press woke the display; its click is dropped.
        std::function<bool()> wake_press;
    };

    // face_parent: the face root (complication, transient); screen: card parent, created below the
    // ring; page_parent: page 1 of the management view.
    AgentView(lv_obj_t* face_parent, lv_obj_t* screen, lv_obj_t* page_parent, int32_t width,
              int32_t height, Callbacks callbacks);
    ~AgentView();

    // 20 px font with CJK for the Chinese sublabels; nullptr hides them (English only).
    void SetZhFont(const lv_font_t* font);
    // Re-places labels after the inherited theme font changed.
    void Relayout();
    void SetShift(int32_t x, int32_t y);

    // Face complication; visible false hides it (full face).
    void SetComplication(const agent_ui::Complication& c, bool visible, int64_t now_ms);
    bool ComplicationShown() const { return cx_visible_; }
    // Transient title / summary under the full face (chat switch), empty title hides it.
    void SetTransient(const std::string& title, const std::string& summary, uint32_t summary_color);
    // Bedside page dot under the clock.
    void SetBedsideDot(bool on, int32_t y);

    // Card
    void ShowCard(bool show);
    bool CardShown() const { return card_shown_; }
    // The card is up and fully slid in.
    bool CardCovering() const { return card_shown_ && lv_obj_get_y(card_root_) == 0; }
    lv_obj_t* card_root() const { return card_root_; }
    void SetCard(const agent_ui::CardView& v);
    // Screen centre of a visible card control (card_tap index) while the card covers the screen.
    bool CardControlCenter(int control, int32_t* x, int32_t* y);
    // Small eyes of the card and the management page: blink 0 (open) to 1 (closed).
    void SetBlink(float blink);

    // Management page 1
    void SetHeader(const std::string& title, bool chats_open);
    void SetRows(const std::vector<agent_ui::Row>& rows, int more);
    // Chats list (expanded header): rows from focus, total for the scroll indicator.
    void SetChats(const std::vector<agent_ui::ChatRow>& chats, int focus);
    bool ChatsOpen() const { return chats_open_; }

    // Periodic redraw of animated glyphs (unknown-progress spin); now_ms = device time.
    void Animate(int64_t now_ms);

private:
    struct GlyphSpec {
        agent_ui::Glyph glyph = agent_ui::Glyph::kNone;
        float progress = -1;
        int radius = 16;
        int stroke = 4;
        int spin = 0;
        const lv_font_t* font = nullptr;
    };
    struct Placement {
        lv_obj_t* label;
        int32_t x, baseline, width;
        lv_text_align_t align;
    };
    struct ChatSlot {
        lv_obj_t* hit = nullptr;
        lv_obj_t* frame = nullptr;
        lv_obj_t* dot = nullptr;
        lv_obj_t* glyph = nullptr;
        lv_obj_t* title = nullptr;
        lv_obj_t* state = nullptr;
        std::string chat_id;
    };
    struct TaskSlot {
        lv_obj_t* hit = nullptr;
        lv_obj_t* glyph = nullptr;
        lv_obj_t* title = nullptr;
        lv_obj_t* status = nullptr;
        std::string task_id;
    };

    int32_t width_, height_;
    Callbacks cb_;
    const lv_font_t* zh_font_ = nullptr;
    std::vector<Placement> placements_;
    std::vector<std::unique_ptr<GlyphSpec>> specs_;
    std::vector<lv_obj_t*> spin_glyphs_;
    std::vector<lv_obj_t*> zh_labels_;
    int64_t last_spin_ms_ = 0;

    // Face
    lv_obj_t* cx_root_ = nullptr;
    lv_obj_t* cx_hit_ = nullptr;
    lv_obj_t* cx_glyph_ = nullptr;
    lv_obj_t* cx_main_ = nullptr;
    lv_obj_t* cx_status_ = nullptr;
    lv_obj_t* cx_more_ = nullptr;
    lv_obj_t* cx_more_text_ = nullptr;
    lv_obj_t* cx_hint_ = nullptr;
    lv_obj_t* tr_title_ = nullptr;
    lv_obj_t* tr_summary_ = nullptr;
    lv_obj_t* bedside_dot_ = nullptr;
    bool cx_visible_ = false;
    std::string cx_task_;
    std::string cx_main_text_;
    agent_ui::Glyph cx_glyph_kind_ = agent_ui::Glyph::kNone;

    // Card
    lv_obj_t* card_root_ = nullptr;
    lv_obj_t* card_content_ = nullptr;
    lv_obj_t* card_eyes_[2] = {nullptr, nullptr};
    lv_obj_t* card_header_ = nullptr;
    lv_obj_t* card_header_zh_ = nullptr;
    lv_obj_t* card_body_ = nullptr;
    lv_obj_t* card_context_ = nullptr;
    lv_obj_t* card_buttons_[2] = {nullptr, nullptr};
    lv_obj_t* card_button_text_[2] = {nullptr, nullptr};
    lv_obj_t* card_button_zh_[2] = {nullptr, nullptr};
    lv_obj_t* card_options_[4] = {nullptr, nullptr, nullptr, nullptr};
    lv_obj_t* card_option_text_[4] = {nullptr, nullptr, nullptr, nullptr};
    lv_obj_t* card_badges_[4] = {nullptr, nullptr, nullptr, nullptr};
    lv_obj_t* card_badge_text_[4] = {nullptr, nullptr, nullptr, nullptr};
    lv_obj_t* card_later_ = nullptr;
    lv_obj_t* card_later_text_ = nullptr;
    lv_obj_t* card_footer_ = nullptr;
    lv_obj_t* card_outcome_glyph_ = nullptr;
    lv_obj_t* card_outcome_ = nullptr;
    bool card_shown_ = false;
    agent_ui::CardKind card_kind_ = agent_ui::CardKind::kNone;
    int card_eye_h_[2] = {28, 28};
    float blink_ = 0;

    // Management page 1
    lv_obj_t* page_root_ = nullptr;
    lv_obj_t* page_eyes_[2] = {nullptr, nullptr};
    lv_obj_t* header_ = nullptr;
    lv_obj_t* header_dot_ = nullptr;
    lv_obj_t* header_title_ = nullptr;
    lv_obj_t* header_chats_ = nullptr;
    lv_obj_t* header_chats_zh_ = nullptr;
    lv_obj_t* header_chevron_ = nullptr;
    lv_obj_t* tasks_title_ = nullptr;
    lv_obj_t* tasks_title_zh_ = nullptr;
    lv_obj_t* empty_ = nullptr;
    TaskSlot task_slots_[3];
    lv_obj_t* more_ = nullptr;
    lv_obj_t* more_text_ = nullptr;
    lv_obj_t* home_hit_ = nullptr;
    lv_obj_t* home_chevron_ = nullptr;
    lv_obj_t* home_text_ = nullptr;
    lv_obj_t* home_zh_ = nullptr;
    lv_obj_t* new_chat_hit_ = nullptr;
    lv_obj_t* new_chat_glyph_ = nullptr;
    lv_obj_t* new_chat_text_ = nullptr;
    ChatSlot chat_slots_[4];
    lv_obj_t* scroll_ = nullptr;
    int scroll_total_ = 0;
    int scroll_focus_ = 0;
    bool chats_open_ = false;
    std::string header_key_;

    lv_obj_t* Box(lv_obj_t* parent, int32_t x, int32_t y, int32_t w, int32_t h);
    lv_obj_t* Text(lv_obj_t* parent, const lv_font_t* font, uint32_t color);
    lv_obj_t* Glyph(lv_obj_t* parent, int32_t cx, int32_t cy, int radius, int stroke);
    lv_obj_t* Chevron(lv_obj_t* parent, int32_t cx, int32_t cy, bool up, uint32_t color);
    lv_obj_t* Hit(lv_obj_t* parent, int32_t x, int32_t y, int32_t w, int32_t h, lv_event_cb_t cb);
    void Place(lv_obj_t* label, int32_t x, int32_t baseline, int32_t width, lv_text_align_t align);
    void ApplyPlacement(const Placement& p);
    void SetGlyph(lv_obj_t* obj, agent_ui::Glyph glyph, float progress);
    void SetLine(lv_obj_t* label, const agent_ui::Line& line);
    void SetText(lv_obj_t* label, const std::string& text);
    void SetZhLabel(lv_obj_t* label, const char* text);
    void BuildFace(lv_obj_t* parent);
    void BuildCard(lv_obj_t* screen);
    void BuildPage(lv_obj_t* parent);
    void LayoutOptions(int n, bool small);
    void Show(lv_obj_t* obj, bool show);

    static void GlyphDrawCb(lv_event_t* e);
    static void ScrollDrawCb(lv_event_t* e);
    static void ChevronDrawCb(lv_event_t* e);
    static void PressCb(lv_event_t* e);
    static void CardSlideCb(void* var, int32_t value);
    static void CardSlideDoneCb(lv_anim_t* a);
    static void MainEnterCb(void* var, int32_t value);

    bool press_waking_ = false;
    bool Clickable(lv_event_t* e);
};

#endif  // WEIXUE_AGENT_VIEW_H
