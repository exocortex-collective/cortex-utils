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

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# The one correct start command. Asserted whole rather than probed for
# properties: there is exactly one right answer, so equality is both the
# simplest assertion and the strongest. It rejects a boot-time `pip install`,
# a `python -c` that shells out, and a `/app/.venv/bin/../../..` path that
# escapes the venv, without needing to name any of them.
EXPECTED_START = ["/app/.venv/bin/cortex-utils"]

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


def _effective_argv(dockerfile: str) -> list[str]:
    """The argv docker would start the container with, from this file alone.

    Three rules, each measured against `docker inspect` rather than read off
    the docs:

    1. Only the LAST ENTRYPOINT and the LAST CMD count; an earlier one of
       either is dead text.
    2. Only the final build stage ships, so an earlier stage's CMD is not ours.
    3. An instruction whose argument is a valid JSON array is exec form;
       ANYTHING ELSE is shell form and becomes /bin/sh -c <the raw text>. That
       includes an exec-looking array with a trailing comment, because docker
       treats `#` as a comment only at the start of a line.

    Resolution is then Entrypoint + Cmd, with no special case: for a
    shell-form ENTRYPOINT docker still keeps the CMD and passes it as $0 of
    the shell, so it never reaches the command -- but it is in the argv.

    Only what the Dockerfile DECLARES; an inherited CMD or ENTRYPOINT from the
    base image is not visible here.
    """
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    joined = joined[joined.rfind("\nFROM ") + 1 :] if "\nFROM " in joined else joined

    def last(kind: str) -> str | None:
        # re.IGNORECASE: docker instruction keywords are case-insensitive.
        found = re.findall(rf"^\s*{kind}\s+(.+)$", joined, re.MULTILINE | re.IGNORECASE)
        return found[-1].strip() if found else None

    def parse(raw: str) -> list[str]:
        try:
            argv = json.loads(raw)
        except json.JSONDecodeError:
            return ["/bin/sh", "-c", raw]
        # A JSON value that is not an array is shell form: measured, docker
        # reads CMD "a", CMD 42 and CMD {"a": 1} all as /bin/sh -c <raw>.
        return argv if isinstance(argv, list) else ["/bin/sh", "-c", raw]

    return [a for part in (last("ENTRYPOINT"), last("CMD")) if part for a in parse(part)]


def test_the_container_starts_with_the_baked_venv():
    """The whole point: the start command must be the console script already
    built into the image, so nothing is resolved or fetched at boot.

    This replaced a substring check for "/app/.venv/bin/" anywhere in the
    command, which passed an `sh -c` wrapper and a trailing `#` comment. The
    rules above and the cases at the bottom came from cortex-triage #97.
    """
    argv = _effective_argv((REPO / "Dockerfile").read_text())
    assert argv, "the Dockerfile declares no CMD or ENTRYPOINT"
    assert argv == EXPECTED_START, (
        f"the container's start command is not the baked venv, so it resolves "
        f"the project at boot.\n  expected: {EXPECTED_START}\n  found:    {argv}"
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


# The rules in _effective_argv are exercised by exactly ONE branch of the real
# Dockerfile (one stage, an exec-form ENTRYPOINT, no CMD), so mutating any of
# the others away leaves every test above green. These are the regression test
# for the rules themselves.
_RIGHT = json.dumps(EXPECTED_START)
_WRONG = json.dumps(["cortex-utils"])
_VIA_UV = json.dumps(["uv", "run"] + EXPECTED_START)

START_CASES = [
    ("the exec-form start command", f"ENTRYPOINT {_RIGHT}", True),
    (
        "a stale earlier ENTRYPOINT with a correct final one",
        f"ENTRYPOINT {_VIA_UV}\nENTRYPOINT {_RIGHT}",
        True,
    ),
    ("a lowercase entrypoint keyword", f"entrypoint {_RIGHT}", True),
    (
        "the correct ENTRYPOINT followed by a wrong one",
        f"ENTRYPOINT {_RIGHT}\nENTRYPOINT {_WRONG}",
        False,
    ),
    ("the command run through uv", f"ENTRYPOINT {_VIA_UV}", False),
    ("a bare console script from PATH", f"ENTRYPOINT {_WRONG}", False),
    (
        "a shell wrapper hiding an install",
        'ENTRYPOINT ["sh", "-c", "pip install -e . && x"]',
        False,
    ),
    ("the right argv with a trailing comment", f"ENTRYPOINT {_RIGHT} # baked venv", False),
    ("a lone shell-form ENTRYPOINT", "ENTRYPOINT " + " ".join(EXPECTED_START), False),
    ("a CMD appended to the correct ENTRYPOINT", f'ENTRYPOINT {_RIGHT}\nCMD ["extra"]', False),
]

# Rules 1-3 as a docker MODEL, asserted on the resolved argv itself. Each
# expectation was taken from `docker inspect` on a busybox build.
RESOLUTION_CASES = [
    ("only the last CMD counts", 'CMD ["a"]\nCMD ["b"]', ["b"]),
    (
        "only the last ENTRYPOINT counts",
        'ENTRYPOINT ["a"]\nENTRYPOINT ["b"]\nCMD ["c"]',
        ["b", "c"],
    ),
    ("CMD supplies arguments to an exec ENTRYPOINT", 'ENTRYPOINT ["a"]\nCMD ["b"]', ["a", "b"]),
    (
        "a shell-form ENTRYPOINT keeps the CMD as $0",
        'ENTRYPOINT a b\nCMD ["c"]',
        ["/bin/sh", "-c", "a b", "c"],
    ),
    ("a shell-form CMD runs through the shell", "CMD a b", ["/bin/sh", "-c", "a b"]),
    (
        "an exec ENTRYPOINT with a shell-form CMD",
        'ENTRYPOINT ["a"]\nCMD b c',
        ["a", "/bin/sh", "-c", "b c"],
    ),
    ("a trailing comment is not a comment", 'CMD ["a"] # note', ["/bin/sh", "-c", '["a"] # note']),
    ("only the final build stage counts", 'FROM x\nCMD ["a"]\nFROM y\nCMD ["b"]', ["b"]),
    ("an earlier stage's CMD does not carry over", 'FROM x\nCMD ["a"]\nFROM y\nRUN true', []),
    ("trailing whitespace is trimmed", "CMD a b   ", ["/bin/sh", "-c", "a b"]),
    ("a bare JSON string", 'CMD "a"', ["/bin/sh", "-c", '"a"']),
    ("a JSON number", "CMD 42", ["/bin/sh", "-c", "42"]),
    ("a JSON object", 'CMD {"a": 1}', ["/bin/sh", "-c", '{"a": 1}']),
    ("neither instruction", "RUN true", []),
]


@pytest.mark.parametrize(
    "body,is_baked", [(b, ok) for _, b, ok in START_CASES], ids=[n for n, _, _ in START_CASES]
)
def test_the_start_command_rules(body: str, is_baked: bool):
    dockerfile = "FROM python:3.13-slim\n" + body + "\n"
    assert (_effective_argv(dockerfile) == EXPECTED_START) is is_baked


@pytest.mark.parametrize(
    "body,argv", [(b, a) for _, b, a in RESOLUTION_CASES], ids=[n for n, _, _ in RESOLUTION_CASES]
)
def test_the_resolution_rules(body: str, argv: list[str]):
    assert _effective_argv("FROM python:3.13-slim\n" + body + "\n") == argv
