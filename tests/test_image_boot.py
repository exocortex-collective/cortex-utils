"""The image must be able to start without the network.

`uv run` re-resolves the project on a container's first start and fetches
hatchling from pypi to do it, so every fresh container needed working DNS --
and a redeploy makes fresh ones. A container DNS failure on ares stopped
cortex-school starting on 2026-10-02; this image runs the ALERTER, so the same
failure here takes out the thing that would have reported it.

Measured on the built image with --network none before this was fixed:
    uv run cortex-utils --help          -> Failed to fetch pypi.org/simple/hatchling
    /app/.venv/bin/cortex-utils --help  -> prints the CLI usage
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent

# The venv's bin DIRECTORY, not `python` specifically: a console script baked
# into that venv needs no resolution at boot either, and pinning an
# interpreter name would refuse a legitimate /app/.venv/bin/<script>.
VENV_BIN = "/app/.venv/bin/"


def _joined(text: str) -> list[str]:
    """Join shell line-continuations, then split.

    A command split across lines is ONE command, and a per-line scan reads its
    halves as unrelated. cortex-school shipped that exact bug: its CI wrote
    `docker run ... \\` on one line and the command on the next, so a per-line
    check walked straight past the thing it existed to catch.
    """
    return re.sub(r"\\\n\s*", " ", text).splitlines()


def test_the_start_command_is_the_baked_venv():
    """Asserts the PROPERTY -- the start command IS the baked venv -- rather
    than forbidding a spelling. Blacklisting "uv run" does not work: `uvx`,
    `uv  run`, `uv<TAB>run` and a bare `python` all walk past it."""
    dockerfile = (REPO / "Dockerfile").read_text()
    # Case-insensitive and continuation-joined: a lowercase or wrapped
    # directive is still the start command.
    cmds = re.findall(
        r"^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", "\n".join(_joined(dockerfile)), re.M | re.I
    )
    assert cmds, "the Dockerfile declares no CMD or ENTRYPOINT"
    # Joined, so an ENTRYPOINT holding the interpreter and a CMD holding only
    # its arguments are judged as the one command they combine into.
    start = " ".join(cmds)
    assert VENV_BIN in start, (
        f"the start command must invoke the baked venv by its absolute path, "
        f"or it is resolving the project at boot: {start}"
    )
    # `\buvx?\b`, not the substring "uv": that would refuse `uvicorn` and a
    # script named like bin/uvloop_worker.py.
    assert not re.search(r"\buvx?\b", start), (
        f"the start command goes through uv, so it needs pypi at boot: {start}"
    )


def test_the_project_is_installed_after_its_source_is_copied():
    """The reason `uv run` was rebuilding at boot in the first place.

    `uv sync` ran BEFORE `COPY src/`, so the project was installed against a
    source tree that did not exist yet and uv found it "not fresh" on every
    first start. Pointing the entrypoint at the venv without fixing this
    yields `ModuleNotFoundError: No module named 'cortex_utils'` -- measured.
    """
    lines = (REPO / "Dockerfile").read_text().splitlines()

    def first_index(pattern: str) -> int:
        for i, line in enumerate(lines):
            if re.search(pattern, line):
                return i
        raise AssertionError(f"no Dockerfile line matches {pattern!r}")

    copy_src = first_index(r"^\s*COPY\s+src/")
    syncs = [i for i, line in enumerate(lines) if re.search(r"^\s*RUN\s+uv\s+sync", line)]
    assert syncs, "the Dockerfile never runs `uv sync`"
    assert any(i > copy_src for i in syncs), (
        "every `uv sync` runs before `COPY src/`, so the project is installed "
        "against a source tree that does not exist yet and the venv is "
        "incomplete at build time"
    )


def test_ci_proves_the_image_starts_with_no_network():
    """A smoke that gives the container a network cannot catch this.

    Select on `docker run`, NOT on an image tag: `docker build -t utils:ci .`
    carries the tag too, and cortex-school's tag-based filter was satisfied by
    the BUILD step -- it stayed green with both smokes replaced by `echo hi`.
    """
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    runs = [c for c in _joined(ci) if "docker run" in c]
    assert runs, (
        "no command in ci.yml runs the image, so nothing proves it can start; "
        "did the image smoke move or get deleted?"
    )
    for cmd in runs:
        assert "--network none" in cmd, (
            f"the CI image smoke gives the container a network, so it cannot "
            f"catch a boot that needs one: {cmd.strip()}"
        )
