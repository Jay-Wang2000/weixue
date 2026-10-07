"""Runtime patches to upstream modules, applied by weixue_main.py before app.py runs."""
import asyncio
import importlib
import json
import random
import re
import time
import uuid

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


def hold_idle_timer_while_replying(no_voice_close_connect):
    """Wraps upstream no_voice_close_connect so that close_connection_no_voice_time counts
    from the end of the reply: every audio frame received while the server holds the turn
    (conn.client_is_speaking, set from the STT result or wake cue until tts stop) refreshes
    conn.last_activity_time."""

    async def wrapper(conn, have_voice):
        if conn.client_is_speaking:
            conn.last_activity_time = time.time() * 1000
        await no_voice_close_connect(conn, have_voice)

    wrapper.weixue_wrapped = no_voice_close_connect
    return wrapper


def is_wake_word(conn, text, remove_punctuation_and_length):
    """True when text is only a configured wake word (case-insensitive, punctuation ignored)."""
    _, filtered = remove_punctuation_and_length(text or "")
    words = {w.casefold() for w in conn.config.get("wakeup_words") or []}
    return bool(filtered) and filtered.casefold() in words


# Transcripts the device microphone produced from a playing wake cue.
CUE_ECHO_WORDS = {"hmm", "hm", "huh", "mm", "mhm", "mmhm", "yes"}
CUE_ECHO_S = 5


def _letters(text):
    return re.sub(r"[^a-z]", "", (text or "").casefold())


def is_cue_echo(conn, text):
    """True when text is only a cue word and a wake cue started less than CUE_ECHO_S ago."""
    started = getattr(conn, "weixue_cue_started", None)
    if started is None or time.monotonic() - started > CUE_ECHO_S:
        return False
    words = CUE_ECHO_WORDS | {_letters(c.get("text")) for c in conn.config.get("wake_cues") or []}
    return _letters(text) in words - {""}


def ignore_bare_wake_word(remove_punctuation_and_length, logger):
    """Replacement for upstream checkWakeupWords: a transcript that is only a configured
    wake word, or the echo of a wake cue, is consumed with no reply."""

    async def check_wakeup_words(conn, text):
        if is_wake_word(conn, text, remove_punctuation_and_length):
            logger.bind(tag=__name__).info(f"wake word transcript ignored: {text}")
            return True
        if is_cue_echo(conn, text):
            logger.bind(tag=__name__).info(f"wake cue echo ignored: {text}")
            return True
        return False

    check_wakeup_words.weixue_replacement = True
    return check_wakeup_words


def never_just_woken_up(_conn):
    """Getter for ConnectionHandler.just_woken_up, which upstream sets on a `listen detect`
    to drop the next 2 s of microphone audio."""
    return False


# The device clears its decode queue when `tts start` puts it in Speaking.
DECODER_RESET_S = 0.2
TTS_READY_S = 3
CUE_DELAY_S = 0.6
# Live microphone audio after the wake: the first WAKE_HOLD_S cannot end the utterance,
# so the wake-word audio and the speech after the device's detect-to-listen gap form one
# ASR segment; VAD results from VAD_FLUSH_S on describe live audio only.
WAKE_HOLD_S = 0.8
VAD_FLUSH_S = 0.25


def hold_wake_utterance(is_vad):
    """Wraps the VAD provider's is_vad(conn, pcm_frame) for the WAKE_HOLD_S after a wake's
    `listen start` (conn.weixue_live_since): an end of utterance is withheld, and voice
    heard after VAD_FLUSH_S sets conn.weixue_live_voice."""

    def wrapper(self, conn, pcm_frame):
        have_voice = is_vad(self, conn, pcm_frame)
        since = getattr(conn, "weixue_live_since", None)
        if since is not None:
            elapsed = time.time() - since
            if elapsed < WAKE_HOLD_S:
                conn.client_voice_stop = False
            if have_voice and elapsed >= VAD_FLUSH_S:
                conn.weixue_live_voice = True
        return have_voice

    wrapper.weixue_wrapped = is_vad
    return wrapper


def wake_cue_player(audio_to_data, send_tts_message, send_audio_message, first, last, logger):
    """Returns play(conn, cue): CUE_DELAY_S after the wake, unless live voice was heard
    since (the user went on speaking), one tts start / sentence_start + audio / tts stop for
    cue["file"], captioned cue["text"]. A turn that starts meanwhile (new sentence_id) drops
    the rest of the cue."""

    async def play(conn, cue):
        deadline = time.monotonic() + TTS_READY_S
        while conn.tts is None:
            if time.monotonic() > deadline:
                logger.bind(tag=__name__).warning("wake cue skipped: TTS not ready")
                return
            await asyncio.sleep(0.05)
        await asyncio.sleep(CUE_DELAY_S)
        if getattr(conn, "weixue_live_voice", False) or conn.client_is_speaking:
            logger.bind(tag=__name__).info("wake cue skipped: user speaking")
            return
        packets = await audio_to_data(cue["file"])
        sentence_id = uuid.uuid4().hex
        conn.sentence_id = sentence_id
        conn.client_abort = False
        await send_tts_message(conn, "start")
        conn.client_is_speaking = True
        conn.weixue_cue_started = time.monotonic()
        await asyncio.sleep(DECODER_RESET_S)
        logger.bind(tag=__name__).info(f"wake cue: {cue.get('text')}")
        await send_audio_message(conn, first, packets, cue.get("text"), sentence_id)
        await send_audio_message(conn, last, [], None, sentence_id)

    return play


def acknowledge_wake(handle, remove_punctuation_and_length, play):
    """Wraps ListenTextMessageHandler.handle when `wake_cues` is configured: a `listen detect`
    whose text is a wake word starts one random cue in the background, and neither it nor
    the next `listen start` resets the audio state, so the wake-word audio the device sends
    before the detect reaches VAD and ASR; that `listen start` sets conn.weixue_live_since."""

    async def wrapper(self, conn, msg_json):
        state = msg_json.get("state")
        if conn.config.get("wake_cues") and (
            (state == "detect" and is_wake_word(conn, msg_json.get("text"), remove_punctuation_and_length))
            or (state == "start" and getattr(conn, "weixue_keep_audio", False))
        ):
            if "mode" in msg_json:
                conn.client_listen_mode = msg_json["mode"]
            if state == "start":
                conn.weixue_keep_audio = False
                conn.weixue_live_since = time.time()
                return None
            conn.weixue_keep_audio = True
            conn.weixue_live_voice = False
            conn.last_activity_time = time.time() * 1000
            cue = random.choice(conn.config["wake_cues"])
            conn.weixue_wake_cue = asyncio.create_task(play(conn, cue))
            return None
        return await handle(self, conn, msg_json)

    wrapper.weixue_wrapped = handle
    return wrapper


DEVICE_EVENT_PREFIX = "notifications/weixue/"


def log_device_events(handle, logger):
    """Wraps McpTextMessageHandler.handle: a device MCP notification whose method starts with
    DEVICE_EVENT_PREFIX is logged with the device id and its params before upstream handles it."""

    async def wrapper(self, conn, msg_json):
        payload = msg_json.get("payload")
        method = payload.get("method") if isinstance(payload, dict) else None
        if isinstance(method, str) and method.startswith(DEVICE_EVENT_PREFIX):
            params = json.dumps(payload.get("params"), ensure_ascii=False, sort_keys=True)
            logger.bind(tag=__name__).info(
                f"device event {method} device={getattr(conn, 'device_id', None)} params={params}"
            )
        return await handle(self, conn, msg_json)

    wrapper.weixue_wrapped = handle
    return wrapper


def apply():
    from config.logger import setup_logging
    from core.connection import ConnectionHandler
    from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
    from core.utils.util import audio_to_data, remove_punctuation_and_length
    import core.http_server as http_server
    import weixue_proactive

    intentHandler = importlib.import_module("core.handle.intentHandler")
    mcpMessageHandler = importlib.import_module("core.handle.textHandler.mcpMessageHandler")
    receiveAudioHandle = importlib.import_module("core.handle.receiveAudioHandle")
    sendAudioHandle = importlib.import_module("core.handle.sendAudioHandle")
    silero = importlib.import_module("core.providers.vad.silero")
    logger = setup_logging()

    clean = ConnectionHandler.__dict__["_clean_response_garbage"].__func__
    if getattr(clean, "weixue_wrapped", None) is None:
        ConnectionHandler._clean_response_garbage = staticmethod(keep_trailing_whitespace(clean))

    if not isinstance(ConnectionHandler.__dict__.get("just_woken_up"), property):
        ConnectionHandler.just_woken_up = property(never_just_woken_up, lambda _conn, _value: None)

    proactive = weixue_proactive.instance()
    if getattr(ConnectionHandler.handle_connection, "weixue_wrapped", None) is None:
        ConnectionHandler.handle_connection = register_sessions(ConnectionHandler.handle_connection, proactive)
    if getattr(ListenTextMessageHandler.handle, "weixue_wrapped", None) is None:
        play = wake_cue_player(
            audio_to_data,
            sendAudioHandle.send_tts_message,
            sendAudioHandle.sendAudioMessage,
            sendAudioHandle.SentenceType.FIRST,
            sendAudioHandle.SentenceType.LAST,
            logger,
        )
        ListenTextMessageHandler.handle = intercept_invoke(
            acknowledge_wake(ListenTextMessageHandler.handle, remove_punctuation_and_length, play),
            proactive,
            weixue_proactive.INVOKE_TEXT,
        )
    if not isinstance(http_server.web, WebWithRoutes):
        http_server.web = WebWithRoutes(
            http_server.web, lambda app: weixue_proactive.add_routes(app, proactive)
        )

    if getattr(silero.VADProvider.is_vad, "weixue_wrapped", None) is None:
        silero.VADProvider.is_vad = hold_wake_utterance(silero.VADProvider.is_vad)

    no_voice = receiveAudioHandle.no_voice_close_connect
    if getattr(no_voice, "weixue_wrapped", None) is None:
        receiveAudioHandle.no_voice_close_connect = hold_idle_timer_while_replying(no_voice)

    if not getattr(intentHandler.checkWakeupWords, "weixue_replacement", False):
        intentHandler.checkWakeupWords = ignore_bare_wake_word(remove_punctuation_and_length, logger)

    mcp_handler = mcpMessageHandler.McpTextMessageHandler
    if getattr(mcp_handler.handle, "weixue_wrapped", None) is None:
        mcp_handler.handle = log_device_events(mcp_handler.handle, logger)
