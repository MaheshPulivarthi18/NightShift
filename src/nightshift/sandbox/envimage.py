"""Environment images: the base sandbox image plus the system packages and toolchains one repo needs.

The Setup agent asks for packages and tools by name; NightShift writes the Dockerfile. A repo's own
Dockerfile is never built, because `docker build` runs its steps outside the sandbox's limits (as root,
with every capability). Package names are checked against Debian's naming rules and tools come from a
fixed list with pinned versions, so nothing the agent writes becomes a shell command at build time.
"""

from __future__ import annotations

import hashlib
import re
import shlex

from nightshift.config import get_settings
from nightshift.sandbox.docker import NETWORK_ERROR, SandboxError, docker_shown, image_exists

APT_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,62}$")  # Debian package names (no version pins or options)
MAX_PACKAGES = 40

# Toolchains the base image lacks, installed as root at build time. Python, Node, Bun, uv, git and a C/C++
# compiler are in the base already; other Python versions come from `uv python install` in the setup.
TOOLS: dict[str, str] = {
    "pnpm": "npm install -g --no-fund --no-audit pnpm@9.15.4 && npm cache clean --force",
    "yarn": "npm install -g --no-fund --no-audit yarn@1.22.22 && npm cache clean --force",
    "go": 'curl -fsSL "https://go.dev/dl/go1.23.4.linux-$(dpkg --print-architecture).tar.gz" '
    "| tar -C /usr/local -xz && ln -s /usr/local/go/bin/go /usr/local/go/bin/gofmt /usr/local/bin/",
    "rust": "curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.83.0 "
    "--no-modify-path && chmod -R a+rwX /opt/rustup /opt/cargo && ln -s /opt/cargo/bin/* /usr/local/bin/",
}
NEEDS_CURL = {"go", "rust"}


def check_request(packages: list[str], tools: list[str]) -> None:
    """Raise SandboxError, with the reason, if the agent asked for something that cannot go in an image."""
    bad = [p for p in packages if not APT_NAME.match(p)]
    if bad:
        raise SandboxError(f"not valid Debian package names: {', '.join(bad)} (give plain names, no versions)")
    if len(packages) > MAX_PACKAGES:
        raise SandboxError(f"too many system packages ({len(packages)}; at most {MAX_PACKAGES})")
    unknown = [t for t in tools if t not in TOOLS]
    if unknown:
        raise SandboxError(f"unknown tools: {', '.join(unknown)} (available: {', '.join(TOOLS)})")


# Downloaded packages are kept in BuildKit's cache between builds (the slim base deletes them after every
# install), so an image with a different package set reuses the downloads of earlier ones.
APT_CACHE = (
    "--mount=type=cache,target=/var/cache/apt,sharing=locked --mount=type=cache,target=/var/lib/apt,sharing=locked"
)


def _apt(packages: list[str]) -> str:
    return (
        f"RUN {APT_CACHE} rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update "
        "&& apt-get install -y --no-install-recommends " + " ".join(map(shlex.quote, packages))
    )


def dockerfile(base: str, packages: list[str], tools: list[str]) -> str:
    """Toolchains first, each its own layer in a fixed order, then the repo's packages: images that need the
    same toolchain share those layers whatever their packages, so only the package layer is built again."""
    tools = sorted(set(tools))
    curl_first = bool(NEEDS_CURL & set(tools))
    lines = [f"FROM {base}", "USER root"]
    if curl_first:
        lines.append(_apt(["curl"]))
    if "rust" in tools:
        lines.append("ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo")
    lines += [f"RUN {TOOLS[t]}" for t in tools]
    rest = sorted(set(packages) - ({"curl"} if curl_first else set()))
    if rest:
        lines.append(_apt(rest))
    lines.append("USER worker")
    return "\n".join(lines) + "\n"


def env_image_name(packages: list[str], tools: list[str]) -> str:
    """The image for this request. With nothing extra to install, that is the base image itself."""
    base = get_settings().sandbox_image
    if not packages and not tools:
        return base
    return "nightshift-env:" + hashlib.sha256(dockerfile(base, packages, tools).encode()).hexdigest()[:12]


async def ensure_env_image(packages: list[str], tools: list[str]) -> str:
    """Build the environment image if it does not exist yet, and return its tag."""
    check_request(packages, tools)
    tag = env_image_name(packages, tools)
    if await image_exists(tag):
        return tag
    content = dockerfile(get_settings().sandbox_image, packages, tools).encode()
    what = "build the environment image: " + ", ".join([*packages, *tools])
    args = ("build", "--progress=plain", "--tag", tag, "-")
    limit = get_settings().image_build_seconds
    res = await docker_shown(*args, stdin=content, timeout=limit, command=what, note="environment image")
    if not res.ok and not res.timed_out and NETWORK_ERROR.search(res.tail(4000)):
        res = await docker_shown(*args, stdin=content, timeout=limit, command=what, note="again after a network error")
    if res.timed_out:
        raise SandboxError(timeout_message(limit, res.tail(6000)))
    if not res.ok:
        raise SandboxError(f"building the environment image failed (exit {res.code}):\n{res.tail()}")
    return tag


def timeout_message(limit: int, output: str) -> str:
    """For the Setup agent's next attempt: where the build was when it was stopped, and what to ask for instead."""
    lines = [x.strip() for x in output.splitlines() if x.strip() and "timed out after" not in x]
    where = "\n".join(lines[-4:]) or "(no output)"
    return (
        f"the environment image build was stopped at its {limit // 60}-minute limit. It was at:\n{where}\n"
        "Ask for fewer or smaller packages (-headless or -dev variants, no -doc or desktop stacks), or a toolchain "
        "from the tools list instead of a distribution package."
    )
