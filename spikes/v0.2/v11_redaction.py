"""Spike V11, re-measured in C3 (design §6.8): what the redactor adds to a turn.

``shape_outputs`` end to end, with redaction off (an identity redactor), patterns only (nothing
installed) and a project with a ~30-value ``.env``, on:

- a 5 MB bundle: a 5 MB stream, a ~5 MB base64 PNG and two 2 KB outputs;
- a 50 MB stream;
- a 20 MB image bundle: four ~5 MB base64 PNGs and a short stream;
- 50 MB streams of other line shapes (review of C3): ``key=value`` logs full of token keys, JSON
  records, NLP tokens, e-mails and URLs, a secret token on every line, and an ``.env`` value on
  every line (the worst cases for the patterns and the value search).

Every text holds planted secrets (``.env`` values and one of each pattern); each run checks that
none of them reaches the shaped text or the full copy. p50/p95 in ms.

``--redactor-only`` times ``Redactor.redact`` alone on the same texts, for the hooks' and nhctl's
Python (``shape_outputs`` needs the gateway's venv, with Pillow).

Run from the repo root:

    PYTHONPATH=plugins/nh/server/src uv run --project plugins/nh/server python spikes/v0.2/v11_redaction.py
    PYTHONPATH=plugins/nh/server/src /usr/bin/python3 spikes/v0.2/v11_redaction.py --redactor-only
"""

from __future__ import annotations

import argparse
import base64
import io
import random
import shutil
import statistics
import string
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nh_gateway._shared import secrets

RUNS = {"5 MB bundle": 20, "50 MB stream": 10, "20 MB image bundle": 10}
LINE = "epoch 12/100 - loss: 0.2345 - acc: 0.9123 - see https://example.com/x?a=1\n"
# 50 MB streams of one line each; "{value}" is the longest .env value (a shorter one, whose
# marker is longer than it, would grow the full copy past prune_outputs_dir's 50 MiB).
PROFILES = {
    "token-kv": "step=1 max_token=512 pad_token=0 loss=0.25 token='the' id=12\n",
    "json-records": '{"id": 12, "user": "a@b.co", "token": "abc12345", "score": 0.5}\n',
    "nlp-tokens": '{"idx": 1, "token": "the", "pos": "DET", "head": 2}\n',
    "emails+urls": "user_12345@example.com visited https://shop.example.com/cart?id=12345\n",
    "token-dense": "token=hf_AbCdEf123456GhIjKlMn step=3\n",
    "value-dense": "login db as etl with {value} ok\n",
}
RUNS.update({f"50 MB {name}": 5 for name in PROFILES})


def fake(rng: random.Random, size: int) -> str:
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(size))


def env_values(rng: random.Random) -> list[tuple[str, str]]:
    """~30 secret-named values of 16-45 chars, as a project's .env holds them."""
    names = ["DB_PASSWORD", "API_TOKEN", "AWS_SECRET_ACCESS_KEY", "WAREHOUSE_PASSWORD"]
    names += [f"SERVICE_{i}_API_KEY" for i in range(26)]
    return [(name, fake(rng, 16 + i)) for i, name in enumerate(names)]


def planted(rng: random.Random, values: list[tuple[str, str]]) -> list[str]:
    """One of each pattern, in the form the redactor must catch (built at run time)."""
    return [
        "token=" + fake(rng, 20),
        "Authorization: Bearer " + fake(rng, 32),
        "AKIA" + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(16)),
        "ghp_" + fake(rng, 36),
        "sk-" + "A1" + fake(rng, 30),
        "xoxb-" + fake(rng, 24),
        "postgresql://app:" + fake(rng, 18) + "@db/prod",
        "-----BEGIN RSA PRIVATE KEY-----\n" + fake(rng, 64) + "\n-----END RSA PRIVATE KEY-----",
        values[0][1],
        values[len(values) // 2][1],
        values[-1][1],
    ]


def secret_parts(secret: str) -> str:
    """The part that must never survive (``token=`` and ``@db/prod`` stay visible)."""
    for lead in ("token=", "Authorization: Bearer "):
        if secret.startswith(lead):
            return secret[len(lead) :]
    if secret.startswith("postgresql://"):
        return secret.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    if secret.startswith("-----BEGIN"):
        return secret.splitlines()[1]
    return secret


def stream_text(size: int, plants: list[str], line: str = LINE) -> str:
    body = line * (size // len(line))
    step = max(1, len(body) // (len(plants) + 1))
    parts, at = [], 0
    for secret in plants:
        parts.append(body[at : at + step])
        parts.append(f" {secret} ")
        at += step
    parts.append(body[at:])
    return "".join(parts)


def png_b64(rng: random.Random, target_chars: int) -> str:
    """A real PNG of noise (it barely compresses), ~target_chars of base64."""
    from PIL import Image

    side = int((target_chars * 3 / 4 / 3) ** 0.5)
    image = Image.frombytes("RGB", (side, side), rng.randbytes(side * side * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=1)
    return base64.b64encode(buffer.getvalue()).decode()


def scenarios(
    rng: random.Random, plants: list[str], images: bool, value: str
) -> dict[str, list[dict]]:
    small = stream_text(2000, plants[:2])
    out = {
        "5 MB bundle": [
            {"output_type": "stream", "name": "stdout", "text": stream_text(5_000_000, plants)},
            {"output_type": "stream", "name": "stderr", "text": small},
            {
                "output_type": "execute_result",
                "execution_count": 1,
                "metadata": {},
                "data": {"text/plain": small},
            },
        ],
        "50 MB stream": [
            {"output_type": "stream", "name": "stdout", "text": stream_text(50_000_000, plants)}
        ],
        "20 MB image bundle": [
            {"output_type": "stream", "name": "stdout", "text": stream_text(2000, plants)}
        ],
    }
    for name, line in PROFILES.items():
        text = stream_text(50_000_000, plants, line.replace("{value}", value))
        out[f"50 MB {name}"] = [{"output_type": "stream", "name": "stdout", "text": text}]
    if images:
        out["5 MB bundle"].insert(1, image_output(png_b64(rng, 5_000_000), plants[0]))
        out["20 MB image bundle"] += [
            image_output(png_b64(rng, 5_000_000), plants[i]) for i in range(4)
        ]
    return out


def image_output(data: str, secret: str) -> dict:
    return {
        "output_type": "display_data",
        "metadata": {},
        "data": {"image/png": data, "text/plain": f"<Figure 1118x1118 {secret}>"},
    }


def text_of(outputs: list[dict]) -> str:
    """What the redactor scans in a bundle: streams and text/plain, never image base64."""
    parts = []
    for out in outputs:
        if out["output_type"] == "stream":
            parts.append(out["text"])
        else:
            parts.append(out["data"].get("text/plain", ""))
    return "\n".join(parts)


class Identity:
    """Redaction off: the v0.1.1 baseline."""

    margin = 1024

    def redact(self, text: str) -> str:
        return text

    def redact_head(self, text: str, limit: int) -> str:
        return text


def timed(fn: Callable[[], Any], runs: int, after: Callable[[], Any] = lambda: None):
    """p50/p95 in ms; ``after`` runs outside the clock (it drops the run's files)."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        times.append((time.perf_counter() - start) * 1000)
        after()
    times.sort()
    p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
    return statistics.median(times), p95


def redactors(project: Path, values: list[tuple[str, str]]) -> dict[str, Any]:
    (project / ".env").write_text("".join(f"{name}={value}\n" for name, value in values))
    return {
        "off": Identity(),
        "patterns only": secrets.PATTERNS_ONLY,
        "30-value .env": secrets.Redactor.for_project(project),
    }


def check(text: str, plants: list[str], where: str) -> None:
    """No planted secret survives; patterns only knows no .env values (the last three)."""
    if "patterns only" in where:
        plants = plants[:-3]
    leaked = [s for s in plants if secret_parts(s) in text]
    assert not leaked, (where, [s[:12] for s in leaked])


def shape_runs(outputs_by_case: dict[str, list[dict]], modes: dict[str, Any], plants: list[str]):
    from nh_gateway.exec import shaping

    original = secrets.current
    rows = []
    try:
        for case, outputs in outputs_by_case.items():
            for mode, redactor in modes.items():
                secrets.current = lambda redactor=redactor: redactor  # type: ignore[assignment]
                with tempfile.TemporaryDirectory() as tmp:
                    # A fresh directory each run, so every run writes its copies.
                    save = Path(tmp) / "outputs"

                    def once(outputs: list[dict] = outputs, save: Path = save) -> Any:
                        return shaping.shape_outputs(
                            outputs, max_chars=2000, max_images=2, image_max_px=768, save_dir=save
                        )

                    def drop(save: Path = save) -> None:
                        shutil.rmtree(save, ignore_errors=True)

                    shaped = once()
                    copies = list(save.glob("*.txt"))
                    assert copies, (case, mode, "no full copy")
                    if mode != "off":
                        check(shaped.text, plants, f"{case} {mode} text")
                        for copy in copies:
                            check(copy.read_text(), plants, f"{case} {mode} full copy")
                    drop()
                    rows.append((case, mode, *timed(once, RUNS[case], drop)))
    finally:
        secrets.current = original  # type: ignore[assignment]
    return rows


def redact_runs(outputs_by_case: dict[str, list[dict]], modes: dict[str, Any], plants: list[str]):
    rows = []
    for case, outputs in outputs_by_case.items():
        text = text_of(outputs)
        for mode, redactor in modes.items():
            if mode == "off":
                continue
            check(redactor.redact(text), plants, f"{case} {mode}")
            rows.append((case, mode, *timed(lambda r=redactor, t=text: r.redact(t), RUNS[case])))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--redactor-only", action="store_true", help="time Redactor.redact alone")
    args = parser.parse_args()
    rng = random.Random(11)
    values = env_values(rng)
    plants = planted(rng, values)
    cases = scenarios(rng, plants, images=not args.redactor_only, value=values[-1][1])
    with tempfile.TemporaryDirectory() as tmp:
        modes = redactors(Path(tmp), values)
        run = redact_runs if args.redactor_only else shape_runs
        rows = run(cases, modes, plants)
    what = "Redactor.redact" if args.redactor_only else "shape_outputs"
    print(f"Python {sys.version.split()[0]}: {what}, p50/p95 ms")
    for case, mode, p50, p95 in rows:
        print(f"  {case:<20} {mode:<14} {p50:8.1f} / {p95:8.1f}  ({RUNS[case]} runs)")


if __name__ == "__main__":
    main()
