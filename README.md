# cterm

Drop into a Google Colab runtime's terminal straight from your local machine.

`cterm` authenticates with Google using the **same OAuth2 flow as the
official Colab VS Code extension**, allocates (or reuses) a Colab runtime, and
bridges your local terminal to the runtime's `/colab/tty` WebSocket — the
exact mechanism the extension's "Open Terminal" command uses.

## Install

```bash
pip install .
# or, for development:
pip install -e .
```

This installs a `cterm` command. You can also run it as a module:

```bash
python -m colab_shell
```

## Usage

```bash
cterm                  # connect to a runtime terminal (default)
cterm connect          # same as above
cterm connect --keep   # don't delete the runtime when you exit
cterm connect --new    # allocate a fresh runtime even if one exists
cterm connect --reauth # force a fresh Google sign-in
cterm connect --mount-drive  # auto-mount Google Drive at /content/drive

cterm list             # list your active Colab runtimes
cterm kill <id>        # delete a specific runtime (id or unique prefix)
cterm kill --all       # delete all your runtimes
cterm logout           # clear cached credentials

cterm stats            # one-shot RAM / disk / GPU usage with sparklines
cterm stats --watch    # live refresh (Ctrl+C to stop)
cterm stats --watch --interval 5  # poll every 5 s

cterm push ./file.txt            # upload to /content/file.txt
cterm push ./file.txt /tmp/x.txt # upload to a specific path
cterm push ./my_dir              # upload a directory recursively

cterm pull /content/output.csv        # download to ./output.csv
cterm pull /content/results ./local   # download a directory

cterm drive            # propagate Google Drive credentials (browser step if needed)

cterm proxy --port 1080              # [EXPERIMENTAL] HTTP+SOCKS5 proxy via Colab
cterm proxy --port 1080 --tor        # exit via the Tor network
```

### Key bindings (inside the terminal session)

| Key | Effect |
|-----|--------|
| **Ctrl+C** | Interrupt the running command on the remote shell (SIGINT). Session stays open. |
| **Arrow keys** | Navigate command history (Up/Down) and move within the current line (Left/Right). |
| **Home / End** | Jump to the start or end of the current line. |
| **PageUp / PageDown** | Scroll through shell history. |

### Behaviour

- **Full interactive terminal:** arrow-key navigation, command history, and
  line editing work on both Windows and Unix. Terminal resizes propagate to
  the remote PTY automatically.
- **Clean display:** the tmux status bar is hidden on connect so the session
  looks and feels like a plain SSH terminal.
- **Keep-alive:** the runtime is pinged periodically so it won't idle out.
- **Auto-delete on exit:** the runtime is released on exit by default. Pass
  `--keep` to leave it running.
- **Robust disconnect:** if the network drops, the session ends cleanly.
- **Credential cache:** tokens are stored in `~/.cterm/token.json` with
  restricted permissions (mode `0600`) and refreshed automatically. Use
  `cterm logout` to clear them.

## Resource stats (`cterm stats`)

Fetches RAM, disk, and GPU metrics from the runtime every 2 seconds (or at
a custom `--interval`) and renders them as live unicode sparklines:

```
  Resource usage  (14:23:01)
  RAM             12.4 GB / 16.0 GB    77.5%  ▁▂▃▅▆▇█▇▆▅
  /content        4.8 GB  / 107.7 GB    4.5%  ▁▁▁▁▁▁▁▁▁▁
  T4 util         ────────────────────  38.0%  ▁▂▄▆▇█▇▅▃▂
  T4 vmem         6.1 GB  / 15.8 GB    38.6%  ▁▂▄▅▆▇█▇▅▄
```

Note: CPU utilisation is **not** exposed by the Colab API. RAM, disk, and GPU
(compute + memory) are the available metrics.

## File transfer (`cterm push` / `cterm pull`)

Uses the standard Jupyter contents API on the runtime. Binary files are
transferred base64-encoded and round-trip correctly. Directories are
transferred recursively.

```bash
cterm push ./model.pt            # -> /content/model.pt
cterm push ./dataset /content/ds # -> /content/ds/ (recursive)
cterm pull /content/results .    # -> ./results/ (recursive)
```

## Google Drive (`cterm drive` / `--mount-drive`)

```bash
# Propagate credentials, then mount inside the shell:
cterm connect --mount-drive

# Or in two steps:
cterm drive            # browser authorize if needed (one-time)
cterm connect          # then mount manually inside the shell
```

The `dfs_ephemeral` credential propagation flow (reversed from the extension)
is used. When authorization is already cached on Google's side no browser
interaction is required.

## Experimental proxy (`cterm proxy`)

Routes your local HTTP and SOCKS5 traffic out through the Colab VM:

```bash
cterm proxy --port 1080
# then in another terminal:
curl --socks5 127.0.0.1:1080 https://ifconfig.me   # shows a Google/Colab IP
curl -x http://127.0.0.1:1080 https://ifconfig.me  # HTTP proxy also works
```

Traffic is carried entirely within Google infrastructure: the local listener
multiplexes connections over Colab's own Jupyter terminal WebSocket
(`/terminals/websocket/{name}`), with a small base64-framed protocol, and
`pproxy` on the VM handles the final outbound request.

**Caveats:** experimental; base64 framing over a PTY adds overhead so
throughput is limited and large downloads may be slow or unreliable; session
dies if the runtime restarts; `pproxy` is installed on the VM automatically.
Do not use for sensitive traffic.

### Tor mode (`--tor`)

Add `--tor` to route all VM-side traffic through the Tor network:

```bash
cterm proxy --port 1080 --tor
# then in another terminal:
curl --socks5 127.0.0.1:1080 https://check.torproject.org/api/ip   # "IsTor": true
curl -x http://127.0.0.1:1080 https://ifconfig.me                  # shows a Tor exit-node IP
```

On the first run, Tor is installed via `apt` and must bootstrap before the
proxy is ready — this typically takes 60–120 seconds. Subsequent runs on
the same runtime reuse the already-running Tor instance and start much faster.
The Tor binary runs entirely within the Colab VM; no traffic leaves Google
infrastructure until it reaches the Tor entry node.

## How it works

1. OAuth2 loopback sign-in (`127.0.0.1`, PKCE, offline access) with scopes
   `email`, `profile`, and `https://www.googleapis.com/auth/colaboratory`.
2. `GET /v1/assignments` to find an existing runtime, or `GET`+`POST
   /tun/m/assign` to allocate one.
3. `GET /v1/runtime-proxy-token` to obtain the per-runtime proxy `token` + `url`.
4. WebSocket connect to `wss://{proxy-url-host}/colab/tty` with the
   `X-Colab-Runtime-Proxy-Token` header, then bridge stdin/stdout.
5. New commands (`stats`, `push`, `pull`, `drive`) use the same proxy token
   to reach the runtime's Jupyter APIs (`/api/colab/resources`,
   `/api/contents/{path}`) and the Colab credential-propagation endpoint.

## Note

This tool reuses the Colab VS Code extension's public OAuth client ID for
personal use. It is unofficial and not affiliated with Google.
