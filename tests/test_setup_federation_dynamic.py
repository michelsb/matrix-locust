import csv
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup_federation"


def federation_env(tmp_path, homeservers):
    env = os.environ.copy()
    env["FED_HOMESERVERS"] = ",".join(homeservers)
    env["MATRIX_DATA_DIR"] = str(tmp_path)
    for index, key in enumerate(homeservers, start=1):
        env_key = key.upper().replace("-", "_")
        env[f"FED_{env_key}_URL"] = f"https://client{index}.example.com"
        env[f"FED_{env_key}_DOMAIN"] = f"server{index}.example.com"
        env[f"FED_{env_key}_PREFIX"] = f"user{index}"
    return env


def run_generate_users(tmp_path, homeservers, total):
    output = tmp_path / "users.csv"
    return subprocess.run(
        [
            sys.executable,
            str(SETUP / "01_generate_users.py"),
            str(total),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=federation_env(tmp_path, homeservers),
        capture_output=True,
        text=True,
    ), output


def test_requires_at_least_two_homeservers(tmp_path):
    result, _ = run_generate_users(tmp_path, ["only"], 3)

    assert result.returncode != 0
    assert "pelo menos 2 homeservers" in result.stderr


def test_rejects_keys_that_collide_as_environment_variables(tmp_path):
    result, _ = run_generate_users(tmp_path, ["sao-paulo", "sao_paulo"], 4)

    assert result.returncode != 0
    assert "colidem nas variáveis" in result.stderr


def test_supports_three_homeservers_without_dropping_remainder(tmp_path):
    result, output = run_generate_users(tmp_path, ["alpha", "beta", "gamma"], 8)

    assert result.returncode == 0, result.stderr
    with output.open(newline="", encoding="utf-8") as csvfile:
        usernames = [row["username"] for row in csv.DictReader(csvfile)]

    assert len(usernames) == 8
    assert sum(name.startswith("user1.") for name in usernames) == 3
    assert sum(name.startswith("user2.") for name in usernames) == 3
    assert sum(name.startswith("user3.") for name in usernames) == 2
