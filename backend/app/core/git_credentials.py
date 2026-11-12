"""Keep Git credentials out of command arguments and persisted origin URLs."""
import base64
import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

_credentials = ContextVar("git_credentials", default=None)


@contextmanager
def git_credentials(token, url):
    handle = _credentials.set((token, urlsplit(url).hostname))
    try:
        yield
    finally:
        _credentials.reset(handle)


def git_environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_CONFIG_", "GIT_TRACE", "GIT_CURL_VERBOSE"))}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    values = [("credential.helper", ""), ("core.hooksPath", os.devnull)]
    from app.core.config import settings
    if settings.app_env.lower() == "production":
        values += [("protocol.allow", "never"), ("protocol.https.allow", "always"), ("http.followRedirects", "false")]
    credentials = _credentials.get()
    if credentials and credentials[0]:
        token, host = credentials
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        values.append((f"http.https://{host}/.extraheader", "AUTHORIZATION: basic " + encoded))
    env["GIT_CONFIG_COUNT"] = str(len(values))
    for index, (key, value) in enumerate(values):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env
