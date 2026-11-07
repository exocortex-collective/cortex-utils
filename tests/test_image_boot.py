"""The image must be able to start without the network.

`uv run` re-resolves the project on a container's first start and fetches
hatchling from pypi to do it, so every fresh container needs working DNS --
and a redeploy, or an ofelia job with `delete: true`, makes fresh ones. This
image runs the alerter, so the same failure takes out the thing that would
have reported it.

Measured from a git-archive build, so the file modes are the ones CI
sees, with --network none:

    pre-fix image,  user=root      -> dns error fetching hatchling
    pre-fix image,  user=appuser   -> same
    fixed image,    user=appuser   -> prints the CLI usage

The pre-fix image also failed when its entrypoint was pointed straight at
/app/.venv/bin/cortex-utils, with ModuleNotFoundError, because `uv sync` ran
before `COPY src/`. That ordering is what these tests pin.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent

# The venv's bin DIRECTORY, not `python`: a console script baked into that venv
# needs no resolution at boot either, and pinning an interpreter name would
# refuse a legitimate /app/.venv/bin/<script>.
VENV_BIN = "/app/.venv/bin/"

# The image tag the CI smoke builds. Commands that run THIS are the ones that
# must have no network; a future `docker run alpine ...` is none of our business.
CI_IMAGE = "utils:ci"


def _joined(text: str) -> list[str]:
    """Join shell line-continuations, then split.

    A command split across lines is ONE command, and a per-line scan reads its
    halves as unrelated.
    """
    return re.sub(r"\\\n\s*", " ", text).splitlines()


def _without_comment(line: str) -> str:
    """Drop a trailing `# ...`, so a flag named in a comment does not satisfy
    a check about the command."""
    return line.split("#", 1)[0]


def test_the_start_command_is_the_baked_venv():
    """Asserts the PROPERTY -- the start command IS the baked venv -- rather
    than forbidding a spelling. Blacklisting "uv run" does not work: `uvx`,
    `uv  run`, `uv<TAB>run` and a bare `python` all walk past it."""
    dockerfile = (REPO / "Dockerfile").read_text()
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
    # `\buvx?\b`, not the substring "uv": that would refuse `uvicorn`.
    assert not re.search(r"\buvx?\b", start), (
        f"the start command goes through uv, so it needs pypi at boot: {start}"
    )


def test_the_project_itself_is_installed_after_its_source_is_copied():
    """The reason `uv run` was rebuilding at boot in the first place.

    `uv sync` ran BEFORE `COPY src/`, so the project was installed against a
    source tree that did not exist yet. Pointing the entrypoint at the venv
    without fixing this yields ModuleNotFoundError.

    A sync after the COPY is not enough on its own: one carrying
    `--no-install-project` reproduces the bug exactly, and an earlier version
    of this test accepted it.
    """
    lines = (REPO / "Dockerfile").read_text().splitlines()

    def first_index(pattern: str) -> int:
        for i, line in enumerate(lines):
            if re.search(pattern, line):
                return i
        raise AssertionError(f"no Dockerfile line matches {pattern!r}")

    copy_src = first_index(r"^\s*COPY\s+src/")
    installing = [
        i
        for i, line in enumerate(lines)
        if re.search(r"^\s*RUN\s+uv\s+sync", line) and "--no-install-project" not in line
    ]
    assert installing, (
        "the Dockerfile never runs a `uv sync` that installs the project "
        "itself -- every one carries --no-install-project"
    )
    assert any(i > copy_src for i in installing), (
        "every project-installing `uv sync` runs before `COPY src/`, so the "
        "project is installed against a source tree that does not exist yet "
        "and the venv is incomplete at build time"
    )


def test_ci_runs_the_image_with_no_network():
    """A smoke that gives the container a network cannot catch this.

    Select on `docker run` of OUR image, not on any `docker run`: a future
    probe against some other image legitimately needs a network. And not on
    the tag alone -- `docker build -t utils:ci .` carries the tag too.
    """
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    runs = [
        c for c in (_without_comment(x) for x in _joined(ci)) if "docker run" in c and CI_IMAGE in c
    ]
    assert runs, (
        f"no command in ci.yml runs {CI_IMAGE}, so nothing proves the image "
        f"can start; did the smoke move or get deleted?"
    )
    for cmd in runs:
        # `--network[= ]none`: both spellings work in docker, so refusing the
        # equals form would be a false positive on a working command.
        assert re.search(r"--network[= ]none", cmd), (
            f"the CI image smoke gives the container a network, so it cannot "
            f"catch a boot that needs one: {cmd.strip()}"
        )
        assert "--user" not in cmd, (
            f"the smoke overrides the image's declared user, so it would not "
            f"catch a regression to a root-only image: {cmd.strip()}"
        )


def test_every_smoke_has_a_failure_handler():
    """Pins the pass condition, not just the command.

    A smoke whose exit status is swallowed, or whose output is never checked,
    selects the right lines and asserts nothing that can fail.

    Everything here is checked on COMMENT-STRIPPED lines and scoped to the
    smoke step. An earlier version matched these strings anywhere in the file,
    so a commented-out grep satisfied it, and one step's `exit 1` satisfied the
    requirement for another's.
    """
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    live = [_without_comment(x) for x in _joined(ci)]
    smoke = [c for c in live if "docker run" in c and CI_IMAGE in c]
    assert smoke, f"no {CI_IMAGE} smoke to check"

    for cmd in smoke:
        # Every spelling of "ignore the exit status", not just `|| true`.
        assert not re.search(r"\|\|\s*(true|:)\s*(;|$)", cmd), (
            f"this smoke swallows the container's exit status, so a container "
            f"that cannot start would not fail the job: {cmd.strip()}"
        )
        # Each smoke needs its OWN handler, on its own joined command.
        assert "exit 1" in cmd, (
            f"this smoke has no failure handler, so a container that cannot "
            f"start would not fail the job: {cmd.strip()}"
        )

    # And something must assert on what was printed, or an image that exits 0
    # without running the CLI passes. On a live line, not in a comment.
    assert any('grep -q "Cortex operational utilities"' in c for c in live), (
        "nothing live checks the smoke's OUTPUT, so an image that exits 0 "
        "without running the CLI would pass"
    )

    # UV_OFFLINE=1 makes even the pre-fix image boot offline, from the warm uv
    # cache the build leaves in /root. Setting it here would silently retire
    # this whole smoke.
    assert not any("UV_OFFLINE" in c for c in live), (
        "the CI step sets UV_OFFLINE, which lets `uv run` resolve from the "
        "build's warm cache -- the smoke would stop discriminating"
    )
