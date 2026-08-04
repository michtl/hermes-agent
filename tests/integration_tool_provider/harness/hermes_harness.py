#!/usr/bin/env python3
"""Reusable, pluggable local hermes harness.

One command stands up a fully configured, ISOLATED hermes CLI profile pointed
at either the local stack or a preview deployment, and health-checks it.

    up      create/refresh an isolated HERMES_HOME profile (re-mints by default)
    doctor  PASS/FAIL health check: NAS, gateway, token, entitlement, round-trip
    run     launch the CLI against the profile and capture a redacted transcript
    down    delete the profile and kill any tmux session it owns
    env     print the export block for the profile (token NEVER printed)
    targets list configured targets and their resolved URLs

Target coordinates live in ``targets.py`` — one block, a few lines per target.

TOKEN EXPIRY IS THE TOP FOOTGUN.  The token is baked into the process env at
LAUNCH (900s TTL locally).  Refreshing auth.json under a RUNNING hermes does
nothing.  Re-run ``up`` and RELAUNCH.

Nothing here ever touches the real ~/.hermes.  It is read (for
OPENROUTER_API_KEY) and never written.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[2]
DEFAULT_HOMES_ROOT = HARNESS_DIR / ".homes"
TRANSCRIPTS_DIR = HARNESS_DIR / "transcripts"

sys.path.insert(0, str(HARNESS_DIR))
from targets import DEFAULT_MODEL, TARGETS, Target, get_target  # noqa: E402

HTTP_TIMEOUT = 15.0
#: doctor warns below this many seconds of token life left.
EXPIRY_WARN_SECONDS = 300

# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

#: Transcripts get committed.  These patterns are stripped on capture and the
#: result is re-grepped to prove the redaction actually took.
_SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+")),
    ("OPENROUTER_KEY", re.compile(r"\bsk-or-v1-[A-Za-z0-9_\-]{8,}")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}")),
    # NOT r"\b[ao]k_": in "oak_..." the \b only holds before the leading "o",
    # where [ao] consumes "o" and then "k_" fails against "a" — so oak_ keys
    # were passing through unredacted.  Alternate the whole prefix instead.
    ("AGENT_KEY", re.compile(r"\b(?:oak|ak)_[A-Za-z0-9_\-]{8,}")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
]


def redact(text: str, *, extra_literals: Optional[List[str]] = None) -> str:
    """Replace every known secret shape (and any supplied literal) with a tag."""
    out = text
    for literal in extra_literals or []:
        if literal and len(literal) >= 12:
            out = out.replace(literal, "<REDACTED:LITERAL>")
    for label, pattern in _SECRET_PATTERNS:
        out = pattern.sub(f"<REDACTED:{label}>", out)
    return out


def find_secrets(text: str, *, extra_literals: Optional[List[str]] = None) -> List[str]:
    """Return labels of secret shapes still present — the redaction self-check."""
    hits: List[str] = []
    for literal in extra_literals or []:
        if literal and len(literal) >= 12 and literal in text:
            hits.append("LITERAL")
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            hits.append(label)
    return sorted(set(hits))


def token_fingerprint(token: str) -> str:
    """A stable, non-reversible handle for a token, safe to print."""
    import hashlib

    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# JWT (local decode only — never verified, never printed)
# --------------------------------------------------------------------------


def decode_jwt_claims(token: str) -> Dict[str, Any]:
    """Decode a JWT payload locally.  No signature check, no network."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode())
        claims = json.loads(raw.decode())
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def token_summary(token: str) -> Dict[str, Any]:
    """The claims doctor surfaces.  Deliberately excludes the token itself."""
    claims = decode_jwt_claims(token)
    exp = claims.get("exp")
    remaining = None
    if isinstance(exp, (int, float)):
        remaining = int(exp - time.time())
    return {
        "sub": claims.get("sub"),
        "aud": claims.get("aud"),
        "iss": claims.get("iss"),
        "org_id": claims.get("org_id"),
        "exp": exp,
        "exp_iso": (
            datetime.fromtimestamp(exp, timezone.utc).isoformat()
            if isinstance(exp, (int, float))
            else None
        ),
        "remaining_seconds": remaining,
        "paid_access": claims.get("paid_access"),
        "fingerprint": token_fingerprint(token) if token else None,
    }


# --------------------------------------------------------------------------
# tiny HTTP (stdlib — the harness must run under any interpreter)
# --------------------------------------------------------------------------


@dataclass
class HttpResult:
    status: int
    body: str
    error: Optional[str] = None

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except Exception:
            return None


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = HTTP_TIMEOUT,
) -> HttpResult:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResult(resp.status, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # A 4xx from the gateway is a REAL answer (route exists, auth refused).
        return HttpResult(exc.code, exc.read().decode("utf-8", "replace"))
    except Exception as exc:
        return HttpResult(0, "", error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------


@dataclass
class Profile:
    name: str
    root: Path

    @property
    def home(self) -> Path:
        return self.root / "hermes-home"

    @property
    def workdir(self) -> Path:
        """CWD for launched runs.

        Deliberately NOT the repo root: hermes loads AGENTS.md from the CWD and
        the repo's is 75k, which would dominate every transcript and make runs
        non-deterministic across branches.
        """
        return self.root / "workdir"

    @property
    def manifest_path(self) -> Path:
        return self.root / "profile.json"

    @property
    def token_path(self) -> Path:
        return self.root / "token"  # 0600, gitignored

    @property
    def tmux_session(self) -> str:
        return f"hermes-harness-{self.name}"

    def read_manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.is_file():
            raise SystemExit(
                f"profile {self.name!r} not found at {self.root}\n"
                f"  run:  {_self_cmd()} up --target <name> --profile {self.name}"
            )
        return json.loads(self.manifest_path.read_text())

    def read_token(self) -> Optional[str]:
        if not self.token_path.is_file():
            return None
        token = self.token_path.read_text().strip()
        return token or None


def _self_cmd() -> str:
    return f"python {Path(__file__).resolve()}"


def resolve_profile(args: argparse.Namespace) -> Profile:
    homes_root = Path(
        args.homes_root or os.environ.get("HARNESS_HOMES_ROOT") or DEFAULT_HOMES_ROOT
    ).expanduser().resolve()
    name = args.profile
    return Profile(name=name, root=homes_root / name)


# --------------------------------------------------------------------------
# token acquisition
# --------------------------------------------------------------------------


def acquire_token(target: Target, args: argparse.Namespace) -> str:
    """Get a user JWT for ``target``, honoring the per-target token source."""
    supplied = (
        (getattr(args, "token", None) or "").strip()
        or (os.environ.get("HARNESS_TOKEN") or "").strip()
    )
    token_file = getattr(args, "token_file", None)
    if not supplied and token_file:
        path = Path(token_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"--token-file not found: {path}")
        supplied = path.read_text().strip()

    if supplied:
        if supplied.count(".") != 2:
            raise SystemExit("supplied token is not a JWT (expected three dot-separated parts)")
        return supplied

    if target.token_source == "supplied":
        # Never silently fall back to the dev route against a preview.
        raise SystemExit(
            f"target {target.name!r} requires a supplied token.\n  " + target.token_help
        )

    mint_url = target.mint_url
    assert mint_url  # dev-mint targets always have one
    result = http_request(
        mint_url,
        method="POST",
        headers={"Authorization": f"Bearer {target.mint_auth_secret}"},
        payload={
            "userId": target.user_id,
            "orgId": target.org_id,
            "clientId": target.client_id,
        },
    )
    if result.error:
        raise SystemExit(f"could not reach NAS to mint a token ({mint_url}): {result.error}")
    if result.status != 200:
        raise SystemExit(
            f"dev-mint failed: HTTP {result.status} from {mint_url}\n"
            f"  body: {redact(result.body)[:300]}"
        )
    body = result.json() or {}
    # camelCase.  snake_case does not exist on this route and silently yields None.
    token = body.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise SystemExit(
            "dev-mint returned no .accessToken (note: the field is camelCase)\n"
            f"  keys: {sorted(body) if isinstance(body, dict) else type(body).__name__}"
        )
    return token.strip()


def read_openrouter_key() -> Tuple[Optional[str], str]:
    """Read OPENROUTER_API_KEY from env or the REAL ~/.hermes/.env (read-only)."""
    env_value = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if env_value:
        return env_value, "environment"
    real_env = Path.home() / ".hermes" / ".env"
    if real_env.is_file():
        for line in real_env.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                value = line.split("=", 1)[1].strip().strip("'\"")
                if value:
                    return value, str(real_env)
    return None, "not found"


# --------------------------------------------------------------------------
# profile materialisation
# --------------------------------------------------------------------------


def build_config_yaml(args: argparse.Namespace) -> str:
    auto_approve = "true" if args.subagent_auto_approve else "false"
    return f"""# Generated by tests/integration_tool_provider/harness — do not hand-edit.
model:
  default: {args.model}
  provider: openrouter
  base_url: https://openrouter.ai/api/v1
  api_mode: chat_completions
  reasoning_effort: {args.reasoning}
toolsets:
  - hermes-cli
agent:
  max_turns: {args.max_turns}
  reasoning_effort: {args.reasoning}
  environment_probe: false
delegation:
  # false (the default) AUTO-DENIES a child's dangerous-command approvals, which
  # silently kills a delegate-to-gateway demo.  Flip with --subagent-auto-approve.
  subagent_auto_approve: {auto_approve}
  max_spawn_depth: {args.max_spawn_depth}
terminal:
  backend: local
telemetry:
  enabled: false
"""


def write_profile(
    profile: Profile,
    target: Target,
    token: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    home = profile.home
    home.mkdir(parents=True, exist_ok=True)
    profile.workdir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    claims = decode_jwt_claims(token)
    exp = claims.get("exp")
    expires_at = (
        datetime.fromtimestamp(exp, timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(exp, (int, float))
        else None
    )

    auth = {
        "version": 1,
        "providers": {
            "nous": {
                "access_token": token,
                "token_type": "Bearer",
                "portal_base_url": target.nas_base_url.rstrip("/"),
                "client_id": target.client_id,
                "obtained_at": now.isoformat().replace("+00:00", "Z"),
                "scope": "openid profile",
                **({"expires_at": expires_at} if expires_at else {}),
            }
        },
        "active_provider": "nous",
        "updated_at": now.isoformat().replace("+00:00", "Z"),
    }
    auth_path = home / "auth.json"
    auth_path.write_text(json.dumps(auth, indent=2))
    auth_path.chmod(0o600)

    (home / "config.yaml").write_text(build_config_yaml(args))

    key, key_source = read_openrouter_key()
    env_path = home / ".env"
    if key:
        env_path.write_text(f"OPENROUTER_API_KEY={key}\n")
        env_path.chmod(0o600)
    else:
        env_path.write_text("# OPENROUTER_API_KEY missing — model calls will fail\n")

    profile.token_path.write_text(token + "\n")
    profile.token_path.chmod(0o600)

    manifest = {
        "profile": profile.name,
        "target": target.name,
        "nas_base_url": target.nas_base_url,
        "gateway_origin": target.gateway_origin,
        "gateway_addressing": target.gateway_addressing,
        "token_source": target.token_source,
        "token_fingerprint": token_fingerprint(token),
        "token_expires_at": expires_at,
        "model": args.model,
        "reasoning": args.reasoning,
        "subagent_auto_approve": bool(args.subagent_auto_approve),
        "max_spawn_depth": args.max_spawn_depth,
        "max_turns": args.max_turns,
        "openrouter_key_source": key_source if key else "MISSING",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "hermes_home": str(home),
        "workdir": str(profile.workdir),
    }
    profile.manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def profile_env(profile: Profile, manifest: Dict[str, Any], token: str) -> Dict[str, str]:
    """The exact process env a launched hermes gets."""
    env = dict(os.environ)
    env.update(
        {
            "HERMES_HOME": str(profile.home),
            # Trusted-operator escape hatch in hermes_cli/auth.py: wins outright
            # over the stored value AND bypasses _NOUS_PORTAL_ALLOWED_HOSTS.
            "HERMES_PORTAL_BASE_URL": manifest["nas_base_url"].rstrip("/"),
            # Read by build_vendor_gateway_url() as f"{vendor.upper()}_GATEWAY_URL"
            # for vendor "tools".  Returned verbatim; joined naively with "/v1/...".
            "TOOLS_GATEWAY_URL": manifest["gateway_origin"],
            "TOOL_GATEWAY_USER_TOKEN": token,
            "PYTHONPATH": str(REPO_ROOT),
            "TZ": "UTC",
            "LANG": "C.UTF-8",
        }
    )
    key, _ = read_openrouter_key()
    if key:
        env["OPENROUTER_API_KEY"] = key
    for name, value in TARGETS[manifest["target"]].extra_env.items():
        env[name] = value
    return env


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

_PASS, _FAIL, _WARN, _INFO = "PASS", "FAIL", "WARN", "INFO"


class Report:
    def __init__(self) -> None:
        self.lines: List[Tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.lines.append((status, name, detail))
        print(f"  [{status:4}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return any(status == _FAIL for status, _, _ in self.lines)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_targets(args: argparse.Namespace) -> int:
    for name in sorted(TARGETS):
        target = TARGETS[name]
        print(f"{name}")
        print(f"  NAS               {target.nas_base_url}")
        print(f"  TOOLS_GATEWAY_URL {target.gateway_origin}")
        print(f"  addressing        {target.gateway_addressing}")
        print(f"  token source      {target.token_source}")
        print(f"  joined search URL {target.gateway_url('/v1/search')}")
        print()
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Token-free reachability + URL-shape check for a target.

    Exists because a preview target cannot be `up`'d without a human-supplied
    token, yet everything EXCEPT the token — config resolution, the path-direct
    join, whether the route actually exists — is checkable right now.  A 401
    from the gateway is a PASS here: it proves the URL reached the provider
    handler.  A 404 is the failure that matters, because that is what a wrong
    URL shape looks like.
    """
    target = get_target(args.target)
    report = Report()
    print(f"probe: target={target.name} (no token used)")
    print()

    report.add(_INFO, "NAS base", target.nas_base_url)
    report.add(_INFO, "TOOLS_GATEWAY_URL", target.gateway_origin)
    report.add(_INFO, "addressing", target.gateway_addressing)

    conn_url = target.gateway_url("/v1/connections")
    report.add(_INFO, "joined /v1/connections", conn_url)
    if target.gateway_addressing == "path":
        ok = "/api/passthrough/tools/v1/connections" in conn_url
        report.add(
            _PASS if ok else _FAIL,
            "path-direct join",
            "origin's /api/passthrough/tools prefix survived the naive join"
            if ok
            else "prefix lost — hermes would hit the wrong route",
        )

    nas = http_request(target.nas_base_url.rstrip("/") + "/api/health", timeout=15)
    if nas.error:
        nas = http_request(target.nas_base_url, timeout=15)
    report.add(
        _FAIL if nas.error else _PASS,
        "NAS reachable",
        nas.error or f"HTTP {nas.status}",
    )

    unauth = http_request(conn_url, method="POST", payload={"toolkits": [], "action": "status"})
    if unauth.error:
        report.add(_FAIL, "gateway reachable", unauth.error)
    else:
        good = unauth.status in (200, 400, 401, 403)
        report.add(
            _PASS if good else _FAIL,
            "gateway route exists",
            f"unauthenticated POST -> HTTP {unauth.status} "
            f"{redact(unauth.body)[:160]}",
        )

    if target.token_source == "dev-mint":
        mint = http_request(
            target.mint_url or "",
            method="POST",
            headers={"Authorization": f"Bearer {target.mint_auth_secret}"},
            payload={"userId": target.user_id, "orgId": target.org_id,
                     "clientId": target.client_id},
        )
        report.add(
            _PASS if mint.status == 200 else _FAIL,
            "dev-mint route",
            f"HTTP {mint.status}" if not mint.error else mint.error,
        )
    else:
        mint_url = f"{target.nas_base_url.rstrip('/')}/api/internal/dev-mint-oauth-token"
        report.add(
            _INFO,
            "token acquisition",
            f"supplied-only; {mint_url} is NODE_ENV-gated (see README)",
        )

    print()
    print(f"probe: {'FAIL' if report.failed else 'PASS'}")
    return 1 if report.failed else 0


def cmd_up(args: argparse.Namespace) -> int:
    target = get_target(args.target)
    profile = resolve_profile(args)

    existing_token = profile.read_token()
    if existing_token and args.no_remint:
        token = existing_token
        source = "reused (--no-remint)"
    else:
        # Re-mint by DEFAULT: the 900s TTL is baked into the process env at
        # launch, so a stale token means a dead session, not a slow one.
        token = acquire_token(target, args)
        source = target.token_source

    profile.root.mkdir(parents=True, exist_ok=True)
    manifest = write_profile(profile, target, token, args)

    summary = token_summary(token)
    print(f"profile   {profile.name}")
    print(f"target    {target.name}")
    print(f"home      {profile.home}")
    print(f"workdir   {profile.workdir}")
    print(f"token     {source}  {summary['fingerprint']}")
    print(f"          sub={summary['sub']}")
    print(f"          aud={summary['aud']}  paid_access={summary['paid_access']}")
    print(f"          exp={summary['exp_iso']}  ({summary['remaining_seconds']}s left)")
    print(f"model     {manifest['model']}  (reasoning={manifest['reasoning']})")
    print(
        f"toggles   subagent_auto_approve={manifest['subagent_auto_approve']}  "
        f"max_spawn_depth={manifest['max_spawn_depth']}"
    )
    print(f"openrouter key from {manifest['openrouter_key_source']}")
    if manifest["openrouter_key_source"] == "MISSING":
        print(
            "  WARNING: OPENROUTER_API_KEY not found in the environment or "
            f"{Path.home()}/.hermes/.env — model calls WILL fail.\n"
            "  Set OPENROUTER_API_KEY in your shell and re-run `up`."
        )
    print()
    print(f"next:  {_self_cmd()} doctor --profile {profile.name}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    profile = resolve_profile(args)
    manifest = profile.read_manifest()
    target = get_target(manifest["target"])
    token = profile.read_token()
    report = Report()

    print(f"doctor: profile={profile.name} target={target.name}")
    print(f"  home={profile.home}")
    print()

    # --- profile files ---
    for label, path in (
        ("auth.json", profile.home / "auth.json"),
        ("config.yaml", profile.home / "config.yaml"),
    ):
        report.add(_PASS if path.is_file() else _FAIL, f"profile file {label}", str(path))

    real_home = (Path.home() / ".hermes").resolve()
    isolated = real_home not in profile.home.resolve().parents and profile.home.resolve() != real_home
    report.add(_PASS if isolated else _FAIL, "isolation", f"HERMES_HOME is not {real_home}")

    key, key_source = read_openrouter_key()
    report.add(
        _PASS if key else _FAIL,
        "OPENROUTER_API_KEY",
        f"source: {key_source}" if key else "missing — model calls will fail",
    )

    # --- token ---
    if not token:
        report.add(_FAIL, "token present", "no token on disk; re-run `up`")
        summary: Dict[str, Any] = {}
    else:
        summary = token_summary(token)
        claims_ok = bool(summary.get("sub")) and bool(summary.get("exp"))
        report.add(
            _PASS if claims_ok else _FAIL,
            "token decodes",
            f"{summary.get('fingerprint')} sub={summary.get('sub')}",
        )
        report.add(_INFO, "token aud", str(summary.get("aud")))
        # Issuer vs target NAS is THE preview failure mode: a token minted
        # against local NAS looks perfectly valid locally and 401s on preview
        # with nothing in the response body explaining why.  Name it here.
        issuer = str(summary.get("iss") or "")
        nas = manifest["nas_base_url"].rstrip("/")
        if issuer and issuer.rstrip("/") != nas:
            report.add(
                _WARN,
                "token issuer",
                f"iss={issuer} but this profile targets {nas} — a cross-stack "
                "token will 401 on the authenticated round-trip below",
            )
        else:
            report.add(_PASS, "token issuer", f"iss={issuer} matches target NAS")
        remaining = summary.get("remaining_seconds")
        if not isinstance(remaining, int):
            report.add(_FAIL, "token exp", "no exp claim")
        elif remaining <= 0:
            report.add(
                _FAIL,
                "token unexpired",
                f"EXPIRED {-remaining}s ago — re-run `up` AND relaunch (a refreshed "
                "auth.json does nothing for a running process)",
            )
        elif remaining < EXPIRY_WARN_SECONDS:
            report.add(
                _WARN,
                "token unexpired",
                f"only {remaining}s left — re-run `up` and RELAUNCH before a long run",
            )
        else:
            report.add(_PASS, "token unexpired", f"{remaining}s left (exp {summary.get('exp_iso')})")
        paid = summary.get("paid_access")
        report.add(
            _PASS if paid is True else _FAIL,
            "entitlement claim",
            f"paid_access={paid} (decoded locally, no network call)",
        )

    # --- NAS ---
    nas_probe = http_request(manifest["nas_base_url"].rstrip("/") + "/api/health", timeout=10)
    if nas_probe.error:
        nas_probe = http_request(manifest["nas_base_url"], timeout=10)
    if nas_probe.error:
        report.add(_FAIL, "NAS reachable", f"{manifest['nas_base_url']}: {nas_probe.error}")
    else:
        report.add(
            _PASS, "NAS reachable", f"{manifest['nas_base_url']} -> HTTP {nas_probe.status}"
        )

    # --- gateway addressing ---
    conn_url = target.gateway_url("/v1/connections")
    report.add(_INFO, "gateway URL join", f"{manifest['gateway_addressing']}: {conn_url}")
    if manifest["gateway_addressing"] == "path" and "/api/passthrough/" not in conn_url:
        report.add(_FAIL, "path-direct form", "preview origin lost its /api/passthrough prefix")
    unauth = http_request(conn_url, method="POST", payload={"toolkits": [], "action": "status"})
    if unauth.error:
        report.add(_FAIL, "gateway reachable", f"{conn_url}: {unauth.error}")
    else:
        # 401 here is the GOOD answer: the route exists and refused an
        # unauthenticated call.  A 404 would mean the URL shape is wrong.
        good = unauth.status in (200, 400, 401, 403)
        report.add(
            _PASS if good else _FAIL,
            "gateway route exists",
            f"unauthenticated POST -> HTTP {unauth.status}"
            + ("" if good else " (404 usually means a wrong URL shape)"),
        )

    # --- authenticated round-trip ---
    if token:
        authed = http_request(
            conn_url,
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
            payload={"toolkits": [], "action": "status"},
        )
        if authed.error:
            report.add(_FAIL, "/v1/connections round-trip", authed.error)
        elif authed.status != 200:
            report.add(
                _FAIL,
                "/v1/connections round-trip",
                f"HTTP {authed.status}: {redact(authed.body, extra_literals=[token])[:200]}",
            )
        else:
            body = authed.json() or {}
            conns = body.get("connections") if isinstance(body, dict) else None
            if not isinstance(conns, list):
                report.add(_FAIL, "/v1/connections round-trip", "no connections array in body")
            else:
                names = [c.get("toolkit") for c in conns if isinstance(c, dict)]
                report.add(
                    _PASS,
                    "/v1/connections round-trip",
                    f"HTTP 200, {len(conns)} toolkit(s): {', '.join(str(n) for n in names)}",
                )
                connected = [
                    c.get("toolkit")
                    for c in conns
                    if isinstance(c, dict) and c.get("status") == "connected"
                ]
                report.add(
                    _INFO,
                    "connected toolkits",
                    ", ".join(str(c) for c in connected) if connected else "none (connect wall)",
                )
    else:
        report.add(_FAIL, "/v1/connections round-trip", "skipped — no token")

    # --- toggles echo ---
    report.add(
        _INFO,
        "toggles",
        f"model={manifest['model']} subagent_auto_approve={manifest['subagent_auto_approve']} "
        f"max_spawn_depth={manifest['max_spawn_depth']}",
    )

    print()
    verdict = "FAIL" if report.failed else "PASS"
    print(f"doctor: {verdict}")
    return 1 if report.failed else 0


def _capture_transcript(
    profile: Profile, manifest: Dict[str, Any], name: str, text: str, token: Optional[str]
) -> Path:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    key, _ = read_openrouter_key()
    literals = [t for t in (token, key) if t]
    clean = redact(text, extra_literals=literals)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = TRANSCRIPTS_DIR / f"{profile.name}-{name}-{stamp}.txt"
    header = (
        f"# harness transcript\n"
        f"# profile={profile.name} target={manifest['target']} model={manifest['model']}\n"
        f"# gateway={manifest['gateway_origin']}\n"
        f"# token_fingerprint={manifest['token_fingerprint']}\n"
        f"# captured={stamp}\n"
        f"# secrets redacted on capture; see redaction self-check below\n\n"
    )
    leaks = find_secrets(clean, extra_literals=literals)
    footer = f"\n\n# redaction self-check: {'CLEAN' if not leaks else 'LEAK ' + ','.join(leaks)}\n"
    path.write_text(header + clean + footer)
    print(f"transcript: {path}")
    print(f"redaction self-check: {'CLEAN' if not leaks else 'LEAK ' + ','.join(leaks)}")
    if leaks:
        raise SystemExit("REFUSING to continue: transcript still contains secret-shaped text")
    return path


def cmd_run(args: argparse.Namespace) -> int:
    profile = resolve_profile(args)
    manifest = profile.read_manifest()
    token = profile.read_token()
    if not token:
        raise SystemExit(f"no token for profile {profile.name!r}; run `up` first")

    summary = token_summary(token)
    remaining = summary.get("remaining_seconds")
    if isinstance(remaining, int) and remaining <= 0:
        raise SystemExit(
            f"token expired {-remaining}s ago. Re-run `up` and RELAUNCH — the token is "
            "baked into the process env at launch, so refreshing auth.json under a "
            "running hermes does nothing."
        )
    if isinstance(remaining, int) and remaining < EXPIRY_WARN_SECONDS:
        print(f"WARNING: token has only {remaining}s left; consider re-running `up` first.")

    prompt = args.prompt
    if args.scenario:
        path = Path(args.scenario)
        if not path.is_absolute():
            for candidate in (Path.cwd() / path, HARNESS_DIR / "scenarios" / path.name, HARNESS_DIR / path):
                if candidate.is_file():
                    path = candidate
                    break
        if not path.is_file():
            raise SystemExit(f"scenario file not found: {args.scenario}")
        prompt = path.read_text().strip()
    if not prompt:
        raise SystemExit("nothing to run: pass a prompt or --scenario")

    env = profile_env(profile, manifest, token)
    label = args.label or ("scenario" if args.scenario else "oneshot")

    if args.tmux:
        return _run_tmux(profile, manifest, env, prompt, label, token, args)
    return _run_oneshot(profile, manifest, env, prompt, label, token, args)


def _run_oneshot(
    profile: Profile,
    manifest: Dict[str, Any],
    env: Dict[str, str],
    prompt: str,
    label: str,
    token: str,
    args: argparse.Namespace,
) -> int:
    python = args.python or str(REPO_ROOT / ".venv" / "bin" / "python")
    cmd = [python, "-m", "hermes_cli.main", "-z", prompt]
    print(f"launching: {python} -m hermes_cli.main -z <prompt>  (cwd={profile.workdir})")
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(profile.workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    elapsed = time.time() - started
    text = (
        f"$ hermes -z {prompt!r}\n"
        f"exit={proc.returncode} elapsed={elapsed:.1f}s\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}\n"
    )
    print(f"exit={proc.returncode} elapsed={elapsed:.1f}s")
    _capture_transcript(profile, manifest, label, text, token)
    return proc.returncode


def _run_tmux(
    profile: Profile,
    manifest: Dict[str, Any],
    env: Dict[str, str],
    prompt: str,
    label: str,
    token: str,
    args: argparse.Namespace,
) -> int:
    if not shutil.which("tmux"):
        raise SystemExit("tmux not on PATH")
    session = profile.tmux_session
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    python = args.python or str(REPO_ROOT / ".venv" / "bin" / "python")
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(profile.workdir),
         python, "-m", "hermes_cli.main"],
        env=env,
        check=True,
    )
    print(f"tmux session: {session} (attach with: tmux attach -t {session})")
    time.sleep(args.tmux_boot_seconds)
    subprocess.run(["tmux", "send-keys", "-t", session, prompt], check=True)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True)

    deadline = time.time() + args.timeout
    pane = ""
    while time.time() < deadline:
        time.sleep(args.poll_seconds)
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", "-2000", "-t", session],
            capture_output=True,
            text=True,
        ).stdout
        if args.until and args.until in pane:
            break
    _capture_transcript(profile, manifest, label, pane, token)
    if not args.keep:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        print(f"tmux session {session} killed (pass --keep to leave it up)")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    profile = resolve_profile(args)
    manifest = profile.read_manifest()
    print(f"export HERMES_HOME={profile.home}")
    print(f"export HERMES_PORTAL_BASE_URL={manifest['nas_base_url'].rstrip('/')}")
    print(f"export TOOLS_GATEWAY_URL={manifest['gateway_origin']}")
    print(f'export TOOL_GATEWAY_USER_TOKEN="$(cat {profile.token_path})"  # 0600, never printed')
    print(f"export PYTHONPATH={REPO_ROOT}")
    print(f"# token fingerprint {manifest['token_fingerprint']} expires {manifest['token_expires_at']}")
    print(f"# cd {profile.workdir} && {REPO_ROOT}/.venv/bin/python -m hermes_cli.main")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    profile = resolve_profile(args)
    if shutil.which("tmux"):
        subprocess.run(["tmux", "kill-session", "-t", profile.tmux_session], capture_output=True)
    if profile.root.exists():
        real_home = (Path.home() / ".hermes").resolve()
        resolved = profile.root.resolve()
        # Belt and braces: never let a mangled --homes-root delete the real home.
        if resolved == real_home or real_home in resolved.parents or resolved == Path.home():
            raise SystemExit(f"refusing to delete {resolved}")
        shutil.rmtree(resolved)
        print(f"removed {resolved}")
    else:
        print(f"nothing to remove at {profile.root}")
    print(f"tmux session {profile.tmux_session} killed if it existed")
    return 0


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes_harness",
        description="Stand up an isolated hermes CLI against a local stack or a preview.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", default=os.environ.get("HARNESS_PROFILE", "default"))
        p.add_argument("--homes-root", default=None, help="override where profiles live")

    p_targets = sub.add_parser("targets", help="list targets and resolved URLs")
    p_targets.set_defaults(func=cmd_targets)

    p_probe = sub.add_parser(
        "probe", help="token-free reachability + URL-shape check for a target"
    )
    p_probe.add_argument("--target", default=os.environ.get("HARNESS_TARGET", "local"),
                         choices=sorted(TARGETS))
    p_probe.set_defaults(func=cmd_probe)

    p_up = sub.add_parser("up", help="create/refresh an isolated profile (re-mints by default)")
    common(p_up)
    p_up.add_argument("--target", default=os.environ.get("HARNESS_TARGET", "local"),
                      choices=sorted(TARGETS))
    p_up.add_argument("--token", default=None, help="supply a JWT (required for preview)")
    p_up.add_argument("--token-file", default=None, help="read the JWT from a file")
    p_up.add_argument("--no-remint", action="store_true",
                      help="reuse the profile's existing token instead of minting a fresh one")
    p_up.add_argument("--model", default=os.environ.get("HARNESS_MODEL", DEFAULT_MODEL))
    p_up.add_argument("--reasoning", default="medium")
    p_up.add_argument("--max-turns", type=int, default=60)
    p_up.add_argument("--subagent-auto-approve", action="store_true",
                      help="let a child's dangerous-command approvals auto-APPROVE "
                           "(default false auto-DENIES and silently breaks delegate demos)")
    p_up.add_argument("--max-spawn-depth", type=int, default=1, choices=[1, 2, 3])
    p_up.set_defaults(func=cmd_up)

    p_doctor = sub.add_parser("doctor", help="PASS/FAIL health check for a profile")
    common(p_doctor)
    p_doctor.set_defaults(func=cmd_doctor)

    p_run = sub.add_parser("run", help="run a prompt/scenario and capture a redacted transcript")
    common(p_run)
    p_run.add_argument("prompt", nargs="?", default=None)
    p_run.add_argument("--scenario", default=None, help="path to a scenario file")
    p_run.add_argument("--label", default=None, help="transcript filename label")
    p_run.add_argument("--tmux", action="store_true", help="drive the interactive CLI in tmux")
    p_run.add_argument("--until", default=None, help="tmux: stop polling once this text appears")
    p_run.add_argument("--keep", action="store_true", help="tmux: leave the session running")
    p_run.add_argument("--tmux-boot-seconds", type=float, default=12.0)
    p_run.add_argument("--poll-seconds", type=float, default=5.0)
    p_run.add_argument("--timeout", type=float, default=300.0)
    p_run.add_argument("--python", default=None, help="interpreter to launch hermes with")
    p_run.set_defaults(func=cmd_run)

    p_env = sub.add_parser("env", help="print the export block (token never printed)")
    common(p_env)
    p_env.set_defaults(func=cmd_env)

    p_down = sub.add_parser("down", help="delete the profile and kill its tmux session")
    common(p_down)
    p_down.set_defaults(func=cmd_down)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
