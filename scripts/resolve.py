#!/usr/bin/env python3
"""Resolve official build inputs once; inspect registries without masking failures."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "linux/amd64"
IMAGE = "ghcr.io/elmakus/pi-orca"
FINGERPRINT_LABEL = "io.github.pi-orca.fingerprint"
RUN_NUMBER_LABEL = "io.github.pi-orca.run-number"
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))


class ResolveError(Exception):
    pass


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        source = urllib.parse.urlsplit(request.full_url)
        target = urllib.parse.urlsplit(new_url)
        if target.scheme != "https" or target.username or target.password:
            raise ResolveError("Refusing an unsafe HTTP redirect")
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None and (source.hostname, source.port or 443) != (target.hostname, target.port or 443):
            redirected.remove_header("Authorization")
        return redirected


def fetch(url, headers=None, data=None):
    request_headers = {"User-Agent": "pi-orca-input-resolver", **(headers or {})}
    if urllib.parse.urlsplit(url).hostname == "api.github.com" and os.getenv("GITHUB_TOKEN"):
        request_headers.setdefault("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    try:
        opener = urllib.request.build_opener(SafeRedirectHandler())
        with opener.open(urllib.request.Request(url, headers=request_headers, data=data), timeout=60) as response:
            return response.read(), response.headers
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ResolveError("Transport failure contacting " + urllib.parse.urlsplit(url).hostname) from None


def get_json(url):
    return json.loads(fetch(url)[0])


def semver(tag):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\+[0-9A-Za-z.-]+)?", tag)
    return tuple(map(int, match.groups())) if match else None


def graphql_releases(repo):
    owner, name = repo.split("/")
    cursor = None
    releases = []
    query = """query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){releases(first:100,after:$cursor,orderBy:{field:CREATED_AT,direction:DESC}){nodes{databaseId tagName isDraft isPrerelease} pageInfo{hasNextPage endCursor}}}}"""
    while True:
        payload = json.dumps({"query": query, "variables": {"owner": owner, "name": name, "cursor": cursor}}).encode()
        response = json.loads(fetch("https://api.github.com/graphql", {"Content-Type": "application/json"}, data=payload)[0])
        if response.get("errors"):
            raise ResolveError("GitHub GraphQL release census failed")
        connection = response["data"]["repository"]["releases"]
        releases.extend({"id": item["databaseId"], "tag_name": item["tagName"], "draft": item["isDraft"], "prerelease": item["isPrerelease"]} for item in connection["nodes"])
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            return releases
        next_cursor = page["endCursor"]
        if not next_cursor or next_cursor == cursor:
            raise ResolveError("GitHub release pagination did not advance")
        cursor = next_cursor


def latest_release(repo):
    candidates = []
    page = 1
    while True:
        try:
            releases = get_json(f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}")
        except urllib.error.HTTPError as error:
            if error.code != 422:
                raise
            if not os.getenv("GITHUB_TOKEN"):
                raise ResolveError("Complete release census exceeds GitHub REST pagination; GITHUB_TOKEN is required for GraphQL cursor pagination") from None
            releases = graphql_releases(repo)
            page = None
        if not isinstance(releases, list):
            raise ResolveError("Invalid release list for " + repo)
        if not releases:
            break
        for release in releases:
            version = semver(release.get("tag_name", ""))
            if version is not None and not release.get("draft") and not release.get("prerelease"):
                candidates.append((version, release))
        if page is None:
            break
        page += 1
    if not candidates:
        raise ResolveError("No stable product release for " + repo)
    return max(candidates, key=lambda item: item[0])[1]


def release_assets(repo, release):
    # The embedded release assets list can be truncated independently of releases.
    result = []
    page = 1
    while True:
        batch = get_json(f"https://api.github.com/repos/{repo}/releases/{release['id']}/assets?per_page=100&page={page}")
        if not batch:
            return result
        result.extend(batch)
        page += 1


def asset_named(assets, name, repo, tag):
    matches = [asset for asset in assets if asset.get("name") == name]
    if len(matches) != 1:
        raise ResolveError("Missing or ambiguous official asset: " + name)
    asset = matches[0]
    expected = f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag, safe='')}/{name}"
    if asset.get("browser_download_url") != expected or asset.get("state") != "uploaded":
        raise ResolveError("Unexpected official asset URL/state: " + name)
    return asset


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ResolveError("Missing or invalid SHA-256")
    return value.lower()


def api_checksum(asset, required=False):
    digest = asset.get("digest")
    if digest is None and not required:
        return None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ResolveError("Missing SHA-256 asset digest: " + asset["name"])
    return sha256(digest[7:])


def checksum_entry(text, filename):
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line)
        if match and match[2] == filename:
            matches.append(sha256(match[1]))
    if len(matches) != 1:
        raise ResolveError("Missing or ambiguous checksum entry: " + filename)
    return matches[0]


def checked_checksum_file(asset, target):
    content = fetch(asset["browser_download_url"])[0]
    expected = api_checksum(asset)
    if expected and hashlib.sha256(content).hexdigest() != expected:
        raise ResolveError("Checksum file digest mismatch")
    checksum = checksum_entry(content.decode("utf-8"), target["name"])
    target_digest = api_checksum(target)
    if target_digest and target_digest != checksum:
        raise ResolveError("Official checksum sources disagree: " + target["name"])
    return checksum


def application_inputs():
    result = {}
    for product, repo in (("pi", "earendil-works/pi"), ("orca", "stablyai/orca"), ("gh", "cli/cli")):
        release = latest_release(repo)
        tag = release["tag_name"]
        version = tag.removeprefix("v")
        assets = release_assets(repo, release)
        filename = {"pi": "pi-linux-x64.tar.gz", "orca": f"orca-ide_{version}_amd64.deb", "gh": f"gh_{version}_linux_amd64.tar.gz"}[product]
        target = asset_named(assets, filename, repo, tag)
        if product == "orca":
            checksum = api_checksum(target, required=True)
        else:
            checksums_name = "SHA256SUMS" if product == "pi" else f"gh_{version}_checksums.txt"
            checksums = asset_named(assets, checksums_name, repo, tag)
            checksum = checked_checksum_file(checksums, target)
        result.update({f"{product}_version": version, f"{product}_url": target["browser_download_url"], f"{product}_sha256": checksum})
    return result


def node_variant(index, release_text, variants):
    versions = [entry["version"] for entry in index if entry.get("lts") and semver(entry.get("version", "")) is not None]
    if not versions:
        raise ResolveError("No stable Node LTS in official dist index")
    version = max(versions, key=semver).removeprefix("v")
    fields = dict(line.split(": ", 1) for line in release_text.splitlines() if ": " in line)
    codename = fields.get("Codename", "")
    if fields.get("Suite") != "stable" or not re.fullmatch(r"[a-z]+", codename):
        raise ResolveError("Invalid current Debian stable Release metadata")
    tag = f"{version}-{codename}-slim"
    for block in re.split(r"\n\s*\n", variants):
        stanza = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith("#"))
        if tag in stanza.get("Tags", "").split(", ") and "amd64" in stanza.get("Architectures", "").split(", "):
            return version, codename, "node:" + tag
    raise ResolveError(f"Official linux/amd64 Node LTS variant {tag} is unavailable; retain the previously tested latest")


def digest_bytes(content, expected=None):
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    if expected is not None and digest != expected:
        raise ResolveError("Registry content digest mismatch")
    return digest


class Registry:
    def __init__(self, host, repository):
        self.host, self.repository = host, repository
        self.authorization = None

    def request(self, path):
        url = f"https://{self.host}/v2/{self.repository}/{path}"
        headers = {"Accept": ACCEPT}
        if self.authorization:
            headers["Authorization"] = self.authorization
        try:
            return fetch(url, headers)
        except urllib.error.HTTPError as error:
            if error.code != 401 or self.authorization:
                raise
            challenge = error.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise ResolveError("Registry did not offer bearer authentication")
        parameters = dict(re.findall(r'(\w+)="([^"\r\n]*)"', challenge))
        realm = parameters.get("realm", "")
        parsed = urllib.parse.urlsplit(realm)
        allowed_hosts = {self.host, "auth.docker.io"} if self.host == "registry-1.docker.io" else {self.host}
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
            raise ResolveError("Untrusted registry authentication realm")
        query = urllib.parse.urlencode({"service": parameters.get("service", self.host), "scope": f"repository:{self.repository}:pull"})
        token_headers = {}
        if self.host == "ghcr.io" and os.getenv("GITHUB_TOKEN"):
            credentials = os.getenv("GITHUB_ACTOR", "") + ":" + os.environ["GITHUB_TOKEN"]
            token_headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
        # Token endpoint errors (including 404) are never missing-image evidence.
        try:
            token = json.loads(fetch(realm + ("&" if "?" in realm else "?") + query, token_headers)[0])
        except urllib.error.HTTPError as error:
            raise ResolveError(f"Registry authentication failed (HTTP {error.code})") from None
        value = token.get("token") or token.get("access_token")
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise ResolveError("Registry returned no valid bearer token")
        self.authorization = "Bearer " + value
        return self.request(path)

    def inspect(self, reference):
        try:
            content, headers = self.request("manifests/" + reference)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {"exists": False, "digest": None, "config_digest": None, "labels": {}}
            raise
        digest = digest_bytes(content, headers.get("Docker-Content-Digest"))
        if reference.startswith("sha256:"):
            digest_bytes(content, reference)
        manifest = json.loads(content)
        if "manifests" in manifest:
            matches = [item for item in manifest["manifests"] if item.get("platform", {}).get("os") == "linux" and item.get("platform", {}).get("architecture") == "amd64"]
            if len(matches) != 1:
                raise ResolveError("Registry index has no unique linux/amd64 image")
            selected = matches[0]["digest"]
            content, _ = self.request("manifests/" + selected)
            digest_bytes(content, selected)
            manifest = json.loads(content)
        config_digest = manifest["config"]["digest"]
        content, _ = self.request("blobs/" + config_digest)
        digest_bytes(content, config_digest)
        config = json.loads(content)
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ResolveError("Registry image is not linux/amd64")
        labels = config.get("config", {}).get("Labels") or {}
        if not isinstance(labels, dict):
            raise ResolveError("Invalid image labels")
        return {"exists": True, "digest": digest, "config_digest": config_digest, "labels": labels}


def registry_inspect(reference):
    if "://" in reference or "/" not in reference:
        raise ResolveError("Expected explicit registry/repository reference")
    host, name = reference.split("/", 1)
    if "@" in name:
        repository, ref = name.rsplit("@", 1)
    else:
        repository, separator, ref = name.rpartition(":")
        if not separator:
            repository, ref = name, "latest"
    if not re.fullmatch(r"[a-z0-9.-]+(?::\d+)?", host) or not re.fullmatch(r"[a-z0-9._/-]+", repository) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", ref):
        raise ResolveError("Invalid registry reference")
    return Registry(host, repository).inspect(ref)


def build_file_hashes(root):
    paths = [root / "Dockerfile", root / "entrypoint.sh", root / ".dockerignore"]
    paths += list((root / "scripts").rglob("*")) + list((root / ".github/workflows").glob("*"))
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths) if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def fingerprint(inputs, files):
    excluded = {"fingerprint", "created", "build_id", "run_number", "tag", "skip"}
    content = {"inputs": {key: value for key, value in inputs.items() if key not in excluded}, "build_files": files}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def should_skip(latest, value, event):
    return event != "workflow_dispatch" and latest["exists"] and latest["labels"].get(FINGERPRINT_LABEL) == value


def resolve(root=ROOT):
    inputs = application_inputs()
    node, debian, base = node_variant(get_json("https://nodejs.org/dist/index.json"), fetch("https://deb.debian.org/debian/dists/stable/Release")[0].decode(), fetch("https://raw.githubusercontent.com/docker-library/official-images/master/library/node")[0].decode())
    base_info = registry_inspect("registry-1.docker.io/library/" + base)
    if not base_info["exists"]:
        raise ResolveError("Selected official Node base is unavailable")
    revision = os.getenv("GITHUB_SHA") or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        raise ResolveError("Expected full source revision")
    now = dt.datetime.now(dt.timezone.utc)
    inputs.update(node_version=node, debian_codename=debian, base_image=base + "@" + base_info["digest"], base_digest=base_info["digest"], platform=PLATFORM, revision=revision, utc_date=now.date().isoformat())
    inputs["fingerprint"] = fingerprint(inputs, build_file_hashes(root))
    run_id = os.getenv("GITHUB_RUN_ID") or uuid.uuid4().hex
    attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
    run_number = os.getenv("GITHUB_RUN_NUMBER", "0")
    if not re.fullmatch(r"[A-Za-z0-9]+", run_id) or not attempt.isdecimal() or not run_number.isdecimal():
        raise ResolveError("Invalid build identity")
    tag = f"pi-{inputs['pi_version']}-orca-{inputs['orca_version']}-rev-{revision[:8]}-{now:%Y%m%d}-{run_id}-{attempt}"
    if len(tag) > 128 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", tag):
        raise ResolveError("Resolved versions cannot form a valid immutable Docker tag")
    inputs.update(created=now.isoformat().replace("+00:00", "Z"), build_id=f"{run_id}-{attempt}", run_number=run_number, tag=tag)
    latest = registry_inspect(IMAGE + ":latest")
    skip = should_skip(latest, inputs["fingerprint"], os.getenv("GITHUB_EVENT_NAME", "local"))
    if not skip and registry_inspect(IMAGE + ":" + tag)["exists"]:
        raise ResolveError("Refusing to reuse an existing version tag")
    return inputs, skip


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if len(sys.argv) > 1 and sys.argv[1] == "registry":
        parser.add_argument("command", choices=["registry"])
        parser.add_argument("--reference", required=True)
        args = parser.parse_args()
        print(json.dumps(registry_inspect(args.reference), sort_keys=True))
    else:
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--github-output", type=Path)
        args = parser.parse_args()
        inputs, skip = resolve()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(inputs, indent=2, sort_keys=True) + "\n")
        if args.github_output:
            with args.github_output.open("a") as output:
                for key, value in {**inputs, "skip": str(skip).lower()}.items():
                    if "\n" in str(value) or "\r" in str(value):
                        raise ResolveError("Invalid multiline GitHub output")
                    output.write(f"{key}={value}\n")
        print(json.dumps({"skip": skip, "tag": inputs["tag"], "fingerprint": inputs["fingerprint"]}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        sys.exit(f"Official source/registry request failed (HTTP {error.code}); latest was not changed")
    except (ResolveError, ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        # Do not emit upstream bodies, authentication headers, or raw transport errors.
        message = str(error) if isinstance(error, ResolveError) else type(error).__name__
        sys.exit("Resolution failed: " + message)
