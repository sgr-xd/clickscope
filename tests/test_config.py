"""Configuration loading: single-cluster env, profiles file, and limit layering."""

from __future__ import annotations

import json
import os

import pytest

from clickscope.config import AppConfig, ConfigError, Limits
from clickscope.registry import ClusterRegistry, UnknownCluster

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits are synthesized on Windows"
)

CH_ENV = (
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "CLICKHOUSE_PASSWORD_FILE",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_SECURE",
    "CLICKSCOPE_PROFILES",
    "CLICKSCOPE_AUDIT_LOG",
    "CLICKSCOPE_MAX_LIMIT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CH_ENV:
        monkeypatch.delenv(name, raising=False)


class TestSingleClusterFromEnv:
    def test_builds_one_cluster_named_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example")
        monkeypatch.setenv("CLICKHOUSE_DATABASE", "metrics")
        config = AppConfig.from_env()
        assert list(config.clusters) == ["default"]
        assert config.default_cluster == "default"
        assert config.clusters["default"].host == "ch.example"
        assert config.clusters["default"].database == "metrics"

    def test_port_defaults_follow_secure_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example")
        assert AppConfig.from_env().clusters["default"].port == 8123
        monkeypatch.setenv("CLICKHOUSE_SECURE", "true")
        assert AppConfig.from_env().clusters["default"].port == 8443

    def test_missing_host_is_an_actionable_error(self) -> None:
        with pytest.raises(ConfigError, match="CLICKSCOPE_PROFILES"):
            AppConfig.from_env()

    def test_password_file_wins_over_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        secret = tmp_path / "pw"
        secret.write_text("from-file\n")
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example")
        monkeypatch.setenv("CLICKHOUSE_PASSWORD", "from-env")
        monkeypatch.setenv("CLICKHOUSE_PASSWORD_FILE", str(secret))
        assert AppConfig.from_env().clusters["default"].password == "from-file"

    def test_password_never_appears_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example")
        monkeypatch.setenv("CLICKHOUSE_PASSWORD", "hunter2")
        cluster = AppConfig.from_env().clusters["default"]
        assert "hunter2" not in repr(cluster)
        assert "hunter2" not in json.dumps(cluster.describe())


def write_profiles(tmp_path, payload: dict) -> str:
    path = tmp_path / "clusters.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return str(path)


class TestProfilesFile:
    def test_loads_multiple_clusters(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        path = write_profiles(
            tmp_path,
            {
                "default": "prod",
                "clusters": {
                    "prod": {"host": "clickhouse-prod.example.com", "database": "analytics"},
                    "dev": {"host": "clickhouse-staging.example.com", "database": "analytics"},
                },
            },
        )
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        config = AppConfig.from_env()
        assert sorted(config.clusters) == ["dev", "prod"]
        assert config.default_cluster == "prod"

    def test_default_falls_back_to_first_cluster(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(tmp_path, {"clusters": {"only": {"host": "h"}}})
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        assert AppConfig.from_env().default_cluster == "only"

    def test_unknown_default_is_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        path = write_profiles(tmp_path, {"default": "nope", "clusters": {"a": {"host": "h"}}})
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        with pytest.raises(ConfigError, match="not defined"):
            AppConfig.from_env()

    def test_cluster_without_host_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(tmp_path, {"clusters": {"a": {"database": "x"}}})
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        with pytest.raises(ConfigError, match="no host"):
            AppConfig.from_env()

    def test_malformed_json_is_reported_clearly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        monkeypatch.setenv("CLICKSCOPE_PROFILES", str(path))
        with pytest.raises(ConfigError, match="not valid JSON"):
            AppConfig.from_env()

    def test_literal_password_is_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        path = write_profiles(tmp_path, {"clusters": {"a": {"host": "h", "password": "hunter2"}}})
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        with pytest.raises(ConfigError, match="must be a reference"):
            AppConfig.from_env()

    def test_absent_password_is_allowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        # A local dev server with no password is a legitimate setup.
        path = write_profiles(tmp_path, {"clusters": {"a": {"host": "h"}}})
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        assert AppConfig.from_env().clusters["a"].password == ""

    @posix_only
    def test_group_writable_file_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        # A ${cmd:...} reference makes the file decide what runs, so its integrity
        # matters more than its confidentiality.
        path = tmp_path / "clusters.json"
        path.write_text(json.dumps({"clusters": {"a": {"host": "h"}}}))
        path.chmod(0o664)
        monkeypatch.setenv("CLICKSCOPE_PROFILES", str(path))
        with pytest.raises(ConfigError, match="writable by other users"):
            AppConfig.from_env()


class TestLimitLayering:
    def test_file_wide_limits_apply_to_every_cluster(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(
            tmp_path,
            {
                "limits": {"max_limit": 500},
                "clusters": {"a": {"host": "h"}, "b": {"host": "h2"}},
            },
        )
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        config = AppConfig.from_env()
        assert config.clusters["a"].limits.max_limit == 500
        assert config.clusters["b"].limits.max_limit == 500

    def test_cluster_limits_override_file_wide(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        path = write_profiles(
            tmp_path,
            {
                "limits": {"max_limit": 500, "max_execution_time": 60},
                "clusters": {
                    "prod": {"host": "h", "limits": {"max_limit": 50}},
                    "dev": {"host": "h2"},
                },
            },
        )
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        config = AppConfig.from_env()
        # prod narrows max_limit but keeps the file-wide execution time
        assert config.clusters["prod"].limits.max_limit == 50
        assert config.clusters["prod"].limits.max_execution_time == 60
        assert config.clusters["dev"].limits.max_limit == 500

    def test_unknown_limit_name_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown limit"):
            Limits().merged({"max_rows": 5}, "test")

    def test_non_integer_limit_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be an integer"):
            Limits().merged({"max_limit": "lots"}, "test")


class TestRegistry:
    @pytest.fixture
    def registry(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> ClusterRegistry:
        path = write_profiles(
            tmp_path,
            {
                "default": "prod",
                "clusters": {"prod": {"host": "h1"}, "dev": {"host": "h2"}},
            },
        )
        monkeypatch.setenv("CLICKSCOPE_PROFILES", path)
        return ClusterRegistry(AppConfig.from_env())

    def test_names_and_default(self, registry: ClusterRegistry) -> None:
        assert registry.names == ["dev", "prod"]
        assert registry.default_name == "prod"

    def test_none_resolves_to_default(self, registry: ClusterRegistry) -> None:
        assert registry.get(None).name == "prod"

    def test_named_cluster_resolves(self, registry: ClusterRegistry) -> None:
        assert registry.get("dev").name == "dev"

    def test_handle_is_cached(self, registry: ClusterRegistry) -> None:
        assert registry.get("dev") is registry.get("dev")

    def test_each_cluster_gets_its_own_policy(self, registry: ClusterRegistry) -> None:
        assert registry.get("dev").policy is not registry.get("prod").policy

    def test_unknown_cluster_names_the_valid_ones(self, registry: ClusterRegistry) -> None:
        with pytest.raises(UnknownCluster) as exc:
            registry.get("staging")
        assert "staging" in str(exc.value)
        assert "prod" in str(exc.value)

    def test_describe_all_hides_passwords_and_marks_default(
        self, registry: ClusterRegistry
    ) -> None:
        described = registry.describe_all()
        assert {d["cluster"] for d in described} == {"dev", "prod"}
        assert [d["is_default"] for d in described if d["cluster"] == "prod"] == [True]
        assert "password" not in json.dumps(described)

    def test_connections_are_lazy(self, registry: ClusterRegistry) -> None:
        # Nothing is connected until a cluster is actually used, so an unreachable
        # box cannot stop the server from starting.
        assert all(not d["connected"] for d in registry.describe_all())
        registry.get("dev")
        connected = {d["cluster"] for d in registry.describe_all() if d["connected"]}
        assert connected == {"dev"}


class TestEmptyEnvironmentValues:
    """A plugin host passes every declared option through, blank ones included.

    Treating "" as a provided value connects as user "" and fails authentication with
    an error that points nowhere near the actual cause.
    """

    def test_blank_user_and_database_fall_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example")
        for name in ("CLICKHOUSE_USER", "CLICKHOUSE_DATABASE", "CLICKHOUSE_PORT"):
            monkeypatch.setenv(name, "")
        cluster = AppConfig.from_env().clusters["default"]
        assert cluster.username == "default"
        assert cluster.database == "default"
        assert cluster.port == 8123

    def test_whitespace_only_is_also_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example")
        monkeypatch.setenv("CLICKHOUSE_USER", "   ")
        assert AppConfig.from_env().clusters["default"].username == "default"

    def test_blank_host_still_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "")
        with pytest.raises(ConfigError):
            AppConfig.from_env()


class TestServerAddressParsing:
    """Host, port and TLS come from one field.

    Three questions in a setup dialog become one, and the forms people already
    copy around — host:port, or a URL — work without translation.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("clickhouse.internal", ("clickhouse.internal", None, None)),
            ("clickhouse.internal:9000", ("clickhouse.internal", 9000, None)),
            ("10.0.0.1:8123", ("10.0.0.1", 8123, None)),
            ("https://abc.clickhouse.cloud:8443", ("abc.clickhouse.cloud", 8443, True)),
            ("https://abc.clickhouse.cloud", ("abc.clickhouse.cloud", None, True)),
            ("http://localhost:8123", ("localhost", 8123, False)),
            ("  spaced.example  ", ("spaced.example", None, None)),
            ("", ("", None, None)),
        ],
    )
    def test_forms(self, value: str, expected: tuple) -> None:
        from clickscope.config import parse_server

        assert parse_server(value) == expected

    def test_non_numeric_port_is_rejected(self) -> None:
        from clickscope.config import parse_server

        with pytest.raises(ConfigError, match="non-numeric port"):
            parse_server("host:notaport")

    def test_url_without_hostname_is_rejected(self) -> None:
        from clickscope.config import parse_server

        with pytest.raises(ConfigError, match="cannot read a hostname"):
            parse_server("https://")

    def test_host_port_reaches_the_cluster_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example:9440")
        cluster = AppConfig.from_env().clusters["default"]
        assert (cluster.host, cluster.port) == ("ch.example", 9440)

    def test_https_url_turns_on_tls_and_its_default_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLICKHOUSE_HOST", "https://abc.clickhouse.cloud")
        cluster = AppConfig.from_env().clusters["default"]
        assert cluster.secure is True
        assert cluster.port == 8443

    def test_explicit_port_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The individual variables remain available for anyone already using them.
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch.example:9440")
        monkeypatch.setenv("CLICKHOUSE_PORT", "8123")
        assert AppConfig.from_env().clusters["default"].port == 8123


class TestCommandLine:
    """`--version` must work on a broken install — that is when you need it.

    The stale-build problem this project hit is diagnosed by asking a running server
    which version it is. That answer has to arrive before any configuration is read,
    or a misconfigured install cannot be identified at all.
    """

    def test_version_needs_no_configuration(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        from clickscope.__main__ import main

        # No CLICKHOUSE_HOST: the clean_env fixture has removed it.
        assert main(["--version"]) == 0
        assert capsys.readouterr().out.startswith("clickscope ")

    def test_help_needs_no_configuration(self, capsys) -> None:
        from clickscope.__main__ import main

        assert main(["--help"]) == 0
        assert "read-only ClickHouse MCP server" in capsys.readouterr().out

    def test_unrecognised_argument_fails_loudly(self, capsys) -> None:
        from clickscope.__main__ import main

        assert main(["--nope"]) == 2
        assert "unrecognised argument" in capsys.readouterr().err

    def test_missing_configuration_still_reports_clearly(self, capsys) -> None:
        from clickscope.__main__ import main

        assert main([]) == 2
        assert "configuration error" in capsys.readouterr().err

    def test_version_matches_the_packaged_metadata(self) -> None:
        from importlib.metadata import version as distribution_version

        from clickscope.server import version

        assert version() == distribution_version("clickscope")
