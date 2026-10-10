// Host test for the vendored mcp_server.cc with 34-mcp-tool-visibility.patch and
// weixue_mcp_policy.h: firmware/tests/run.sh
#include "mcp_server.h"

#include "board.h"
#include "esp_log.h"

#include <cstdio>
#include <set>
#include <string>

namespace {

int failures = 0;

void Check(bool ok, const std::string& what) {
    std::printf("%s %s\n", ok ? "PASS" : "FAIL", what.c_str());
    if (!ok) {
        failures++;
    }
}

int calls_run = 0;

// Tools the 1.75C board file registers on top of the common and user-only tools, plus names an
// injection or camera tool would use.
void RegisterBoardTools() {
    auto& mcp = McpServer::GetInstance();
    auto counted = [](const PropertyList&) -> ReturnValue {
        calls_run++;
        return true;
    };
    mcp.AddTool("self.system.reconfigure_wifi", "Enter WiFi configuration mode.", PropertyList(),
                counted);
    mcp.AddTool("self.screen.set_bedside", "Bedside screen.",
                PropertyList({Property("mode", kPropertyTypeString, std::string("status"))}),
                [](const PropertyList&) -> ReturnValue {
                    calls_run++;
                    return std::string("{\"night\":false}");
                });
    mcp.AddTool("self.imu.get_state", "Motion sensor state.", PropertyList(), counted);
    mcp.AddTool("self.diag.gesture", "Plays a gesture.",
                PropertyList({Property("gesture", kPropertyTypeString, std::string("pick_up"))}),
                counted);
    mcp.AddTool("self.diag.touch", "Plays a touch.", PropertyList(), counted);
    mcp.AddTool("self.camera.take_photo", "Takes a photo.", PropertyList(), counted);
}

// Sends one JSON-RPC request and returns the parsed reply (owned by the caller).
cJSON* Request(const std::string& json) {
    std::string reply;
    McpServer::GetInstance().ParseMessage(json, [&reply](const std::string& payload) {
        reply = payload;
    });
    return cJSON_Parse(reply.c_str());
}

std::set<std::string> ListedNames(const std::string& params) {
    std::set<std::string> names;
    std::string cursor;
    for (int page = 0; page < 10; page++) {
        std::string p = params;
        if (!cursor.empty()) {
            p = "{\"cursor\":\"" + cursor + "\"}";
        }
        cJSON* reply =
            Request("{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":" + p + "}");
        cJSON* result = cJSON_GetObjectItem(reply, "result");
        cJSON* tools = cJSON_GetObjectItem(result, "tools");
        cJSON* tool = nullptr;
        cJSON_ArrayForEach(tool, tools) {
            names.insert(cJSON_GetObjectItem(tool, "name")->valuestring);
        }
        cJSON* next = cJSON_GetObjectItem(result, "nextCursor");
        cursor = cJSON_IsString(next) ? next->valuestring : "";
        cJSON_Delete(reply);
        if (cursor.empty()) {
            break;
        }
    }
    return names;
}

bool Logged(const std::string& line) {
    for (const auto& l : HostLogLines()) {
        if (l == line) {
            return true;
        }
    }
    return false;
}

void TestClassify() {
    for (const char* name : {"self.get_device_status", "self.audio_speaker.set_volume",
                             "self.screen.set_brightness", "self.screen.set_theme",
                             "self.screen.set_bedside", "self.imu.get_state"}) {
        Check(ClassifyMcpTool(name) == McpToolVisibility::kAgentVisible,
              std::string(name) + " is agent-visible");
    }
    for (const char* name :
         {"self.system.reconfigure_wifi", "self.diag.gesture", "self.diag.touch",
          "self.diag.simulate_face", "self.camera.take_photo", "self.reboot",
          "self.upgrade_firmware", "self.get_system_info", "self.screen.snapshot",
          "self.screen.preview_image", "self.screen.get_info", "self.assets.set_download_url",
          "self.lamp.set", "", "self.imu.get_state ", "SELF.IMU.GET_STATE"}) {
        Check(ClassifyMcpTool(name) == McpToolVisibility::kOperatorOnly,
              "'" + std::string(name) + "' is operator-only");
    }
}

void TestToolsList() {
    const std::set<std::string> want = {"self.get_device_status", "self.audio_speaker.set_volume",
                                        "self.screen.set_brightness", "self.screen.set_bedside",
                                        "self.imu.get_state"};
    Check(ListedNames("{}") == want, "tools/list lists the agent-visible tools only");
    Check(ListedNames("{\"withUserTools\":true}") == want,
          "tools/list with withUserTools lists no operator-only tool");
    Check(ListedNames("{\"cursor\":\"self.diag.gesture\"}").empty(),
          "a cursor naming an operator-only tool lists no operator-only tool after it");
    Check(Logged("I MCP: Add tool: self.diag.gesture [operator-only]") &&
              Logged("I MCP: Add tool: self.reboot [user] [operator-only]") &&
              Logged("I MCP: Add tool: self.screen.set_bedside"),
          "tool registration logs the class");
}

void CheckRefused(const std::string& name) {
    int before = calls_run;
    HostLogLines().clear();
    cJSON* reply = Request("{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/call\",\"params\":"
                           "{\"name\":\"" + name + "\",\"arguments\":{}}}");
    cJSON* error = cJSON_GetObjectItem(reply, "error");
    cJSON* code = cJSON_GetObjectItem(error, "code");
    cJSON* message = cJSON_GetObjectItem(error, "message");
    Check(cJSON_IsNumber(code) && code->valueint == -32601 && cJSON_IsString(message) &&
              std::string(message->valuestring) == "Tool not available over MCP" &&
              cJSON_GetObjectItem(reply, "result") == nullptr,
          "tools/call " + name + " is refused");
    Check(calls_run == before, "tools/call " + name + " runs no callback");
    Check(Logged("W MCP: tools/call refused: " + name + " is operator-only"),
          "tools/call " + name + " logs the refusal");
    cJSON_Delete(reply);
}

void TestToolsCall() {
    for (const char* name : {"self.diag.gesture", "self.diag.touch", "self.system.reconfigure_wifi",
                             "self.camera.take_photo", "self.reboot", "self.upgrade_firmware",
                             "self.assets.set_download_url", "self.lamp.set"}) {
        CheckRefused(name);
    }

    int before = calls_run;
    cJSON* reply = Request(
        "{\"jsonrpc\":\"2.0\",\"id\":8,\"method\":\"tools/call\",\"params\":"
        "{\"name\":\"self.screen.set_bedside\",\"arguments\":{\"mode\":\"status\"}}}");
    cJSON* result = cJSON_GetObjectItem(reply, "result");
    cJSON* content = cJSON_GetArrayItem(cJSON_GetObjectItem(result, "content"), 0);
    cJSON* text = cJSON_GetObjectItem(content, "text");
    Check(cJSON_IsString(text) && std::string(text->valuestring) == "{\"night\":false}" &&
              calls_run == before + 1,
          "tools/call self.screen.set_bedside runs");
    cJSON_Delete(reply);

    reply = Request(
        "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":"
        "{\"name\":\"self.audio_speaker.set_volume\",\"arguments\":{\"volume\":30}}}");
    Check(cJSON_GetObjectItem(reply, "result") != nullptr &&
              Board::GetInstance().codec_.volume_ == 30,
          "tools/call self.audio_speaker.set_volume runs");
    cJSON_Delete(reply);
}

}  // namespace

int main() {
    auto& mcp = McpServer::GetInstance();
    RegisterBoardTools();
    mcp.AddCommonTools();
    mcp.AddUserOnlyTools();
    TestClassify();
    TestToolsList();
    TestToolsCall();
    std::printf("%s: %d failure(s)\n", failures ? "FAILED" : "OK", failures);
    return failures ? 1 : 0;
}
