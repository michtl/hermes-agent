"""Offline probes for the local hermes harness.

Fast, hermetic, no network, no live stack.  These lock the parts of the harness
that are easy to get silently wrong:

* the per-target URL join (preview's path-direct form is the one that bites),
* that the join matches hermes's OWN join in ``tool_provider_gateway._post``,
* the env block a launched hermes actually receives,
* token decode / expiry classification,
* transcript redaction, including the self-check that proves it took,
* the isolation invariant: the profile is never the real ~/.hermes.

Run:
    cd /home/daimon/github/hermes-agent/.worktrees/composio-bridge && \
      TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0 .venv/bin/python -m pytest \
      tests/integration_tool_provider/harness/test_harness.py -q
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

HARNESS_DIR = Path(__file__).resolve().parent
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))

import hermes_harness as hh  # noqa: E402
import targets as tg  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_jwt(**claims) -> str:
    """Build an UNSIGNED, non-credential JWT-shaped string for offline tests.

    Signature is the literal "sig" — this authenticates to nothing anywhere.
    It exists purely to drive local decode/expiry/redaction logic.
    """
    def seg(obj) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def args_ns(**over):
    import argparse

    base = dict(
        model=tg.DEFAULT_MODEL,
        reasoning="medium",
        max_turns=60,
        subagent_auto_approve=False,
        max_spawn_depth=1,
    )
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
# target config + URL joining
# --------------------------------------------------------------------------


def test_both_targets_configured():
    assert set(tg.TARGETS) >= {"local", "preview"}


def test_local_uses_host_addressing_and_dev_mint():
    local = tg.get_target("local")
    assert local.gateway_addressing == "host"
    assert local.token_source == "dev-mint"
    assert local.nas_base_url == "http://127.0.0.1:3111"
    assert local.gateway_url("/v1/search") == "http://tools-gateway.localhost:3009/v1/search"
    assert local.mint_url == "http://127.0.0.1:3111/api/internal/dev-mint-oauth-token"


def test_preview_uses_path_direct_form():
    """The whole preview caveat, pinned.

    A ``*.vercel.app`` host can never satisfy the ``{provider}-gateway.<domain>``
    rewrite, so the origin must carry ``/api/passthrough/tools`` and the join
    must preserve it.
    """
    preview = tg.get_target("preview")
    assert preview.gateway_addressing == "path"
    assert preview.gateway_origin.endswith("/api/passthrough/tools")
    for path in ("/v1/search", "/v1/schemas", "/v1/execute", "/v1/connections"):
        url = preview.gateway_url(path)
        assert url.endswith(f"/api/passthrough/tools{path}"), url
        # The bug this guards: urljoin() would drop the prefix entirely.
        assert "/api/passthrough/tools/v1/" in url


def test_preview_has_no_headless_mint():
    preview = tg.get_target("preview")
    assert preview.token_source == "supplied"
    assert preview.mint_url is None
    assert "NO headless mint" in preview.token_help


def test_join_matches_hermes_own_join():
    """The harness must not invent a URL shape hermes would not produce.

    ``tool_provider_gateway._post`` builds ``f"{origin.rstrip('/')}{path}"``.
    Reproduce that literally and compare.
    """
    for target in tg.TARGETS.values():
        for path in ("/v1/search", "/v1/connections"):
            expected = f"{target.gateway_origin.rstrip('/')}{path}"
            assert target.gateway_url(path) == expected


def test_gateway_env_var_name_matches_hermes_derivation():
    """hermes derives the env name as f"{vendor.upper()}_GATEWAY_URL".

    Vendor is ``tools`` (TOOL_PROVIDER_VENDOR), so the name is TOOLS_GATEWAY_URL
    — note the S.  Getting this wrong silently falls back to the production
    default origin instead of erroring.
    """
    sys.path.insert(0, str(HARNESS_DIR.parents[2]))
    from tools.tool_provider_gateway import TOOL_PROVIDER_VENDOR

    derived = f"{TOOL_PROVIDER_VENDOR.upper().replace('-', '_')}_GATEWAY_URL"
    assert derived == "TOOLS_GATEWAY_URL"


def test_gateway_url_rejects_relative_path():
    with pytest.raises(ValueError):
        tg.get_target("local").gateway_url("v1/search")


def test_unknown_target_exits_with_known_list():
    with pytest.raises(SystemExit) as excinfo:
        tg.get_target("staging-typo")
    assert "known targets" in str(excinfo.value)


# --------------------------------------------------------------------------
# token decode
# --------------------------------------------------------------------------


def test_token_summary_surfaces_required_claims():
    exp = int(time.time()) + 900
    token = make_jwt(
        sub=tg.DEFAULT_USER_ID,
        aud="hermes-cli:hermes-agent",
        exp=exp,
        paid_access=True,
        org_id=tg.DEFAULT_ORG_ID,
    )
    summary = hh.token_summary(token)
    assert summary["sub"] == tg.DEFAULT_USER_ID
    assert summary["aud"] == "hermes-cli:hermes-agent"
    assert summary["exp"] == exp
    assert summary["paid_access"] is True
    assert 800 < summary["remaining_seconds"] <= 900


def test_token_summary_never_contains_the_token():
    token = make_jwt(sub="x", exp=int(time.time()) + 60, paid_access=True)
    blob = json.dumps(hh.token_summary(token))
    assert token not in blob
    assert token.split(".")[1] not in blob
    assert token.split(".")[2] not in blob


def test_expired_token_reports_negative_remaining():
    token = make_jwt(sub="x", exp=int(time.time()) - 30, paid_access=True)
    assert hh.token_summary(token)["remaining_seconds"] < 0


def test_garbage_token_decodes_to_empty_not_crash():
    assert hh.decode_jwt_claims("not-a-jwt") == {}
    assert hh.decode_jwt_claims("a.b.c") == {}


def test_fingerprint_is_stable_and_non_reversible():
    token = make_jwt(sub="x", exp=1, paid_access=True)
    fp = hh.token_fingerprint(token)
    assert fp == hh.token_fingerprint(token)
    assert fp.startswith("sha256:")
    assert len(fp) < 30
    assert token[:20] not in fp


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def test_redacts_jwt_shaped_text():
    token = make_jwt(sub="secret-user", exp=1, paid_access=True)
    text = f"Authorization: Bearer {token}\nrest of transcript"
    clean = hh.redact(text)
    assert token not in clean
    assert "<REDACTED:" in clean
    assert "rest of transcript" in clean
    assert hh.find_secrets(clean) == []


@pytest.mark.parametrize(
    "secret",
    [
        "sk-or-v1-0123456789abcdef0123456789abcdef",
        "ak_0123456789abcdefghij",
        "oak_0123456789abcdefghij",
        "sk-ant-api03-0123456789abcdef",
    ],
)
def test_redacts_key_shapes(secret):
    clean = hh.redact(f"key={secret} tail")
    assert secret not in clean
    assert "tail" in clean
    assert hh.find_secrets(clean) == []


def test_literal_redaction_catches_non_matching_secret():
    """A token that matches no pattern is still scrubbed via extra_literals."""
    weird = "totally-opaque-credential-value-12345"
    clean = hh.redact(f"tok={weird}", extra_literals=[weird])
    assert weird not in clean
    assert hh.find_secrets(clean, extra_literals=[weird]) == []


def test_find_secrets_actually_detects_a_leak():
    """The self-check must be capable of failing, or it proves nothing."""
    token = make_jwt(sub="x", exp=1, paid_access=True)
    assert hh.find_secrets(f"leaked {token}") == ["JWT"]


# --------------------------------------------------------------------------
# profile materialisation
# --------------------------------------------------------------------------


def test_write_profile_creates_isolated_files(tmp_path):
    profile = hh.Profile(name="unit", root=tmp_path / "unit")
    profile.root.mkdir(parents=True)
    token = make_jwt(sub=tg.DEFAULT_USER_ID, exp=int(time.time()) + 900, paid_access=True)
    with mock.patch.object(hh, "read_openrouter_key", return_value=("sk-or-v1-testkey0000", "test")):
        manifest = hh.write_profile(profile, tg.get_target("local"), token, args_ns())

    auth = json.loads((profile.home / "auth.json").read_text())
    assert auth["providers"]["nous"]["access_token"] == token
    assert auth["providers"]["nous"]["portal_base_url"] == "http://127.0.0.1:3111"

    config = (profile.home / "config.yaml").read_text()
    assert f"default: {tg.DEFAULT_MODEL}" in config
    assert "subagent_auto_approve: false" in config
    assert "max_spawn_depth: 1" in config

    assert (profile.home / ".env").read_text().startswith("OPENROUTER_API_KEY=")
    assert manifest["token_fingerprint"] == hh.token_fingerprint(token)
    # The manifest is a committed-ish artifact; it must not carry the token.
    assert token not in json.dumps(manifest)


def test_secret_files_are_owner_only(tmp_path):
    profile = hh.Profile(name="unit", root=tmp_path / "unit")
    profile.root.mkdir(parents=True)
    token = make_jwt(sub="x", exp=int(time.time()) + 900, paid_access=True)
    with mock.patch.object(hh, "read_openrouter_key", return_value=("sk-or-v1-testkey0000", "test")):
        hh.write_profile(profile, tg.get_target("local"), token, args_ns())
    for path in (profile.home / "auth.json", profile.home / ".env", profile.token_path):
        assert oct(path.stat().st_mode)[-3:] == "600", path


def test_toggles_reach_config_yaml(tmp_path):
    profile = hh.Profile(name="unit", root=tmp_path / "unit")
    profile.root.mkdir(parents=True)
    token = make_jwt(sub="x", exp=int(time.time()) + 900, paid_access=True)
    args = args_ns(subagent_auto_approve=True, max_spawn_depth=3, model="anthropic/claude-sonnet-5")
    with mock.patch.object(hh, "read_openrouter_key", return_value=("sk-or-v1-testkey0000", "test")):
        manifest = hh.write_profile(profile, tg.get_target("local"), token, args)
    config = (profile.home / "config.yaml").read_text()
    assert "subagent_auto_approve: true" in config
    assert "max_spawn_depth: 3" in config
    assert "default: anthropic/claude-sonnet-5" in config
    assert manifest["subagent_auto_approve"] is True


@pytest.mark.parametrize("target_name", ["local", "preview"])
def test_profile_env_matches_target(tmp_path, target_name):
    target = tg.get_target(target_name)
    profile = hh.Profile(name="unit", root=tmp_path / "unit")
    profile.root.mkdir(parents=True)
    token = make_jwt(sub="x", exp=int(time.time()) + 900, paid_access=True)
    with mock.patch.object(hh, "read_openrouter_key", return_value=("sk-or-v1-testkey0000", "test")):
        manifest = hh.write_profile(profile, target, token, args_ns())
        env = hh.profile_env(profile, manifest, token)

    assert env["HERMES_HOME"] == str(profile.home)
    assert env["HERMES_PORTAL_BASE_URL"] == target.nas_base_url.rstrip("/")
    assert env["TOOLS_GATEWAY_URL"] == target.gateway_origin
    assert env["TOOL_GATEWAY_USER_TOKEN"] == token
    # The env hermes gets must reproduce the documented request URL.
    assert f"{env['TOOLS_GATEWAY_URL'].rstrip('/')}/v1/search" == target.gateway_url("/v1/search")


def test_profile_home_is_never_the_real_hermes_home(tmp_path):
    profile = hh.Profile(name="unit", root=tmp_path / "unit")
    real = (Path.home() / ".hermes").resolve()
    assert profile.home.resolve() != real
    assert real not in profile.home.resolve().parents
    assert hh.DEFAULT_HOMES_ROOT.resolve() != real


# --------------------------------------------------------------------------
# token acquisition policy
# --------------------------------------------------------------------------


def test_preview_without_token_fails_loudly_and_never_calls_dev_mint():
    """The rule: never silently try the dev route against a preview."""
    calls = []

    def spy(*a, **kw):
        calls.append(a)
        raise AssertionError("dev-mint must not be attempted for a supplied-token target")

    with mock.patch.object(hh, "http_request", spy):
        with pytest.raises(SystemExit) as excinfo:
            hh.acquire_token(
                tg.get_target("preview"),
                args_ns(token=None, token_file=None),
            )
    message = str(excinfo.value)
    assert "requires a supplied token" in message
    assert "NO headless mint" in message
    assert "--token" in message
    assert calls == []


def test_supplied_token_is_used_verbatim_for_preview():
    token = make_jwt(sub="x", exp=int(time.time()) + 900, paid_access=True)
    got = hh.acquire_token(tg.get_target("preview"), args_ns(token=token, token_file=None))
    assert got == token


def test_dev_mint_reads_camelcase_access_token():
    """snake_case does not exist on this route and silently yields None."""
    body = json.dumps({"accessToken": "a.b.c", "expiresIn": 900})
    with mock.patch.object(hh, "http_request", return_value=hh.HttpResult(200, body)):
        got = hh.acquire_token(tg.get_target("local"), args_ns(token=None, token_file=None))
    assert got == "a.b.c"


def test_dev_mint_missing_token_field_is_explicit():
    body = json.dumps({"access_token": "a.b.c"})  # wrong casing on purpose
    with mock.patch.object(hh, "http_request", return_value=hh.HttpResult(200, body)):
        with pytest.raises(SystemExit) as excinfo:
            hh.acquire_token(tg.get_target("local"), args_ns(token=None, token_file=None))
    assert "camelCase" in str(excinfo.value)


def test_mint_failure_body_is_redacted_in_the_error():
    leaked = make_jwt(sub="oops", exp=1, paid_access=True)
    with mock.patch.object(hh, "http_request", return_value=hh.HttpResult(500, leaked)):
        with pytest.raises(SystemExit) as excinfo:
            hh.acquire_token(tg.get_target("local"), args_ns(token=None, token_file=None))
    assert leaked not in str(excinfo.value)


# --------------------------------------------------------------------------
# doctor diagnostics
# --------------------------------------------------------------------------


def _doctor_lines(tmp_path, target_name, token, http_stub):
    import argparse

    profile = hh.Profile(name="unit", root=tmp_path / "unit")
    profile.root.mkdir(parents=True)
    with mock.patch.object(hh, "read_openrouter_key", return_value=("sk-or-v1-testkey0000", "t")):
        hh.write_profile(profile, tg.get_target(target_name), token, args_ns())
        with mock.patch.object(hh, "resolve_profile", return_value=profile), \
             mock.patch.object(hh, "http_request", http_stub):
            code = hh.cmd_doctor(argparse.Namespace(profile="unit", homes_root=None))
    return code


def test_doctor_warns_on_cross_stack_token(tmp_path, capsys):
    """A local-issued token on a preview profile 401s with no explanation.

    Doctor must name the issuer mismatch rather than leave a bare 401 — this
    is the single most likely preview failure.
    """
    token = make_jwt(
        sub="u", iss="http://127.0.0.1:3111", exp=int(time.time()) + 900, paid_access=True
    )
    stub = mock.Mock(return_value=hh.HttpResult(401, '{"error":{"code":"AUTH_ERROR"}}'))
    _doctor_lines(tmp_path, "preview", token, stub)
    out = capsys.readouterr().out
    assert "[WARN] token issuer" in out
    assert "cross-stack token will 401" in out
    assert "doctor: FAIL" in out


def test_doctor_passes_issuer_when_it_matches(tmp_path, capsys):
    token = make_jwt(
        sub="u", iss="http://127.0.0.1:3111", exp=int(time.time()) + 900, paid_access=True
    )
    body = json.dumps({"connections": [{"toolkit": "hackernews", "status": "disconnected"}]})

    def stub(url, **kw):
        if "Authorization" in (kw.get("headers") or {}):
            return hh.HttpResult(200, body)
        return hh.HttpResult(401, '{"error":{"code":"AUTH_ERROR"}}')

    code = _doctor_lines(tmp_path, "local", token, stub)
    out = capsys.readouterr().out
    assert "[PASS] token issuer" in out
    assert "doctor: PASS" in out
    assert code == 0


def test_doctor_fails_an_expired_token_and_says_relaunch(tmp_path, capsys):
    token = make_jwt(
        sub="u", iss="http://127.0.0.1:3111", exp=int(time.time()) - 10, paid_access=True
    )
    stub = mock.Mock(return_value=hh.HttpResult(401, "{}"))
    code = _doctor_lines(tmp_path, "local", token, stub)
    out = capsys.readouterr().out
    assert "[FAIL] token unexpired" in out
    assert "relaunch" in out.lower()
    assert code == 1


def test_doctor_never_prints_the_token(tmp_path, capsys):
    token = make_jwt(
        sub="u", iss="http://127.0.0.1:3111", exp=int(time.time()) + 900, paid_access=True
    )
    stub = mock.Mock(return_value=hh.HttpResult(401, "{}"))
    _doctor_lines(tmp_path, "local", token, stub)
    out = capsys.readouterr().out
    assert token not in out
    assert hh.find_secrets(out) == []


# --------------------------------------------------------------------------
# down safety
# --------------------------------------------------------------------------


def test_down_refuses_to_delete_the_real_hermes_home():
    import argparse

    profile = hh.Profile(name="x", root=(Path.home() / ".hermes"))
    with mock.patch.object(hh, "resolve_profile", return_value=profile), \
         mock.patch.object(hh.shutil, "which", return_value=None), \
         mock.patch.object(hh.shutil, "rmtree", side_effect=AssertionError("must not delete")):
        with pytest.raises(SystemExit) as excinfo:
            hh.cmd_down(argparse.Namespace(profile="x", homes_root=None))
    assert "refusing to delete" in str(excinfo.value)
