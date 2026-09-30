from __future__ import annotations

from pathlib import Path

import responses
import yaml

from ansible_lockfile import main as cli_main


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "integration"
    / "data"
    / "success"
    / "single-collection"
)


@responses.activate
def test_cli_single_collection(tmp_path: Path, monkeypatch):
    base = "https://galaxy.ansible.com"
    monkeypatch.chdir(tmp_path)

    infile = FIXTURE / "ansible.in.yaml"
    expected = yaml.safe_load((FIXTURE / "expected.lock.yaml").read_text(encoding="utf-8"))

    responses.add(
        responses.GET,
        f"{base}/api/v3/plugin/ansible/content/published/collections/index/community/general/versions/13.3.0/",
        json={
            "version": "13.3.0",
            "download_url": (
                "https://galaxy.ansible.com/api/v3/plugin/ansible/content/"
                "published/collections/artifacts/community-general-13.3.0.tar.gz"
            ),
            "artifact": {
                "filename": "community-general-13.3.0.tar.gz",
                "sha256": "46506254911a675da601abe0adce1bc5d00886ec9c6edb71e930244db7ed9f4d",
                "size": 2825915,
            },
            "metadata": {"dependencies": {}},
            "namespace": {"name": "community"},
            "name": "general",
        },
    )

    outfile = tmp_path / "ansible.lock.yaml"
    generic = tmp_path / "artifacts.lock.yaml"
    rc = cli_main(
        [
            str(infile),
            f"--outfile={outfile}",
            f"--export-generic={generic}",
        ]
    )
    assert rc == 0
    got = yaml.safe_load(outfile.read_text(encoding="utf-8"))
    assert got == expected

    generic_data = yaml.safe_load(generic.read_text(encoding="utf-8"))
    assert generic_data["metadata"]["version"] == "1.0"
    assert generic_data["artifacts"][0]["checksum"].startswith("sha256:")


def test_cli_print_schema():
    assert cli_main(["--print-schema"]) == 0
