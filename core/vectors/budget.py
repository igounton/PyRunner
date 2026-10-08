"""A bounded HTTP session for consumers with a registry call deadline."""

import time

import requests
from urllib3.util import Timeout


class BudgetSession(requests.Session):
    bounded = True

    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise requests.Timeout("Call budget exhausted")
        return value

    def request(self, method, url, **kwargs):
        remaining = self.remaining()
        kwargs.update(
            timeout=Timeout(total=remaining, connect=min(3, remaining), read=remaining),
            stream=True,
            allow_redirects=False,
        )
        response = super().request(method, url, **kwargs)
        try:
            if 300 <= response.status_code < 400:
                raise requests.RequestException("Provider redirects are not supported")
            chunks, size = [], 0
            while True:
                remaining = self.remaining()
                sock = getattr(getattr(getattr(response.raw, "_fp", None), "fp", None), "raw", None)
                sock = getattr(sock, "_sock", None)
                if sock is not None:
                    sock.settimeout(remaining)
                chunk = response.raw.read1(16384, decode_content=True)
                if not chunk:
                    break
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise requests.RequestException("Provider response exceeds 8 MB")
                chunks.append(chunk)
            self.remaining()
            response._content = b"".join(chunks)
            response._content_consumed = True
            return response
        finally:
            response.close()
