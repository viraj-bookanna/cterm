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
colab-shell                 # connect to a runtime terminal (default)
colab-shell connect         # same as above
colab-shell connect --keep  # don't delete the runtime when you exit
colab-shell connect --reauth# force a fresh Google sign-in

colab-shell list            # list your active Colab runtimes
colab-shell kill <id>       # delete a specific runtime (id or unique prefix)
colab-shell kill --all      # delete all your runtimes
colab-shell logout          # clear cached credentials
```

Press **Ctrl+]** to disconnect from the remote terminal.

### Behaviour

- **Keep-alive:** while you're in the shell, the runtime is pinged periodically
  so it won't idle out from inactivity.
- **Auto-delete on exit:** by default the runtime is released when you exit, so
  you don't keep burning compute hours. Pass `--keep` to leave it running.
- **Credential cache:** tokens are cached in `~/.colab-shell/token.json` and
  refreshed automatically. Use `colab-shell logout` to clear them.

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
