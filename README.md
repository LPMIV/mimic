# mimic

Intercept any app, then call it from Python like a library.

```python
from hinge_client import Hinge

acc = Hinge()                 # reuses your captured session
recs = acc.get_recommendations()
acc.like(subject_id, comment="hi lol")
```

You don't write `hinge_client.py`. mimic captures your own app traffic and an AI
generates the client from it.

## How it works

Most apps authenticate every request with the same bundle of values: a bearer
token, some device ids, a session id, cookies. They're stable across calls.
Capture them once from a real request you made, and you can replay them on new
requests to the same API.

```
capture traffic   ->   extract auth   ->   generate client
  (mitmproxy)         (mimic.Session)      (an AI reads the
                                            captured endpoints)
```

The generated client is plain Python on top of `mimic.App`, and you edit it like
any other file. It gives you named methods, body templates, and the multi-step
call chaining mobile APIs tend to need (fetch a token in one call, spend it in
the next).

## Install

```bash
sh install.sh
```

Installs [`uv`](https://astral.sh/uv) if you don't have it, then mimic in an
isolated tool env. mitmproxy isn't a separate install; mimic launches it via
`uvx` on first `record`. (Manual: `uv tool install mimic-client`.)

```bash
mimic doctor                    # confirm proxy + an AI CLI are available
```

## Use it (iPhone)

```bash
mimic record                    # starts the proxy, prints the iPhone steps
```

`record` fills in your Mac's LAN IP and walks you through it:

1. iPhone -> Wi-Fi -> Configure Proxy -> Manual -> `<your-mac-ip>:8080`
2. Safari -> `http://mitm.it` -> install the Apple profile
3. Settings -> General -> About -> Certificate Trust Settings -> turn on full
   trust for mitmproxy. This step is easy to miss and nothing works without it.
4. open the app, use it normally

Then:

```bash
mimic hosts                     # list captured hosts; pick your API host
mimic learn  prod-api.hingeaws.net    # see the endpoints mimic saw
mimic gen    prod-api.hingeaws.net    # generate hinge_client.py
```

Then `from hinge_client import Hinge; Hinge().get_recommendations()`.

## Generate with Codex (no API key)

Claude remains the default; `--generator opencode` is also available. To use
[Codex CLI](https://developers.openai.com/codex/cli), install a recent version
and sign in with your ChatGPT account using `codex login`. mimic reuses that
login and does not ask for an API key or read/copy your credentials. Account
eligibility and usage limits still apply.

Codex generation requires a **local review step before uploading any prompt**:

```bash
# 1. Prepare a local draft. No AI CLI/model is called.
umask 077
mimic gen api.example.com --generator codex --har traffic.har --prompt-only > prompt.txt

# 2. Open prompt.txt in your editor and review/redact the ENTIRE file.
#    For live mitmweb capture, omit --har traffic.har in step 1.

# 3. Explicitly submit only the file you reviewed. No capture is read again.
mimic gen api.example.com --generator codex --reviewed-prompt prompt.txt -o example_client.py
```

`--reviewed-prompt` is your instruction to send that entire file to the selected
generator. It is not proof that the file is safe. Keep review files and captures
private, outside version control. Review paths, query values, request/response
bodies and free text for secrets, identifiers, messages and other personal data.
Replace real values with synthetic examples or parameters and remove irrelevant
endpoints. Do not upload live credentials or data you do not have permission to share.

The Codex draft omits HTTP headers, redacts a limited list of common credential
and personal-data keys in JSON/query strings, and omits non-JSON or truncated
bodies. **This is not comprehensive secret detection.** Unknown fields, URL
paths and free-text values can still contain private data. Original captures
and session authentication are unchanged. Plain `--prompt-only` with the other
generators keeps the existing, unredacted behavior; review that output too.

Codex runs non-interactively with an empty temporary working directory, a
read-only sandbox, no approval prompts, no session persistence, user config
loading disabled, and shell/apps/plugins/hooks/multi-agent/memory/image tools
disabled. Web search is disabled. These settings reduce incidental context and
tool access; they are **not a complete isolation guarantee** against global
skills or administrator configuration. Use a dedicated OS account if you need
a stronger boundary. Normal Codex authentication is still used. This requires a
CLI that supports `codex exec --ignore-user-config`; mimic fails without sending
the prompt if that flag is missing. Update Codex if your version is too old.

An optional `--model MODEL` selects a Codex model; otherwise Codex's built-in
default is used (user model/config overrides are intentionally not loaded).
mimic sends the prompt over stdin, reads the final answer separately from CLI
logs, and checks Python syntax without executing it. Review generated code
before importing or running it: valid syntax does not make it safe or correct.
Generation alone never calls the captured API or runs the resulting client.

## The library

Three ways to build a session by hand, if you don't want codegen:

```python
from mimic import Session

Session.from_mitm("prod-api.hingeaws.net")        # pull auth from mitmweb
Session.from_curl(open("copied.txt").read())      # paste "Copy as cURL" from devtools
Session(base_url="https://x.com", headers={...})  # explicit
```

`.get(path)`, `.post(path, json=...)`, and the other common HTTP verb helpers
return parsed JSON and raise `requests.HTTPError` for failed responses. If your
token rotates, a `401` on an idempotent request triggers one re-pull from
mitmweb and a retry. Non-idempotent requests are not retried unless you explicitly
pass `refresh=True`.

## Capture backends

- **mitmproxy** for iOS apps (the default). mimic reads its JSON flow API and
  runs it via `uvx`, so there's nothing extra to install.
- **cURL / paste** for anything with a web version. `Copy as cURL` in devtools,
  then `Session.from_curl(text)`. No proxy, no cert.
- **HAR file** for web apps and anything you can capture in a browser. In Chrome
  or Firefox devtools, open the Network tab, right-click a request, and choose
  "Save all as HAR". Then `mimic hosts --har traffic.har` and
  `mimic gen api.example.com --har traffic.har`, or build a session directly with
  `Session.from_har("traffic.har", "api.example.com")`. No proxy, no cert.

## Limitations

Two auth schemes get in the way, for different reasons:

- **Certificate pinning** (banking, Instagram). The app rejects the mitmproxy
  cert, so the proxy sees no traffic and nothing shows up in `mimic hosts`. This
  blocks *capture*, not replay — get past the pin and the rest works normally.
  `mimic unpin <ipa|bundle-id>` sets up a Frida-based bypass; see
  [docs/pinning.md](docs/pinning.md).
- **DPoP / sender-constrained tokens.** Each request carries a fresh proof
  signed by a private key that never leaves the device, so captured requests
  don't replay. This defeats the core model, not just capture; there's no clean
  workaround. See [docs/dpop.md](docs/dpop.md).

If `mimic hosts` shows the app's API host, you're good.

## Ethics

Use it on your own accounts and data. It replays your session; it is not a tool
for accessing anyone else's. Respect each app's terms of service.

## License

MIT, see [LICENSE](LICENSE). Provided as-is, no warranty. Use on your own
accounts and data; you are responsible for complying with each app's terms.

## Development

```bash
python -m pip install -e . pytest
python -m pytest -q
```

Default codegen tests use synthetic data and a fake CLI process. They do not
upload traffic, authenticate to a provider, or execute generated clients.

To opt into a real signed-in Codex smoke test (consumes your account usage):

```bash
codex login status
MIMIC_LIVE_CODEX=1 python -m pytest tests/test_codegen_live.py -q
```

This test generates a local synthetic HAR, checks redaction, edits the prompt,
and submits only its reviewed synthetic contents to Codex. It validates the
returned client without importing or running it, and never calls the example
API. The Codex runtime must be writable: a startup error mentioning a read-only
filesystem is a storage problem, even when `codex login status` succeeds.
