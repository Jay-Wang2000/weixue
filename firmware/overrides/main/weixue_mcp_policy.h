#ifndef WEIXUE_MCP_POLICY_H
#define WEIXUE_MCP_POLICY_H

#include <string_view>

// Device MCP tool classes (DESIGN.md ADR-13). Only agent-visible tools are listed by tools/list
// or run by tools/call on the MCP channel; every other name is operator-only.
enum class McpToolVisibility { kAgentVisible, kOperatorOnly };

inline constexpr std::string_view kAgentVisibleMcpTools[] = {
    "self.get_device_status",
    "self.audio_speaker.set_volume",
    "self.screen.set_brightness",
    "self.screen.set_theme",
    "self.screen.set_bedside",
    "self.imu.get_state",
};

inline McpToolVisibility ClassifyMcpTool(std::string_view name) {
    for (std::string_view visible : kAgentVisibleMcpTools) {
        if (name == visible) {
            return McpToolVisibility::kAgentVisible;
        }
    }
    return McpToolVisibility::kOperatorOnly;
}

#endif  // WEIXUE_MCP_POLICY_H
