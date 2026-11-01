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
cterm connect          # same as above - runtime SURVIVES disconnect (new default)
cterm connect --rm     # delete the runtime when this session exits (opt-in)
cterm connect --new    # allocate a fresh runtime even if one exists
cterm connect --reauth # force a fresh Google sign-in
cterm connect --no-browser # print the sign-in URL instead of opening a browser (headless hosts)
cterm connect --mount-drive  # auto-mount Google Drive at /content/drive
cterm connect --self   # inject a self-keep-alive daemon into the runtime

cterm types            # list eligible runtime variants and accelerators
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

cterm drive            # mount Google Drive at /content/drive (browser step if needed)

cterm proxy --port 1080              # [EXPERIMENTAL] HTTP+SOCKS5 proxy via Colab
cterm proxy --port 1080 --tor        # exit via the Tor network

cterm ssh                            # [EXPERIMENTAL] SSH shell via Colab tunnel
cterm ssh -N -L 8888:localhost:8888  # port-forward only, no shell

cterm --version        # show version and exit
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
- **Persistence by default:** disconnecting (including an accidental network
  drop) leaves the runtime running - your work is not destroyed. Pass `--rm`
  to opt in to auto-delete on exit, or use `cterm kill` later to release the
  runtime explicitly. The old `--keep` flag is accepted for backward
  compatibility but is now a no-op (it's the default behavior).
- **Robust disconnect:** if the network drops, the session ends cleanly.
- **Credential cache:** tokens are stored in `~/.cterm/token.json` with
  restricted permissions (mode `0600`) and refreshed automatically. Use
  `cterm logout` to clear them.
- **Headless sign-in:** cterm auto-detects headless Linux (no `DISPLAY` /
  `WAYLAND_DISPLAY`) and in that case prints the Google sign-in URL to the
  terminal instead of trying to launch a browser (which would otherwise hang
  or crash on servers with no browser installed). Pass `--no-browser` to
  force this behaviour from any machine, `--browser` to force the open
  attempt, or set `CTERM_NO_BROWSER=1` to apply it to every subcommand.

## Runtime types (GPU / TPU)

By default `cterm` allocates a CPU runtime. To request a different type, first
query what your account has available:

```bash
cterm types
```

Sample output:

```
Eligible runtime types for this account:

  VARIANT               ACCELERATOR(S)
  --------------------  --------------------
  (none)                CPU  (default, no flags needed)
  GPU                   T4, A100, L4
  TPU                   V5E1
```

Then pass `--variant` and optionally `--accelerator` when allocating. These
flags are accepted by both `cterm connect` and `cterm ssh`. Because changing
the runtime type requires a fresh allocation, `--new` is also needed when a
runtime is already running.

```bash
cterm --new --variant GPU                          # GPU, API picks the model
cterm --new --variant GPU --accelerator T4         # request a specific model
cterm --new --variant GPU --accelerator A100
cterm --new --variant TPU
cterm --new --variant TPU --accelerator V5E1

# runtimes already survive disconnect by default; add --rm if you want
# the runtime deleted when this session exits:
cterm --new --rm --variant GPU
```

The values printed by `cterm types` are the exact strings the API expects —
no guessing or translation is performed.

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
# Auto-mount when opening a session:
cterm connect --mount-drive

# Or mount on an already-running runtime:
cterm drive            # browser authorize if needed, then mounts automatically
```

`cterm drive` propagates ephemeral credentials via the Colab API
(`dfs_ephemeral`) and then runs the Drive FUSE binary directly on the VM —
the same mechanism `google.colab.drive.mount()` uses internally, without
requiring an IPython kernel.  When authorization is already cached on Google's
side no browser interaction is required; subsequent mounts within the same
session are instant.

## Self-keep-alive (`cterm --self`)

Keep a runtime alive indefinitely without any local polling process.
With `--self`, a lightweight Python daemon is injected directly into the
Colab VM when the terminal starts. It pings the keep-alive endpoint from
inside the runtime using your auth tokens, and auto-refreshes them before
they expire — so the runtime stays alive even after you close your local
terminal, without cterm running on your machine.

```bash
cterm connect --self   # connect, inject the daemon, leave the runtime up on exit
```

A single `--self` call is enough. The injector waits (up to 15 s) until the
daemon's PID file appears on the VM before returning, and it reuses an
existing daemon if one is already running - so repeating the command does
not stack duplicate pingers.

Because the new default is to leave the runtime running on disconnect,
`--self` no longer needs to be combined with any other flag. If for some
reason you want the runtime deleted when this specific session exits, pair
it with `--rm`:

```bash
cterm connect --self --rm   # inject daemon, but also delete this runtime on exit
                            # (unusual; mostly useful for test runs)
```

The daemon runs in the background on the VM:
- PID written to `/tmp/.cterm_keepalive.pid`
- Logs at `/tmp/.cterm_keepalive.log`
- Touch `/tmp/.cterm_keepalive.stop` on the runtime to stop it gracefully

To terminate the runtime from your machine:

```bash
cterm kill        # unassigns the runtime (the authoritative kill path)
cterm kill --all  # kill all runtimes
```

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

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--port PORT` | `1080` | Local TCP port to listen on. |
| `--vm-proxy-port PORT` | `8764` | Port `pproxy` listens on inside the VM. |
| `--tor` | off | Route VM traffic through the Tor network. |

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
proxy is ready — this typically takes 60-120 seconds. Subsequent runs on
the same runtime reuse the already-running Tor instance and start much faster.
The Tor binary runs entirely within the Colab VM; no traffic leaves Google
infrastructure until it reaches the Tor entry node.

## Experimental SSH (`cterm ssh`)

Opens an SSH session to the Colab VM over the same muxed WebSocket tunnel used
by `cterm proxy`. `openssh-server` is installed and configured on the VM
automatically on first use; a temporary Ed25519 keypair is generated locally
so the connection is **passwordless and requires no user interaction**.

```bash
cterm ssh                            # interactive shell
cterm ssh -N -L 8888:localhost:8888  # Jupyter port-forward, no shell
cterm ssh -N -R 9000:localhost:9000  # reverse tunnel back to your machine
cterm ssh -v                         # verbose SSH debug output

# allocate a GPU runtime and forward a Gradio port
cterm --new --keep --variant GPU ssh -N -L 7860:localhost:7860
```

Any arguments after the known `cterm` flags are forwarded verbatim to the
local `ssh` binary, inserted before the host (`root@127.0.0.1`). The
mandatory auth flags (`-i <keyfile>`, `-o IdentitiesOnly=yes`, etc.) are
always prepended, so standard SSH options and forwarding rules work as-is.

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--port PORT` | `2222` | Local TCP port for the SSH tunnel. |
| `--new` | off | Allocate a fresh runtime even if one already exists. |
| `--keep` | off | Do not delete the runtime when the session ends. |
| `--variant` / `--accelerator` | CPU | Same as `cterm connect`. |

**Caveats:** experimental; the base64-framed tunnel adds latency compared with
direct SSH; throughput is limited. Suitable for interactive use and
lightweight port-forwarding; not recommended for bulk data transfer.

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
