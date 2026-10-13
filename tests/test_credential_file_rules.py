"""Check secret-file ignore rules without reading any real credential file."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CredentialFileRulesTests(unittest.TestCase):
    def test_private_paths_ignored_and_public_template_not_ignored(self):
        # The workspace Git metadata is unavailable in this environment. Test
        # the actual rules in a disposable repository, never alter workspace Git.
        with tempfile.TemporaryDirectory(prefix="neuraflow-ignore-check-") as directory:
            subprocess.run(["git", "init", "--quiet", directory], check=True,
                           capture_output=True, text=True)
            shutil.copyfile(ROOT / ".gitignore", Path(directory) / ".gitignore")
            private_paths = [
                "config/local/providers.env", "config/local/providers.env.bak",
                "config/local/quickcommerce.env", "config/local/quickcommerce.env.bak",
                "config/local/nested/credentials.txt", ".env", ".env.local",
                "config/local/apify_binding.json", "data/benchmarks/state/apify/events.jsonl",
                "data/history/snapshot.json", "media/live/run/image.jpg",
                "outputs/live/run/report.pdf", "media/reviews/live/run/page.png",
            ]
            result = subprocess.run(
                ["git", "-c", "core.excludesFile=/dev/null", "check-ignore",
                 "--no-index", "--stdin"],
                input="\n".join(private_paths + ["config/providers.env.example", "config/quickcommerce.env.example"]) + "\n",
                cwd=directory, capture_output=True, text=True, check=True,
            )
            self.assertEqual(set(result.stdout.splitlines()), set(private_paths))

    def test_public_template_has_no_apify_key(self):
        lines = (ROOT / "config/providers.env.example").read_text(encoding="utf-8").splitlines()
        settings = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(settings, ["APIFY_TOKEN="])

    def test_public_template_has_no_commerce_key(self):
        lines = (ROOT / "config/quickcommerce.env.example").read_text(encoding="utf-8").splitlines()
        settings = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(settings, ["QUICKCOMMERCE_API_KEY="])


if __name__ == "__main__":
    unittest.main()
