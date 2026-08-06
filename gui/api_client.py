"""HTTP helpers for the GUI backend API."""
import json
import urllib.error
import urllib.request

from gui.constants import BASE


def http_json(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    base: str = BASE,
    timeout: int = 60,
) -> dict | list:
    """Generic JSON HTTP call; returns parsed JSON body."""
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    r = urllib.request.urlopen(req, timeout=timeout)
    raw = r.read()
    return json.loads(raw) if raw else {}


def http_get(path: str, *, base: str = BASE, timeout: int = 60):
    return http_json("GET", path, base=base, timeout=timeout)


def http_post(path: str, body: dict, *, base: str = BASE, timeout: int = 60):
    return http_json("POST", path, body, base=base, timeout=timeout)


def http_delete(path: str, *, base: str = BASE, timeout: int = 60):
    return http_json("DELETE", path, base=base, timeout=timeout)


def call_text_rewrite(
    text: str,
    kind: str,
    mode: str,
    extra_hint: str = "",
    *,
    base: str = BASE,
) -> str:
    """调用 /text/simplify 或 /text/beautify，返回改写后的文本。"""
    endpoint = "/text/simplify" if mode == "simplify" else "/text/beautify"
    body = {"text": text, "kind": kind, "extra_hint": extra_hint or ""}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}{endpoint}", data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=120)
        return json.loads(r.read()).get("result") or ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            detail = payload.get("detail") or raw
        except Exception:
            detail = ""
        if isinstance(detail, list):
            detail = "; ".join(str(x) for x in detail)
        msg = f"HTTP Error {e.code}: {detail or e.reason or 'Bad Request'}"
        raise RuntimeError(msg) from e
