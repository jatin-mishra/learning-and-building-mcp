
import base64
import hashlib
import json
import secrets
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
import jwt

AUTH_SERVER = "http://localhost:9000"
RESOURCE = "http://localhost:8000/mcp"            # the server we want a token FOR
REDIRECT_URI = "http://localhost:8765/callback"   # where the browser comes back to us


def http_json(url: str, data: dict | None = None, form: bool = False) -> dict:
    body, headers = None, {}
    if data is not None and form:
        body, headers = urlencode(data).encode(), {"Content-Type": "application/x-www-form-urlencoded"}
    elif data is not None:
        body, headers = json.dumps(data).encode(), {"Content-Type": "application/json"}
    with urllib.request.urlopen(urllib.request.Request(url, body, headers)) as response:
        return json.loads(response.read())


def wait_for_redirect() -> dict:
    """Run a one-shot web server and return the query string the browser brings back."""
    result = {}
    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            result.update({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done! You can close this tab.")
        def log_message(self, *args):
            pass
    with HTTPServer(("127.0.0.1", 8765), Callback) as server:
        server.handle_request()
    return result

def main():
    # 1. Discover: ask the authorization server what it supports.
    meta = http_json(f"{AUTH_SERVER}/.well-known/oauth-authorization-server")
    assert "S256" in meta["code_challenge_methods_supported"], "no PKCE support: refuse to continue"
    print("1. Discovered endpoints:", meta["authorization_endpoint"], meta["token_endpoint"])
    
    
    # 2. Register: get a client_id (Dynamic Client Registration, fine for a local demo).
    client = http_json(meta["registration_endpoint"],
                       {"client_name": "Try-OAuth script", "redirect_uris": [REDIRECT_URI]})
    print("2. Registered as", client["client_id"])

    # 3. PKCE: a random secret (verifier) and its SHA-256 fingerprint (challenge).
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    # 4. Send the user to the authorization server to approve. Only the challenge travels here.
    url = meta["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT_URI,
        "scope": "notes:read", "resource": RESOURCE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("3. Opening the browser to approve...")
    webbrowser.open(url)
    answer = wait_for_redirect()

    # 5. Check the answer really belongs to this request, from this server.
    assert answer.get("state") == state, "state mismatch: not our request"
    assert answer.get("iss") == meta["issuer"], "issuer mismatch: possible mix-up attack"
    if "error" in answer:
        raise SystemExit(f"Not approved: {answer['error']}")
    print("4. Got an authorization code:", answer["code"][:10] + "...")

    # 6. Swap the code (plus the secret verifier) for an access token.
    token = http_json(meta["token_endpoint"], {
        "grant_type": "authorization_code", "code": answer["code"], "redirect_uri": REDIRECT_URI,
        "client_id": client["client_id"], "code_verifier": verifier, "resource": RESOURCE}, form=True)
    print("5. Access token:", token["access_token"][:40] + "...")

    # 7. Look inside. (Reading a JWT needs no key; trusting it does. Servers verify the signature.)
    print("6. Claims:", json.dumps(jwt.decode(token["access_token"], options={"verify_signature": False}), indent=2))

if __name__ == "__main__":
    main()