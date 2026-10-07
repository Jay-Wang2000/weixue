"""Runtime patches to upstream modules, applied by weixue_main.py before app.py runs."""
import asyncio
import importlib
import json
import random
import re
import time
import uuid
from collections import deque
from difflib import SequenceMatcher

_RESPONSE_VALUE = re.compile(r'"response"\s*:\s*"')
_JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _hex4(text, i):
    digits = text[i : i + 4]
    if len(digits) < 4:
        return None
    try:
        return int(digits, 16)
    except ValueError:
        return -1


def decode_direct_answer(arguments):
    """Replacement for ConnectionHandler._extract_direct_answer_response: the decoded `response`
    string of direct_answer arguments; for incomplete streamed arguments, the longest prefix
    decodable so far (no quotes, braces or partial escapes), so each call extends the last."""
    if not arguments:
        return ""
    try:
        data = json.loads(arguments)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        value = data.get("response")
        return value if isinstance(value, str) else ""
    match = _RESPONSE_VALUE.search(arguments)
    if match is None:
        return ""
    out, i, n = [], match.end(), len(arguments)
    while i < n:
        c = arguments[i]
        if c == '"':
            break
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        if arguments[i + 1] != "u":
            out.append(_JSON_ESCAPES.get(arguments[i + 1], arguments[i + 1]))
            i += 2
            continue
        code = _hex4(arguments, i + 2)
        if code is None:
            break
        if 0xD800 <= code < 0xDC00:
            if i + 12 > n:
                break
            low = _hex4(arguments, i + 8) if arguments[i + 6 : i + 8] == "\\u" else -1
            if 0xDC00 <= low < 0xE000:
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                i += 12
                continue
        out.append(chr(code) if 0 <= code and not 0xD800 <= code < 0xE000 else "\ufffd")
        i += 6
    return "".join(out)


decode_direct_answer.weixue_replacement = True


def keep_decoded_text(text):
    """Replacement for ConnectionHandler._clean_response_garbage: decode_direct_answer output
    carries no JSON framing, so streamed text is passed on unchanged."""
    return text


keep_decoded_text.weixue_replacement = True


def register_sessions(handle_connection, proactive):
    """Wraps ConnectionHandler.handle_connection: while it runs, a session whose device identity
    proactive.auth verifies is registered for proactive delivery; on its end a pending wake cue
    is cancelled."""

    async def wrapper(self, ws):
        device = proactive.auth.device(ws.request.headers)
        if device:
            proactive.on_session_open(device, self, ws)
        try:
            return await handle_connection(self, ws)
        finally:
            cancel_wake_cue(self)
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


def ignore_bare_wake_word(remove_punctuation_and_length, logger):
    """Replacement for upstream checkWakeupWords: a transcript that is only a configured
    wake word is consumed with no reply."""

    async def check_wakeup_words(conn, text):
        if is_wake_word(conn, text, remove_punctuation_and_length):
            logger.bind(tag=__name__).info(f"wake word transcript ignored: {text}")
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
    heard after VAD_FLUSH_S sets conn.weixue_live_voice. The start of every utterance is kept
    as conn.weixue_voice_onset (monotonic time, sentence_id, server holding the turn)."""

    def wrapper(self, conn, pcm_frame):
        before = getattr(conn, "client_have_voice", False)
        have_voice = is_vad(self, conn, pcm_frame)
        if not before and getattr(conn, "client_have_voice", False):
            conn.weixue_voice_onset = (time.monotonic(), conn.sentence_id, conn.client_is_speaking)
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


_SCHEDULED = object()


def _closed(conn):
    stop = getattr(conn, "stop_event", None)
    return stop is not None and stop.is_set()


def wake_cue_player(audio_to_data, send_tts_message, send_audio_message, first, last, logger):
    """Returns play(conn, cue, turn): CUE_DELAY_S after the wake, one tts start /
    sentence_start + audio / tts stop for cue["file"], captioned cue["text"], under a sentence_id
    kept as conn.weixue_cue_sentence. turn is
    conn.sentence_id when the cue was scheduled (default: at the call). Before the cue takes
    the connection, and after every await, it is dropped once a newer turn owns the connection
    (sentence_id changed), live voice was heard, the server holds a turn or the connection
    closed."""

    async def play(conn, cue, turn=_SCHEDULED):
        if turn is _SCHEDULED:
            turn = conn.sentence_id

        def superseded():
            return (
                _closed(conn)
                or conn.sentence_id != turn
                or getattr(conn, "weixue_live_voice", False)
                or conn.client_is_speaking
            )

        deadline = time.monotonic() + TTS_READY_S
        while conn.tts is None:
            if time.monotonic() > deadline:
                logger.bind(tag=__name__).warning("wake cue skipped: TTS not ready")
                return
            await asyncio.sleep(0.05)
        await asyncio.sleep(CUE_DELAY_S)
        if superseded():
            logger.bind(tag=__name__).info("wake cue skipped: user speaking or newer turn")
            return
        packets = await audio_to_data(cue["file"])
        if superseded():
            logger.bind(tag=__name__).info("wake cue dropped after loading: user speaking or newer turn")
            return
        sentence_id = uuid.uuid4().hex
        conn.sentence_id = sentence_id
        conn.weixue_cue_sentence = sentence_id
        conn.client_abort = False

        def owned():
            return not _closed(conn) and conn.sentence_id == sentence_id

        await send_tts_message(conn, "start")
        if not owned():
            return
        conn.client_is_speaking = True
        await asyncio.sleep(DECODER_RESET_S)
        if not owned():
            return
        logger.bind(tag=__name__).info(f"wake cue: {cue.get('text')}")
        await send_audio_message(conn, first, packets, cue.get("text"), sentence_id)
        if owned():
            await send_audio_message(conn, last, [], None, sentence_id)

    return play


def cancel_wake_cue(conn):
    task = getattr(conn, "weixue_wake_cue", None)
    if task is not None and not task.done():
        task.cancel()


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
            cancel_wake_cue(conn)
            conn.weixue_wake_cue = asyncio.create_task(play(conn, cue, conn.sentence_id))
            return None
        return await handle(self, conn, msg_json)

    wrapper.weixue_wrapped = handle
    return wrapper


# Playback record and echo guard

# An utterance is checked against the reply sentences playing when it started: the current
# sentence while the server held the turn, or one whose packets ran until less than
# ECHO_TAIL_S before it. Wake cues are not kept. ECHO_SENTENCES most recent sentences are
# kept per connection.
ECHO_TAIL_S = 1.0
ECHO_SENTENCES = 6
# Transcripts shorter than ECHO_SHORT_CHARS letters must match a whole sentence to
# ECHO_SHORT_RATIO (by letters); longer ones must have ECHO_COVER_RATIO of their words
# (Chinese: characters) in order inside one.
ECHO_SHORT_CHARS = 6
ECHO_SHORT_RATIO = 0.8
ECHO_COVER_RATIO = 0.7
_ECHO_TOKEN = re.compile(r"[0-9a-z]+|[\u3400-\u9fff]")


class SentencePlayback:
    """Audio handed to the device for one sentence_id: frames counts audio packets sent while
    it was the current, unaborted sentence; done is set once its last packet went out unaborted;
    failed is set when a text segment (FIRST) got no audio before the next FIRST or the LAST,
    or note_synthesis_failure reported one."""

    def __init__(self):
        self.frames = 0
        self.failed = False
        self.segment = None
        self.done = asyncio.Event()

    def count(self, sentence_type, packets, first, last):
        self.frames += packets
        if sentence_type == first:
            if self.segment == 0:
                self.failed = True
            self.segment = packets
        elif self.segment is not None:
            self.segment += packets
            if sentence_type == last and self.segment == 0:
                self.failed = True


def track_playback(conn, sentence_id):
    records = conn.__dict__.setdefault("weixue_playback", {})
    records[sentence_id] = SentencePlayback()
    return records[sentence_id]


def untrack_playback(conn, sentence_id):
    getattr(conn, "weixue_playback", {}).pop(sentence_id, None)


def note_synthesis_failure(conn, sentence_id):
    """Called by a TTS provider (any thread) when synthesis for sentence_id raised."""
    record = getattr(conn, "weixue_playback", {}).get(sentence_id)
    if record is not None:
        record.failed = True


def _packets(audios):
    if isinstance(audios, (bytes, bytearray)):
        return 1 if audios else 0
    return len(audios or ())


def watch_sent_audio(send, first, last):
    """Wraps upstream sendAudioMessage: for the connection's current sentence, while not
    aborted, counts audio packets into its track_playback record and sets done after its last
    packet, and keeps each captioned sentence other than a wake cue with the time of its latest
    packet for echo_of_playback."""

    async def wrapper(conn, sentence_type, audios, text, sentence_id=None):
        sid = conn.sentence_id if sentence_id is None else sentence_id
        current = sid == conn.sentence_id and not conn.client_abort
        record = getattr(conn, "weixue_playback", {}).get(sid) if current else None
        if current:
            spoken = conn.__dict__.setdefault("weixue_spoken", deque(maxlen=ECHO_SENTENCES))
            if sentence_type == first and text and sid != getattr(conn, "weixue_cue_sentence", None):
                now = time.monotonic()
                spoken.append({"sid": sid, "text": text, "first": now, "last": now})
            elif spoken and spoken[-1]["sid"] == sid:
                spoken[-1]["last"] = time.monotonic()
            if record is not None:
                record.count(sentence_type, _packets(audios), first, last)
        try:
            return await send(conn, sentence_type, audios, text, sentence_id)
        finally:
            if record is not None and sentence_type == last and not conn.client_abort:
                if conn.weixue_spoken and conn.weixue_spoken[-1]["sid"] == sid:
                    conn.weixue_spoken[-1]["last"] = time.monotonic()
                record.done.set()

    wrapper.weixue_wrapped = send
    wrapper.weixue_playback = True
    return wrapper


def _echo_tokens(text):
    if isinstance(text, str) and text.strip().startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "content" in data:
                text = str(data["content"])
        except ValueError:
            pass
    return _ECHO_TOKEN.findall((text or "").casefold())


def echo_of_playback(conn, text):
    """The played sentence text matches, else None. Only sentences playing when the utterance
    started (conn.weixue_voice_onset) are compared; without an onset nothing matches."""
    onset = getattr(conn, "weixue_voice_onset", None)
    heard = _echo_tokens(text)
    letters = "".join(heard)
    if onset is None or not letters:
        return None
    started, onset_sid, holding = onset
    for entry in reversed(getattr(conn, "weixue_spoken", ())):
        if entry["first"] > started:
            continue
        current = holding and entry["sid"] == onset_sid
        if not current and started > entry["last"] + ECHO_TAIL_S:
            continue
        said = _echo_tokens(entry["text"])
        if not said:
            continue
        if len(letters) < ECHO_SHORT_CHARS:
            if SequenceMatcher(None, letters, "".join(said), autojunk=False).ratio() >= ECHO_SHORT_RATIO:
                return entry["text"]
            continue
        blocks = SequenceMatcher(None, heard, said, autojunk=False).get_matching_blocks()
        if sum(b.size for b in blocks) / len(heard) >= ECHO_COVER_RATIO:
            return entry["text"]
    return None


def guard_playback_echo(start_to_chat, logger):
    """Wraps the ASR path's startToChat: a transcript echo_of_playback matches is dropped
    before upstream can treat it as barge-in; any other transcript cancels a pending wake cue
    and starts the turn."""

    async def wrapper(conn, text):
        played = echo_of_playback(conn, text)
        if played is not None:
            logger.bind(tag=__name__).info(f"playback echo ignored: {text!r} matches {played!r}")
            return None
        cancel_wake_cue(conn)
        return await start_to_chat(conn, text)

    wrapper.weixue_wrapped = start_to_chat
    return wrapper


def bind_device_auth(init, proactive):
    """Wraps SimpleHttpServer.__init__: proactive device requests use the server.auth of its config."""

    def wrapper(self, config, *args, **kwargs):
        init(self, config, *args, **kwargs)
        proactive.configure_auth(config)

    wrapper.weixue_wrapped = init
    return wrapper


def apply_playback_guards(proactive, logger):
    """Playback record, echo guard and proactive device auth. Runs before the wake cue player
    is built, so the cue sends through the wrapped sendAudioMessage."""
    import core.http_server as http_server

    send_audio = importlib.import_module("core.handle.sendAudioHandle")
    tts_base = importlib.import_module("core.providers.tts.base")
    asr_base = importlib.import_module("core.providers.asr.base")
    kinds = send_audio.SentenceType
    for module in (send_audio, tts_base):
        if not getattr(module.sendAudioMessage, "weixue_playback", False):
            module.sendAudioMessage = watch_sent_audio(module.sendAudioMessage, kinds.FIRST, kinds.LAST)
    if getattr(asr_base.startToChat, "weixue_wrapped", None) is None:
        asr_base.startToChat = guard_playback_echo(asr_base.startToChat, logger)
    server = http_server.SimpleHttpServer
    if getattr(server.__init__, "weixue_wrapped", None) is None:
        server.__init__ = bind_device_auth(server.__init__, proactive)


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


def unlisted(handler, device_id):
    """True when server.auth admits only allowed_devices (auth enabled, list non-empty) and
    device_id is not on it. `handler` carries upstream's auth_enable and allowed_devices."""
    return bool(handler.auth_enable and handler.allowed_devices) and device_id not in handler.allowed_devices


def refuse_unlisted_ota(handle_post, web, logger):
    """Wraps OTAHandler.handle_post: with auth enabled and allowed_devices set, a Device-Id not on
    the list gets 403 and no websocket section; listed devices get upstream's response."""

    async def wrapper(self, request):
        device_id = request.headers.get("device-id", "")
        if unlisted(self, device_id):
            logger.bind(tag=__name__).warning(f"OTA refused: device {device_id!r} is not in allowed_devices")
            response = web.json_response({"success": False, "message": "device not allowed"}, status=403)
            self._add_cors_headers(response)
            return response
        return await handle_post(self, request)

    wrapper.weixue_wrapped = handle_post
    return wrapper


def refuse_unlisted_websocket(handle_auth, authentication_error, logger):
    """Wraps WebSocketServer._handle_auth: with auth enabled and allowed_devices set, a Device-Id
    not on the list is refused whatever token it presents."""

    async def wrapper(self, websocket):
        device_id = websocket.request.headers.get("device-id")
        if unlisted(self, device_id):
            logger.bind(tag=__name__).warning(f"WebSocket refused: device {device_id!r} is not in allowed_devices")
            raise authentication_error("device not in allowed_devices")
        return await handle_auth(self, websocket)

    wrapper.weixue_wrapped = handle_auth
    return wrapper


def apply():
    from aiohttp import web
    from config.logger import setup_logging
    from core.api.ota_handler import OTAHandler
    from core.auth import AuthenticationError
    from core.connection import ConnectionHandler
    from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
    from core.utils.util import audio_to_data, remove_punctuation_and_length
    import core.http_server as http_server
    import weixue_proactive

    websocket_server = importlib.import_module("core.websocket_server")
    intentHandler = importlib.import_module("core.handle.intentHandler")
    mcpMessageHandler = importlib.import_module("core.handle.textHandler.mcpMessageHandler")
    receiveAudioHandle = importlib.import_module("core.handle.receiveAudioHandle")
    sendAudioHandle = importlib.import_module("core.handle.sendAudioHandle")
    silero = importlib.import_module("core.providers.vad.silero")
    logger = setup_logging()

    for name, replacement in (
        ("_extract_direct_answer_response", decode_direct_answer),
        ("_clean_response_garbage", keep_decoded_text),
    ):
        if not getattr(ConnectionHandler.__dict__[name].__func__, "weixue_replacement", False):
            setattr(ConnectionHandler, name, staticmethod(replacement))

    if not isinstance(ConnectionHandler.__dict__.get("just_woken_up"), property):
        ConnectionHandler.just_woken_up = property(never_just_woken_up, lambda _conn, _value: None)

    proactive = weixue_proactive.instance()
    apply_playback_guards(proactive, logger)
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

    if getattr(OTAHandler.handle_post, "weixue_wrapped", None) is None:
        OTAHandler.handle_post = refuse_unlisted_ota(OTAHandler.handle_post, web, logger)
    ws_server = websocket_server.WebSocketServer
    if getattr(ws_server._handle_auth, "weixue_wrapped", None) is None:
        ws_server._handle_auth = refuse_unlisted_websocket(ws_server._handle_auth, AuthenticationError, logger)
