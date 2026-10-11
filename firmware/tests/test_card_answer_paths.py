"""Host source checks (ADR-13): no build has a server-scripted card answer; card decisions leave
the device only from a tap on the card (stdlib unittest)."""
import re
import unittest
from pathlib import Path

FW = Path(__file__).resolve().parents[1]
BOARD = FW / "overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75"


def sources():
    files = [p for p in (FW / "overrides").rglob("*") if p.is_file()]
    files += sorted((FW / "patches").glob("*.patch"))
    files += [FW / "build.sh"]
    return files


def body(text, signature):
    """The brace-balanced body of the first function whose definition starts with signature."""
    start = text.index(signature)
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i:j + 1]
    raise AssertionError(f"unbalanced {signature}")


class CardAnswerPaths(unittest.TestCase):
    def test_no_scripted_answer_anywhere(self):
        guards = (r'[ -z "${WEIXUE_AGENT_LOOPBACK:-}" ] || die ',
                  r'! LC_ALL=C grep -qa debug_reply build/xiaozhi.bin || die ')
        build = (FW / "build.sh").read_text(encoding="utf-8")
        for g in guards:
            self.assertIn(g, build)
        for path in sources():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            text = "\n".join(l for l in lines if not l.startswith(guards))
            for word in ("debug_reply", "DebugReply", "WEIXUE_AGENT_LOOPBACK", "agent_loopback", "loopback_"):
                self.assertNotIn(word, text, f"{word} in {path.relative_to(FW)}")

    def test_responses_leave_only_from_the_ui(self):
        callers = []
        for path in sources():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "SendResponse(" in line and not re.match(r"\+?\s*(uint32_t (WeixueAgent::)?SendResponse\(|//)", line):
                    callers.append((path.name, line.strip()))
        self.assertEqual(sorted(c[0] for c in callers), ["face_display_agent.cc", "weixue_agent.cc"], callers)
        agent = (BOARD / "weixue_agent.cc").read_text(encoding="utf-8")
        self.assertIn("SendResponse(", body(agent, "bool WeixueAgent::AckSeen("))
        for fn in ("void WeixueAgent::Apply(", "void WeixueAgent::Tick(", "void WeixueAgent::OnReceipt("):
            self.assertNotIn("SendResponse", body(agent, fn), fn)

    def test_card_decisions_come_from_the_card_tap(self):
        ui = (BOARD / "face_display_agent.cc").read_text(encoding="utf-8")
        calls = [l.strip() for l in ui.splitlines() if "AgentCardTap(" in l]
        self.assertEqual(calls, ["cb.card_tap = [this](int index) { AgentCardTap(index); };",
                                 "void FaceDisplay::AgentCardTap(int index) {"])
        tap = body(ui, "void FaceDisplay::AgentCardTap(")
        self.assertIn("agent_.Tap(index, now)", tap)
        view = (BOARD / "agent_view.cc").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"cb_\.card_tap\(", view)), 3, "Approve/Deny, options, Later")

    def test_diag_card_is_a_pointer_tap(self):
        patch = (FW / "patches/33-board-diag-injection.patch").read_text(encoding="utf-8")
        card = body(patch, "bool DiagCard(")
        self.assertIn("display_->DiagCardPoint(", card)
        self.assertIn("diag_pointer_.Tap(x, y)", card)
        self.assertNotIn("AgentCardTap", patch)
        self.assertNotIn("SendResponse", patch)


if __name__ == "__main__":
    unittest.main()
