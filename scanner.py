import urllib.request
import urllib.parse
import json
import os

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "1247283950")

url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
data = urllib.parse.urlencode({"chat_id": CHAT, "text": "MSNR Scanner test message"}).encode()

try:
    with urllib.request.urlopen(url, data=data, timeout=10) as r:
        res = json.loads(r.read())
    print("Result:", res)
except Exception as e:
    print("Error:", e)
