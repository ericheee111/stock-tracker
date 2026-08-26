from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stock_tracker.deployment.hybrid_h4 import (
    HybridH4Error,
    StaticBuildConfig,
    build_static_site,
    verify_static_build,
)

ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_MARKER = "H4_FORBIDDEN_MARKER"


def _config(*, host: str = "cloudflare", allow_loopback_http: bool = True) -> StaticBuildConfig:
    return StaticBuildConfig(
        web_origin="http://127.0.0.1:8788" if allow_loopback_http else "https://app.example",
        api_origin="http://localhost:8081" if allow_loopback_http else "https://engine.example.ts.net",
        engine_id="hybrid-h4-test-engine",
        build_id="0123456789abcdef",
        expected_api_major=1,
        host=host,
        allow_loopback_http=allow_loopback_http,
    )


class TestHybridH4StaticBuild(unittest.TestCase):
    def test_production_origins_are_https_and_exact(self) -> None:
        normalized = _config(allow_loopback_http=False).normalized()
        self.assertEqual(normalized.web_origin, "https://app.example")
        self.assertEqual(normalized.api_origin, "https://engine.example.ts.net")
        invalid = (
            StaticBuildConfig(
                web_origin="http://app.example",
                api_origin="https://engine.example.ts.net",
                engine_id="engine",
                build_id="build",
            ),
            StaticBuildConfig(
                web_origin="https://app.example/path",
                api_origin="https://engine.example.ts.net",
                engine_id="engine",
                build_id="build",
            ),
            StaticBuildConfig(
                web_origin="https://app.example",
                api_origin="http://engine.example.ts.net",
                engine_id="engine",
                build_id="build",
            ),
        )
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(HybridH4Error):
                config.normalized()

    def test_build_is_no_secret_deterministic_and_pins_cloudflare_csp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            result1 = build_static_site(
                ROOT / "web",
                first,
                _config(),
                forbidden_values=(_FORBIDDEN_MARKER,),
            )
            result2 = build_static_site(
                ROOT / "web",
                second,
                _config(),
                forbidden_values=(_FORBIDDEN_MARKER,),
            )
            self.assertTrue(result1["passed"])
            self.assertTrue(result2["passed"])
            manifest1 = json.loads((first / "deployment-manifest.json").read_text(encoding="utf-8"))
            manifest2 = json.loads((second / "deployment-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest1, manifest2)
            self.assertFalse(manifest1["contains_private_access"])
            headers = (first / "_headers").read_text(encoding="utf-8")
            self.assertIn("connect-src 'self' http://localhost:8081", headers)
            self.assertNotIn("connect-src *", headers)
            runtime = (first / "runtime-config.js").read_text(encoding="utf-8")
            self.assertNotIn(_FORBIDDEN_MARKER, runtime)
            self.assertNotIn("privateAccess", runtime)
            self.assertFalse((first / "runtime-config.example.js").exists())
            self.assertFalse(any(path.suffix == ".pyc" for path in first.rglob("*")))
            self.assertTrue(
                verify_static_build(first, forbidden_values=(_FORBIDDEN_MARKER,))["passed"]
            )

    def test_github_fallback_is_honest_about_response_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            result = build_static_site(ROOT / "web", output, _config(host="github"))
            manifest = json.loads((output / "deployment-manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(result["passed"])
            self.assertFalse(manifest["response_headers_supported"])
            self.assertFalse((output / "_headers").exists())
            self.assertTrue((output / ".nojekyll").is_file())
            index = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn("Content-Security-Policy", index)

    def test_tamper_and_unmanifested_files_fail_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            build_static_site(ROOT / "web", output, _config())
            (output / "runtime-config.js").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(HybridH4Error, "hash/size mismatch"):
                verify_static_build(output)

            build_static_site(ROOT / "web", output, _config())
            (output / "extra.txt").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(HybridH4Error, "file set"):
                verify_static_build(output)

    def test_build_refuses_source_ancestor_or_descendant_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "web"
            source.mkdir()
            (source / "index.html").write_text(
                "<html><head></head><body></body></html>", encoding="utf-8"
            )
            with self.assertRaises(HybridH4Error):
                build_static_site(source, source / "build", _config())
            with self.assertRaises(HybridH4Error):
                build_static_site(source, root, _config())

    def test_text_is_utf8_lf_and_local_launchers_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "web"
            (source / "js").mkdir(parents=True)
            (source / "index.html").write_bytes(
                b"<html>\r\n<head></head>\r\n<body><script src=\"js/app.js\"></script></body>\r\n</html>\r\n"
            )
            (source / "js" / "app.js").write_bytes(b"window.TEST = true;\r\n")
            (source / "start.bat").write_text("echo local only\r\n", encoding="utf-8")
            output = root / "site"
            build_static_site(source, output, _config())
            self.assertNotIn(b"\r", (output / "index.html").read_bytes())
            self.assertNotIn(b"\r", (output / "js" / "app.js").read_bytes())
            self.assertFalse((output / "start.bat").exists())

    def test_manifest_duplicate_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            build_static_site(ROOT / "web", output, _config())
            manifest_path = output / "deployment-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"].append(dict(manifest["files"][0]))
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HybridH4Error, "duplicate"):
                verify_static_build(output)

    def test_failed_activation_restores_previous_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "site"
            build_static_site(ROOT / "web", output, _config())
            original_manifest = (output / "deployment-manifest.json").read_bytes()
            real_verify = verify_static_build

            def fail_after_activation(path, *, forbidden_values=()):
                if Path(path).resolve() == output.resolve():
                    raise HybridH4Error("injected activation verification failure")
                return real_verify(path, forbidden_values=forbidden_values)

            with mock.patch(
                "stock_tracker.deployment.hybrid_h4.verify_static_build",
                side_effect=fail_after_activation,
            ), self.assertRaisesRegex(HybridH4Error, "injected activation"):
                build_static_site(ROOT / "web", output, _config())
            self.assertEqual(
                (output / "deployment-manifest.json").read_bytes(),
                original_manifest,
            )
            self.assertTrue(real_verify(output)["passed"])

    def test_build_refuses_unapproved_static_file_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "web"
            source.mkdir()
            (source / "index.html").write_text(
                "<html><head></head><body></body></html>", encoding="utf-8"
            )
            (source / "private.pem").write_text(
                "-----BEGIN PRIVATE KEY-----\nfixture\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(HybridH4Error, "unapproved"):
                build_static_site(source, root / "output", _config())

    def test_build_refuses_symlink_source_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "web"
            source.mkdir()
            (source / "index.html").write_text(
                "<html><head></head><body></body></html>", encoding="utf-8"
            )
            target = root / "outside.txt"
            target.write_text("outside", encoding="utf-8")
            link = source / "linked.txt"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this host")
            with self.assertRaisesRegex(HybridH4Error, "symlink"):
                build_static_site(source, root / "output", _config())


    def test_github_pages_workflow_derives_origin_and_preserves_nojekyll(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "deploy-hybrid-h4-github-pages.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("uses: actions/checkout@v6", workflow)
        self.assertIn("uses: actions/setup-python@v6", workflow)
        self.assertIn("uses: actions/configure-pages@v5", workflow)
        self.assertIn("id: pages", workflow)
        self.assertIn(
            "STOCK_TRACKER_WEB_ORIGIN: ${{ steps.pages.outputs.origin }}",
            workflow,
        )
        self.assertIn("uses: actions/upload-pages-artifact@v5", workflow)
        self.assertIn("include-hidden-files: true", workflow)
        self.assertIn("uses: actions/deploy-pages@v4", workflow)
        self.assertNotIn("STOCK_TRACKER_PRIVATE_ACCESS", workflow)
        self.assertNotIn("STOCK_TRACKER_NEW_PRIVATE_ACCESS", workflow)


if __name__ == "__main__":
    unittest.main()
