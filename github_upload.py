"""Create or update one file in ONE GitHub repository via the contents API.

Fixed host api.github.com, no redirects followed, token only in a header and
never in messages or logs.
"""
import base64
import http.client
import json
import re
import ssl
from urllib.parse import quote

HOST = "api.github.com"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
PATH_RE = re.compile(r"(data|status)/[0-9A-Za-z_.-]{1,80}\.(json|md)|signals/\d{4}-\d{2}-\d{2}\.jsonl")


class UploadError(Exception):
    def __init__(self, category, status=None):
        super().__init__(category)
        self.status = status


def _https(method, path, headers, body):
    conn = http.client.HTTPSConnection(HOST, timeout=20, context=ssl.create_default_context())
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read(5_000_000)
    finally:
        conn.close()


class GitHubUploader:
    def __init__(self, token, repo, *, send=None):
        if not REPO_RE.fullmatch(repo or ""):
            raise ValueError("invalid repo")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{20,255}", token):
            raise ValueError("invalid token format")
        self.__token = token
        self.repo = repo
        self.__send = send or _https

    def __repr__(self):
        return f"GitHubUploader({self.repo}, <redacted>)"

    def _call(self, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.__token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "trading-review-export"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, payload = self.__send(method, path, headers, data)
        except Exception:
            raise UploadError("network_error") from None
        return status, payload

    def put_file(self, repo_path, content_bytes, message):
        if not PATH_RE.fullmatch(repo_path):
            raise UploadError("path_not_allowed")
        api = f"/repos/{self.repo}/contents/{quote(repo_path)}"
        status, payload = self._call("GET", api)
        sha = None
        if status == 200:
            try:
                info = json.loads(payload)
                sha = info.get("sha")
                if base64.b64decode(info.get("content", "")) == content_bytes:
                    return "unchanged"
            except Exception:
                raise UploadError("bad_response", status) from None
        elif status != 404:
            raise UploadError(_category(status), status)
        body = {"message": message, "content": base64.b64encode(content_bytes).decode("ascii")}
        if sha:
            body["sha"] = sha
        status, _ = self._call("PUT", api, body)
        if status not in (200, 201):
            raise UploadError(_category(status), status)
        return "updated" if sha else "created"


def _category(status):
    if status in (401, 403):
        return "github_token_invalid_or_expired"
    if status == 404:
        return "github_repo_not_found_or_no_access"
    if status == 409:
        return "github_conflict"
    if status == 422:
        return "github_rejected"
    return "github_http_error"
