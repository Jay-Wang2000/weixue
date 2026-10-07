// Host test for weixue_agent_msg.h: firmware/tests/run.sh
#include "weixue_agent_msg.h"

#include <cstdio>
#include <string>

namespace {

using namespace weixue_agent;

int failures = 0;

void Check(bool ok, const char* what) {
    std::printf("%s %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) {
        failures++;
    }
}

ParseResult ParseText(const std::string& json) {
    cJSON* root = cJSON_Parse(json.c_str());
    ParseResult r = Parse(root);
    cJSON_Delete(root);
    return r;
}

const char* kApproval =
    R"({"kind":"agent","v":1,"seq":1739,"ts":1791380000123,"chat_id":"c_7f3a","task_id":"t_91b2",)"
    R"("state":"needs_approval","title":"Living room","summary":"Turn on living room lights",)"
    R"("lang":"en","card_id":"i_44c0","options":[{"id":"approve","label":"Approve"},)"
    R"({"id":"deny","label":"Deny"}],"risk":"reversible","timeout_s":300,"delivery":"when_idle",)"
    R"("speaks":true,"seen":false,"future_field":{"x":1},)"
    R"("debug_reply":{"kind":"approve","option_id":"approve"}})";

void TestApproval() {
    auto r = ParseText(kApproval);
    Check(r.message.has_value() && r.error == nullptr, "contract example parses");
    if (!r.message) return;
    const Item& it = r.message->item;
    Check(r.message->kind == Message::Kind::kItem, "kind agent is an item");
    Check(it.seq == 1739 && it.ts == 1791380000123LL, "seq and ts");
    Check(it.chat_id == "c_7f3a" && it.task_id == "t_91b2", "ids");
    Check(it.state == State::kNeedsApproval, "state needs_approval");
    Check(it.options.size() == 2 && it.options[0].id == "approve" && it.options[1].label == "Deny",
          "options");
    Check(it.card_id == "i_44c0" && it.risk == Risk::kReversible && it.timeout_s == 300,
          "card_id, risk, timeout_s");
    Check(it.tier == Tier::kWhenIdle && it.speaks && !it.seen, "delivery, speaks, seen");
    Check(it.debug_reply && it.debug_reply->kind == "approve" &&
              it.debug_reply->option_id == "approve",
          "debug_reply");
}

void TestStatesAndTiers() {
    const char* states[] = {"working", "needs_input", "needs_approval", "unread",     "done",
                            "failed",  "cancelled",   "interrupted",    "stopped"};
    int ok = 0;
    for (const char* s : states) {
        std::string json = std::string(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t",)") +
                           R"("summary":"x","state":")" + s + R"("})";
        auto r = ParseText(json);
        if (r.message && std::string(StateName(r.message->item.state)) == s) ok++;
    }
    Check(ok == 9, "all nine states parse and round-trip their names");
    const char* tiers[] = {"interrupt", "when_idle", "silent"};
    ok = 0;
    for (const char* t : tiers) {
        std::string json =
            std::string(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"working",)") +
            R"("summary":"x","delivery":")" + t + R"("})";
        auto r = ParseText(json);
        if (r.message && std::string(TierName(r.message->item.tier)) == t) ok++;
    }
    Check(ok == 3, "all three delivery tiers parse");
    auto d = ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"working","summary":"x"})");
    Check(d.message && d.message->item.tier == Tier::kWhenIdle && !d.message->item.speaks &&
              d.message->item.timeout_s == -1 && !d.message->item.debug_reply,
          "defaults: when_idle, silent speech, no timeout, no debug_reply");
}

void TestDrops() {
    Check(ParseText(R"({"kind":"agent","v":2,"seq":5,"chat_id":"c","task_id":"t","state":"working","summary":"x"})").error != nullptr,
          "v 2 dropped");
    Check(ParseText(R"({"kind":"agent","seq":5,"chat_id":"c","task_id":"t","state":"working","summary":"x"})").error != nullptr,
          "missing v dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"sleeping","summary":"x"})").error != nullptr,
          "unknown state dropped");
    Check(ParseText(R"({"kind":"agent_x","v":1,"seq":5})").error != nullptr, "unknown kind dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":0,"chat_id":"c","task_id":"t","state":"working","summary":"x"})").error != nullptr,
          "seq 0 dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":1.5,"chat_id":"c","task_id":"t","state":"working","summary":"x"})").error != nullptr,
          "fractional seq dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"0123456789012345678901234567890123456","state":"working","summary":"x"})").error != nullptr,
          "37-char task_id dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"té","state":"working","summary":"x"})").error != nullptr,
          "non-ASCII task_id dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"working"})").error != nullptr,
          "missing summary dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"needs_input","summary":"x","options":[{"id":"a","label":"A"}]})").error != nullptr,
          "options without card_id dropped");
    Check(ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"working","summary":"x","delivery":"loud"})").error != nullptr,
          "unknown delivery dropped");
    Check(ParseText("[1,2]").error != nullptr, "non-object payload dropped");
    for (const char* id : {"c/x", "c?x", "c#x"}) {
        std::string chat = std::string(R"({"kind":"agent","v":1,"seq":5,"chat_id":")") + id +
                           R"(","task_id":"t","state":"working","summary":"x"})";
        std::string card = std::string(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t",)") +
                           R"("state":"needs_input","summary":"x","card_id":")" + id +
                           R"(","options":[{"id":"a","label":"A"}]})";
        Check(ParseText(chat).error != nullptr && ParseText(card).error != nullptr,
              "ids with '/', '?' or '#' dropped");
    }
}

void TestTruncation() {
    std::string summary(70, 'a');
    std::string zh;
    for (int i = 0; i < 65; ++i) zh += "\xe7\x81\xaf";  // 灯
    auto r = ParseText(std::string(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"working","summary":")") +
                       zh + R"(","title":")" + summary + R"("})");
    Check(r.message && r.message->item.summary.size() == 60 * 3, "summary truncated to 60 CJK code points");
    Check(r.message && r.message->item.title.size() == 24, "title truncated to 24 characters");
    Check(TruncateLabel("Approve all lights now") == "Approve all ", "Latin label truncated to width 12");
    Check(TruncateLabel("\xe6\x89\xb9\xe5\x87\x86\xe6\x89\x80\xe6\x9c\x89\xe7\x81\xaf") ==
              "\xe6\x89\xb9\xe5\x87\x86\xe6\x89\x80\xe6\x9c\x89",
          "CJK label truncated to 4 characters");
    Check(TruncateChars("ab\xe7\x81", 3) == "ab", "a cut UTF-8 sequence is not emitted");
    auto five = ParseText(R"({"kind":"agent","v":1,"seq":5,"chat_id":"c","task_id":"t","state":"needs_input","summary":"x","card_id":"i1",)"
                          R"("options":[{"id":"1","label":"a"},{"id":"2","label":"b"},{"id":"3","label":"c"},{"id":"4","label":"d"},{"id":"5","label":"e"}]})");
    Check(five.message && five.message->item.options.size() == 4, "options beyond 4 ignored");
}

void TestSync() {
    auto r = ParseText(R"({"kind":"agent_sync","v":1,"seq":1745,"task_ids":["t_91b2","t_0c11"]})");
    Check(r.message && r.message->kind == Message::Kind::kSync && r.message->sync.seq == 1745 &&
              r.message->sync.task_ids.size() == 2,
          "agent_sync parses");
    Check(ParseText(R"({"kind":"agent_sync","v":1,"seq":3})").error != nullptr, "agent_sync without task_ids dropped");
    cJSON* p = cJSON_Parse(R"({"kind":"agent_sync"})");
    Check(IsAgentPayload(p), "agent_sync is an agent payload");
    cJSON_Delete(p);
    p = cJSON_Parse(R"({"kind":"other"})");
    Check(!IsAgentPayload(p), "other kinds are not agent payloads");
    cJSON_Delete(p);
}

void TestBuilders() {
    Response resp;
    resp.kind = "approve";
    resp.chat_id = "c_7f3a";
    resp.task_id = "t_91b2";
    resp.card_id = "i_44c0";
    resp.option_id = "approve";
    resp.via = "loopback";
    std::string json = BuildResponse(2918841203u, 12, resp);
    cJSON* root = cJSON_Parse(json.c_str());
    cJSON* params = cJSON_GetObjectItem(root, "params");
    Check(root && std::string(cJSON_GetObjectItem(root, "method")->valuestring) ==
                      "notifications/weixue/agent_response",
          "agent_response method");
    Check(params && cJSON_GetObjectItem(params, "boot")->valuedouble == 2918841203.0 &&
              cJSON_GetObjectItem(params, "seq")->valueint == 12 &&
              std::string(cJSON_GetObjectItem(params, "via")->valuestring) == "loopback" &&
              std::string(cJSON_GetObjectItem(params, "card_id")->valuestring) == "i_44c0",
          "agent_response params");
    Check(cJSON_GetObjectItem(params, "text") == nullptr && cJSON_GetObjectItem(params, "ack_upto") == nullptr,
          "unset fields omitted");
    cJSON_Delete(root);

    Response shown;
    shown.kind = "shown";
    shown.ack_upto = 1745;
    json = BuildResponse(1, 2, shown);
    Check(json.find("\"ack_upto\":1745") != std::string::npos, "shown carries ack_upto");
    Response bad;
    bad.kind = "maybe";
    Check(BuildResponse(1, 2, bad).empty(), "unknown response kind not built");
    Response longtext;
    longtext.kind = "answer";
    longtext.text = std::string(400, '"');
    json = BuildResponse(1, 3, longtext);
    Check(!json.empty() && json.size() <= kMaxNotificationBytes, "answer text kept under 512 bytes");

    json = BuildPlayed(7, 13, "9f2c", false, 1840, "wake");
    Check(json.find("\"complete\":false") != std::string::npos &&
              json.find("\"cut_at_ms\":1840") != std::string::npos &&
              json.find("\"reason\":\"wake\"") != std::string::npos,
          "played cut carries cut_at_ms and reason");
    json = BuildPlayed(7, 14, "9f2c", true, 0, nullptr);
    Check(json.find("\"complete\":true") != std::string::npos &&
              json.find("cut_at_ms") == std::string::npos && json.find("reason") == std::string::npos,
          "played complete carries no cut fields");
}

}  // namespace

int main() {
    TestApproval();
    TestStatesAndTiers();
    TestDrops();
    TestTruncation();
    TestSync();
    TestBuilders();
    std::printf("%s: %d failure(s)\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
