"""Runtime patches to upstream modules, applied by weixue_main.py before app.py runs."""
import re

_CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def keep_trailing_whitespace(clean):
    """Wraps upstream ConnectionHandler._clean_response_garbage, which strips trailing
    whitespace from every streamed chunk. The chunk's trailing whitespace is restored when
    no closing garbage was removed from its end and its last kept character is not CJK."""

    def wrapper(text):
        cleaned = clean(text)
        if not cleaned:
            return cleaned
        body = text.rstrip()
        if body and cleaned[-1] == body[-1] and not _CJK.match(cleaned[-1]):
            return cleaned + text[len(body):]
        return cleaned

    wrapper.weixue_wrapped = clean
    return wrapper


def register_sessions(handle_connection, proactive):
    """Wraps ConnectionHandler.handle_connection so the proactive registry maps device MAC to open sessions."""

    async def wrapper(self, ws):
        device = (ws.request.headers.get("device-id") or "").strip().lower()
        if device:
            proactive.on_session_open(device, self)
        try:
            return await handle_connection(self, ws)
        finally:
            if device:
                proactive.on_session_close(device, self)

    wrapper.weixue_wrapped = handle_connection
    return wrapper


def intercept_invoke(handle, proactive, invoke_text):
    """Wraps ListenTextMessageHandler.handle: a `listen detect` carrying invoke_text delivers queued proactive messages."""

    async def wrapper(self, conn, msg_json):
        if msg_json.get("state") == "detect" and msg_json.get("text") == invoke_text:
            if "mode" in msg_json:
                conn.client_listen_mode = msg_json["mode"]
            await proactive.on_invoke(conn)
            return None
        return await handle(self, conn, msg_json)

    wrapper.weixue_wrapped = handle
    return wrapper


class WebWithRoutes:
    """Stands in for aiohttp.web inside core.http_server; every Application it creates gets add(app)."""

    def __init__(self, web, add):
        self._web = web
        self._add = add

    def __getattr__(self, name):
        return getattr(self._web, name)

    def Application(self, *args, **kwargs):
        app = self._web.Application(*args, **kwargs)
        self._add(app)
        return app


def apply():
    from core.connection import ConnectionHandler
    from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
    import core.http_server as http_server
    import weixue_proactive

    clean = ConnectionHandler.__dict__["_clean_response_garbage"].__func__
    if getattr(clean, "weixue_wrapped", None) is None:
        ConnectionHandler._clean_response_garbage = staticmethod(keep_trailing_whitespace(clean))

    proactive = weixue_proactive.instance()
    if getattr(ConnectionHandler.handle_connection, "weixue_wrapped", None) is None:
        ConnectionHandler.handle_connection = register_sessions(ConnectionHandler.handle_connection, proactive)
    if getattr(ListenTextMessageHandler.handle, "weixue_wrapped", None) is None:
        ListenTextMessageHandler.handle = intercept_invoke(
            ListenTextMessageHandler.handle, proactive, weixue_proactive.INVOKE_TEXT
        )
    if not isinstance(http_server.web, WebWithRoutes):
        http_server.web = WebWithRoutes(
            http_server.web, lambda app: weixue_proactive.add_routes(app, proactive)
        )
