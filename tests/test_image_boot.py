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

# The one place the container is actually run. Parameterised by tag so the
# negative control can run it against a broken image; see the second test.
SMOKE = "ci/smoke.sh"

# The real image, and the deliberately broken one the control builds.
CI_IMAGE = "utils:ci"
BROKEN_IMAGE = "utils:broken"


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
    `--no-install-project` reproduces the bug exactly.
    """
    # _joined, not splitlines: a `uv sync` whose --no-install-project sits on
    # a continuation line reads as a project-installing sync to a per-line
    # scan, which is exactly the case this test exists to refuse.
    lines = _joined((REPO / "Dockerfile").read_text())

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


def test_the_boot_probe_runs_the_image_with_no_network():
    """A probe that gives the container a network cannot catch this."""
    script = (REPO / SMOKE).read_text()
    runs = [c for c in (_without_comment(x) for x in _joined(script)) if "docker run" in c]
    assert runs, f"{SMOKE} runs no container, so nothing proves the image starts"
    for cmd in runs:
        # `--network[= ]none`: both spellings work in docker, so refusing the
        # equals form would be a false positive on a working command.
        assert re.search(r"--network[= ]none", cmd), (
            f"the boot probe gives the container a network, so it cannot "
            f"catch a boot that needs one: {cmd.strip()}"
        )
        assert "--user" not in cmd, (
            f"the probe overrides the image's declared user, so it would not "
            f"catch a regression to a root-only image: {cmd.strip()}"
        )

    # The exit status alone is not enough, and this half is NOT covered by the
    # negative control: the broken image dies at exec, so it is rejected
    # whether or not the output is ever checked. An image that exits 0 without
    # running the CLI is what this pins.
    assert 'grep -q "Cortex operational utilities"' in script, (
        f"{SMOKE} never checks what the container PRINTED, so an image that "
        f"exits 0 without running the CLI would pass the probe"
    )

    # Measured: with `set +e` the probe still rejects the broken image, because
    # its LAST command fails there -- so the negative control does not catch
    # this one. It is the probe's only failure mechanism; there are no
    # per-command handlers to fall back on.
    # On a comment-stripped DIRECTIVE, not the file text: the script explains
    # itself in a comment that mentions set -e, which satisfies a naive
    # substring check while `set +e` is in force.
    directives = [_without_comment(x).strip() for x in _joined(script)]
    assert any(re.match(r"set\s+-[a-z]*e", d) for d in directives), (
        f"{SMOKE} does not abort on error, so a failing check before the last "
        f"one would not fail the probe"
    )


def test_the_negative_control_runs_the_same_probe():
    """The probe takes its tag as an argument so the control can run that
    exact script against a deliberately broken image.
    """
    live = [_without_comment(x) for x in _joined((REPO / ".github/workflows/ci.yml").read_text())]

    tags = {m for c in live if SMOKE in c for m in re.findall(r"utils:\S+", c)}
    assert {CI_IMAGE, BROKEN_IMAGE} <= tags, (
        f"ci.yml must run {SMOKE} against both {CI_IMAGE} and {BROKEN_IMAGE}, "
        f"or the control does not exercise the probe itself; found {tags or 'none'}"
    )

    # And the image must not be run anywhere else. A second, inline definition
    # of "it boots" is one the control does not cover.
    stray = [c.strip() for c in live if re.search(r"docker\s+(?:container\s+)?run", c)]
    assert not stray, (
        f"ci.yml runs a container directly rather than through {SMOKE}, so "
        f"that check is outside the negative control: {stray}"
    )
