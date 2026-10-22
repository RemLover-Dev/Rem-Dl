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
    const r = await fetch('/auth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:value})});
    if (r.ok) location.replace('/');
    else showForm('Invalid access token.');
  } catch (_) { showForm('Unable to connect. Try again.'); }
}
document.querySelector('form').onsubmit = e => {e.preventDefault(); login(document.querySelector('input').value);};
if (token) login(token);
else showForm('');
</script>'''


class AccessGuard:
    def __init__(self, application, token=None, hosts=None):
        self.application = application
        self.token = token or os.environ.get('REMS_ACCESS_TOKEN') or secrets.token_urlsafe(32)
        if len(self.token) < 32:
            raise ValueError('REMS_ACCESS_TOKEN must contain at least 32 characters')
        self.cookie_value = secrets.token_urlsafe(32)
        # A different name per process/port avoids collisions between app instances.
        self.cookie_name = 'rems_' + secrets.token_hex(8)
        self.hosts = set(hosts or ('localhost', '127.0.0.1', '::1'))
        self.hosts.update(h.strip().lower() for h in os.environ.get('REMS_ALLOWED_HOSTS', '').split(',') if h.strip())

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
            if not host or host.lower() not in self.hosts:
                return respond(Response('Untrusted host', status=403))
            origin = req.headers.get('Origin')
            if origin and origin != req.host_url.rstrip('/'):
                return respond(Response('Cross-origin requests are forbidden', status=403))
            if req.headers.get('Sec-Fetch-Site') == 'cross-site':
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
            if not self._valid(candidate, self.token):
                return respond(Response('Unauthorized', status=401))
            response = Response(status=204)
            response.set_cookie(self.cookie_name, self.cookie_value, httponly=True,
                                samesite='Strict', secure=req.scheme == 'https', path='/')
            return respond(response)

        authenticated = self._valid(req.cookies.get(self.cookie_name), self.cookie_value)
        authorization = req.headers.get('Authorization', '')
        if authorization.startswith('Bearer '):
            authenticated = authenticated or self._valid(authorization[7:], self.token)
        if not authenticated:
            if req.path == '/' and req.method == 'GET':
                return respond(Response(LOGIN_PAGE, content_type='text/html; charset=utf-8'))
            return respond(Response('Unauthorized', status=401))
        return self.application(environ, secure_start)
