// Host render of the agent UI screens (AgentView + agent_ui on desktop LVGL) to PNG, with the lit
// share of each screen: firmware/tests/render/render.sh <out_dir>. The eyes and ring are drawn
// here from the same geometry as FaceDisplay (the face itself is not rendered by this program).
#include "agent_ui.h"
#include "agent_view.h"

#include <lvgl.h>
#include <zlib.h>

#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

LV_FONT_DECLARE(font_noto_sans_basic_30_4);

namespace {

using namespace agent_ui;
using weixue_agent::Item;
using weixue_agent::Store;

constexpr int kW = 466, kH = 466;
uint32_t g_tick = 0;
std::vector<uint16_t> g_fb(kW * kH);
RingHints g_ring;
bool g_listen_ring = false;

uint32_t Tick() { return g_tick; }

void Flush(lv_display_t* disp, const lv_area_t* area, uint8_t* px) {
    const auto* src = reinterpret_cast<const uint16_t*>(px);
    int32_t w = area->x2 - area->x1 + 1;
    for (int32_t y = area->y1; y <= area->y2; y++) {
        memcpy(&g_fb[y * kW + area->x1], src + (y - area->y1) * w, w * 2);
    }
    lv_display_flush_ready(disp);
}

void Chunk(FILE* f, const char* type, const std::vector<uint8_t>& data) {
    uint8_t len[4] = {uint8_t(data.size() >> 24), uint8_t(data.size() >> 16), uint8_t(data.size() >> 8),
                      uint8_t(data.size())};
    fwrite(len, 1, 4, f);
    std::vector<uint8_t> body(type, type + 4);
    body.insert(body.end(), data.begin(), data.end());
    fwrite(body.data(), 1, body.size(), f);
    uint32_t crc = crc32(0, body.data(), body.size());
    uint8_t c[4] = {uint8_t(crc >> 24), uint8_t(crc >> 16), uint8_t(crc >> 8), uint8_t(crc)};
    fwrite(c, 1, 4, f);
}

// Writes the frame buffer as PNG (outside the round panel in dark grey) and returns the lit share
// of the screen: pixels inside the circle with any channel above 8.
double WritePng(const std::string& path) {
    std::vector<uint8_t> raw;
    raw.reserve(kH * (1 + kW * 3));
    long lit = 0;
    for (int y = 0; y < kH; y++) {
        raw.push_back(0);
        for (int x = 0; x < kW; x++) {
            uint16_t p = g_fb[y * kW + x];
            uint8_t r = ((p >> 11) & 0x1F) * 255 / 31, g = ((p >> 5) & 0x3F) * 255 / 63, b = (p & 0x1F) * 255 / 31;
            float dx = x - 232.5f, dy = y - 232.5f;
            bool inside = dx * dx + dy * dy <= 233.0f * 233.0f;
            if (!inside) r = g = b = 0x15;
            if (inside && (r > 8 || g > 8 || b > 8)) lit++;
            raw.push_back(r);
            raw.push_back(g);
            raw.push_back(b);
        }
    }
    uLongf size = compressBound(raw.size());
    std::vector<uint8_t> z(size);
    compress2(z.data(), &size, raw.data(), raw.size(), 9);
    z.resize(size);
    FILE* f = fopen(path.c_str(), "wb");
    const uint8_t sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};
    fwrite(sig, 1, 8, f);
    std::vector<uint8_t> ihdr = {0, 0, uint8_t(kW >> 8), uint8_t(kW), 0, 0, uint8_t(kH >> 8), uint8_t(kH), 8, 2, 0, 0, 0};
    Chunk(f, "IHDR", ihdr);
    Chunk(f, "IDAT", z);
    Chunk(f, "IEND", {});
    fclose(f);
    return 100.0 * lit / (kW * kH);
}

void RingDraw(lv_event_t* e) {
    lv_layer_t* layer = lv_event_get_layer(e);
    lv_draw_arc_dsc_t a;
    lv_draw_arc_dsc_init(&a);
    a.center = {233, 233};
    a.radius = 232;
    a.width = 9;
    if (g_listen_ring) {
        a.color = lv_color_hex(color::kEye);
        a.opa = 192;
        a.start_angle = 0;
        a.end_angle = 360;
        lv_draw_arc(layer, &a);
        return;
    }
    for (int i = 0; i < g_ring.count; i++) {
        const Arc& arc = g_ring.arcs[i];
        a.color = lv_color_hex(arc.color);
        a.opa = arc.opa;
        a.rounded = arc.rounded;
        a.start_angle = ClockToLvDeg(arc.start);
        a.end_angle = (ClockToLvDeg(arc.start) + arc.span) % 360;
        lv_draw_arc(layer, &a);
    }
}

// Eyes of the face as FaceDisplay::AgentPose / TargetShapes place them (centre-relative).
void Eyes(lv_obj_t* parent, Pose pose) {
    struct E { float x, y, w, h, r; } e[2];
    if (pose == Pose::kFull) {
        e[0] = {-80, -20, 100, 116, 30};
        e[1] = {80, -20, 100, 116, 30};
    } else {
        for (int i = 0; i < 2; i++) e[i] = {i == 0 ? -48.0f : 48.0f, -90, 60, 70, 20};
        if (pose == Pose::kAttentive) {
            for (auto& x : e) { x.w = 62; x.h = 66; x.r = 10; x.y = -92; }
        } else if (pose == Pose::kHappy) {
            // The happy arch is cut inside the eye's draw on the device; here a shorter eye.
            for (auto& x : e) { x.y -= 6; x.h = 35; x.y -= 17; }
        } else if (pose == Pose::kConfused) {
            e[0].h *= 0.7f;
            e[1].h *= 1.08f;
            e[1].y -= 4;
        }
    }
    for (int i = 0; i < 2; i++) {
        lv_obj_t* o = lv_obj_create(parent);
        lv_obj_remove_style_all(o);
        lv_obj_set_size(o, lroundf(e[i].w), lroundf(e[i].h));
        lv_obj_set_pos(o, lroundf(233 + e[i].x - e[i].w / 2), lroundf(233 + e[i].y - e[i].h / 2));
        lv_obj_set_style_radius(o, lroundf(e[i].r), 0);
        lv_obj_set_style_bg_color(o, lv_color_hex(color::kEye), 0);
        lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    }
}

struct Scene {
    lv_obj_t* screen;
    lv_obj_t* face;
    lv_obj_t* page;
    AgentView* view;
};

const lv_font_t* g_text_font = nullptr;
const lv_font_t* g_zh_font = nullptr;

Scene NewScene() {
    Scene s;
    s.screen = lv_obj_create(nullptr);
    lv_obj_remove_style_all(s.screen);
    lv_obj_set_style_bg_color(s.screen, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(s.screen, LV_OPA_COVER, 0);
    lv_obj_set_style_text_font(s.screen, g_text_font, 0);
    lv_obj_set_style_text_color(s.screen, lv_color_white(), 0);
    lv_obj_remove_flag(s.screen, LV_OBJ_FLAG_SCROLLABLE);
    s.face = lv_obj_create(s.screen);
    lv_obj_remove_style_all(s.face);
    lv_obj_set_size(s.face, kW, kH);
    lv_obj_t* ring = lv_obj_create(s.screen);
    lv_obj_remove_style_all(ring);
    lv_obj_set_size(ring, kW, kH);
    lv_obj_add_event_cb(ring, RingDraw, LV_EVENT_DRAW_MAIN, nullptr);
    s.page = lv_obj_create(s.screen);
    lv_obj_remove_style_all(s.page);
    lv_obj_set_size(s.page, kW, kH);
    lv_obj_set_style_bg_color(s.page, lv_color_black(), 0);
    lv_obj_add_flag(s.page, LV_OBJ_FLAG_HIDDEN);
    s.view = new AgentView(s.face, s.screen, s.page, kW, kH, AgentView::Callbacks());
    lv_obj_move_to_index(s.view->card_root(), lv_obj_get_index(ring));
    s.view->SetZhFont(g_zh_font);
    lv_screen_load(s.screen);
    g_ring = RingHints();
    g_listen_ring = false;
    return s;
}

void Clock(lv_obj_t* face) {
    lv_obj_t* l = lv_label_create(face);
    lv_label_set_text(l, "17:08");
    lv_obj_set_style_text_color(l, lv_color_hex(color::kLabel), 0);
    lv_obj_set_pos(l, 196, 3);
}

void Finish(Scene& s, const std::string& out, const char* name) {
    for (int i = 0; i < 60; i++) {
        g_tick += 20;
        lv_timer_handler();
    }
    lv_obj_invalidate(s.screen);
    lv_refr_now(nullptr);
    double lit = WritePng(out + "/" + name + ".png");
    printf("%-22s lit=%.2f%% screen\n", name, lit);
    delete s.view;
    lv_obj_delete(s.screen);
}

int64_t g_seq = 1000;

Item MakeItem(const std::string& task, State state, const std::string& title, const std::string& chat = "c_home") {
    Item it;
    it.seq = ++g_seq;
    it.chat_id = chat;
    it.task_id = task;
    it.state = state;
    it.title = title;
    it.summary = title;
    it.tier = Tier::kSilent;
    return it;
}

struct Rig {
    Store store;
    Model model;
    void Add(const Item& it, int64_t now) {
        store.Apply(it, now);
        model.SetTasks(store.tasks(), now, "09:40");
    }
};

void Populate(Rig& r) {
    Item home = MakeItem("t_home", State::kWorking, "Home dev");
    r.Add(home, 0);
    Item sunset = MakeItem("t_sun", State::kWorking, "Sunset lights");
    sunset.has_progress = true;
    sunset.progress_step = 2;
    sunset.progress_total = 5;
    r.Add(sunset, 0);
    Item ap = MakeItem("t_lr", State::kNeedsApproval, "Living room lights");
    ap.card_id = "i_lr";
    ap.timeout_s = 300;
    ap.risk = Risk::kReversible;
    ap.summary = "Turn on living room lights";
    ap.options = {{"approve", "Approve"}, {"deny", "Deny"}};
    r.Add(ap, 2000);
    Item wr = MakeItem("t_wr", State::kUnread, "Weekly report");
    r.Add(wr, 3000);
    Item kids = MakeItem("t_kids", State::kUnread, "Kids' homework", "c_kids");
    r.Add(kids, 4000);
    Item menu = MakeItem("t_menu", State::kDone, "周末菜单", "c_menu");
    r.Add(menu, 5000);
    Item trip = MakeItem("t_trip", State::kDone, "Trip to Kyoto", "c_trip");
    r.Add(trip, 6000);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <out_dir>\n", argv[0]);
        return 2;
    }
    std::string out = argv[1];
    lv_init();
    lv_tick_set_cb(Tick);
    lv_display_t* disp = lv_display_create(kW, kH);
    static std::vector<uint8_t> buf(kW * kH * 2);
    lv_display_set_buffers(disp, buf.data(), nullptr, buf.size(), LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_flush_cb(disp, Flush);
    g_text_font = &font_noto_sans_basic_30_4;
    g_zh_font = nullptr;

    const int64_t now = 4 * 60000;

    {  // idle, working
        Rig r;
        Item sunset = MakeItem("t_sun", State::kWorking, "Sunset lights");
        sunset.has_progress = true;
        sunset.progress_step = 2;
        sunset.progress_total = 5;
        r.Add(sunset, 0);
        r.Add(MakeItem("t_b", State::kWorking, "Backup"), -1);
        Item again = sunset;
        again.seq = ++g_seq;
        r.Add(again, now - 1000);
        Scene s = NewScene();
        Clock(s.face);
        Complication c = r.model.Face(now);
        Eyes(s.face, c.pose);
        s.view->SetComplication(c, true, now);
        g_ring = r.model.Ring(1000, 0);
        Finish(s, out, "agent-idle-working");
    }
    {  // idle, unread
        Rig r;
        r.Add(MakeItem("t_wr", State::kUnread, "Weekly report"), 0);
        Scene s = NewScene();
        Clock(s.face);
        Complication c = r.model.Face(500);
        Eyes(s.face, c.pose);
        s.view->SetComplication(c, true, 500);
        g_ring = r.model.Ring(5000, 0);
        Finish(s, out, "agent-idle-unread");
    }
    {  // needs approval hint
        Rig r;
        Populate(r);
        Scene s = NewScene();
        Clock(s.face);
        Complication c = r.model.Face(4000);
        Eyes(s.face, c.pose);
        s.view->SetComplication(c, true, 4000);
        g_ring = r.model.Ring(2500, 0);
        Finish(s, out, "agent-needs-approval");
    }
    {  // approval card
        Rig r;
        Populate(r);
        r.model.OpenCard("t_lr", 4000);
        Scene s = NewScene();
        s.view->SetCard(r.model.Card(4000));
        s.view->ShowCard(true);
        g_ring = r.model.Ring(2500, 0);
        Finish(s, out, "agent-approval-card");
    }
    {  // approval card, sending
        Rig r;
        Populate(r);
        r.model.OpenCard("t_lr", 4000);
        r.model.Tap(0, 4500);
        Scene s = NewScene();
        s.view->SetCard(r.model.Card(4600));
        s.view->ShowCard(true);
        g_ring = r.model.Ring(2500, 0);
        Finish(s, out, "agent-approval-sending");
    }
    {  // approval outcome
        Rig r;
        Populate(r);
        r.model.OpenCard("t_lr", 4000);
        auto d = r.model.Tap(0, 4500);
        r.model.OnDecisionSent(d.card_id, d.token, true, 4600);
        Scene s = NewScene();
        s.view->SetCard(r.model.Card(4700));
        s.view->ShowCard(true);
        Finish(s, out, "agent-approval-approved");
    }
    {  // irreversible approval, tap only
        Rig r;
        Item ap = MakeItem("t_lock", State::kNeedsApproval, "Front door");
        ap.card_id = "i_lock";
        ap.timeout_s = 25;
        ap.risk = Risk::kIrreversible;
        ap.summary = "Unlock the front door";
        r.Add(ap, 0);
        r.model.OpenCard("t_lock", 100);
        Scene s = NewScene();
        s.view->SetCard(r.model.Card(16000));
        s.view->ShowCard(true);
        g_ring = r.model.Ring(16000, 0);
        Finish(s, out, "agent-approval-irreversible");
    }
    for (int n : {2, 3, 4}) {  // clarification cards
        Rig r;
        Item q = MakeItem("t_q", State::kNeedsInput, "Evening tidy-up");
        q.card_id = "q_1";
        q.summary = "Which room should I start with?";
        q.has_progress = true;
        q.progress_step = 1;
        q.progress_total = 4;
        const char* labels[] = {"Kitchen", "Bedroom", "Hallway", "All"};
        for (int i = 0; i < n; i++) q.options.push_back({std::string("o") + char('1' + i), labels[i]});
        r.Add(q, 0);
        r.model.OpenCard("t_q", 100);
        Scene s = NewScene();
        s.view->SetCard(r.model.Card(200));
        s.view->ShowCard(true);
        g_ring = r.model.Ring(2000, 0);
        std::string name = "agent-clarification-" + std::to_string(n);
        Finish(s, out, name.c_str());
    }
    {  // management page 1
        Rig r;
        Populate(r);
        Scene s = NewScene();
        lv_obj_remove_flag(s.page, LV_OBJ_FLAG_HIDDEN);
        s.view->SetHeader(r.model.CurrentTitle(), false);
        int more = 0;
        auto rows = r.model.Rows(4000, 3, &more);
        s.view->SetRows(rows, more);
        Finish(s, out, "agent-management");
    }
    {  // management chats
        Rig r;
        Populate(r);
        Scene s = NewScene();
        lv_obj_remove_flag(s.page, LV_OBJ_FLAG_HIDDEN);
        s.view->SetHeader(r.model.CurrentTitle(), true);
        auto chats = r.model.Chats(3 * 3600000LL);
        int focus = 0;
        for (size_t i = 0; i < chats.size(); i++) {
            if (chats[i].current) focus = static_cast<int>(i);
        }
        s.view->SetChats(chats, focus);
        Finish(s, out, "agent-management-chats");
    }
    {  // new chat transient
        Scene s = NewScene();
        Clock(s.face);
        Eyes(s.face, Pose::kFull);
        s.view->SetTransient("New chat", "Ready. What's up?", color::kValue);
        g_listen_ring = true;
        Finish(s, out, "agent-new-chat");
    }
    {  // chat switch result
        Scene s = NewScene();
        Clock(s.face);
        Eyes(s.face, Pose::kFull);
        s.view->SetTransient("Kids' homework", "Last: Kids' homework", color::kValue);
        Finish(s, out, "agent-chat-switched");
    }
    return 0;
}
