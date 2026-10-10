"""Device MCP tool classes on the server side (DESIGN.md ADR-13): the device MCP client keeps and
calls only agent-visible device tools. The list equals kAgentVisibleMcpTools in
firmware/overrides/main/weixue_mcp_policy.h."""

AGENT_VISIBLE_DEVICE_TOOLS = frozenset(
    {
        "self.get_device_status",
        "self.audio_speaker.set_volume",
        "self.screen.set_brightness",
        "self.screen.set_theme",
        "self.screen.set_bedside",
        "self.imu.get_state",
    }
)


def is_agent_visible(name):
    """True when `name` (the device's tool name, not the sanitized one) is agent-visible."""
    return isinstance(name, str) and name in AGENT_VISIBLE_DEVICE_TOOLS


def keep_agent_visible(add_tool, logger):
    """Wraps MCPClient.add_tool: a tool from the device's tools/list that is not agent-visible is
    dropped and logged, so it never reaches the model's function list."""

    async def wrapper(self, tool_data):
        name = tool_data.get("name") if isinstance(tool_data, dict) else None
        if not is_agent_visible(name):
            logger.bind(tag=__name__).warning(f"device tool {name!r} dropped: not agent-visible")
            return None
        return await add_tool(self, tool_data)

    wrapper.weixue_wrapped = add_tool
    return wrapper


def refuse_hidden_calls(call_mcp_tool, logger):
    """Wraps call_mcp_tool: a call whose device tool name is not agent-visible is logged and
    raises ValueError without sending anything to the device."""

    async def wrapper(conn, mcp_client, tool_name, *args, **kwargs):
        mapping = getattr(mcp_client, "name_mapping", None) or {}
        name = mapping.get(tool_name, tool_name)
        if not is_agent_visible(name):
            logger.bind(tag=__name__).warning(f"device tool call {name!r} refused: not agent-visible")
            raise ValueError(f"device tool {tool_name} is not available")
        return await call_mcp_tool(conn, mcp_client, tool_name, *args, **kwargs)

    wrapper.weixue_wrapped = call_mcp_tool
    return wrapper


def apply():
    import importlib

    from config.logger import setup_logging

    logger = setup_logging()
    package = importlib.import_module("core.providers.tools.device_mcp")
    client = importlib.import_module("core.providers.tools.device_mcp.mcp_client")
    handler = importlib.import_module("core.providers.tools.device_mcp.mcp_handler")
    executor = importlib.import_module("core.providers.tools.device_mcp.mcp_executor")

    for cls in {client.MCPClient, handler.MCPClient, package.MCPClient}:
        if getattr(cls.add_tool, "weixue_wrapped", None) is None:
            cls.add_tool = keep_agent_visible(cls.add_tool, logger)
    for module in (handler, executor, package):
        call = getattr(module, "call_mcp_tool", None)
        if call is not None and getattr(call, "weixue_wrapped", None) is None:
            module.call_mcp_tool = refuse_hidden_calls(call, logger)
