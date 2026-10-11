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
    R"("speaks":true,"seen":false,"future_field":{"x":1}})";

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

    std::string scripted = kApproval;
    scripted.insert(scripted.size() - 1, R"(,"debug_reply":{"kind":"approve","option_id":"approve"})");
    auto s = ParseText(scripted);
    Check(s.message && s.message->item.card_id == "i_44c0" && s.message->item.options.size() == 2,
          "a debug_reply field is an ignored unknown field");
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
              d.message->item.timeout_s == -1,
          "defaults: when_idle, silent speech, no timeout");
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
    std::string json = BuildResponse(2918841203u, 12, resp);
    cJSON* root = cJSON_Parse(json.c_str());
    cJSON* params = cJSON_GetObjectItem(root, "params");
    Check(root && std::string(cJSON_GetObjectItem(root, "method")->valuestring) ==
                      "notifications/weixue/agent_response",
          "agent_response method");
    Check(params && cJSON_GetObjectItem(params, "boot")->valuedouble == 2918841203.0 &&
              cJSON_GetObjectItem(params, "seq")->valueint == 12 &&
              std::string(cJSON_GetObjectItem(params, "via")->valuestring) == "tap" &&
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

void TestReceipt() {
    auto r = ParseText(R"({"kind":"agent_ack","v":1,"boot":3419484792,"seq":3,"response":"approve",)"
                       R"("card_id":"i_44c0","result":"accepted","future":1})");
    Check(r.message && r.message->kind == Message::Kind::kReceipt, "agent_ack is a receipt");
    if (r.message) {
        const Receipt& rc = r.message->receipt;
        Check(rc.boot == 3419484792u && rc.seq == 3 && rc.response == "approve" && rc.card_id == "i_44c0",
              "receipt boot, seq, response, card_id");
        Check(rc.Recorded(), "accepted is recorded");
    }
    r = ParseText(R"({"kind":"agent_ack","v":1,"boot":1,"seq":4,"response":"approve","result":"duplicate",)"
                  R"("decided":"deny"})");
    Check(r.message && r.message->receipt.Recorded() && r.message->receipt.decided == "deny",
          "duplicate is recorded, with the card's decision");
    r = ParseText(R"({"kind":"agent_ack","v":1,"boot":1,"seq":5,"response":"deny","result":"stale"})");
    Check(r.message && !r.message->receipt.Recorded(), "stale is not recorded");
    Check(!ParseText(R"({"kind":"agent_ack","v":1,"boot":1,"seq":5,"response":"deny","result":"maybe"})").message,
          "unknown result dropped");
    Check(!ParseText(R"({"kind":"agent_ack","v":1,"seq":5,"response":"deny","result":"stale"})").message,
          "receipt without boot dropped");
    Check(!ParseText(R"({"kind":"agent_ack","v":1,"boot":1,"seq":4294967296,"response":"deny","result":"stale"})")
               .message,
          "receipt seq beyond 32 bits dropped");
    cJSON* p = cJSON_Parse(R"({"kind":"agent_ack"})");
    Check(IsAgentPayload(p), "agent_ack is an agent payload");
    cJSON_Delete(p);
    Check(NeedsReceipt("approve") && NeedsReceipt("cancel") && NeedsReceipt("ack_seen") && !NeedsReceipt("shown"),
          "every response but shown waits for a receipt");
}

std::string InvokeOf(const char* json) {
    cJSON* root = cJSON_Parse(json);
    std::string text = InvokeText(root);
    cJSON_Delete(root);
    return text;
}

void TestInvokeText() {
    Check(InvokeOf(R"({"pending":true,"speak":true,"id":"9f2c41d0aa","lease":"b7e1_0-Z"})") ==
              "weixue:proactive:9f2c41d0aa:b7e1_0-Z",
          "poll answer id and lease bind the invocation");
    Check(InvokeOf(R"({"pending":true,"speak":true,"id":null,"lease":null})") == "weixue:proactive",
          "no id or lease: bare marker");
    Check(InvokeOf(R"({"speak":true,"id":"abc"})") == "weixue:proactive", "lease missing: bare marker");
    Check(InvokeOf(R"({"speak":true,"id":"a:b","lease":"c"})") == "weixue:proactive",
          "a ':' in the id is not carried");
    Check(InvokeOf(R"({"speak":true,"id":"abc","lease":""})") == "weixue:proactive", "empty lease: bare marker");
    std::string long_id(kMaxBindingChars + 1, 'a');
    Check(InvokeOf((R"({"speak":true,"id":")" + long_id + R"(","lease":"c"})").c_str()) == "weixue:proactive",
          "over-long id: bare marker");
    Check(InvokeOf(R"({"speak":true,"id":7,"lease":"c"})") == "weixue:proactive", "non-string id: bare marker");
}

void TestPollStep() {
    Check(NextPollStep(200, true, true) == PollStep::kInvoke, "200 speak opens a session");
    Check(NextPollStep(200, true, false) == PollStep::kRepoll, "200 cards only polls again");
    Check(NextPollStep(200, false, true) == PollStep::kRetrySoon,
          "200 with a truncated body polls again soon, no session");
    Check(NextPollStep(204, false, true) == PollStep::kWait, "204 waits");
    Check(NextPollStep(0, false, true) == PollStep::kBackoff && NextPollStep(503, false, true) == PollStep::kBackoff,
          "no answer or another status backs off");
    Check(PollBodyComplete(2519, 2519) && PollBodyComplete(0, 17), "whole body, or no Content-Length");
    Check(!PollBodyComplete(2519, 1260) && !PollBodyComplete(2519, 0), "short body (1260 of 2519 bytes)");
}

// agent_chats (v1.2), as the gateway builds it.
void TestChats() {
    auto r = ParseText(
        R"({"kind":"agent_chats","v":1,"seq":1746,"ts":1791380000123,"current":"cht_b","chats":[)"
        R"({"id":"cht_b","title":"Living room lights tonight","state":"needs_you","age_s":120},)"
        R"({"id":"cht_a","state":"sleeping","age_s":86400,"future":1},{"id":"cht_c","title":"客厅","state":"unread"}]})");
    Check(r.message.has_value() && r.message->kind == Message::Kind::kChats, "agent_chats parses");
    if (!r.message) return;
    const Chats& c = r.message->chats;
    Check(c.seq == 1746 && c.current == "cht_b" && c.chats.size() == 3, "seq, current, entries");
    Check(c.chats[0].title == "Living room lights tonig" && c.chats[0].state == ChatState::kNeedsYou &&
              c.chats[0].age_s == 120,
          "first entry: title cut to 24 chars, needs_you, age");
    Check(c.chats[1].title.empty() && c.chats[1].state == ChatState::kIdle && c.chats[1].age_s == 86400,
          "untitled entry, unknown state reads idle");
    Check(c.chats[2].title == "客厅" && c.chats[2].state == ChatState::kUnread && c.chats[2].age_s == 0, "CJK title");
    auto none = ParseText(R"({"kind":"agent_chats","v":1,"seq":2,"chats":[]})");
    Check(none.message && none.message->chats.current.empty() && none.message->chats.chats.empty(),
          "no current, no chats");
    Check(!ParseText(R"({"kind":"agent_chats","v":1,"seq":3,"chats":[{"id":"bad id"}]})").message,
          "a bad chat id drops the list");
    Check(!ParseText(R"({"kind":"agent_chats","v":1,"seq":3,"current":"a/b","chats":[]})").message,
          "a bad current drops the list");
    Check(!ParseText(R"({"kind":"agent_chats","v":1,"seq":3})").message, "chats is required");
    std::string many = R"({"kind":"agent_chats","v":1,"seq":4,"chats":[)";
    for (int i = 0; i < 15; i++) many += std::string(i ? "," : "") + R"({"id":"c)" + std::to_string(i) + R"("})";
    many += "]}";
    auto capped = ParseText(many);
    Check(capped.message && capped.message->chats.chats.size() == kMaxChatEntries, "at most 12 entries");
    cJSON* root = cJSON_Parse(R"({"kind":"agent_chats","v":1,"seq":1,"chats":[]})");
    Check(IsAgentPayload(root), "agent_chats is an agent payload");
    cJSON_Delete(root);
    Check(kFeatureLevel == 2, "feature level 2");
}

}  // namespace

int main() {
    TestApproval();
    TestStatesAndTiers();
    TestDrops();
    TestTruncation();
    TestSync();
    TestBuilders();
    TestReceipt();
    TestPollStep();
    TestInvokeText();
    TestChats();
    std::printf("%s: %d failure(s)\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
