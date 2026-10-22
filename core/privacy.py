"""Remove configured credentials and common credential fields from worker logs."""
import os
import re
from urllib.parse import quote, quote_plus

_SECRET_NAMES = (
    'RULE34_API_KEY', 'GELBOORU_API_KEY', 'KONACHAN_PASSWORD', 'SANKA_PASSWORD',
    'ZEROCHAN_PASSWORD', 'PINTEREST_PASSWORD', 'PIXIV_REFRESH_TOKEN', 'PIXIV_COOKIE',
    'DANBOORU_API_KEY', 'GSBOORU_API_KEY', 'REMS_ACCESS_TOKEN',
)


def redact(message):
    text = str(message)
    for name in _SECRET_NAMES:
        value = os.getenv(name, '')
        if len(value) >= 4:
            for variant in {value, quote(value, safe=''), quote_plus(value)}:
                text = text.replace(variant, '[redacted]')
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', text)
    text = re.sub(r'(?i)([?&](?:api_key|password|password_hash|access_token|refresh_token|token)=)[^&\s]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[redacted]', text)
    return text
