"""Access control outside Flask, so Engine.IO receives the same protection."""
import hmac
import os
import secrets
from urllib.parse import quote, urlsplit

from werkzeug.wrappers import Request, Response

LOGIN_PAGE = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Rems Dl — Sign in</title>
<style>body{background:#17191f;color:#eee;font:16px system-ui;max-width:420px;margin:12vh auto;padding:24px}input,button{font:inherit;padding:12px;box-sizing:border-box;width:100%;margin:8px 0}button{cursor:pointer}p{line-height:1.5}</style>
<h1>Rems Dl</h1><p id="loginHint" hidden>Enter the access token shown by the app, or configured in REMS_ACCESS_TOKEN.</p>
<form hidden><input type="password" aria-label="Access token" autocomplete="off" required><button>Sign in</button></form><p id="status" role="status">Opening Rems Dl…</p>
<script>
const BASE = "__REMS_BASE__";
const token = new URLSearchParams(location.hash.slice(1)).get('rems-token');
if (token) history.replaceState(null, '', location.pathname);
function showForm(message) {
  document.querySelector('form').hidden = false;
  document.getElementById('loginHint').hidden = false;
  document.getElementById('status').textContent = message;
  document.querySelector('input').focus();
}
async function login(value) {
  try {
    const r = await fetch(BASE + '/auth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:value})});
    if (r.ok) location.replace(BASE + '/');
    else if (r.status === 401) showForm('Invalid access token.');
    else if (r.status === 403) showForm('Request blocked (403) — check REMS_ALLOWED_HOSTS and the reverse-proxy Host header.');
    else if (r.status === 404) showForm('Sign-in endpoint not found (404) — check the reverse-proxy path configuration.');
    else if (r.status >= 500) showForm('Server error (' + r.status + '). Check the app logs.');
    else showForm('Request failed (HTTP ' + r.status + ').');
  } catch (_) { showForm('Unable to connect. Try again.'); }
}
document.querySelector('form').onsubmit = e => {e.preventDefault(); login(document.querySelector('input').value);};
if (token) login(token);
else showForm('');
</script>'''


def normalize_base_path(value):
    """REMS_BASE_PATH → '' or '/remdl' (leading slash, no trailing slash)."""
    p = (value or '').strip().replace('\\', '/')
    if not p or p == '/':
        return ''
    if not p.startswith('/'):
        p = '/' + p
    return p.rstrip('/') or ''


class AccessGuard:
    def __init__(self, application, token=None, hosts=None):
        self.application = application
        configured_token = token or os.environ.get('REMS_ACCESS_TOKEN')
        self.token_generated = not bool(configured_token)
        self.token = configured_token or secrets.token_urlsafe(32)
        if len(self.token) < 32:
            raise ValueError('REMS_ACCESS_TOKEN must contain at least 32 characters')
        self.cookie_value = secrets.token_urlsafe(32)
        # failed /auth attempts per client IP: backoff makes LAN brute-force
        # slow without any lockout that a shared NAT could trigger
        self._auth_fails = {}
        # A different name per process/port avoids collisions between app instances.
        self.cookie_name = 'rems_' + secrets.token_hex(8)
        self.hosts = set(hosts or ('localhost', '127.0.0.1', '::1'))
        self.hosts.update(h.strip().lower() for h in os.environ.get('REMS_ALLOWED_HOSTS', '').split(',') if h.strip())
        # Hostname-only forms of every entry, so a config value matches a Host
        # header with or without its port (and vice versa).
        self._host_names = set()
        for h in self.hosts:
            self._host_names.add(h)
            hn = self._hostname(h)
            if hn:
                self._host_names.add(hn)
        self.base_path = normalize_base_path(os.environ.get('REMS_BASE_PATH'))

    @staticmethod
    def _hostname(value):
        try:
            return urlsplit('//' + str(value)).hostname
        except ValueError:
            return None

    def _host_allowed(self, host):
        """Match with or without a port: example.com ≍ example.com:8443."""
        if not host:
            return False
        host = str(host).lower()
        if host in self._host_names:
            return True
        hn = self._hostname(host)
        return bool(hn) and hn.lower() in self._host_names

    def launch_url(self, url):
        return url + '#rems-token=' + quote(self.token, safe='')

    def _valid(self, value, expected):
        return bool(value) and hmac.compare_digest(str(value).encode(), expected.encode())

    def __call__(self, environ, start_response):
        req = Request(environ)

        def respond(response):
            return response(environ, secure_start)

        def secure_start(status, headers, exc_info=None):
            headers += [('X-Content-Type-Options', 'nosniff'),
                        ('X-Frame-Options', 'DENY'), ('Referrer-Policy', 'no-referrer'),
                        ('Content-Security-Policy', "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"),
                        ('Cross-Origin-Resource-Policy', 'same-origin')]
            if req.path == '/' or req.path == '/auth' or req.path.startswith('/api/'):
                # Keep media caches fast, but never cache credentials or API state.
                if not req.path.startswith(('/api/gallery/file/', '/api/gallery/thumb/', '/api/gallery/display/', '/api/gallery/preview/')):
                    headers = [(k, v) for k, v in headers if k.lower() != 'cache-control']
                    headers.append(('Cache-Control', 'no-store'))
            return start_response(status, headers, exc_info)

        try:
            host = urlsplit('//' + req.host).hostname
            if not host or not self._host_allowed(host):
                print(f"[AccessGuard] blocked request: Host header {req.host!r} is not in the allowed list "
                      f"(REMS_ALLOWED_HOSTS={','.join(sorted(self.hosts)) or 'defaults only'})")
                return respond(Response('Untrusted host', status=403))
            origin = req.headers.get('Origin')
            if origin:
                # Behind a TLS-terminating proxy the app sees http while the
                # browser origin is https — same site, not cross-site. Compare
                # host only; the host allowlist above already gates trust.
                o = urlsplit(origin)
                origin_netloc = (o.netloc or '').lower()
                origin_host = (o.hostname or '').lower()
                if origin_netloc != req.host.lower() and not self._host_allowed(origin_host):
                    print(f"[AccessGuard] blocked request: Origin header {origin!r} does not match "
                          f"Host {req.host!r} or the allowed list")
                    return respond(Response('Cross-origin requests are forbidden', status=403))
            if req.headers.get('Sec-Fetch-Site') == 'cross-site':
                print("[AccessGuard] blocked request: Sec-Fetch-Site: cross-site "
                      f"(Origin={req.headers.get('Origin')!r}, Referer={req.headers.get('Referer')!r})")
                return respond(Response('Cross-site requests are forbidden', status=403))
        except ValueError:
            return respond(Response('Invalid host', status=400))

        if req.path == '/auth' and req.method == 'POST':
            if (req.content_length or 0) > 4096:
                return respond(Response('Request too large', status=413))
            # Bound reads even if a non-browser client omits Content-Length.
            import json
            try:
                data = json.loads(req.stream.read(4097))
                candidate = data.get('token') if isinstance(data, dict) else None
            except (ValueError, TypeError):
                candidate = None
            # brute-force backoff: each consecutive miss from one IP delays
            # its next check up to 30s (5 misses are free, then it climbs)
            import time as _time
            client_ip = (req.remote_addr or 'unknown')[:64]
            fails = self._auth_fails.get(client_ip, 0)
            # cap: a longer sleep pins a threading-mode thread longer per hit
            if fails >= 5:
                _time.sleep(min(2 ** (fails - 5), 10))
            if len(self._auth_fails) > 1000:
                # trim oldest, never bulk-clear: a reset lets an attacker
                # clear their own backoff by spamming past the cap
                for k in list(self._auth_fails)[:len(self._auth_fails) - 500]:
                    self._auth_fails.pop(k, None)
            if not self._valid(candidate, self.token):
                self._auth_fails[client_ip] = fails + 1
                return respond(Response('Unauthorized', status=401))
            self._auth_fails.pop(client_ip, None)
            response = Response(status=204)
            # req.scheme is http behind a TLS-terminating proxy — the browser
            # Origin tells us whether the cookie will actually travel over https.
            secure = req.scheme == 'https' or (req.headers.get('Origin') or '').startswith('https://')
            response.set_cookie(self.cookie_name, self.cookie_value, httponly=True,
                                samesite='Strict', secure=secure,
                                path=self.base_path or '/')
            return respond(response)

        authenticated = self._valid(req.cookies.get(self.cookie_name), self.cookie_value)
        authorization = req.headers.get('Authorization', '')
        if authorization.startswith('Bearer '):
            authenticated = authenticated or self._valid(authorization[7:], self.token)
        if not authenticated:
            # The app is a local desktop tool: anything connecting from this
            # machine (127.0.0.1/::1, socket-verified, not spoofable) skips the
            # token so the user never signs in. Remote clients still need it.
            # Host/Origin/cross-site checks above still apply to loopback.
            if (req.remote_addr or '') in ('127.0.0.1', '::1'):
                authenticated = True
        if not authenticated:
            if req.path == '/' and req.method == 'GET':
                page = LOGIN_PAGE.replace('__REMS_BASE__', self.base_path)
                return respond(Response(page, content_type='text/html; charset=utf-8'))
            return respond(Response('Unauthorized', status=401))
        return self.application(environ, secure_start)
