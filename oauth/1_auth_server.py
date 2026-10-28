import jwt
import html
import time
import json
import urllib
import base64
import secrets
import hashlib
from urllib.parse import urlparse, parse_qs, urlencode
from http.cookies import SimpleCookie 
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "http://localhost:9000" # SELF
SCOPES = {"notes:read", "notes:write"}

RESOURCES = {"http://localhost:8000/mcp", "http://localhost:8001/mcp"} # Servers We issue tokens for

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

KEY_ID = "key-" + secrets.token_hex(4) 

clients = {}
pending = {}
codes = {}

METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "registration_endpoint": f"{ISSUER}/register",
    "jwks_uri": f"{ISSUER}/jwks.json",

    "scopes_supported": sorted(SCOPES),
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code"],
    "code_challenge_methods_supported": ["S256"],

    "token_endpoint_auth_methods_supported": ["none"],

    "client_id_metadata_document_supported": True,
    "authorization_response_iss_parameter_supported": True,
}




def public_jwks() -> dict:
    """The public half of our signing key. Anyone may read it to check our signatures."""
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key(), as_dict=True)
    return {"keys": [{**jwk, "kid": KEY_ID, "use": "sig", "alg": "RS256"}]}


def safe_redirect(uri: str) -> bool:
    """Redirect URIs must be HTTPS, or plain HTTP on this machine, with no hidden characters."""
    if any(ord(c) < 33 or c in '"#<>' for c in uri):   # \r\n would smuggle in extra headers
        return False
    url = urlparse(uri)
    return url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1"))

def find_client(client_id: str) -> dict | None:
    if client_id in clients:                       # registered through /register
        return clients[client_id]
    if client_id.startswith("https://"):           # Client ID Metadata Document: the ID is a URL
        try:                                       # (a real server also blocks private IPs and redirects,
            with urllib.request.urlopen(client_id, timeout=5) as response:   # and caches: see SSRF)
                doc = json.loads(response.read(65536))
        except (OSError, ValueError):
            return None
        if isinstance(doc, dict) and doc.get("client_id") == client_id \
                and isinstance(doc.get("redirect_uris"), list):
            return doc
    return None


def check_authorize_request(q: dict) -> tuple[dict | None, str | None]:
    """Validate an authorization request. Returns (client, error)."""

    client = find_client(q.get("client_id", ""))

    if not client:
        return None, "Unknown client_id."
    
    redirect_uri = q.get("redirect_uri", "")

    if redirect_uri not in client["redirect_uris"] or not safe_redirect(redirect_uri):   # exact match
        return None, "redirect_uri is not registered for this client."
    
    if q.get("response_type") != "code":
        return None, "Only response_type=code is supported."
    
    if not q.get("code_challenge") or q.get("code_challenge_method") != "S256":
        return None, "PKCE is required: send code_challenge with code_challenge_method=S256."
    
    if q.get("resource") not in RESOURCES:
        return None, "Send resource=<a server this desk knows> so the token is bound to it."
    
    if not set(q.get("scope", "").split()) <= SCOPES:
        return None, f"Unknown scope. Allowed: {' '.join(sorted(SCOPES))}"
    
    return client, None


def consent_page(client: dict, q: dict, request_id: str) -> str:
    return f"""
    <!doctype html>
    <title>Allow access?</title>
    <body style="font-family:sans-serif;max-width:32rem;margin:4rem auto">
        <h2>{html.escape(client.get("client_name", "An app"))} wants to use your notes</h2>
        <p>Signed in as <b>demo-user</b> (toy server: there is no real login).</p>
        <p>Access requested: <b>{html.escape(q.get("scope") or "notes:read")}</b><br>For server: <code>{html.escape(q["resource"])}</code><br>You will be sent back to: <code>{html.escape(urlparse(q["redirect_uri"]).netloc)}</code></p>
        <form method="post" action="/authorize">
            <input type="hidden" name="request_id" value="{request_id}">
            <button name="decision" value="allow">Allow</button> <button name="decision" value="deny">Deny</button>
        </form>
    </body>"""


class Handler(BaseHTTPRequestHandler):

    def send(self, status: int, body: dict | str, headers: dict | None = None):
        data = json.dumps(body) if isinstance(body, dict) else body
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if isinstance(body, dict) else "text/html")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")          # no hiding our page inside another site
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data.encode())

    def form(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        return {k: v[0] for k, v in parse_qs(raw).items()}

    # ---------- GET: metadata, public keys, and the consent page ----------
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/.well-known/oauth-authorization-server":
            return self.send(200, METADATA)
        if url.path == "/jwks.json":
            return self.send(200, public_jwks())
        if url.path == "/authorize":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            client, error = check_authorize_request(q)
            if error:   # don't redirect: an unverified redirect_uri may be the attack itself
                return self.send(400, f"<h2>Bad request</h2><p>{html.escape(error)}</p>")
            request_id = secrets.token_urlsafe(16)          # the request waits here, not in the page
            pending[request_id] = q
            cookie = f"consent={request_id}; HttpOnly; SameSite=Strict; Path=/authorize"
            return self.send(200, consent_page(client, q, request_id), {"Set-Cookie": cookie})
        self.send(404, {"error": "not_found"})


    def do_POST(self):
        if self.path == "/register":
            return self.register()
        if self.path == "/authorize":
            return self.decide()
        if self.path == "/token":
            return self.token()
        self.send(404, {"error": "not_found"})

    def register(self):
        """Dynamic Client Registration (RFC 7591). Deprecated in MCP, but handy on localhost."""
        meta = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        uris = meta.get("redirect_uris") or []
        if not uris or not all(isinstance(u, str) and safe_redirect(u) for u in uris):
            return self.send(400, {"error": "invalid_redirect_uri"})
        client_id = "client-" + secrets.token_urlsafe(8)
        clients[client_id] = {"client_id": client_id, "client_name": meta.get("client_name", "Unnamed app"),
                              "redirect_uris": uris}
        self.send(201, {**clients[client_id], "client_id_issued_at": int(time.time()),
                        "grant_types": ["authorization_code"], "response_types": ["code"],
                        "token_endpoint_auth_method": "none"})

    def decide(self):
        form = self.form()
        request_id = form.get("request_id", "")
        # The cookie proves this click came from the browser that saw OUR consent page (no CSRF).
        cookie = SimpleCookie(self.headers.get("Cookie", "")).get("consent")
        q = pending.pop(request_id, None)
        if q is None or cookie is None or not secrets.compare_digest(cookie.value, request_id):
            return self.send(400, "<h2>Bad request</h2><p>This approval link is unknown or expired.</p>")
        answer = {"state": q.get("state", ""), "iss": ISSUER}
        if form.get("decision") == "allow":
            code = secrets.token_urlsafe(32)
            codes[code] = {"client_id": q["client_id"], "redirect_uri": q["redirect_uri"],
                           "challenge": q["code_challenge"], "scope": q.get("scope") or "notes:read",
                           "resource": q["resource"], "user": "demo-user", "expires": time.time() + 60}
            answer["code"] = code
        else:
            answer["error"] = "access_denied"
        separator = "&" if "?" in q["redirect_uri"] else "?"
        self.send(302, "", {"Location": q["redirect_uri"] + separator + urlencode(answer)})
        
    def token(self):
        q = self.form()
        if q.get("grant_type") != "authorization_code":
            return self.send(400, {"error": "unsupported_grant_type"})
        
        grant = codes.pop(q.get("code", ""), None)            # pop: a code works exactly once
        if not grant or grant["expires"] < time.time():
            return self.send(400, {"error": "invalid_grant", "error_description": "Unknown or expired code."})
        
        if q.get("client_id") != grant["client_id"] or q.get("redirect_uri") != grant["redirect_uri"]:
            return self.send(400, {"error": "invalid_grant", "error_description": "Client or redirect mismatch."})
        
        digest = hashlib.sha256(q.get("code_verifier", "").encode()).digest()

        if not secrets.compare_digest(base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), grant["challenge"]):
            return self.send(400, {"error": "invalid_grant", "error_description": "PKCE check failed."})
        
        if q.get("resource", grant["resource"]) != grant["resource"]:
            return self.send(400, {"error": "invalid_target"})
        
        now = int(time.time())

        claims = {"iss": ISSUER, "sub": grant["user"], "aud": grant["resource"], "scope": grant["scope"],
                  "client_id": grant["client_id"], "iat": now, "exp": now + 900, "jti": secrets.token_hex(8)}
        
        access_token = jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KEY_ID})

        self.send(200, {"access_token": access_token, "token_type": "Bearer", "expires_in": 900, "scope": grant["scope"]})        

if __name__ == "__main__":
    print(f"Authorization server on {ISSUER}  (dev only, Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", 9000), Handler).serve_forever()

# use of Key_ID ? 
# public exponent in generate keys
# client registration is deprecated in MCP ? is it like while connecting the server, it creates client id one time. where is client secret?
