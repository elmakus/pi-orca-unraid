#!/usr/bin/env python3
"""Isolated Docker smoke; never emit captured runtime output or credentials."""
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


class SmokeFailure(Exception):
    pass


def run(args, timeout=45, check=True):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise SmokeFailure("command failed or exceeded its deadline") from None
    if check and result.returncode:
        raise SmokeFailure("command returned a failure (output withheld)")
    return result


def decode(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        raise SmokeFailure("invalid JSON response (output withheld)") from None


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


def main(image):
    name = "pi-orca-smoke-" + uuid.uuid4().hex[:12]
    temporary = Path(tempfile.mkdtemp(prefix=name + "-"))
    home, workspace = temporary / "home", temporary / "workspace"
    stage = "fixture setup"

    def execute(*args, timeout=45, check=True):
        return run(["docker", "exec", name, *args], timeout=timeout, check=check)

    def cli(*args, timeout=45):
        value = decode(execute("timeout", "--kill-after=2s", str(timeout - 3) + "s",
                               "orca-ide", *args, "--json", timeout=timeout).stdout)
        require(value.get("ok") is True, "Orca command did not report success")
        return value["result"]

    def terminal_contains(handle, needle, deadline=30):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            result = cli("terminal", "read", "--terminal", handle)
            tail = result["terminal"]["tail"]
            require(isinstance(tail, list), "unexpected terminal output shape")
            if needle in "\n".join(tail):
                return
            time.sleep(0.5)
        raise SmokeFailure("real PTY output deadline exceeded")

    try:
        for directory in (home, workspace):
            directory.mkdir()
            # Only these disposable fixtures are writable; no production bind is touched.
            directory.chmod(0o777)
        stage = "image manifest"
        manifest = decode(run(["docker", "run", "--rm", "--network", "none",
                               "--cap-drop=ALL", "--security-opt=no-new-privileges",
                               "--entrypoint", "/bin/cat", image,
                               "/usr/local/share/pi-orca/build.json"]).stdout)
        stage = "negative native status"
        negative = run(["docker", "run", "--rm", "--network", "none",
                        "--cap-drop=ALL", "--security-opt=no-new-privileges",
                        "--entrypoint", "/usr/bin/timeout", image,
                        "--kill-after=2s", "8s", "orca-ide", "status"], check=False)
        require(negative.returncode == 1, "offline native status did not return exit 1")
        stage = "hardened runtime startup"
        run(["docker", "run", "--detach", "--name", name, "--hostname", name,
             "--cap-drop=ALL", "--security-opt=no-new-privileges",
             "--stop-timeout", "30", "--mount", f"type=bind,src={home},dst=/home/agent",
             "--mount", f"type=bind,src={workspace},dst=/workspace",
             image, "--no-pairing"])
        stage = "native runtime readiness"
        end = time.monotonic() + 180
        ready = False
        while time.monotonic() < end:
            attempt = execute("timeout", "--kill-after=2s", "8s", "orca-ide", "status", "--json",
                              timeout=12, check=False)
            if attempt.returncode == 0:
                status = decode(attempt.stdout)
                result = status.get("result", {})
                runtime = result.get("runtime", {})
                if (status.get("ok") is True and runtime.get("reachable") is True
                        and runtime.get("state") == "ready"
                        and result.get("graph", {}).get("state") == "ready"):
                    ready = True
                    break
            time.sleep(2)
        require(ready, "native runtime did not become ready")
        execute("timeout", "--kill-after=2s", "8s", "orca-ide", "status")
        stage = "versions and nonroot tools"
        for binary, key in (("pi", "pi_version"), ("orca-ide", "orca_version"),
                            ("gh", "gh_version"), ("node", "node_version")):
            version = execute(binary, "--version").stdout
            require(re.search(r"(?<![\d.])" + re.escape(manifest[key]) + r"(?![\d.])", version),
                    binary + " version differs from resolved manifest")
        execute("sh", "-ec", """
            test "$(id -u)" = 1000 && test "$(id -g)" = 1000
            test "$HOME" = /home/agent && test "$(pwd)" = /workspace
            touch /home/agent/.write-test /workspace/.write-test
            for tool in npm python3 git ssh gh rg fd jq curl wget dumb-init; do command -v "$tool" >/dev/null; done
            python3 -m venv /home/agent/smoke-venv
            /home/agent/smoke-venv/bin/python -m pip --version >/dev/null
            mkdir -p "$HOME/.local/bin" "$NPM_CONFIG_PREFIX/bin"
            printf '#!/bin/sh\\nexit 99\\n' > "$HOME/.local/bin/pi"
            cp "$HOME/.local/bin/pi" "$HOME/.local/bin/orca-ide"
            chmod +x "$HOME/.local/bin/pi" "$HOME/.local/bin/orca-ide"
            test "$(command -v pi)" = /usr/local/bin/pi
            test "$(command -v orca-ide)" = /usr/local/bin/orca-ide
            test "$(stat -c %u /opt/pi/pi)" = 0 && test ! -w /opt/pi/pi
            test "$(stat -c %u /opt/Orca)" = 0 && test ! -w /opt/Orca
        """)
        execute("python3", "-c", "import socket; s=socket.create_connection(('127.0.0.1',6769),5); s.close()")
        stage = "real repository and worktree"
        execute("sh", "-ec", """
            mkdir /workspace/repository && cd /workspace/repository
            git init -q
            git -c user.name=Smoke -c user.email=smoke@example.invalid commit --allow-empty -qm smoke
        """)
        repo = cli("repo", "add", "--path", "/workspace/repository")["repo"]["id"]
        worktree = cli("worktree", "create", "--repo", "id:" + repo,
                       "--name", "smoke", "--setup", "skip")["worktree"]["id"]
        stage = "real Orca PTY"
        handle = cli("terminal", "create", "--worktree", "id:" + worktree)["terminal"]["handle"]
        nonce = uuid.uuid4().hex
        cli("terminal", "send", "--terminal", handle, "--text",
            "node -e \"console.log('PTY_' + '" + nonce + "')\"", "--enter")
        terminal_contains(handle, "PTY_" + nonce)
        stage = "managed Pi RPC startup"
        launch_managed_pi(cli, execute, worktree)
        stage = "child startup and full shutdown"
        child_code = ("import os,time; "
                      "open('/workspace/child-ready','w').write(str(os.getpid())); "
                      "time.sleep(300)")
        cli("terminal", "send", "--terminal", handle, "--text",
            "python3 -c " + shlex.quote(child_code), "--enter")
        end = time.monotonic() + 15
        while not (workspace / "child-ready").exists() and time.monotonic() < end:
            time.sleep(0.25)
        require((workspace / "child-ready").exists(), "long-lived PTY child did not start")
        child_pid = (workspace / "child-ready").read_text().strip()
        require(child_pid.isdigit(), "invalid child PID")
        execute("kill", "-0", child_pid)
        started = time.monotonic()
        run(["docker", "stop", "--time", "30", name], timeout=35)
        elapsed = time.monotonic() - started
        state = decode(run(["docker", "inspect", "--format", "{{json .State}}", name]).stdout)
        require(elapsed <= 30 and state["Running"] is False and state["Pid"] == 0,
                "container did not fully stop within 30 seconds")
        require(state["ExitCode"] != 137 and not state["OOMKilled"], "container required forced termination")
        print("PASS: resolved versions, UID/GID1000, fresh binds, tools/venv, core PATH, native status/negative status")
        print("PASS: port6769, Orca PTY, managed Pi RPC without generation, live child and full shutdown <=30s")
    except (SmokeFailure, KeyError, TypeError, ValueError, OSError):
        print("FAIL: " + stage + "; sensitive command output withheld", file=sys.stderr)
        return 1
    finally:
        run(["docker", "rm", "--force", name], timeout=35, check=False)
        # Remove only agent-owned files inside the disposable binds, without root capabilities.
        run(["docker", "run", "--rm", "--network", "none", "--user", "1000:1000",
             "--cap-drop=ALL", "--security-opt=no-new-privileges",
             "--mount", f"type=bind,src={home},dst=/smoke-home",
             "--mount", f"type=bind,src={workspace},dst=/smoke-workspace",
             "--entrypoint", "/bin/sh", image,
             "-ec", "find /smoke-home /smoke-workspace -mindepth 1 -delete"], timeout=30, check=False)
        shutil.rmtree(temporary, ignore_errors=True)
    return 0


def launch_managed_pi(cli, execute, worktree):
    # The fake provider is loopback-only; no prompt or model generation is ever requested.
    execute("mkdir", "-p", "/home/agent/.pi/agent")
    models = {"providers": {"smoke": {"baseUrl": "http://127.0.0.1:1/v1",
              "api": "openai-responses", "apiKey": "smoke-not-a-secret",
              "models": [{"id": "smoke-model"}]}}}
    settings = {"defaultProvider": "smoke", "defaultModel": "smoke-model"}
    for filename, content in (("models.json", models), ("settings.json", settings)):
        execute("python3", "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                "/home/agent/.pi/agent/" + filename, json.dumps(content))
    result = cli("agent", "launch", "--agent", "pi", "--worktree", "id:" + worktree,
                 "--surface", "chat", timeout=90)
    outcome = result.get("outcome", {})
    require(outcome.get("kind") == "structured" and outcome.get("sessionId") and outcome.get("handle"),
            "Pi launch did not return a managed structured session")
    execute("python3", "-c", """
import json, pathlib, socket, sys, uuid
metadata = json.loads(pathlib.Path('/home/agent/.config/orca/orca-runtime.json').read_text())
endpoint = next(t['endpoint'] for t in metadata['transports'] if t['kind'] == 'unix')
request_id = str(uuid.uuid4())
request = {'id': request_id, 'authToken': metadata['authToken'],
           'method': 'agentSession.options', 'params': {'sessionId': sys.argv[1]}}
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(20)
    connection.connect(endpoint)
    connection.sendall((json.dumps(request) + '\\n').encode())
    stream = connection.makefile('rb')
    while True:
        line = stream.readline(1048576)
        assert line
        response = json.loads(line)
        if '_keepalive' in response:
            continue
        assert response.get('id') == request_id and response.get('ok') is True
        assert response.get('_meta', {}).get('runtimeId', metadata['runtimeId']) == metadata['runtimeId']
        break
for process in pathlib.Path('/proc').iterdir():
    if not process.name.isdigit():
        continue
    try:
        args = (process / 'cmdline').read_bytes().split(b'\\0')
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    if b'--mode' in args and b'rpc' in args and any(arg in (b'/opt/pi/pi', b'/usr/local/bin/pi', b'pi') for arg in args):
        break
else:
    raise SystemExit(1)
""", outcome["sessionId"])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: runtime-smoke.py IMAGE", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
