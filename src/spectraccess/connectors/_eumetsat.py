"""Private Data Store diagnostics shared by EUMETSAT connectors."""
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from xml.etree import ElementTree

_ACTIVE_AUTH = ContextVar("eumetsat_auth", default=((), None))


def _auth_values(credential=None, token=None):
    credentials = getattr(token, "credentials", ())
    values = (credential.account, credential.secret) if credential is not None else tuple(credentials)
    return tuple(value for value in (*values, getattr(token, "_access_token", "")) if value)


@contextmanager
def _eumdac_context(credential=None, token=None):
    marker = _ACTIVE_AUTH.set((_auth_values(credential, token), token))
    try:
        yield
    finally:
        _ACTIVE_AUTH.reset(marker)


def _redact(text, values=()):
    # Same exact-value/colon-part treatment as core credentials.describe_error,
    # without truncating log records or chained exception messages.
    parts = {part for value in values for part in (value, *value.split(":")) if part}
    for part in sorted(parts, key=len, reverse=True):
        text = text.replace(part, "<redacted>")
    text = re.sub(r"(?i)(Bearer\s+)[^\s'\"},<;)]+", r"\1<redacted>", text)
    return re.sub(r"(?i)(access_token=)[^&\s'\"<]+", r"\1<redacted>", text)


def _sanitize_chain(exc, values):
    def clean(value):
        if isinstance(value, str):
            return _redact(value, values)
        if isinstance(value, tuple):
            return tuple(clean(item) for item in value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {clean(key): clean(item) for key, item in value.items()}
        return value
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        current.args = clean(current.args)
        for name, value in vars(current).items():
            setattr(current, name, clean(value))
        pending.extend((current.__cause__, current.__context__))


class _EumdacRedaction(logging.Filter):
    """Sanitize upstream token and request records before any handler sees them."""
    def filter(self, record):
        values, token = _ACTIVE_AUTH.get()
        values = (*values, *_auth_values(token=token))
        message = _redact(record.getMessage(), values)
        if record.exc_info:
            _sanitize_chain(record.exc_info[1], values)
            record.exc_text = None
        record.msg, record.args = message, ()
        return True


def _install_log_redaction():
    # token.py and request.py both emit on this same upstream logger.
    logger = logging.getLogger("eumdac")
    if not any(isinstance(item, _EumdacRedaction) for item in logger.filters):
        logger.addFilter(_EumdacRedaction())


_install_log_redaction()


class EUMETSATProviderError(RuntimeError):
    """Redacted provider failure with HTTP status and failing stage."""
    def __init__(self, message, *, status_code, stage):
        super().__init__(message)
        self.status_code, self.stage = status_code, stage


class EUMETSATAuthorizationError(EUMETSATProviderError):
    """An authenticated EUMETSAT account lacks collection access."""
    provider_name = "Data Store"

    def __init__(self, collection, *, status_code, stage, provider_message):
        self.collection = collection
        super().__init__(
            f"{self.provider_name} EUMETSAT {stage} (HTTP {status_code}), collection {collection}: {provider_message}. "
            "The account is not authorised for this collection. Check the collection licence. The EUMETSAT Data Store FAQ "
            "says licence changes reach the Data Store after the next Data Store login, "
            "and propagation can take up to an hour.", status_code=status_code, stage=stage)


def _http_status(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        response = getattr(exc, "response", None)
        status = (getattr(response, "status_code", None) or
                  (getattr(exc, "extra_info", None) or {}).get("status"))
        if isinstance(status, int):
            return status
        exc = exc.__cause__ or exc.__context__
    return None


def _provider_message(exc):
    current, seen = exc, set()
    fallback = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if response is not None and response.text:
            text = response.text
            break
        info = getattr(current, "extra_info", None) or {}
        fallback = fallback or info.get("text") or info.get("response")
        current = current.__cause__ or current.__context__
    else:
        text = str(fallback) if fallback else str(exc)
    try:
        root = ElementTree.fromstring(text)
        messages = [" ".join(node.itertext()) for node in root.iter()
                    if node.tag.rsplit("}", 1)[-1].lower() == "exceptiontext"]
        if messages:
            return "\n".join(messages)
    except ElementTree.ParseError:
        pass
    return text


def _provider_failure(exc, stage, collection=None, *, values=(), provider_name="Data Store",
                      provider_error=EUMETSATProviderError, authorization_error=EUMETSATAuthorizationError):
    from spectraccess.core.credentials import CredentialRejected, describe_error

    values = (*values, *_ACTIVE_AUTH.get()[0])
    status = _http_status(exc)
    message = _redact(_provider_message(exc), values)
    # Reuse core credential redaction and its 500-character diagnostic bound.
    original_length = len(" ".join(message.split()))
    for value in values or ("",):
        message = describe_error(RuntimeError(message), secret=value).removeprefix("RuntimeError: ")
    if original_length > 500:
        message += " [truncated]"
    _sanitize_chain(exc, values)
    detail = f"{provider_name} EUMETSAT {stage} (HTTP {status if status is not None else 'unavailable'}): {message}"
    if status in (401, 403) and collection is not None:
        return authorization_error(collection, status_code=status, stage=stage, provider_message=message)
    if status in (401, 403) and stage == "token authentication":
        failure = CredentialRejected("eumetsat", "credential source")
        failure.args = (detail,)
        failure.status_code, failure.stage = status, stage
        return failure
    return provider_error(detail, status_code=status, stage=stage)


