"""Explicit host-configured loopback business adapter with queryable idempotent receipts."""

import json
from urllib.parse import urlsplit

import httpx

from .ticket_store import EvidenceUnavailable, TicketReceipt


def load_http_business(path):
    """Only the trusted operator supplies this private config, never task arguments."""
    config = json.loads(path.read_text(encoding="utf-8"))
    if set(config) != {"base_url", "token"}:
        raise ValueError("Business config requires base_url and token only")
    return HttpTicketStore(config["base_url"], config["token"])


class HttpTicketStore:
    def __init__(self, base_url, token):
        if not isinstance(base_url, str) or not isinstance(token, str) or not token:
            raise ValueError("Business endpoint and token must be nonempty strings")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Business URL must explicitly target a loopback HTTP mock")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method, path, body=None):
        try:
            with httpx.Client(timeout=3, trust_env=False) as client:
                response = client.request(
                    method,
                    self.base_url + path,
                    json=body,
                    headers={"Authorization": "Bearer " + self.token},
                )
                response.raise_for_status()
                if len(response.content) > 65536:
                    raise ValueError("Oversized business response")
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise EvidenceUnavailable("Business HTTP evidence unavailable") from None

    def initialize(self):
        return self._request("GET", "/workspace")["workspace_id"]

    def ticket(self, workspace_id=None):
        result = self._request("POST", "/ticket", {"workspace_id": workspace_id})
        return result["status"], result["revision"]

    def receipt(self, workspace_id, operation_id):
        result = self._request(
            "POST",
            "/receipt",
            {"workspace_id": workspace_id, "operation_id": operation_id},
        )
        return TicketReceipt(**result) if result is not None else None

    def apply(self, workspace_id, operation_id, digest, ticket_id, status):
        try:
            return TicketReceipt(
                **self._request(
                    "POST",
                    "/apply",
                    {
                        "workspace_id": workspace_id,
                        "operation_id": operation_id,
                        "digest": digest,
                        "ticket_id": ticket_id,
                        "status": status,
                    },
                )
            )
        except EvidenceUnavailable:
            # A transport failure may hide a committed write. Query; never blindly resend.
            receipt = self.receipt(workspace_id, operation_id)
            if receipt is None:
                raise EvidenceUnavailable("Business outcome remains unknown") from None
            return receipt
