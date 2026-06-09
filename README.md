# colab-shell

Drop into a Google Colab runtime's terminal straight from your local machine.

`colab-shell` authenticates with Google using the **same OAuth2 flow as the
official Colab VS Code extension**, allocates (or reuses) a Colab runtime, and
bridges your local terminal to the runtime's `/colab/tty` WebSocket - the exact
mechanism the extension's "Open Terminal" command uses.

## Install

```bash
pip install .
# or, for development:
pip install -e .
```

This installs a `colab-shell` command. You can also run it as a module:

```bash
python -m colab_shell
```

## Usage

```bash
colab-shell                  # connect to a runtime terminal (default)
colab-shell connect          # same as above
colab-shell connect --keep   # don't delete the runtime when you exit
colab-shell connect --new    # allocate a fresh runtime even if one exists
colab-shell connect --reauth # force a fresh Google sign-in

colab-shell list             # list your active Colab runtimes
colab-shell kill <id>        # delete a specific runtime (id or unique prefix)
colab-shell kill --all       # delete all your runtimes
colab-shell logout           # clear cached credentials
```

### Key bindings

| Key | Effect |
|-----|--------|
| **Ctrl+C** | Interrupt the running command on the remote shell (SIGINT). The session stays open. |
| **Ctrl+]** | Disconnect from the remote terminal and return to your local shell. |
| **Arrow keys** | Navigate command history (Up/Down) and move within the current line (Left/Right). |
| **Home / End** | Jump to the start or end of the current line. |
| **PageUp / PageDown** | Scroll through shell history. |

### Behaviour

- **Full interactive terminal:** arrow-key navigation, command history, and
  line editing all work on both Windows and Unix. Terminal resizes are
  propagated to the remote PTY automatically.
- **Keep-alive:** while you're in the shell, the runtime is pinged
  periodically so it won't idle out from inactivity.
- **Auto-delete on exit:** by default the runtime is released when you exit,
  so you don't keep burning compute hours. Pass `--keep` to leave it running.
- **Robust disconnect:** if the network connection drops, the session ends
  cleanly and returns you to your local prompt without a traceback.
- **Credential cache:** tokens are stored in `~/.colab-shell/token.json` with
  restricted permissions (mode `0600`) and refreshed automatically. Use
  `colab-shell logout` to clear them.

## How it works

1. OAuth2 loopback sign-in (`127.0.0.1`, PKCE, offline access) with scopes
   `email`, `profile`, and `https://www.googleapis.com/auth/colaboratory`.
2. `GET /v1/assignments` to find an existing runtime, or `GET`+`POST
   /tun/m/assign` to allocate one.
3. `GET /v1/runtime-proxy-token` to obtain the per-runtime proxy `token` + `url`.
4. WebSocket connect to `wss://{proxy-url-host}/colab/tty` with the
   `X-Colab-Runtime-Proxy-Token` header, then bridge stdin/stdout.

## Note

This tool reuses the Colab VS Code extension's public OAuth client ID for
personal use. It is unofficial and not affiliated with Google.
