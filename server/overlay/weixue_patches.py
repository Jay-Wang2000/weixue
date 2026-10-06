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


def apply():
    from core.connection import ConnectionHandler

    clean = ConnectionHandler.__dict__["_clean_response_garbage"].__func__
    if getattr(clean, "weixue_wrapped", None) is None:
        ConnectionHandler._clean_response_garbage = staticmethod(keep_trailing_whitespace(clean))
