#!/usr/bin/env python3
"""Apply and regression-test the resolved upstream Pi/codex-lb patch revision.

The upstream owner's patch targets a Pi source layout without Pi 1.5's
identityHeaders. Only that specific known drift is rebased automatically;
all other failures stop the image build without publishing it.
"""
import argparse
from pathlib import Path
import subprocess

MODES = (
    "server-error", "top-level-server-error", "rate-limit", "partial-output",
    "explicit-anchor", "client-error", "stream-incomplete", "operation-in-progress",
    "owner-unavailable", "repeated-server-error",
)


def run(*args, cwd=None, check=True):
    result = subprocess.run([str(x) for x in args], cwd=cwd, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(map(str,args))}: {result.stderr[-2500:] or result.stdout[-2500:]}")
    return result


def replace_once(text, before, after, count=1):
    if text.count(before) != count:
        raise RuntimeError(f"Unsupported Pi source layout for {before[:55]!r}: expected {count}, got {text.count(before)}")
    return text.replace(before, after)


def owner_identity_compat(package, provider):
    rejects = list(package.rglob("*.rej"))
    expected = provider.with_suffix(provider.suffix + ".rej")
    if rejects != [expected]:
        raise RuntimeError(f"Unexpected upstream patch reject files: {rejects}")
    rejected = expected.read_text()
    if rejected.count("@@ -") != 1 or "const cacheSessionId = options?.cacheRetention" not in rejected or "const sseHeaders = buildSSEHeaders(model.headers" not in rejected:
        raise RuntimeError("Unrecognized upstream patch mismatch; refusing automatic rebase")
    source = provider.read_text()
    for old,new,n in (
        ('const cacheSessionId = options?.cacheRetention === "none" ? undefined : options?.sessionId;',
         'const originalCacheSessionId = options?.cacheRetention === "none" ? undefined : options?.sessionId;\n            let cacheSessionId = resolveCodexSessionAlias(originalCacheSessionId);',1),
        ('const codexSessionId = clampOpenAIPromptCacheKey(cacheSessionId);',
         'let codexSessionId = clampOpenAIPromptCacheKey(cacheSessionId);',1),
        ('const websocketRequestId = codexSessionId || uuidv7();',
         'let websocketRequestId = codexSessionId || uuidv7();',1),
        ('const sseHeaders = buildSSEHeaders(identityHeaders,',
         'let sseHeaders = buildSSEHeaders(identityHeaders,',1),
        ('const websocketHeaders = buildWebSocketHeaders(identityHeaders,',
         'let websocketHeaders = buildWebSocketHeaders(identityHeaders,',1),
        ('const bodyJson = JSON.stringify(body);',
         'let bodyJson = JSON.stringify(body);',1),
        ('buildSSEHeaders(model.headers, options?.headers, accountId, apiKey, codexSessionId);',
         'buildSSEHeaders(identityHeaders, model.headers, options?.headers, accountId, apiKey, codexSessionId);',2),
        ('buildWebSocketHeaders(model.headers, options?.headers, accountId, apiKey, websocketRequestId);',
         'buildWebSocketHeaders(identityHeaders, model.headers, options?.headers, accountId, apiKey, websocketRequestId);',1),
    ):
        source = replace_once(source, old, new, n)
    provider.write_text(source)
    expected.unlink()
    print("Rebased upstream owner-migration patch for Pi identity headers", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--patches", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    package = args.package.resolve()
    patches = args.patches.resolve()
    assert (package / "package.json").is_file()
    provider = package / "node_modules/@earendil-works/pi-ai/dist/api/openai-codex-responses.js"
    assert provider.is_file()
    for name in ("bearer-auth", "transient-retry", "owner-migration"):
        patch = patches / "patches" / (name + ".patch")
        if not patch.is_file():
            raise RuntimeError(f"Upstream patch missing: {name}")
        result = run("git", "apply", "--check", patch, cwd=package, check=False)
        if result.returncode == 0:
            run("git", "apply", patch, cwd=package)
        elif name == "owner-migration":
            partial = run("git", "apply", "--reject", "--whitespace=nowarn", patch, cwd=package, check=False)
            if partial.returncode == 0:
                raise RuntimeError("Unexpected clean --reject after failed --check")
            owner_identity_compat(package, provider)
        else:
            raise RuntimeError(f"Upstream patch {name} is incompatible: {result.stderr[-2000:]}")
        print(f"Applied upstream {name}", flush=True)
    run("node", "--check", provider)
    run("node", patches / "src/rebuild-bundle.mjs", package)
    cli = package / "dist/bundle/cli.js"
    assert run("node", cli, "--version").stdout.strip() == args.version
    chunks = list((package / "dist/bundle/chunks").glob("*.js"))
    matched = [p for p in chunks if "codexSessionAliases" in p.read_text(errors="replace")]
    if len(matched) != 1:
        raise RuntimeError("Expected exactly one patched bundled native Codex provider")
    if not (package / "dist/bundle/chunks/codemode-worker.js").is_file():
        raise RuntimeError("Code Mode worker missing from rebuilt Pi")
    for file in (provider, matched[0]):
        run("node", patches / "tests/bearer-auth.mjs", file)
        for mode in MODES:
            run("node", patches / "tests/recovery.mjs", file, mode, "combined")
    print(f"All 22 patch regression checks passed for Pi {args.version}", flush=True)


if __name__ == "__main__":
    main()
