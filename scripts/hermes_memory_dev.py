#!/usr/bin/env python3
"""Run a pinned Hermes CLI with a separate, encrypted development memory stack."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--memoryd", default="/usr/libexec/bianbu-agent-memoryd")
    parser.add_argument("--sdk-path", default=os.environ.get("BIANBU_MEMORY_SDK_PATH", "/usr/lib/python3/dist-packages"), type=Path)
    parser.add_argument("--model-provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--model-base-url", default="")
    parser.add_argument("hermes_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    expected = subprocess.check_output(["git", "-C", str(source), "rev-parse", "v0.21.7^{commit}"], text=True).strip()
    if actual != expected:
        raise RuntimeError("Hermes source must be at v0.21.7")
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("This Hermes development profile is already running.")
        home = state / "hermes"
        plugin = home / "plugins" / "bianbu"
        plugin.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(__file__).resolve().parents[1] / "adapters/hermes/__init__.py", plugin / "__init__.py")
        config = home / "config.yaml"
        if not config.exists():
            lines = ["memory:", "  provider: bianbu"]
            if args.model_provider:
                lines += ["model:", f"  default: {json.dumps(args.model)}",
                          f"  provider: {json.dumps(args.model_provider)}",
                          f"  base_url: {json.dumps(args.model_base_url)}"]
            config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        secret = state / "account-secret"
        if not secret.exists():
            # Exclusive creation; the file is private from the first byte.
            with os.fdopen(os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
                output.write(secrets.token_hex(32) + "\n")
        socket = state / "memoryd.sock"
        env = dict(os.environ, HERMES_HOME=str(home), BIANBU_AGENT_MEMORY_SOCKET=str(socket))
        env['PYTHONPATH'] = str(args.sdk_path) + os.pathsep + env.get('PYTHONPATH', '')
        # Integration stack uses lexical recall; don't touch gateway/NPU instances.
        daemon_env = dict(env, AGENT_MEMORY_EMBEDDING_DISABLED="1")
        with (state / "memoryd.log").open("a") as log:
            daemon = subprocess.Popen([
                args.memoryd, "--development-identity", "--development-agent-name", "hermes-v0217",
                "--socket", str(socket), "--database", str(state / "memory.db"),
                "--key-package", str(state / "memory.keypkg"),
                "--account-secret-file", str(secret), "--initialize-key-package",
            ], env=daemon_env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 15
                while not socket.is_socket():
                    if daemon.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(f"Development memoryd failed; inspect {state / 'memoryd.log'}")
                    time.sleep(0.05)
                forwarded = args.hermes_args
                if forwarded[:1] == ["--"]:
                    forwarded = forwarded[1:]
                bootstrap = (
                    "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                    "sys.path.insert(0, sys.argv.pop(1)); "
                    "from hermes_cli.main import main; sys.exit(main())"
                )
                return subprocess.call([
                    sys.executable, "-c", bootstrap, str(source), str(args.sdk_path), *forwarded,
                ], env=env)
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
