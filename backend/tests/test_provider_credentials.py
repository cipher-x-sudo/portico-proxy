import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from provider_auth import load_provider_auth  # noqa: E402
import gateway  # noqa: E402


class ProviderCredentialResolutionTests(unittest.TestCase):
    def test_db_credentials_win_over_auth_txt(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "NC"
            provider.mkdir()
            (provider / "profile.ovpn").write_text("client\n", encoding="utf-8")
            (provider / "auth.txt").write_text("file-user\nfile-pass\n", encoding="utf-8")

            result = load_provider_auth(
                "NC/profile.ovpn",
                root,
                {"NC": {"username": "db-user", "password": "db-pass"}},
            )

            self.assertEqual(result.provider, "NC")
            self.assertEqual(result.username, "db-user")
            self.assertEqual(result.password, "db-pass")

    def test_auth_txt_fallback_still_works_without_db_credentials(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "NC"
            provider.mkdir()
            (provider / "profile.ovpn").write_text("client\n", encoding="utf-8")
            (provider / "auth.txt").write_text("file-user\nfile-pass\n", encoding="utf-8")

            result = load_provider_auth("NC/profile.ovpn", root, {})

            self.assertEqual(result.provider, "NC")
            self.assertEqual(result.username, "file-user")
            self.assertEqual(result.password, "file-pass")

    def test_missing_credentials_reports_all_fallbacks(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "NC"
            provider.mkdir()
            (provider / "profile.ovpn").write_text("client\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "provider credentials table"):
                load_provider_auth("NC/profile.ovpn", root, {})

    def test_upload_without_auth_file_for_db_mode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = gateway.save_ovpn_upload_batch(
                root,
                "NC",
                "",
                "",
                [{"filename": "profile.ovpn", "data": b"client\n"}],
                write_auth_file=False,
            )

            self.assertTrue((root / "NC" / "profile.ovpn").is_file())
            self.assertFalse((root / "NC" / "auth.txt").exists())
            self.assertEqual(result["credentialsStoredIn"], "none")


if __name__ == "__main__":
    unittest.main()
