import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

SPEC = importlib.util.spec_from_file_location("resolve", Path(__file__).resolve().parents[1] / "scripts/resolve.py")
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


class FixedDatetime(r.dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 4, 12, 0, 0, tzinfo=r.dt.timezone.utc)


def release(tag, **kwargs):
    return {"id": 123, "tag_name": tag, "draft": False, "prerelease": False, **kwargs}


def http_error(code, challenge=None):
    return urllib.error.HTTPError("https://ghcr.io/v2/test/image/manifests/latest", code, "failure", {"WWW-Authenticate": challenge} if challenge else {}, None)


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def asset(name, checksum=None):
    result = {"name": name, "state": "uploaded", "browser_download_url": "https://github.com/owner/repo/releases/download/v1.0.0/" + name}
    if checksum is not None:
        result["digest"] = "sha256:" + checksum
    return result


class ReleasesTests(unittest.TestCase):
    def test_numeric_order_and_product_filtering_across_all_pages(self):
        pages = [[release("v1.9.9"), release("v99.0.0", draft=True), release("v98.0.0", prerelease=True)], [release("mobile-android-v90.0.0"), release("v80.0.0-rc.1"), release("v1.10.0")], [release("v1.2.99")], []]
        with patch.object(r, "get_json", side_effect=pages) as fetch:
            self.assertEqual(r.latest_release("owner/repo")["tag_name"], "v1.10.0")
            self.assertEqual(fetch.call_count, 4)
            self.assertTrue(fetch.call_args.args[0].endswith("page=4"))

    def test_stable_semver_only(self):
        self.assertGreater(r.semver("v10.1.0"), r.semver("v9.99.99"))
        self.assertEqual(r.semver("v1.2.3+build.4"), (1, 2, 3))
        for tag in ("v1.2.3-rc.1", "v01.2.3", "android-v1.2.3", "v1.2", "nightly"):
            self.assertIsNone(r.semver(tag))

    def test_no_stable_release_fails(self):
        with patch.object(r, "get_json", side_effect=[[release("v2.0.0-beta")], []]):
            with self.assertRaises(r.ResolveError):
                r.latest_release("owner/repo")

    def test_assets_are_paginated_separately(self):
        with patch.object(r, "get_json", side_effect=[[{"name": "one"}], [{"name": "two"}], []]) as fetch:
            self.assertEqual(len(r.release_assets("owner/repo", {"id": 42})), 2)
            self.assertIn("/42/assets?", fetch.call_args.args[0])

    def test_rest_limit_requires_authenticated_cursor_census(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}), patch.object(r, "get_json", side_effect=http_error(422)):
            with self.assertRaisesRegex(r.ResolveError, "GITHUB_TOKEN"):
                r.latest_release("owner/repo")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test"}), patch.object(r, "get_json", side_effect=[[release("v1.0.0")], http_error(422)]), patch.object(r, "graphql_releases", return_value=[release("v2.0.0")]) as cursor:
            self.assertEqual(r.latest_release("owner/repo")["tag_name"], "v2.0.0")
            cursor.assert_called_once_with("owner/repo")

    def test_graphql_cursor_pages_and_errors(self):
        def page(tag, more, cursor):
            return encoded({"data": {"repository": {"releases": {"nodes": [{"databaseId": 1, "tagName": tag, "isDraft": False, "isPrerelease": False}], "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}}), {}
        with patch.object(r, "fetch", side_effect=[page("v1.0.0", True, "cursor1"), page("v2.0.0", False, "cursor2")]) as fetch:
            self.assertEqual(len(r.graphql_releases("owner/repo")), 2)
            request = json.loads(fetch.call_args.kwargs["data"])
            self.assertEqual(request["variables"]["cursor"], "cursor1")
        with patch.object(r, "fetch", return_value=(encoded({"errors": [{"message": "private"}]}), {})):
            with self.assertRaises(r.ResolveError):
                r.graphql_releases("owner/repo")


class IntegrityTests(unittest.TestCase):
    def test_exact_checksum_filename_and_binary_marker(self):
        checksum = "a" * 64
        self.assertEqual(r.checksum_entry(checksum + "  other\n" + checksum + " *pi-linux-x64.tar.gz\n", "pi-linux-x64.tar.gz"), checksum)
        for content in ("", checksum + "  other", "bad  pi-linux-x64.tar.gz", (checksum + "  pi-linux-x64.tar.gz\n") * 2):
            with self.assertRaises(r.ResolveError):
                r.checksum_entry(content, "pi-linux-x64.tar.gz")

    def test_missing_wrong_algorithm_and_invalid_api_digests(self):
        for digest in (None, "sha512:" + "a" * 128, "sha256:bad"):
            with self.assertRaises(r.ResolveError):
                r.api_checksum({"name": "orca.deb", "digest": digest}, required=True)

    def test_official_checksums_must_agree(self):
        text = ("a" * 64 + "  payload\n").encode()
        checksums = asset("SHA256SUMS", hashlib.sha256(text).hexdigest())
        target = asset("payload", "a" * 64)
        with patch.object(r, "fetch", return_value=(text, {})):
            self.assertEqual(r.checked_checksum_file(checksums, target), "a" * 64)
            target["digest"] = "sha256:" + "b" * 64
            with self.assertRaisesRegex(r.ResolveError, "disagree"):
                r.checked_checksum_file(checksums, target)
            checksums["digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(r.ResolveError, "file digest mismatch"):
                r.checked_checksum_file(checksums, target)

    def test_wrong_asset_product_or_source_fails(self):
        with self.assertRaises(r.ResolveError):
            r.asset_named([asset("arm64")], "amd64", "owner/repo", "v1.0.0")
        target = asset("payload")
        target["browser_download_url"] = "https://example.com/payload"
        with self.assertRaises(r.ResolveError):
            r.asset_named([target], "payload", "owner/repo", "v1.0.0")


class BaseTests(unittest.TestCase):
    def setUp(self):
        self.index = [{"version": "v27.0.0-alpha.1", "lts": "Future"}, {"version": "v26.9.0", "lts": "LTS"}, {"version": "v26.13.0", "lts": "LTS"}, {"version": "v28.0.0", "lts": False}]
        self.release = "Suite: stable\nCodename: trixie\nVersion: 13.7\n"
        self.variants = "Tags: 26.13.0-trixie-slim, lts-slim\nArchitectures: amd64, arm64v8\n"

    def test_current_stable_lts_numeric_and_exact_variant(self):
        self.assertEqual(r.node_variant(self.index, self.release, self.variants), ("26.13.0", "trixie", "node:26.13.0-trixie-slim"))

    def test_no_silent_old_debian_or_missing_amd64_fallback(self):
        for variants in (self.variants.replace("trixie", "bookworm"), self.variants.replace("amd64, ", "")):
            with self.assertRaises(r.ResolveError):
                r.node_variant(self.index, self.release, variants)
        with self.assertRaises(r.ResolveError):
            r.node_variant(self.index, self.release.replace("stable", "testing"), self.variants)


class FingerprintTests(unittest.TestCase):
    def test_all_managed_inputs_and_day_change_fingerprint(self):
        values = {"pi_version": "1.0.0", "pi_sha256": "a" * 64, "orca_version": "1.0.0", "orca_sha256": "b" * 64, "gh_version": "2.0.0", "gh_sha256": "c" * 64, "node_version": "26.0.0", "debian_codename": "trixie", "base_digest": "sha256:" + "d" * 64, "utc_date": "2026-11-04", "revision": "e" * 40, "platform": "linux/amd64"}
        original = r.fingerprint(values, {"Dockerfile": "one"})
        for key in values:
            with self.subTest(key=key):
                self.assertNotEqual(original, r.fingerprint({**values, key: values[key] + "changed"}, {"Dockerfile": "one"}))
        self.assertNotEqual(original, r.fingerprint(values, {"Dockerfile": "two"}))
        self.assertEqual(original, r.fingerprint({**values, "created": "different", "build_id": "123", "run_number": "4", "tag": "unique"}, {"Dockerfile": "one"}))

    def test_skip_requires_tested_latest_same_fingerprint_and_not_dispatch(self):
        latest = {"exists": True, "labels": {r.FINGERPRINT_LABEL: "same"}}
        self.assertTrue(r.should_skip(latest, "same", "schedule"))
        self.assertFalse(r.should_skip(latest, "changed", "schedule"))
        self.assertFalse(r.should_skip(latest, "same", "workflow_dispatch"))
        self.assertFalse(r.should_skip({"exists": False, "labels": {}}, "same", "schedule"))

    def test_file_content_not_mtime_is_hashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Dockerfile").write_text("first")
            first = r.build_file_hashes(root)
            (root / "Dockerfile").write_text("second")
            self.assertNotEqual(first, r.build_file_hashes(root))


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.apps = {"pi_version": "1.5.0", "pi_url": "https://github.com/pi", "pi_sha256": "a" * 64, "orca_version": "1.4.248", "orca_url": "https://github.com/orca", "orca_sha256": "b" * 64, "gh_version": "2.103.3", "gh_url": "https://github.com/gh", "gh_sha256": "c" * 64}
        self.missing = {"exists": False, "digest": None, "config_digest": None, "labels": {}}
        self.base = {"exists": True, "digest": "sha256:" + "d" * 64}

    def resolve_with(self, attempt="1", collision=False):
        env = {"GITHUB_SHA": "e" * 40, "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": attempt, "GITHUB_RUN_NUMBER": "77", "GITHUB_EVENT_NAME": "workflow_dispatch"}
        with patch.dict(os.environ, env), patch.object(r.dt, "datetime", FixedDatetime), patch.object(r, "application_inputs", return_value=self.apps.copy()), patch.object(r, "get_json", return_value=[]), patch.object(r, "fetch", return_value=(b"", {})), patch.object(r, "node_variant", return_value=("26.13.0", "trixie", "node:26.13.0-trixie-slim")), patch.object(r, "latest_patch_commit", return_value="f" * 40), patch.object(r, "build_file_hashes", return_value={"Dockerfile": "hash"}), patch.object(r, "registry_inspect", side_effect=[self.base, self.missing, {**self.missing, "exists": collision}]):
            return r.resolve()

    def test_full_manifest_and_rerun_identity(self):
        first, skip = self.resolve_with("1")
        second, _ = self.resolve_with("2")
        self.assertFalse(skip)
        self.assertNotEqual(first["tag"], second["tag"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertTrue(first["tag"].endswith("-12345-1"))
        self.assertTrue(second["tag"].endswith("-12345-2"))
        self.assertEqual(first["run_number"], "77")
        self.assertEqual(first["base_image"], "node:26.13.0-trixie-slim@" + self.base["digest"])
        self.assertEqual(first["platform"], "linux/amd64")
        self.assertEqual(first["patch_commit"], "f" * 40)
        self.assertTrue(first["created"].endswith("Z"))

    def test_existing_version_tag_is_never_reused(self):
        with self.assertRaisesRegex(r.ResolveError, "reuse"):
            self.resolve_with(collision=True)




class RedirectTests(unittest.TestCase):
    def redirect(self, request, target):
        return r.SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)

    def test_cross_origin_redirect_never_forwards_credentials(self):
        for credential in ("Bearer registry-secret", "Bearer github-secret", "Basic github-credentials"):
            with self.subTest(credential_type=credential.split()[0]):
                request = r.urllib.request.Request("https://ghcr.io/v2/test/image/blobs/sha256:abc", headers={"Authorization": credential})
                redirected = self.redirect(request, "https://pkg-containers.githubusercontent.com/blob")
                self.assertFalse(redirected.has_header("Authorization"))
                self.assertEqual(request.get_header("Authorization"), credential)
                different_port = self.redirect(request, "https://ghcr.io:8443/blob")
                self.assertFalse(different_port.has_header("Authorization"))

    def test_same_origin_preserves_authentication(self):
        request = r.urllib.request.Request("https://ghcr.io/start", headers={"Authorization": "Bearer registry-secret"})
        redirected = self.redirect(request, "https://ghcr.io:443/target")
        self.assertEqual(redirected.get_header("Authorization"), "Bearer registry-secret")

    def test_non_https_and_embedded_credentials_are_rejected(self):
        request = r.urllib.request.Request("https://ghcr.io/start", headers={"Authorization": "Bearer registry-secret"})
        for target in ("http://ghcr.io/target", "http://other.example/target", "ftp://other.example/target", "https://user:password@ghcr.io/target"):
            with self.subTest(target=target), self.assertRaises(r.ResolveError):
                self.redirect(request, target)


class RegistryTests(unittest.TestCase):
    def test_absence_only_for_manifest_404(self):
        for code in (401, 403, 429, 500):
            with self.subTest(code=code), patch.object(r.Registry, "request", side_effect=http_error(code)):
                with self.assertRaises(urllib.error.HTTPError):
                    r.registry_inspect("ghcr.io/test/image:latest")
        with patch.object(r.Registry, "request", side_effect=http_error(404)):
            self.assertEqual(r.registry_inspect("ghcr.io/test/image:latest"), {"exists": False, "digest": None, "config_digest": None, "labels": {}})
        with patch.object(r.Registry, "request", side_effect=r.ResolveError("Transport failure")):
            with self.assertRaises(r.ResolveError):
                r.registry_inspect("ghcr.io/test/image:latest")

    def test_bearer_challenge_and_token_failure_not_absence(self):
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:test/image:pull"'
        with patch.object(r, "fetch", side_effect=[http_error(401, challenge), (encoded({"token": "test-token"}), {}), http_error(404)]):
            self.assertFalse(r.registry_inspect("ghcr.io/test/image:latest")["exists"])
        for code in (401, 403, 404, 500):
            with self.subTest(code=code), patch.object(r, "fetch", side_effect=[http_error(401, challenge), http_error(code)]):
                with self.assertRaisesRegex(r.ResolveError, "authentication failed"):
                    r.registry_inspect("ghcr.io/test/image:latest")
    def test_untrusted_auth_realm_rejected(self):
        challenge = 'Bearer realm="https://evil.example/token",service="ghcr.io"'
        with patch.object(r, "fetch", side_effect=http_error(401, challenge)):
            with self.assertRaisesRegex(r.ResolveError, "Untrusted"):
                r.registry_inspect("ghcr.io/test/image:latest")

    def test_index_selects_real_amd64_and_verifies_all_content(self):
        config = encoded({"os": "linux", "architecture": "amd64", "config": {"Labels": {r.FINGERPRINT_LABEL: "fp", r.RUN_NUMBER_LABEL: "7"}}})
        config_digest = r.digest_bytes(config)
        manifest = encoded({"config": {"digest": config_digest}})
        manifest_digest = r.digest_bytes(manifest)
        index = encoded({"manifests": [{"digest": "sha256:" + "0" * 64, "platform": {"os": "unknown", "architecture": "unknown"}}, {"digest": manifest_digest, "platform": {"os": "linux", "architecture": "amd64"}}]})
        with patch.object(r.Registry, "request", side_effect=[(index, {"Docker-Content-Digest": r.digest_bytes(index)}), (manifest, {}), (config, {})]):
            result = r.registry_inspect("ghcr.io/test/image:latest")
            self.assertEqual(result["digest"], r.digest_bytes(index))
            self.assertEqual(result["config_digest"], config_digest)
            self.assertEqual(result["labels"][r.RUN_NUMBER_LABEL], "7")
        with patch.object(r.Registry, "request", side_effect=[(index, {}), (manifest + b" ", {})]):
            with self.assertRaisesRegex(r.ResolveError, "digest mismatch"):
                r.registry_inspect("ghcr.io/test/image:latest")
        with patch.object(r.Registry, "request", side_effect=[(manifest, {}), http_error(404)]):
            with self.assertRaises(urllib.error.HTTPError):
                r.registry_inspect("ghcr.io/test/image:latest")

    def test_single_manifest_rejects_wrong_platform(self):
        config = encoded({"os": "linux", "architecture": "arm64"})
        manifest = encoded({"config": {"digest": r.digest_bytes(config)}})
        with patch.object(r.Registry, "request", side_effect=[(manifest, {}), (config, {})]):
            with self.assertRaisesRegex(r.ResolveError, "not linux/amd64"):
                r.registry_inspect("ghcr.io/test/image:latest")


if __name__ == "__main__":
    unittest.main()
