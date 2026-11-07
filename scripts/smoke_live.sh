#!/usr/bin/env bash
# Live smoke test on Vultr Serverless Inference: one call to each allowed model, and the answer's "model"
# field must be the model asked for. Costs a fraction of a cent. Run only on purpose, after the coupon:
#   infisical run -- scripts/smoke_live.sh   (the key comes from your secret store)
# The key is read from VULTR_INFERENCE_API_KEY in the environment and never printed or written.
set -euo pipefail

if [ -z "${VULTR_INFERENCE_API_KEY:-}" ]; then
    echo "VULTR_INFERENCE_API_KEY is not set; nothing was called" >&2
    exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export SMOKE_URL="${COACHCOVER_INFERENCE_URL:-https://api.vultrinference.com/v1}"
export SMOKE_ROOT="$ROOT"

python3 -I - <<'PY'
import base64, json, os, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

url, key, root = os.environ["SMOKE_URL"], os.environ["VULTR_INFERENCE_API_KEY"], Path(os.environ["SMOKE_ROOT"])
card = base64.b64encode((root / "tests/fixtures/cards/c2-scan.png").read_bytes()).decode()
image = [{"type": "text", "text": "Return JSON with the keys name and exp for this certificate."},
         {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{card}"}}]


def post(path, body, headers):
    req = urllib.request.Request(url + path, data=body, headers={"Authorization": f"Bearer {key}", **headers}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode()), int((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode(errors="replace")[:200]}, int((time.monotonic() - started) * 1000)


def chat(model, content):
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": 200,
                       "messages": [{"role": "user", "content": content}]}).encode()
    return post("/chat/completions", body, {"Content-Type": "application/json"})


def speech():
    boundary = uuid.uuid4().hex
    audio = (root / "tests/fixtures/audio/en-callout.ogg").read_bytes()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-large-v3-turbo\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\nverbose_json\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="en-callout.ogg"\r\n'
            f'Content-Type: audio/ogg\r\n\r\n').encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    return post("/audio/transcriptions", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})


checks = [
    ("whisper-large-v3-turbo", speech),
    ("muse-glimmer-30b", lambda: chat("muse-glimmer-30b", "Answer with the single word: ready")),
    ("nemotron-3-nano-omni-30b-a3b-reasoning", lambda: chat("nemotron-3-nano-omni-30b-a3b-reasoning", image)),
    ("raon-1-mini-preview", lambda: chat("raon-1-mini-preview", "Answer with the single word: ready")),
    ("nemotron-3.5-content-safety", lambda: chat("nemotron-3.5-content-safety", "Can someone swap my Saturday lesson?")),
]
failed = 0
for model, call in checks:
    status, body, ms = call()
    answered = body.get("model")
    ok = status == 200 and answered == model
    failed += not ok
    note = "" if ok else (f" error={body.get('error')}" if status != 200 else f" answered by {answered!r}")
    print(f"{'OK  ' if ok else 'FAIL'} {model:42} http={status} ms={ms} model={answered!r} usage={body.get('usage')}{note}")
print(f"{len(checks) - failed} of {len(checks)} models answered as themselves")
sys.exit(1 if failed else 0)
PY
