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
from nightshift.sandbox.docker import NETWORK_ERROR, SandboxError, _docker, image_exists

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


def dockerfile(base: str, packages: list[str], tools: list[str]) -> str:
    packages = sorted(set(packages) | ({"curl"} if NEEDS_CURL & set(tools) else set()))
    lines = [f"FROM {base}", "USER root"]
    if packages:
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            + " ".join(map(shlex.quote, packages))
            + " && rm -rf /var/lib/apt/lists/*"
        )
    if "rust" in tools:
        lines.append("ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo")
    lines += [f"RUN {TOOLS[t]}" for t in sorted(set(tools))]
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
    res = await _docker("build", "--tag", tag, "-", stdin=content, timeout=1200)
    if not res.ok and NETWORK_ERROR.search(res.tail(4000)):
        res = await _docker("build", "--tag", tag, "-", stdin=content, timeout=1200)
    if not res.ok:
        raise SandboxError(f"building the environment image failed (exit {res.code}):\n{res.tail()}")
    return tag
