"""HTTP timeouts for the Readarr/Chaptarr and slskd clients.

Neither client sets a timeout by default, so a server that accepts a connection but
never answers (a hung container, a half-open connection after a network blip) could
freeze R:soul indefinitely. A timeout turns that into an ordinary error that the
existing retry and resume logic already handles.
"""

from typing import Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter

Timeout = Union[float, Tuple[float, float]]


class TimeoutAdapter(HTTPAdapter):
    """An HTTPAdapter that applies a default timeout to requests that don't set one."""

    def __init__(self, timeout: Timeout, *args, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        return super().send(request, **kwargs)


def apply_timeout(session: requests.Session, timeout: Optional[float]) -> None:
    """Give every request made through `session` a default timeout (seconds; 0/None = none)."""
    if not timeout or timeout <= 0:
        return
    adapter = TimeoutAdapter(timeout)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
