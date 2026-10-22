import hashlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

import source_index.__main__ as source_index_module
from bot.rag_store import RagStore
from source_index.__main__ import (
    SOURCE_POLICY_VERSION,
    GitHubRateLimitError,
    GitHubSourceClient,
    Repository,
    SourceHistoryChange,
    SourceHistoryCommit,
    SourceHistorySettings,
    chunks_for,
    contains_history_secret,
    contains_secret,
    deployed_refs,
    document_policy,
    github_token_provider,
    history_chunks_for,
    inferred_festival,
    load_repositories,
    load_state,
    metadata_digest_for,
    metadata_for,
    preflight_once,
    redact_source_secrets,
    repository_manifest_digest,
    repository_state_is_current,
    safe_source_path,
    select_history_commits,
    source_history_settings,
    source_upload_concurrency,
    stale_managed_document_keys,
    state_entry_matches_chunk,
    sync_once,
    valid_processed_history_commits,
)
from source_index.github_auth import GitHubAppTokenProvider


def curated_repository() -> Repository:
    return Repository(
        name="YagamiFes-IT/YagamiFes-IT-Knowledge",
        ref="main",
        include=("knowledge/",),
        source_type="knowledge_repository",
        authority="curated",
        status="current",
        front_matter_statuses=("active", "historical"),
    )


def test_source_path_allowlist_excludes_generated_credentials_and_lockfiles():
    includes = ("lib/", "functions/", "Dockerfile")

    assert safe_source_path("lib/auth.dart", includes, 100)
    assert safe_source_path("functions/auth.js", includes, 100)
    assert safe_source_path("Dockerfile", includes, 100)
    assert not safe_source_path("node_modules/pkg/index.js", includes, 100)
    assert not safe_source_path("lib/firebase_options.dart", includes, 100)
    assert not safe_source_path("functions/package-lock.json", includes, 100)
    assert not safe_source_path("bot/trusted_knowledge.json", ("bot/",), 100)
    assert not safe_source_path(".env", includes, 100)
    assert not safe_source_path("lib/../credentials.py", includes, 100)
    assert not safe_source_path("lib/newline\n.py", includes, 100)


def test_source_path_allowlist_respects_file_and_directory_boundaries():
    assert safe_source_path("bot/corpus.py", ("bot/",), 100)
    assert not safe_source_path("bottle/corpus.py", ("bot/",), 100)
    assert safe_source_path("main.py", ("main.py",), 100)
    assert not safe_source_path("main.py.backup", ("main.py",), 100)


def test_source_secret_scanner_allows_environment_lookup_but_blocks_literal():
    assert not contains_secret('token = os.environ.get("GITHUB_TOKEN", "")')
    assert contains_secret('api_key = "1234567890abcdefghijklmnop"')
    assert contains_secret('{"client_secret": "1234567890abcdefghijklmnop"}')
    assert contains_secret('password = "Prod(Real)Secret123!"')
    assert contains_secret('password = "!ProdRealSecret123"')
    assert contains_secret("パスワード: 本番用秘密文字列12345678")
    assert contains_history_secret("+パスワード: 本番用秘密文字列12345678")


@pytest.mark.parametrize(
    "assignment",
    (
        'const dbPassword = "ProdRealSecret123";',
        "DB_PASSWORD=ProdRealSecret123",
        'serviceClientSecret = "ProdRealSecret123"',
        'password = SecretStr("ProdRealSecret123")',
        'password = f"ProdRealSecret123"',
        "password = `ProdRealSecret123`",
        "password = process.env.DB_PASSWORD || `ProdRealSecret123`",
        'const dbPassword =\n  "ProdRealSecret123";',
        'password: SecretStr = SecretStr("ProdRealSecret123")',
        'password = SecretStr(value="ProdRealSecret123")',
    ),
)
def test_source_secret_scanner_blocks_common_live_secret_forms(assignment):
    assert contains_secret(assignment)
    assert contains_history_secret(f"+{assignment}")


def test_source_secret_scanner_blocks_yaml_block_scalars():
    assert contains_secret("password: |\n  ProdRealSecret123")
    assert contains_secret("password: >- # stored credential\n\n  ProdRealSecret123")
    assert contains_secret("password: |\n  example-password\n  ProdRealSecret123")
    assert not contains_secret("password: |\n  example-password")
    assert contains_history_secret("+password: |\n+  ProdRealSecret123")
    assert contains_secret("- password: |2-\n    ProdRealSecret123")
    assert contains_history_secret("+- password: >2+\n+    ProdRealSecret123")


@pytest.mark.parametrize(
    "assignment",
    (
        'password = process.env.DB_PASSWORD || "ProdRealSecret123"',
        'password = x ?? "ProdRealSecret123"',
        'password = x ?: "ProdRealSecret123"',
        'password = condition ? "ProdRealSecret123" : process.env.PASSWORD',
        'password = condition ? process.env.PASSWORD : "ProdRealSecret123"',
        'api_key = os.getenv("API_KEY", "ProdRealSecret123")',
        'apiKey = os.getenv("API_KEY", default="ProdRealSecret123")',
        'password = settings.get("PASSWORD", "ProdRealSecret123")',
        (
            'dbPassword = String.fromEnvironment("DB_PASSWORD", '
            'defaultValue: "ProdRealSecret123")'
        ),
        (
            "const dbPassword =\n"
            "  process.env.DB_PASSWORD ||\n"
            '  "ProdRealSecret123";'
        ),
        'password = os.getenv("DB_PASSWORD", SecretStr("ProdRealSecret123"))',
        'password = os.getenv("DB_PASSWORD") or SecretStr("ProdRealSecret123")',
        "token = read_secret('TOKEN') or production-token-value",
    ),
)
def test_source_secret_scanner_blocks_concrete_fallbacks(assignment):
    assert contains_secret(assignment)


def test_source_secret_scanner_allows_placeholder_fallback():
    assert not contains_secret(
        'password = process.env.DB_PASSWORD || "example-password"'
    )


@pytest.mark.parametrize(
    "assignment",
    (
        "password: " + "production-value-" * 2,
        "api_key=" + "0123456789abcdef" * 2,
        "client_secret: " + "runtime-credential-" * 2,
        "export ACCESS_TOKEN=" + "service-token-value-" * 2,
    ),
)
def test_source_secret_scanner_blocks_unquoted_concrete_scalars(assignment):
    assert contains_secret(assignment)


@pytest.mark.parametrize(
    "assignment",
    (
        "token: SecretStr",
        "token: str",
        "token = os.environ['GITHUB_TOKEN']",
        "api_key: ${API_KEY}",
        'api_key: "${API_KEY}"',
        'password: "%PASSWORD%"',
        'password: "{{ vault_password }}"',
        'password: "<PASSWORD>"',
        'client_secret: read_secret("CLIENT_SECRET")',
        'serviceClientSecret = read_secret("CLIENT_SECRET")',
        "const dbPassword = process.env.DB_PASSWORD",
        "DB_PASSWORD=${DB_PASSWORD}",
        "dbPassword: string",
        "password: replace-with-your-password",
        'api_key = "example-api-key-for-docs"',
        'password = SecretStr("example-password")',
        'password = SecretStr(value="example-password")',
        'password = "replace-with-your-password"',
        'client_secret = "not-a-real-secret"',
        'api_key = "your_api_key_here"',
        'token = "xxxxx"',
        (
            'password: SecretStr = SecretStr(os.getenv("DB_PASSWORD", ""))'
        ),
        'password = os.getenv(\n  "DB_PASSWORD",\n  ""\n)',
        "const dbPassword =\n  process.env.DB_PASSWORD;",
        "password = process.env.DB_PASSWORD || `example-password`",
        "token = access_token",
        "reset_token = []",
        "client_secret = {}",
    ),
)
def test_source_secret_scanner_allows_types_references_and_placeholders(assignment):
    assert not contains_secret(assignment)


@pytest.mark.parametrize(
    "source",
    (
        "page_token = payload.nextPageToken",
        "headers: {'idToken': idToken}",
        "token: fcmToken,",
        "const fcmToken = userSnapshot.data()?.fcmToken;",
        'const geminiApiKey = defineSecret("GEMINI_API_KEY");',
        '"google-api-key": re.compile(r"\\bAIza[0-9A-Za-z_-]{30,}\\b")',
        (
            "registerPushToken = onCall(\n"
            '  {region: "asia-northeast1", timeoutSeconds: 30},\n'
            "  runRegisterPushToken,\n"
            ");"
        ),
        'token = os.environ.get("DISCORD_TOKEN", "")',
        "if not token:\n    log.error('DISCORD_TOKEN is not set')",
        "token == null ? 'missing' : 'available'",
        "auth: {uid: 'visitor-1', token: {isAdmin: true}}",
        "YAGAPON_FIREBASE_KNOWLEDGE_API_KEY=${YAGAPON_FIREBASE_KNOWLEDGE_API_KEY:-}",
    ),
)
def test_source_secret_scanner_allows_runtime_identifiers_and_definitions(source):
    assert not contains_secret(source)


def test_source_secret_scanner_allows_explicit_secret_manager_resource_name():
    source = (
        "YAGAPON_GOOGLE_OAUTH_SECRET="
        "projects/yagamiatlas/secrets/yagapon-google-drive-oauth/versions/latest\r\n"
    )

    assert not contains_secret(source)
    assert redact_source_secrets(source) == (source, 0)


def test_source_secret_scanner_does_not_allow_concrete_shell_default():
    assert contains_secret("API_KEY=${API_KEY:-ProdRealSecret123}")
    assert contains_secret('token: {value: "ProdRealSecret123"}')


@pytest.mark.parametrize(
    "source",
    (
        'password = re.compile("ProdRealSecret123")',
        'password = re.compile("Actual[Prod]Secret{9}")',
        'token = onCall("ProdRealSecret123")',
        'getToken = onCall("ActualProdSecret982734")',
        'token: {uid: "ProdRealSecret123"}',
        'token = {"sub": "admin-real-prod-token"}',
        "token: {sub: ActualProdValue982734}",
        "token: {firebase: {uid: ActualProdValue982734}}",
        'token: {uid: "secr3t"}',
        'password = defineSecret("THIS_IS_THE_ACTUAL_PASSWORD")',
        'geminiApiKey = defineSecret("ACTUAL_PROD_PASSWORD")',
        'password = re.compile(r"\\bACTUALPRODPASSWORD[A-Z]{20}")',
        '"google-api-key": re.compile("AIzaProdRealSecret982734")',
        '"google-api-key": re.compile("AIza[Prod]RealSecret{20}")',
        'const cfg = enabled ? {password: "ActualProdSecret982734"} : {}',
        "API_KEY=ProdApiKeyValue982734",
        "TOKEN=ACTUALPRODSECRET",
        "export API_KEY=ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        (
            "DB_PASSWORD_SECRET=actualpassword\n"
            "DB_PASSWORD_FILE=/run/secrets/database-password"
        ),
        'token := "ProdRealSecret123"',
        'token += "ProdRealSecret123"',
        'token ??= "ProdRealSecret123"',
        'token == "ProdRealSecret123"',
        'authorization="Bearer eyJfixtureHeader.fixturePayload.fixtureSignature"',
        'token = "Prod" + "Real" + "Secret"',
        'token = "Prod" "Real" "Secret"',
        'token = "a" * 40',
        "token = bytes([80, 114, 111, 100, 83, 101, 99, 114, 101, 116])",
        "token = bytes((80, 114, 111, 100))",
        "token = bytes([80, 114]) + bytes([111, 100])",
        "token = bytes([80, 114] + [111, 100])",
        "token = bytes((80, 114) + (111, 100))",
        "token = bytes(map(int, [80, 114, 111, 100]))",
        'token = """ProdReal\nSecret123"""',
        "token = `ProdReal\nSecret123`",
        'token = {uid: request.auth.uid, ["roles"]: ["ProdRealSecret123"]}',
        'token = {uid: request.auth.uid, ... ["ProdRealSecret123"]}',
        'token = {uid: request.auth.uid, ...leak("ProdRealSecret123")}',
        'token = get("ProdRealSecret123")',
        'token = values["ProdRealSecret123"]',
        'token = "abc123"',
        "token=abc123",
        "PASSWORD=abc123",
        'token = "prod-available-secret"',
        'password = "missing-real-password"',
        'token = "live-example-token"',
        'password = "actual-placeholder-password"',
        'client_secret = "example-live-actual-secret"',
        'token = "ProdReal\\\nSecret123"',
        "token = `ProdReal\\\nSecret123`",
        "TOKEN=ProdReal\\\nSecret123",
        'token: "Prod\n  Secret123"',
        'TOKEN="Prod\nSecret123"',
        "TOKEN='Prod\nSecret123'",
        'token /* credential */ = "ProdRealSecret123"',
        'token /* credential\ncontinued */ = "ProdRealSecret123"',
        'config["token"] = "ProdRealSecret123"',
        'config [ "token" ] = "ProdRealSecret123"',
        'config/* gap */[`token`]/* gap */="ProdRealSecret123"',
        'token\n=\n"ProdRealSecret123"',
        'token **= "ProdRealSecret123"',
        'token //= "ProdRealSecret123"',
        '$token = "ProdRealSecret123"',
        'headers["Authorization"] = "Bearer ProdRealSecret123"',
        (
            "token = {uid: request.auth.uid, "
            '/* unparsed */ roles: ["ProdRealSecret123"]}'
        ),
        'token = read_secret(value="ProdRealSecret123")',
        'token = secret("ProdRealSecret123")',
        'token = config.get("ProdRealSecret123")',
        "https://example.invalid/path?token=ProdRealSecret123",
        "https://user:ProdRealSecret123@example.invalid/path",
        "postgresql://user:ProdRealSecret123@example.invalid/database",
        "redis://:ProdRealSecret123@example.invalid/0",
        'secret("ProdRealSecret123")',
        'secret(value="ProdRealSecret123")',
        'api_key(value="ProdRealSecret123")',
        "<token>ProdRealSecret123</token>",
        '<input name="password" value="ProdRealSecret123">',
        '<input id="api_key" value="ProdRealSecret123">',
        '<input type="password" value="ProdRealSecret123">',
        '<meta name="api_key" content="ProdRealSecret123">',
        '<config key="token" value="ProdRealSecret123"/>',
        "<input name=password value=ProdRealSecret123>",
        "<meta name=api_key content=ProdRealSecret123>",
        "<config key=token value=ProdRealSecret123>",
        'echo "${TOKEN:=ProdRealSecret123}"',
    ),
)
def test_source_secret_scanner_blocks_adversarial_literal_contexts(source):
    assert contains_secret(source)
    diff = "\n".join(f"+{line}" for line in source.splitlines())
    assert contains_history_secret(diff)


@pytest.mark.parametrize(
    "source",
    (
        "token == null",
        "token === undefined",
        "token => token.length",
        'token = "Bearer " + access_token',
        'headers["Authorization"] = "Bearer " + access_token',
        'headers["Authorization"] = f"Bearer {token}"',
        'headers["Authorization"] = `Bearer ${token}`',
        'token = read_secret("DISCORD_TOKEN")',
        'token = config.get("DISCORD_TOKEN")',
        "token = bytes(token_bytes)",
        "https://example.invalid/path?token=${TOKEN}",
        "https://user:${PASSWORD}@example.invalid/path",
        "redis://:${PASSWORD}@example.invalid/0",
        "<token>${TOKEN}</token>",
        '<input name="password" value="${PASSWORD}">',
        '<input id="api_key" value="${API_KEY}">',
        '<input type="password" value="${PASSWORD}">',
        '<meta name="api_key" content="replace-with-your-api-key">',
        '<config key="token" value="${TOKEN}"/>',
        '<config token="${TOKEN}"/>',
        "<input name=password value=${PASSWORD}>",
        'echo "${TOKEN:-}"',
    ),
)
def test_source_secret_scanner_does_not_treat_comparisons_as_assignments(source):
    assert not contains_secret(source)


@pytest.mark.parametrize(
    "source",
    (
        "token: {uid: request.auth.uid, isAdmin: true, exp: 1234567890}",
        "token: {firebase: {uid: request.auth.uid, isAdmin: false}}",
        'token: {uid: "visitor-fixture", sign_in_provider: "password"}',
        "token: {roles: roles, departments: user.departments}",
    ),
)
def test_source_secret_scanner_allows_safe_claim_scalars(source):
    assert not contains_secret(source)


@pytest.mark.parametrize(
    "source",
    (
        (
            '"github-token": re.compile('
            'r"\\bgithub_pat_[A-Za-z0-9_]{20,}\\b|'
            '\\bgh[pousr]_[A-Za-z0-9]{30,}\\b")'
        ),
        (
            '"discord-token": re.compile('
            'r"\\bmfa\\.[A-Za-z0-9_-]{20,}\\b|'
            '\\b[MN][A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{6}'
            '\\.[A-Za-z0-9_-]{20,}\\b")'
        ),
        (
            '"oauth-token": re.compile('
            'r"\\b(?:ya29\\.[A-Za-z0-9_-]{20,}|1//[A-Za-z0-9_-]{20,})\\b")'
        ),
    ),
)
def test_source_secret_scanner_allows_exact_reviewed_scanner_definitions(source):
    assert not contains_secret(source)


def test_current_source_redacts_isolated_literal_but_keeps_safe_context():
    source = "\n".join(
        (
            "def connect():",
            '    password = "ProdRealSecret123"',
            "    return client()",
        )
    )

    sanitized = redact_source_secrets(source)

    assert sanitized is not None
    content, count = sanitized
    assert count == 1
    assert "ProdRealSecret123" not in content
    assert "def connect():" in content
    assert "return client()" in content
    assert not contains_secret(content)


def test_current_source_redacts_direct_embedded_secret_without_assignment():
    source = "\n".join(
        (
            "# Connection example",
            "https://example.invalid/path?token=ProdRealSecret123",
            "# End example",
        )
    )

    sanitized = redact_source_secrets(source)

    assert sanitized is not None
    content, count = sanitized
    assert count == 1
    assert "ProdRealSecret123" not in content
    assert "# Connection example" in content
    assert "# End example" in content
    assert not contains_secret(content)


@pytest.mark.parametrize(
    "safe_assignment",
    (
        '    password = os.getenv(\n        "DB_PASSWORD",\n        ""\n    )',
        (
            "    const geminiApiKey = defineSecret(\n"
            '        "GEMINI_API_KEY"\n'
            "    );"
        ),
        (
            "    registerPushToken = onCall(\n"
            '        {region: "asia-northeast1", timeoutSeconds: 30},\n'
            "        runRegisterPushToken,\n"
            "    );"
        ),
        '    token = os.environ[\n        "DISCORD_TOKEN"\n    ]',
    ),
)
def test_current_source_redaction_keeps_safe_multiline_assignment(
    safe_assignment,
):
    source = "\n".join(
        (
            "def load():",
            safe_assignment,
            '    api_key = "ProdRealSecret123"',
            "    return value",
        )
    )

    sanitized = redact_source_secrets(source)

    assert sanitized is not None
    content, count = sanitized
    assert count == 1
    assert safe_assignment in content
    assert "ProdRealSecret123" not in content
    assert "return value" in content
    assert not contains_secret(content)


def test_current_source_redacts_bounded_multiline_but_fails_closed_for_private_key():
    multiline = (
        "def connect():\n"
        '    password = SecretStr(\n        "ProdRealSecret123"\n    )\n'
        "    return client()"
    )
    private_key = "-----BEGIN " + "PRIVATE KEY-----\nfixture-only\n"

    sanitized = redact_source_secrets(multiline)
    assert sanitized is not None
    content, count = sanitized
    assert count == 3
    assert "ProdRealSecret123" not in content
    assert "def connect():" in content
    assert "return client()" in content
    assert not contains_secret(content)
    assert redact_source_secrets(private_key) is None


@pytest.mark.parametrize("separator", ("\r", "\v", "\f", "\x85", "\u2028", "\u2029"))
def test_current_source_fails_closed_for_unsupported_line_separators(separator):
    source = (
        f"def connect():{separator}"
        f"    password = SecretStr({separator}"
        f'        "ProdRealSecret123"{separator}'
        f"    ){separator}"
        f"    return client(){separator}"
    )

    assert redact_source_secrets(source) is None


def test_current_source_keeps_non_secret_unicode_line_separator():
    source = "first paragraph\u2028second paragraph"

    assert redact_source_secrets(source) == (source, 0)


@pytest.mark.parametrize(
    "source",
    (
        "token /*" + "x" * 119_990,
        "<input name=password " + "x" * 119_978,
    ),
)
def test_source_secret_scanner_handles_adversarial_prefixes_in_linear_time(source):
    started = time.perf_counter()
    detected = contains_secret(source)
    scan_elapsed = time.perf_counter() - started

    started = time.perf_counter()
    sanitized = redact_source_secrets(source)
    redact_elapsed = time.perf_counter() - started

    assert not detected
    assert sanitized == (source, 0)
    assert scan_elapsed < 0.5
    assert redact_elapsed < 0.5


@pytest.mark.parametrize(
    "source",
    (
        ("token /* " * 15_000)[:120_000],
        ('token="${TOKEN}"; ' * 8_000)[:120_000],
    ),
)
def test_source_secret_scanner_fails_closed_for_dense_minified_lines(source):
    started = time.perf_counter()
    detected = contains_secret(source)
    scan_elapsed = time.perf_counter() - started

    started = time.perf_counter()
    sanitized = redact_source_secrets(source)
    redact_elapsed = time.perf_counter() - started

    assert detected
    assert sanitized is None
    assert scan_elapsed < 0.5
    assert redact_elapsed < 0.5


def test_source_secret_scanner_fails_closed_when_assignment_rhs_exceeds_bound():
    assignment = "password = choose_secret(\n" + "  runtime_value,\n" * 20 + ")"

    assert contains_secret(assignment)


def test_history_secret_scanner_fails_closed_for_removed_credentials():
    # Assemble unmistakably synthetic values at runtime so the repository does
    # not itself contain a contiguous token/private-key fixture.
    unquoted = "+client_secret: " + "runtime-value-" * 2
    jwt = "+authorization: Bearer " + ".".join(
        ("eyJfixtureHeader", "fixturePayload", "fixtureSignature")
    )
    pem = "+-----BEGIN " + "PRIVATE KEY-----\nfixture-only"

    assert contains_secret(unquoted)
    assert contains_history_secret(unquoted)
    assert contains_history_secret(jwt)
    assert contains_history_secret(pem)


def test_curated_document_policy_allows_only_approved_front_matter():
    source = "---\nstatus: active\nauthority: reviewed_record\nfestival: 27\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/example.md", source) == (
        "reviewed_record",
        "active",
        27,
        "restricted",
        None,
        None,
    )


def test_curated_document_policy_keeps_only_explicitly_reviewed_legacy_pages_visible():
    source = "---\nstatus: active\nfestival: 27\n---\n本文"

    reviewed = document_policy(
        curated_repository(),
        "knowledge/operations/yagapon-intake-retry.md",
        source,
    )
    unknown = document_policy(
        curated_repository(),
        "knowledge/operations/unknown-legacy.md",
        source,
    )

    assert reviewed[3] == "guild"
    assert unknown[3] == "restricted"


def test_repository_policy_uses_resolved_deployment_authority():
    repository = Repository(
        name="nyuji0524/Yagapon_Chatbot",
        ref="main",
        include=("bot/",),
    )

    assert document_policy(
        repository,
        "bot/client.py",
        "print('running')",
        default_authority="deployed_implementation",
        default_status="deployed",
    ) == ("deployed_implementation", "deployed", None, "guild", None, None)


def test_curated_document_policy_rejects_draft_and_missing_front_matter():
    draft = "---\nstatus: draft\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/draft.md", draft) is None
    assert document_policy(curated_repository(), "knowledge/plain.md", "本文") is None


def test_curated_document_policy_rejects_non_mapping_front_matter():
    source = "---\n- status\n- active\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/invalid.md", source) is None


def test_curated_document_policy_rejects_invalid_yaml():
    source = "---\nstatus: [active\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/invalid-yaml.md", source) is None


def test_curated_document_policy_accepts_crlf_front_matter():
    source = "---\r\nstatus: historical\r\nfestival: 27\r\n---\r\n本文"

    assert document_policy(curated_repository(), "knowledge/history.md", source) == (
        "curated",
        "historical",
        27,
        "restricted",
        None,
        None,
    )


def test_curated_document_policy_preserves_explicit_channel_visibility():
    source = (
        "---\nstatus: active\nfestival: 28\n"
        "access_scope: channel\nchannel_id: 123456\n---\n本文"
    )

    assert document_policy(curated_repository(), "knowledge/private.md", source) == (
        "curated",
        "active",
        28,
        "channel",
        123456,
        None,
    )


def test_curated_channel_scope_without_channel_id_fails_closed():
    source = "---\nstatus: active\naccess_scope: channel\n---\n本文"

    assert document_policy(curated_repository(), "knowledge/private.md", source) is None


def test_curated_document_date_is_propagated_to_search_metadata():
    source = (
        "---\nstatus: active\nfestival: 28\ndate: 2026-10-10\n"
        "access_scope: guild\n---\n本文"
    )
    policy = document_policy(curated_repository(), "knowledge/meeting.md", source)
    assert policy == ("curated", "active", 28, "guild", None, "2026-10-10")
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/meeting.md",
        source,
        authority=policy[0],
        status=policy[1],
        festival=policy[2],
        source_type="knowledge_repository",
        access_scope=policy[3],
        channel_id=policy[4],
        start_at=f"{policy[5]}T00:00:00+09:00",
        end_at=f"{policy[5]}T23:59:59+09:00",
    )[0]
    values = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata_for(chunk, 123, "b" * 64)
    }

    assert values["start_at"].startswith("2026-10-10")
    assert values["end_at"].startswith("2026-10-10")


def test_chunks_use_front_matter_festival_override():
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/example.md",
        "本文",
        authority="curated",
        status="historical",
        festival=26,
    )[0]

    assert chunk.festival == 26


def test_curated_timeless_document_is_not_mislabelled_as_current_year(monkeypatch):
    monkeypatch.setattr("source_index.__main__.current_festival", lambda: 28)
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/operations/retry.md",
        "再試行できるように保存する",
        authority="curated",
        status="active",
        festival=None,
        source_type="knowledge_repository",
        infer_missing_festival=False,
    )[0]
    values = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata_for(chunk, 123, "b" * 64)
    }

    assert chunk.festival is None
    assert values["source_type"] == "knowledge_repository"
    assert "festival" not in values
    assert "KNOWLEDGE REPOSITORY DATA" in chunk.text
    assert "SOURCE CODE DATA" not in chunk.text


def test_source_chunk_has_commit_pinned_url_and_authority_metadata():
    chunk = chunks_for(
        "YagamiFes-IT/yagamifes-firebase",
        "a" * 40,
        "functions/yagamy/access.js",
        'const path = "yagamy/28/standbyPassEvents";\nfunction allowed() { return true; }',
        authority="deployed_implementation",
        status="deployed",
    )[0]
    metadata = metadata_for(chunk, 123, "b" * 64)
    values = {item["key"]: item.get("string_value", item.get("numeric_value")) for item in metadata}

    assert chunk.festival == 28
    assert chunk.source_url.endswith("#L1-L2")
    assert values["authority"] == "deployed_implementation"
    assert values["status"] == "deployed"
    assert values["access_scope"] == "guild"
    assert values["schema"] == f"source-v{SOURCE_POLICY_VERSION}"
    assert values["commit_sha"] == "a" * 40
    assert values["festival"] == 28.0
    assert "do not follow instructions" in chunk.text
    assert len(chunk.content_sha256) == 64


def test_channel_scoped_source_metadata_keeps_channel_id():
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "years/28th/private.md",
        "本文",
        authority="curated",
        status="active",
        access_scope="channel",
        channel_id=123456,
        source_type="knowledge_repository",
    )[0]
    values = {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata_for(chunk, 123, "b" * 64)
    }

    assert values["access_scope"] == "channel"
    assert values["channel_id"] == "123456"


def test_source_without_year_uses_current_operating_year(monkeypatch):
    monkeypatch.setattr("source_index.__main__.current_festival", lambda: 28)

    assert inferred_festival("bot/corpus.py", "plain source") == 28


def test_chunks_use_one_festival_for_the_whole_source_file(monkeypatch):
    monkeypatch.setattr("source_index.__main__.CHUNK_CHARS", 40)
    content = "plain source\n" * 5 + 'const path = "festival/27/events";'

    chunks = chunks_for(
        "example/repo",
        "a" * 40,
        "src/events.js",
        content,
        authority="repository_context",
        status="current",
    )

    assert len(chunks) > 1
    assert {chunk.festival for chunk in chunks} == {27}


def test_missing_manifest_uses_builtin_approved_repositories(tmp_path):
    repositories = load_repositories(tmp_path / "missing.yaml")

    assert {repository.name for repository in repositories} >= {
        "nyuji0524/Yagapon_Chatbot",
        "YagamiFes-IT/yagamifes-firebase",
        "YagamiFes-IT/Yagamy",
    }
    knowledge = next(
        repository
        for repository in repositories
        if repository.name == "YagamiFes-IT/YagamiFes-IT-Knowledge"
    )
    assert knowledge.source_type == "knowledge_repository"
    assert knowledge.private
    assert knowledge.fetch == "archive"
    support = next(
        repository
        for repository in repositories
        if repository.name == "YagamiFes-IT/Yaga_Support"
    )
    assert support.private
    assert support.history
    assert support.fetch == "archive"
    web = next(
        repository
        for repository in repositories
        if repository.name == "YagamiFes-IT/Yagamifes_Web_2025"
    )
    assert web.private
    assert web.history
    assert web.fetch == "tree"
    yagamy = next(
        repository
        for repository in repositories
        if repository.name == "YagamiFes-IT/Yagamy"
    )
    assert yagamy.ref == "main"
    assert yagamy.private
    assert yagamy.history
    assert yagamy.fetch == "tree"
    assert yagamy.include == (
        "lib/",
        "test/",
        "docs/readMe26th/",
        "docs/implementations/",
        "docs/requirements/",
        "docs/map-navigation/",
        "pubspec.yaml",
        "firebase.json",
    )


def test_checked_in_manifest_covers_every_reviewed_source(monkeypatch):
    monkeypatch.delenv("YAGAPON_SOURCE_REPOSITORIES", raising=False)
    monkeypatch.setenv("YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST", "true")
    manifest = source_index_module.Path(source_index_module.__file__).with_name(
        "repositories.yaml"
    )
    repositories = load_repositories(manifest)

    assert {repository.name for repository in repositories} == {
        "nyuji0524/Yagapon_Chatbot",
        "YagamiFes-IT/Yaga_Support",
        "YagamiFes-IT/yagamifes-firebase",
        "YagamiFes-IT/Yagamifes_Web_2025",
        "YagamiFes-IT/Yagamy",
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
    }
    assert {repository.name for repository in repositories if repository.history} == {
        "YagamiFes-IT/Yaga_Support",
        "YagamiFes-IT/yagamifes-firebase",
        "YagamiFes-IT/Yagamifes_Web_2025",
        "YagamiFes-IT/Yagamy",
    }
    fetches = {repository.name: repository.fetch for repository in repositories}
    assert fetches["YagamiFes-IT/Yagamifes_Web_2025"] == "tree"
    assert fetches["YagamiFes-IT/Yagamy"] == "tree"
    assert fetches["YagamiFes-IT/Yaga_Support"] == "archive"
    assert fetches["YagamiFes-IT/yagamifes-firebase"] == "archive"
    assert fetches["YagamiFes-IT/YagamiFes-IT-Knowledge"] == "archive"


def test_repository_env_can_only_narrow_reviewed_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / "repositories.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "repositories": [
                    {
                        "name": "example/approved",
                        "ref": "main",
                        "include": ["src/"],
                    },
                    {
                        "name": "example/also-approved",
                        "ref": "main",
                        "include": ["app/"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "YAGAPON_SOURCE_REPOSITORIES",
        "example/approved",
    )

    repositories = load_repositories(manifest)

    assert [repository.name for repository in repositories] == ["example/approved"]

    monkeypatch.setenv(
        "YAGAPON_SOURCE_REPOSITORIES",
        "example/approved,example/not-in-manifest",
    )
    with pytest.raises(RuntimeError, match="outside the reviewed manifest"):
        load_repositories(manifest)


def test_full_manifest_guard_rejects_even_a_reviewed_fixed_subset(
    monkeypatch,
    tmp_path,
):
    manifest = tmp_path / "repositories.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "repositories": [
                    {
                        "name": "example/approved",
                        "ref": "main",
                        "include": ["src/"],
                    },
                    {
                        "name": "example/future-entry",
                        "ref": "main",
                        "include": ["app/"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST", "true")
    monkeypatch.setenv("YAGAPON_SOURCE_REPOSITORIES", "example/approved")

    with pytest.raises(
        RuntimeError,
        match="must be empty when YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST=true",
    ):
        load_repositories(manifest)


def test_repository_manifest_cannot_select_zero_sources(tmp_path):
    manifest = tmp_path / "repositories.yaml"
    manifest.write_text("repositories: []\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="selection is empty"):
        load_repositories(manifest)


def _history_settings(**overrides) -> SourceHistorySettings:
    values = {
        "since": "2024-09-30T15:00:00Z",
        "until": None,
        "max_commits": 2_000,
        "max_pages": 20,
        "max_files_per_commit": 300,
        "max_bytes_per_repository": 20_000_000,
        "commits_per_cycle": 100,
        "min_rate_remaining": 200,
        "max_snapshot_fallbacks_per_repository": 10,
    }
    values.update(overrides)
    return SourceHistorySettings(**values)


def _history_commit(value: str, day: int = 1) -> SourceHistoryCommit:
    return SourceHistoryCommit(
        sha=value * 40,
        committed_at=datetime(2025, 10, day, tzinfo=timezone.utc),
        message=f"commit {value}",
        source_url=f"https://github.com/example/repo/commit/{value * 40}",
    )


def test_source_history_settings_default_supports_measured_repository_size(monkeypatch):
    for name in (
        "YAGAPON_SOURCE_HISTORY_SINCE",
        "YAGAPON_SOURCE_HISTORY_UNTIL",
        "YAGAPON_SOURCE_HISTORY_MAX_COMMITS",
        "YAGAPON_SOURCE_HISTORY_MAX_PAGES",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = source_history_settings()

    assert settings.since == "2024-09-30T15:00:00Z"
    assert settings.max_commits == 2_000
    assert settings.max_pages * 100 >= settings.max_commits
    assert settings.commits_per_cycle == 100


def test_source_history_digest_tracks_snapshot_fallback_coverage():
    assert _history_settings(
        max_snapshot_fallbacks_per_repository=10
    ).digest != _history_settings(
        max_snapshot_fallbacks_per_repository=11
    ).digest


def test_private_tree_fetch_reads_only_allowlisted_blobs_and_preserves_rate_reserve(
    monkeypatch,
):
    client = GitHubSourceClient(lambda _repository: "installation-token")
    allowed = b"export const current = true;\n"
    responses = [
        (
            {
                "truncated": False,
                "tree": [
                    {
                        "path": "src/current.ts",
                        "type": "blob",
                        "size": len(allowed),
                        "sha": "a" * 40,
                    },
                    {
                        "path": "large/private.bin",
                        "type": "blob",
                        "size": 200_000_000,
                        "sha": "b" * 40,
                    },
                ],
            },
            {"X-RateLimit-Remaining": "900"},
        ),
        (
            {
                "encoding": "base64",
                "content": source_index_module.base64.b64encode(allowed).decode(),
            },
            {"X-RateLimit-Remaining": "899"},
        ),
    ]
    monkeypatch.setattr(
        client,
        "_request_with_headers",
        lambda *_args: responses.pop(0),
    )

    assert client.selected_files(
        "example/private",
        "c" * 40,
        ("src/",),
        _history_settings(),
    ) == [("src/current.ts", allowed)]
    assert responses == []

    monkeypatch.setattr(
        client,
        "_request_with_headers",
        lambda *_args: (
            {"truncated": False, "tree": []},
            {"X-RateLimit-Remaining": "200"},
        ),
    )
    with pytest.raises(GitHubRateLimitError, match="existing indexed generation"):
        client.selected_files(
            "example/private",
            "c" * 40,
            ("src/",),
            _history_settings(),
        )


def test_github_app_token_is_cached_and_restricted(monkeypatch):
    now = [1_700_000_000.0]
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self.payload).encode()

    def opener(request, timeout):
        assert timeout == 30
        requests.append(request)
        body = json.loads(request.data)
        assert body == {
            "repositories": ["Yaga_Support", "yagamifes-firebase"],
            "permissions": {"contents": "read"},
        }
        assert request.headers["Authorization"] == "Bearer app.jwt"
        return Response(
            {
                "token": f"installation-{len(requests)}",
                "expires_at": datetime.fromtimestamp(
                    now[0] + 3_600,
                    timezone.utc,
                ).isoformat(),
                "permissions": {"contents": "read", "metadata": "read"},
                "repositories": [
                    {"name": "Yaga_Support"},
                    {"name": "yagamifes-firebase"},
                ],
            }
        )

    monkeypatch.setattr("source_index.github_auth.jwt.encode", lambda *_a, **_kw: "app.jwt")
    provider = GitHubAppTokenProvider(
        app_id="Iv1abc",
        installation_id="123",
        private_key="-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
        repositories=("Yaga_Support", "yagamifes-firebase"),
        clock=lambda: now[0],
        opener=opener,
    )

    assert provider() == "installation-1"
    assert provider() == "installation-1"
    assert len(requests) == 1
    now[0] += 3_301
    assert provider() == "installation-2"
    assert len(requests) == 2


def test_github_app_rejects_unnecessary_read_permission(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "token": "installation-token",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "permissions": {
                        "contents": "read",
                        "metadata": "read",
                        "members": "read",
                    },
                    "repositories": [{"name": "Yaga_Support"}],
                }
            ).encode()

    monkeypatch.setattr("source_index.github_auth.jwt.encode", lambda *_a, **_kw: "app.jwt")
    provider = GitHubAppTokenProvider(
        app_id="123",
        installation_id="456",
        private_key="-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
        repositories=("Yaga_Support",),
        clock=lambda: 1_700_000_000,
        opener=lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(RuntimeError, match="beyond contents:read"):
        provider()


def test_github_app_rejects_repository_outside_requested_scope(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "token": "installation-token",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "permissions": {"contents": "read", "metadata": "read"},
                    "repositories": [
                        {"name": "yaga_support"},
                        {"name": "Unrequested_Private_Repository"},
                    ],
                }
            ).encode()

    monkeypatch.setattr("source_index.github_auth.jwt.encode", lambda *_a, **_kw: "app.jwt")
    provider = GitHubAppTokenProvider(
        app_id="123",
        installation_id="456",
        private_key="-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
        repositories=("Yaga_Support",),
        clock=lambda: 1_700_000_000,
        opener=lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(RuntimeError, match="outside the configured restriction") as exc_info:
        provider()
    assert "Unrequested_Private_Repository" not in str(exc_info.value)


def test_github_app_router_is_anonymous_for_public_repository(monkeypatch):
    repositories = [
        Repository("public/repo", "main", ("src/",)),
        Repository("org/private", "main", ("src/",), private=True),
    ]
    secrets = {
        "YAGAPON_SOURCE_GITHUB_APP_ID": "123",
        "YAGAPON_SOURCE_GITHUB_APP_INSTALLATION_ID": "456",
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY": (
            "-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----"
        ),
    }
    monkeypatch.setattr(
        source_index_module,
        "read_secret",
        lambda name: secrets.get(name, ""),
    )
    provider_kwargs = {}

    def provider(**kwargs):
        provider_kwargs.update(kwargs)
        return lambda: "installation-token"

    monkeypatch.setattr(source_index_module, "GitHubAppTokenProvider", provider)

    router = github_token_provider(repositories)

    assert provider_kwargs["repositories"] == ("private",)
    assert router("public/repo") == ""
    assert router("org/private") == "installation-token"
    assert router("org/not-allowlisted") == ""


def test_private_repository_without_credentials_fails_clearly(monkeypatch):
    monkeypatch.setattr(source_index_module, "read_secret", lambda _name: "")
    repository = Repository(
        "org/private",
        "main",
        ("src/",),
        private=True,
    )

    with pytest.raises(RuntimeError, match="require a read-only GitHub App"):
        github_token_provider([repository])


def test_history_listing_pages_and_stops_at_rate_reserve(monkeypatch):
    client = GitHubSourceClient(lambda _repository: "token")
    pages = [
        (
            [
                {
                    "sha": "a" * 40,
                    "commit": {
                        "message": "new feature",
                        "committer": {"date": "2025-10-02T00:00:00Z"},
                    },
                }
            ],
            {"Link": '<next>; rel="next"', "X-RateLimit-Remaining": "999"},
        ),
        (
            [
                {
                    "sha": "b" * 40,
                    "commit": {
                        "message": "older change",
                        "committer": {"date": "2025-10-01T00:00:00Z"},
                    },
                }
            ],
            {"X-RateLimit-Remaining": "998"},
        ),
    ]
    monkeypatch.setattr(client, "_request_with_headers", lambda *_args: pages.pop(0))

    commits = client.list_history_commits(
        "example/repo",
        "c" * 40,
        _history_settings(max_pages=2),
    )

    assert [commit.sha for commit in commits] == ["a" * 40, "b" * 40]

    monkeypatch.setattr(
        client,
        "_request_with_headers",
        lambda *_args: ([], {"X-RateLimit-Remaining": "200"}),
    )
    with pytest.raises(GitHubRateLimitError, match="existing indexed generation"):
        client.list_history_commits(
            "example/repo",
            "c" * 40,
            _history_settings(),
        )


def test_history_details_records_omitted_patch_and_exact_sha_source(monkeypatch):
    client = GitHubSourceClient("")
    content = "def current():\n    return True\n"
    unquoted_patch = "+client_secret: " + "fixture-value-" * 2
    jwt_patch = "+authorization: Bearer " + ".".join(
        ("eyJfixtureHeader", "fixturePayload", "fixtureSignature")
    )
    pem_patch = "+-----BEGIN " + "PRIVATE KEY-----\n+fixture-only"

    def request(_repository, path):
        if path.startswith("/commits/"):
            return (
                {
                    "files": [
                        {
                            "filename": "src/current.py",
                            "status": "modified",
                            "additions": 2,
                            "deletions": 1,
                        },
                        {
                            "filename": "src/config.py",
                            "status": "modified",
                            "patch": '+api_key = "1234567890abcdefghijklmnop"',
                            "additions": 1,
                            "deletions": 0,
                        },
                        {
                            "filename": ".env",
                            "status": "modified",
                            "patch": "+SECRET=value",
                            "additions": 1,
                            "deletions": 0,
                        },
                        {
                            "filename": "src/config.yaml",
                            "status": "modified",
                            "patch": unquoted_patch,
                            "additions": 1,
                            "deletions": 0,
                        },
                        {
                            "filename": "src/auth.py",
                            "status": "modified",
                            "patch": jwt_patch,
                            "additions": 1,
                            "deletions": 0,
                        },
                        {
                            "filename": "src/key.py",
                            "status": "modified",
                            "patch": pem_patch,
                            "additions": 2,
                            "deletions": 0,
                        },
                    ]
                },
                {"X-RateLimit-Remaining": "900"},
            )
        if path.startswith("/contents/src/current.py"):
            return (
                {
                    "type": "file",
                    "size": len(content.encode()),
                    "encoding": "base64",
                    "content": source_index_module.base64.b64encode(
                        content.encode()
                    ).decode(),
                },
                {"X-RateLimit-Remaining": "899"},
            )
        raise AssertionError(path)

    monkeypatch.setattr(client, "_request_with_headers", request)
    details = client.history_details(
        "example/repo",
        [_history_commit("a")],
        ("src/",),
        _history_settings(),
    )

    assert len(details[0].changes) == 5
    current, secret, unquoted, jwt, pem = details[0].changes
    assert current.patch_state == "omitted"
    assert "patch omitted" in current.patch
    assert current.source_excerpt == content
    assert secret.patch_state == "redacted_secret"
    assert "credential-like" in secret.patch
    for redacted in (unquoted, jwt, pem):
        assert redacted.patch_state == "redacted_secret"
        assert redacted.patch == "[patch redacted by credential-like content policy]"


def test_history_chunk_has_exact_sha_citation_and_mandatory_fields():
    commit = SourceHistoryCommit(
        sha="a" * 40,
        committed_at=datetime(2025, 10, 9, 12, 0, tzinfo=timezone.utc),
        message="Implement queue operation",
        source_url=f"https://github.com/example/repo/commit/{'a' * 40}",
        changes=(
            SourceHistoryChange(
                path="src/queue.py",
                status="modified",
                patch="@@ -1 +1 @@\n-old\n+new",
                patch_state="available",
            ),
        ),
    )
    repository = Repository("example/repo", "main", ("src/",), history=True)

    chunk = history_chunks_for(repository, commit)[0]
    values = _metadata_map(metadata_for(chunk, 1, "b" * 64))

    assert chunk.source_type == "source_code_history"
    assert chunk.authority == "repository_history"
    assert chunk.status == "historical"
    assert chunk.access_scope == "guild"
    assert chunk.festival == 27
    assert chunk.source_url.endswith("/commit/" + "a" * 40)
    assert 'commit_message: "Implement queue operation"' in chunk.text
    assert "changed_path: src/queue.py" in chunk.text
    assert "change_status: modified" in chunk.text
    assert "patch_state: available" in chunk.text
    assert values["change_status"] == "modified"
    assert values["start_at"] == "2025-10-09T12:00:00Z"


def test_history_selection_is_bounded_round_robin_and_resumes():
    repositories = [
        Repository("example/one", "main", ("src/",), history=True),
        Repository("example/two", "main", ("src/",), history=True),
    ]
    catalogs = {
        "example/one": [_history_commit("a"), _history_commit("b")],
        "example/two": [_history_commit("c"), _history_commit("d")],
    }
    processed = {"example/one": {"a" * 40}, "example/two": set()}

    selected = select_history_commits(repositories, catalogs, processed, 3)

    assert [item.sha for item in selected["example/one"]] == ["b" * 40]
    assert [item.sha for item in selected["example/two"]] == [
        "c" * 40,
        "d" * 40,
    ]


def test_processed_history_cursor_requires_complete_remote_checkpoint():
    repository = Repository(
        "example/repo",
        "main",
        ("src/",),
        history=True,
    )
    commit = SourceHistoryCommit(
        **{
            **_history_commit("a").__dict__,
            "changes": (
                SourceHistoryChange(
                    path="src/app.py",
                    status="modified",
                    patch="+new",
                ),
            ),
        }
    )
    chunk = history_chunks_for(repository, commit)[0]
    entry = _source_state_entry(chunk, "documents/history")
    previous = {
        "policy_version": SOURCE_POLICY_VERSION,
        "manifest_digest": repository_manifest_digest(repository),
        "history_settings_digest": _history_settings().digest,
        "history_processed_commits": [commit.sha],
        "history_commit_document_counts": {commit.sha: 1},
    }

    assert valid_processed_history_commits(
        repository,
        previous,
        {chunk.key: entry},
        [commit],
        _history_settings(),
        remote_names={"documents/history"},
        local_remote_names={chunk.key: "documents/history"},
    ) == {commit.sha}
    assert not valid_processed_history_commits(
        repository,
        previous,
        {chunk.key: entry},
        [commit],
        _history_settings(),
        remote_names=set(),
        local_remote_names={chunk.key: "documents/history"},
    )


def test_unreadable_release_metadata_is_non_fatal(tmp_path, monkeypatch):
    release = tmp_path / "releases" / "current.json"

    def denied(_self):
        raise PermissionError("denied")

    monkeypatch.setattr("pathlib.Path.exists", denied)

    assert deployed_refs(release) == {}


def test_source_upload_concurrency_is_bounded(monkeypatch):
    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "99")
    assert source_upload_concurrency() == 8

    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "0")
    assert source_upload_concurrency() == 1

    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "invalid")
    assert source_upload_concurrency() == 2


def test_source_state_reindexes_when_policy_commit_or_festival_changes():
    chunk = chunks_for(
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "a" * 40,
        "knowledge/example.md",
        "本文",
        authority="curated",
        status="historical",
        festival=27,
        source_type="knowledge_repository",
        infer_missing_festival=False,
    )[0]
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()
    current = {
        "sha256": digest,
        "content_sha256": chunk.content_sha256,
        "commit": chunk.commit,
        "authority": chunk.authority,
        "status": chunk.status,
        "source_type": chunk.source_type,
        "change_status": chunk.change_status,
        "festival": chunk.festival,
        "policy_version": SOURCE_POLICY_VERSION,
        "schema": f"source-v{SOURCE_POLICY_VERSION}",
        "metadata_sha256": metadata_digest_for(chunk, 0, digest),
        "remote_name": "documents/current",
    }

    assert state_entry_matches_chunk(current, chunk, digest)
    assert not state_entry_matches_chunk(
        current,
        chunk,
        digest,
        remote_names={"documents/missing"},
    )
    assert not state_entry_matches_chunk(
        current,
        chunk,
        digest,
        local_remote_names={chunk.key: "documents/mismatched"},
    )
    assert state_entry_matches_chunk(
        current,
        chunk,
        digest,
        remote_names={"documents/current"},
        local_remote_names={chunk.key: "documents/current"},
    )
    for key, replacement in (
        ("policy_version", 1),
        ("commit", "b" * 40),
        ("festival", 28),
        ("sha256", "0" * 64),
    ):
        assert not state_entry_matches_chunk(
            {**current, key: replacement},
            chunk,
            digest,
        )


def test_repository_fast_path_requires_a_complete_nonempty_checkpoint():
    repository = Repository(
        name="example/repo",
        ref="main",
        include=("src/",),
    )
    commit = "a" * 40
    previous = {
        "commit": commit,
        "authority": "repository_context",
        "status": "current",
        "source_type": "source_code",
        "policy_version": SOURCE_POLICY_VERSION,
        "manifest_digest": repository_manifest_digest(repository),
        "document_count": 1,
    }
    entry = {
        "document_key": "source-key",
        "commit": commit,
        "policy_version": SOURCE_POLICY_VERSION,
        "schema": f"source-v{SOURCE_POLICY_VERSION}",
        "source_type": "source_code",
        "authority": "repository_context",
        "status": "current",
        "festival": 28,
        "remote_name": "documents/source",
        "metadata_sha256": "b" * 64,
    }

    assert repository_state_is_current(
        repository,
        previous,
        [entry],
        commit=commit,
        authority="repository_context",
        status="current",
    )
    assert not repository_state_is_current(
        repository,
        {**previous, "document_count": 0},
        [],
        commit=commit,
        authority="repository_context",
        status="current",
    )
    assert not repository_state_is_current(
        repository,
        previous,
        [{**entry, "remote_name": ""}],
        commit=commit,
        authority="repository_context",
        status="current",
    )
    assert not repository_state_is_current(
        repository,
        previous,
        [entry],
        commit=commit,
        authority="repository_context",
        status="current",
        remote_names={"documents/some-other-document"},
    )
    assert not repository_state_is_current(
        repository,
        previous,
        [entry],
        commit=commit,
        authority="repository_context",
        status="current",
        remote_names={"documents/source"},
        local_remote_names={"source-key": "documents/other"},
    )


def test_subset_sync_only_removes_documents_for_configured_repositories():
    documents = {
        "bot-old": {"repository": "nyuji0524/Yagapon_Chatbot"},
        "knowledge-live": {"repository": "YagamiFes-IT/YagamiFes-IT-Knowledge"},
        "firebase-preserved": {"repository": "YagamiFes-IT/yagamifes-firebase"},
    }

    assert stale_managed_document_keys(
        documents,
        {"knowledge-live"},
        {"YagamiFes-IT/YagamiFes-IT-Knowledge"},
    ) == set()
    assert stale_managed_document_keys(
        documents,
        set(),
        {"nyuji0524/Yagapon_Chatbot"},
    ) == {"bot-old"}


def _source_state_entry(chunk, remote_name: str, guild_id: int = 1) -> dict:
    digest = hashlib.sha256(chunk.text.encode()).hexdigest()
    return {
        "document_key": chunk.key,
        "sha256": digest,
        "content_sha256": chunk.content_sha256,
        "remote_name": remote_name,
        "repository": chunk.repository,
        "path": chunk.path,
        "commit": chunk.commit,
        "festival": chunk.festival,
        "authority": chunk.authority,
        "status": chunk.status,
        "source_type": chunk.source_type,
        "change_status": chunk.change_status,
        "policy_version": SOURCE_POLICY_VERSION,
        "schema": f"source-v{SOURCE_POLICY_VERSION}",
        "metadata_sha256": metadata_digest_for(chunk, guild_id, digest),
    }


def _metadata_map(metadata: list[dict]) -> dict:
    return {
        item["key"]: item.get("string_value", item.get("numeric_value"))
        for item in metadata
    }


@pytest.mark.asyncio
async def test_source_preflight_is_a_safe_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "false")
    monkeypatch.setattr(
        source_index_module,
        "source_history_settings",
        lambda: pytest.fail("disabled preflight must not parse source settings"),
    )
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        state=tmp_path / "state.json",
        guild_id=None,
    )

    assert await preflight_once(args) == {"disabled": True, "preflight": True}
    assert not args.state.exists()


@pytest.mark.asyncio
async def test_source_preflight_rejects_a_stale_production_repository_filter(
    tmp_path,
    monkeypatch,
):
    manifest = tmp_path / "repositories.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "repositories": [
                    {"name": "example/one", "ref": "main", "include": ["src/"]},
                    {"name": "example/two", "ref": "main", "include": ["app/"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST", "true")
    monkeypatch.setenv("YAGAPON_SOURCE_REPOSITORIES", "example/one")
    monkeypatch.setattr(source_index_module, "source_history_settings", lambda: None)
    monkeypatch.setattr(
        source_index_module,
        "GitHubSourceClient",
        lambda *_args: pytest.fail("invalid selection must fail before GitHub access"),
    )
    args = SimpleNamespace(
        manifest=manifest,
        state=tmp_path / "state.json",
        guild_id=None,
    )

    with pytest.raises(
        RuntimeError,
        match="must be empty when YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST=true",
    ):
        await preflight_once(args)


@pytest.mark.asyncio
async def test_source_preflight_resolves_every_repo_and_samples_one_document(
    tmp_path,
    monkeypatch,
):
    repositories = [
        Repository("example/one", "main", ("src/",)),
        Repository("example/two", "release", ("app/",)),
    ]
    provider = object()
    resolved = []
    history_checks = []
    list_calls = []

    class FakeGitHubClient:
        def resolve(self, repository, ref):
            resolved.append((repository, ref))
            return f"resolved-{repository}"

    def documents():
        yield SimpleNamespace(name="documents/one")
        raise AssertionError("preflight must not enumerate the File Search store")

    def list_documents(**kwargs):
        list_calls.append(kwargs)
        return documents()

    fake_genai_client = SimpleNamespace(
        file_search_stores=SimpleNamespace(
            documents=SimpleNamespace(list=list_documents),
        ),
    )
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "fixture-google-key")
    monkeypatch.delenv("YAGAPON_CORPUS_STORE_NAME", raising=False)
    monkeypatch.setattr(
        source_index_module,
        "source_history_settings",
        lambda: history_checks.append(True),
    )
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: repositories,
    )
    monkeypatch.setattr(
        source_index_module,
        "github_token_provider",
        lambda loaded: provider if loaded == repositories else pytest.fail(),
    )
    monkeypatch.setattr(
        source_index_module,
        "GitHubSourceClient",
        lambda loaded_provider: (
            FakeGitHubClient()
            if loaded_provider is provider
            else pytest.fail("unexpected token provider")
        ),
    )
    monkeypatch.setattr(
        source_index_module,
        "ConfigManager",
        lambda: SimpleNamespace(get_corpus=lambda guild_id: f"stores/{guild_id}"),
    )
    monkeypatch.setattr(
        source_index_module.genai,
        "Client",
        lambda *, api_key: (
            fake_genai_client
            if api_key == "fixture-google-key"
            else pytest.fail("unexpected Google API key")
        ),
    )
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        state=tmp_path / "state.json",
        guild_id=123,
    )

    result = await preflight_once(args)

    assert history_checks == [True]
    assert resolved == [("example/one", "main"), ("example/two", "release")]
    assert list_calls == [
        {"parent": "stores/123", "config": {"page_size": 20}},
    ]
    assert result == {
        "preflight": True,
        "repositories_checked": 2,
        "repositories": ["example/one", "example/two"],
        "file_search_store": "stores/123",
        "remote_documents_sampled": 1,
        "state_documents": 0,
    }
    assert not args.state.exists()


@pytest.mark.asyncio
async def test_source_preflight_rejects_invalid_history_settings_before_network(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SOURCE_HISTORY_MAX_COMMITS", "not-an-integer")
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: pytest.fail("invalid settings must fail before network setup"),
    )
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        state=tmp_path / "state.json",
        guild_id=123,
    )

    with pytest.raises(RuntimeError, match="MAX_COMMITS must be an integer"):
        await preflight_once(args)


@pytest.mark.asyncio
async def test_source_preflight_rejects_invalid_state_before_network(
    tmp_path,
    monkeypatch,
):
    state = tmp_path / "state.json"
    state.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setattr(source_index_module, "source_history_settings", lambda: None)
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: pytest.fail("invalid state must fail before network setup"),
    )
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        state=state,
        guild_id=123,
    )

    with pytest.raises(RuntimeError, match="state must be a JSON object"):
        await preflight_once(args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            {"documents": {"source:one": "broken"}},
            "document entry must be a JSON object",
        ),
        (
            {"documents": {}, "repositories": {"example/repo": []}},
            "repository entry must be a JSON object",
        ),
        (
            {
                "documents": {"source:one": {"repository": ["example/repo"]}},
            },
            "document field must be a string: repository",
        ),
        (
            {
                "documents": {},
                "repositories": {
                    "example/repo": {"history_commit_document_counts": {"abc": "1"}}
                },
            },
            "history counts must be non-negative integers",
        ),
    ),
)
async def test_source_preflight_rejects_nested_corrupt_state_before_network(
    tmp_path,
    monkeypatch,
    payload,
    message,
):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setattr(source_index_module, "source_history_settings", lambda: None)
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: pytest.fail("invalid state must fail before network setup"),
    )
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        state=state,
        guild_id=123,
    )

    with pytest.raises(RuntimeError, match=message):
        await preflight_once(args)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ('{"documents": []}', "documents must be a JSON object"),
        (
            '{"documents": {}, "repositories": []}',
            "repositories must be a JSON object",
        ),
        ("{not-json", "state is unreadable"),
    ),
)
def test_load_state_rejects_corrupt_shapes(tmp_path, payload, message):
    state = tmp_path / "state.json"
    state.write_text(payload, encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        load_state(state)


@pytest.mark.asyncio
async def test_history_sync_selects_bounded_details_and_reports_cursor(
    tmp_path,
    monkeypatch,
):
    repository = Repository(
        "example/repo",
        "main",
        ("src/",),
        history=True,
    )
    catalog = [_history_commit("a"), _history_commit("b"), _history_commit("c")]

    class FakeClient:
        selected = []

        def resolve(self, _repository, _ref):
            return "f" * 40

        def list_history_commits(self, _repository, _commit, _settings):
            return catalog

        def archive(self, _repository, _commit, _include):
            return [("src/app.py", b"print('current')")]

        def history_details(self, _repository, commits, _include, _settings):
            self.selected = [commit.sha for commit in commits]
            return [
                SourceHistoryCommit(
                    sha=commit.sha,
                    committed_at=commit.committed_at,
                    message=commit.message,
                    source_url=commit.source_url,
                    changes=(
                        SourceHistoryChange(
                            path="src/app.py",
                            status="modified",
                            patch=f"+{commit.sha}",
                        ),
                    ),
                )
                for commit in commits
            ]

    client = FakeClient()
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SOURCE_HISTORY_COMMITS_PER_CYCLE", "2")
    monkeypatch.setattr(source_index_module, "read_secret", lambda _key: "")
    monkeypatch.setattr(source_index_module, "GitHubSourceClient", lambda _token: client)
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: [repository],
    )
    monkeypatch.setattr(
        source_index_module,
        "ConfigManager",
        lambda: SimpleNamespace(get_corpus=lambda _guild_id: "stores/test"),
    )
    monkeypatch.setattr(source_index_module, "deployed_refs", lambda _path: {})
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        guild_id=1,
        dry_run=True,
        state=tmp_path / "state.json",
        release=tmp_path / "release.json",
    )

    result = await sync_once(args)

    assert client.selected == ["a" * 40, "b" * 40]
    assert result["history_commits_selected"] == 2
    assert result["history_commits_remaining"] == 1
    assert result["indexed"] == 3


@pytest.mark.asyncio
async def test_history_rate_failure_preserves_existing_generation(
    tmp_path,
    monkeypatch,
):
    repository = Repository(
        "example/repo",
        "main",
        ("src/",),
        history=True,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": {
                    "existing": {
                        "repository": repository.name,
                        "remote_name": "documents/existing",
                    }
                },
                "repositories": {},
            }
        ),
        encoding="utf-8",
    )
    original = state_path.read_text(encoding="utf-8")

    class FakeClient:
        def resolve(self, _repository, _ref):
            return "f" * 40

        def list_history_commits(self, *_args):
            raise GitHubRateLimitError("rate reserve reached")

    class FakeCorpus:
        def __init__(self):
            self.rag_store = SimpleNamespace(document_references=lambda _guild: [])

        async def list_remote_documents(self, _store):
            return []

        async def _upload_document(self, *_args):
            raise AssertionError("rate failure must happen before upload")

        async def _delete_documents(self, *_args):
            raise AssertionError("rate failure must not delete")

    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setattr(source_index_module, "read_secret", lambda _key: "")
    monkeypatch.setattr(
        source_index_module,
        "GitHubSourceClient",
        lambda _token: FakeClient(),
    )
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: [repository],
    )
    monkeypatch.setattr(
        source_index_module,
        "ConfigManager",
        lambda: SimpleNamespace(get_corpus=lambda _guild_id: "stores/test"),
    )
    monkeypatch.setattr(source_index_module, "CorpusManager", FakeCorpus)
    monkeypatch.setattr(source_index_module, "deployed_refs", lambda _path: {})
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        guild_id=1,
        dry_run=False,
        state=state_path,
        release=tmp_path / "release.json",
    )

    with pytest.raises(GitHubRateLimitError, match="rate reserve"):
        await sync_once(args)

    assert state_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_failed_source_generation_restores_previous_sqlite_mirror(
    tmp_path,
    monkeypatch,
):
    repository = Repository(
        name="example/repo",
        ref="main",
        include=("src/",),
    )
    old_commit = "a" * 40
    new_commit = "b" * 40
    old_files = [("src/one.py", "old one"), ("src/two.py", "old two")]
    new_files = [("src/one.py", b"new one"), ("src/two.py", b"new two")]
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    remote_documents: dict[str, dict] = {}
    documents = {}

    for index, (path, content) in enumerate(old_files, start=1):
        chunk = chunks_for(
            repository.name,
            old_commit,
            path,
            content,
            authority="repository_context",
            status="current",
            festival=28,
        )[0]
        remote_name = f"documents/old-{index}"
        digest = hashlib.sha256(chunk.text.encode()).hexdigest()
        upload_metadata = metadata_for(chunk, 1, digest)
        rag_store.upsert_document(remote_name, chunk.text, upload_metadata)
        documents[chunk.key] = _source_state_entry(chunk, remote_name)
        remote_documents[remote_name] = _metadata_map(upload_metadata)

    state = {
        "schema_version": 1,
        "documents": documents,
        "repositories": {},
    }
    state_path = tmp_path / "source-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    original_state = state_path.read_text(encoding="utf-8")

    class FakeCorpus:
        def __init__(self):
            self.rag_store = rag_store
            self.uploads = 0

        async def list_remote_documents(self, _store_name):
            return [
                {"name": name, "metadata": metadata}
                for name, metadata in remote_documents.items()
            ]

        async def _upload_document(self, _store, _display, text, metadata):
            self.uploads += 1
            if self.uploads == 2:
                return None
            remote_name = "documents/new-one"
            rag_store.upsert_document(remote_name, text, metadata)
            remote_documents[remote_name] = _metadata_map(metadata)
            return remote_name

        async def _delete_documents(self, names):
            deleted = [name for name in names if name in remote_documents]
            for name in deleted:
                remote_documents.pop(name)
            rag_store.delete_remote_documents(deleted)
            return len(deleted)

    corpus = FakeCorpus()
    client = SimpleNamespace(
        resolve=lambda _repository, _ref: new_commit,
        archive=lambda _repository, _commit, _includes: new_files,
    )
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setenv("YAGAPON_SOURCE_UPLOAD_CONCURRENCY", "1")
    monkeypatch.setattr(source_index_module, "read_secret", lambda _key: "")
    monkeypatch.setattr(
        source_index_module,
        "GitHubSourceClient",
        lambda _token: client,
    )
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: [repository],
    )
    monkeypatch.setattr(
        source_index_module,
        "ConfigManager",
        lambda: SimpleNamespace(get_corpus=lambda _guild_id: "stores/test"),
    )
    monkeypatch.setattr(source_index_module, "CorpusManager", lambda: corpus)
    monkeypatch.setattr(source_index_module, "deployed_refs", lambda _path: {})
    monkeypatch.setattr(source_index_module, "current_festival", lambda: 28)
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        guild_id=1,
        dry_run=False,
        state=state_path,
        release=tmp_path / "release.json",
    )

    with pytest.raises(RuntimeError, match="Source generation failed"):
        await sync_once(args)

    assert state_path.read_text(encoding="utf-8") == original_state
    for index, (path, content) in enumerate(old_files, start=1):
        chunk = chunks_for(
            repository.name,
            old_commit,
            path,
            content,
            authority="repository_context",
            status="current",
            festival=28,
        )[0]
        restored = rag_store.get_document(chunk.key)
        assert restored["remote_name"] == f"documents/old-{index}"
        assert content in restored["body"]
    assert "documents/new-one" not in remote_documents


@pytest.mark.parametrize(
    "archive",
    [
        [],
        [("src/config.py", b'api_key = "1234567890abcdefghijklmnop"')],
    ],
    ids=("empty", "all-excluded"),
)
@pytest.mark.asyncio
async def test_empty_or_all_excluded_archive_fails_closed_without_replacing_checkpoint(
    tmp_path,
    monkeypatch,
    archive,
):
    repository = Repository(
        name="example/repo",
        ref="main",
        include=("src/",),
    )
    old_commit = "a" * 40
    old_chunk = chunks_for(
        repository.name,
        old_commit,
        "src/config.py",
        "print('safe')",
        authority="repository_context",
        status="current",
        festival=28,
    )[0]
    old_remote = "documents/old"
    old_digest = hashlib.sha256(old_chunk.text.encode()).hexdigest()
    old_metadata = metadata_for(old_chunk, 1, old_digest)
    rag_store = RagStore(tmp_path / "rag.sqlite3")
    rag_store.upsert_document(old_remote, old_chunk.text, old_metadata)
    state = {
        "schema_version": 1,
        "documents": {
            old_chunk.key: _source_state_entry(old_chunk, old_remote),
        },
        "repositories": {},
    }
    state_path = tmp_path / "source-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    original_state = state_path.read_text(encoding="utf-8")

    class FakeCorpus:
        def __init__(self):
            self.rag_store = rag_store

        async def list_remote_documents(self, _store_name):
            return [{"name": old_remote, "metadata": _metadata_map(old_metadata)}]

        async def _upload_document(self, *_args, **_kwargs):
            raise AssertionError("an excluded archive must not upload")

        async def _delete_documents(self, _names):
            raise AssertionError("an excluded archive must not delete")

    corpus = FakeCorpus()
    client = SimpleNamespace(
        resolve=lambda _repository, _ref: "b" * 40,
        archive=lambda _repository, _commit, _includes: archive,
    )
    monkeypatch.setenv("YAGAPON_SOURCE_INDEX_ENABLED", "true")
    monkeypatch.setattr(source_index_module, "read_secret", lambda _key: "")
    monkeypatch.setattr(
        source_index_module,
        "GitHubSourceClient",
        lambda _token: client,
    )
    monkeypatch.setattr(
        source_index_module,
        "load_repositories",
        lambda _manifest: [repository],
    )
    monkeypatch.setattr(
        source_index_module,
        "ConfigManager",
        lambda: SimpleNamespace(get_corpus=lambda _guild_id: "stores/test"),
    )
    monkeypatch.setattr(source_index_module, "CorpusManager", lambda: corpus)
    monkeypatch.setattr(source_index_module, "deployed_refs", lambda _path: {})
    args = SimpleNamespace(
        manifest=tmp_path / "repositories.yaml",
        guild_id=1,
        dry_run=False,
        state=state_path,
        release=tmp_path / "release.json",
    )

    with pytest.raises(RuntimeError, match="produced no approved documents"):
        await sync_once(args)

    assert state_path.read_text(encoding="utf-8") == original_state
    assert rag_store.get_document(old_chunk.key)["remote_name"] == old_remote
