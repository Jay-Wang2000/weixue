#ifndef WEIXUE_DIAG_CARD_H
#define WEIXUE_DIAG_CARD_H

#include <algorithm>
#include <cstdlib>
#include <string>
#include <vector>

#include "agent_ui.h"

// `diag card approve|deny|later|option <n>` (diag build): the card control a finger would tap,
// as AgentView's card_tap index (0 Approve, 1 Deny, option n-1, -1 Later).
namespace diag_card {

struct Target {
    bool ok = false;
    int control = 0;
    std::string error;
};

inline Target Fail(const std::string& error) {
    Target t;
    t.error = error;
    return t;
}

inline Target Resolve(const std::vector<std::string>& args, const agent_ui::CardView& card) {
    using agent_ui::CardKind;
    using agent_ui::CardPhase;
    const std::string what = args.empty() ? "" : args[0];
    bool known = (args.size() == 1 && (what == "approve" || what == "deny" || what == "later")) ||
                 (args.size() == 2 && what == "option");
    if (!known) {
        return Fail("card needs approve, deny, later or option <n>");
    }
    if (card.kind == CardKind::kNone) {
        return Fail("no card open");
    }
    if (card.phase == CardPhase::kOutcome) {
        return Fail("the card shows its outcome");
    }
    Target t;
    t.ok = true;
    if (what == "approve" || what == "deny") {
        if (card.kind != CardKind::kApproval) {
            return Fail(what + " needs an approval card");
        }
        t.control = what == "approve" ? 0 : 1;
        return t;
    }
    if (card.kind != CardKind::kQuestion) {
        return Fail(what + " needs a question card");
    }
    int shown = static_cast<int>(std::min<size_t>(card.options.size(), 4));
    if (what == "later") {
        if (shown > 0) {
            return Fail("later needs a question without options");
        }
        t.control = -1;
        return t;
    }
    if (shown == 0) {
        return Fail("the question has no options; use later");
    }
    char* end = nullptr;
    long n = std::strtol(args[1].c_str(), &end, 10);
    if (args[1].empty() || *end != '\0' || n < 1 || n > shown) {
        return Fail("option needs 1-" + std::to_string(shown));
    }
    t.control = static_cast<int>(n) - 1;
    return t;
}

}  // namespace diag_card

#endif  // WEIXUE_DIAG_CARD_H
