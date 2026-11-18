import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import payment_proxy as proxy
import deploy_release


NGINX = """
server {
    listen 443 ssl;
    server_name example.test;
    location ^~ /api/internal/ { return 404; }
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
"""


def config(trust="10.83.0.1"):
    return {"services": {"api": {
        "image": "api@sha256:" + "a" * 64,
        "command": ["uvicorn", "app.main:app", "--no-proxy-headers"],
        "ports": [{"host_ip": "127.0.0.1", "published": "8000", "target": 8000}],
        "networks": {"backend": {}},
        "environment": {"PAYMENT_TRUSTED_PROXY_IPS": trust, "API_PUBLIC_URL": "https://example.test"},
    }}, "networks": {"backend": {"name": "test-bridge"}}}


def bridge(gateway="10.83.0.1"):
    return {"Id": "network-id", "Driver": "bridge", "Scope": "local", "Internal": False,
            "IPAM": {"Config": [{"Gateway": gateway}]}, "Options": {}}


class ProxyContractTests(unittest.TestCase):
    def test_empty_stale_extra_and_broad_trust_are_blocked(self):
        for trust in ("", "*", "0.0.0.0/0", "::/0", "10.83.0.0/24", "10.83.0.1,10.83.0.2", "10.84.0.1"):
            with self.subTest(trust=trust), self.assertRaises(proxy.ProxyError):
                _, _, trusted, _ = proxy.api_contract(config(trust))
                proxy.bridge_contract(bridge(), trusted)

    def test_exact_ipv4_ipv6_and_host_cidr(self):
        for trust, address in (("10.83.0.1", "10.83.0.1"), ("10.83.0.1/32", "10.83.0.1"), ("fd83::1/128", "fd83::1")):
            _, network, actual, port = proxy.api_contract(config(trust))
            self.assertEqual((network, actual, port), ("test-bridge", address, 8000))
            proxy.bridge_contract(bridge(address), actual)

    def test_uncontrolled_api_shapes_fail(self):
        for mutate in (
            lambda a: a["ports"][0].update(host_ip="0.0.0.0"),
            lambda a: a["ports"][0].update(host_ip="::"),
            lambda a: a["ports"].append({"host_ip": "127.0.0.1", "target": 8001}),
            lambda a: a.update(command=["uvicorn", "app.main:app"]),
            lambda a: a.update(entrypoint=["custom-proxy-wrapper"]),
            lambda a: a["command"].append("--proxy-headers"),
            lambda a: a.update(network_mode="host"),
            lambda a: a["networks"].update(other={}),
        ):
            candidate = config()
            mutate(candidate["services"]["api"])
            with self.assertRaises(proxy.ProxyError):
                proxy.api_contract(candidate)

    def test_bridge_modes_fail(self):
        for changes in ({"Driver": "host"}, {"Scope": "swarm"}, {"Internal": True},
                        {"Options": {"com.docker.network.bridge.gateway_mode_ipv4": "routed"}}):
            network = bridge()
            network.update(changes)
            with self.assertRaises(proxy.ProxyError):
                proxy.bridge_contract(network, "10.83.0.1")

    def test_effective_nginx_accepts_overwrite_append_and_controlled_realip(self):
        for xff in ("$remote_addr", "$proxy_add_x_forwarded_for"):
            text = NGINX.replace("$proxy_add_x_forwarded_for", xff)
            text = text.replace("location /api/", "set_real_ip_from 10.9.0.0/24; real_ip_header CF-Connecting-IP; location /api/")
            proxy.validate_nginx(text, "example.test", 8000)

    def test_wrong_nginx_route_or_trust_fails(self):
        for candidate in (
            NGINX.replace(":8000/", ":18180/"),
            NGINX.replace("$proxy_add_x_forwarded_for", "$http_x_forwarded_for"),
            NGINX.replace("location /api/", "set_real_ip_from 0.0.0.0/0; location /api/"),
            NGINX.replace("location /api/", "include override.conf; location /api/"),
            NGINX.replace("proxy_pass", "include override.conf; proxy_pass"),
            NGINX.replace("proxy_pass", "proxy_bind 10.9.0.2; proxy_pass"),
            NGINX.replace("location /api/", "location ~ /api/payments { return 200; } location /api/"),
            NGINX.replace("location /api/", "location /api/payments { return 200; } location /api/"),
            NGINX.replace("example.test", "wrong.test"), NGINX + NGINX,
        ):
            with self.subTest(candidate=candidate), self.assertRaises(proxy.ProxyError):
                proxy.validate_nginx(candidate, "example.test", 8000)

    def test_stale_gateway_blocks_before_probe_and_healthy_api_cannot_mask_it(self):
        run = Mock(side_effect=[json.dumps(config()), json.dumps([bridge("10.84.0.1")])])
        with patch.object(proxy, "probe_transport") as probe, self.assertRaises(proxy.ProxyError):
            proxy.validate_release(run, ["docker", "compose"], nginx_text=NGINX)
        probe.assert_not_called()

    def test_missing_network_and_bad_candidate_block_before_probe(self):
        for response, candidate in (
            (RuntimeError("network absent"), NGINX),
            (json.dumps([bridge()]), NGINX.replace(":8000/", ":18180/")),
        ):
            run = Mock(side_effect=[json.dumps(config()), response])
            with patch.object(proxy, "probe_transport") as probe, self.assertRaises(proxy.ProxyError):
                proxy.validate_release(run, ["docker", "compose"], nginx_text=NGINX, candidate_nginx=candidate)
            probe.assert_not_called()

    def test_repository_production_nginx_and_unrelated_sites(self):
        production = Path(__file__).resolve().parents[1] / "nginx" / "syfc.moonlitstarway.top"
        proxy.validate_nginx(production.read_text(encoding="utf-8"), "syfc.moonlitstarway.top", 8000)
        other_site = "server { listen 443 ssl; server_name other.test; set_real_ip_from 0.0.0.0/0; }"
        proxy.validate_nginx(NGINX + other_site, "example.test", 8000)

    def test_running_environment_or_network_drift_is_rejected(self):
        for stale_trust, net_id in (("10.84.0.1", "network-id"), ("10.83.0.1", "replaced-id")):
            runtime = {"Config": {"Image": config()["services"]["api"]["image"],
                                  "Cmd": ["uvicorn", "--no-proxy-headers"],
                                  "Env": ["PAYMENT_TRUSTED_PROXY_IPS=" + stale_trust]},
                       "HostConfig": {"PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]}},
                       "NetworkSettings": {"Networks": {"test-bridge": {"NetworkID": net_id}}}}
            run = Mock(side_effect=[json.dumps(config()), json.dumps([bridge()]), "api-id", json.dumps([runtime])])
            with patch.object(proxy, "probe_transport") as probe, self.assertRaises(proxy.ProxyError):
                proxy.validate_release(run, ["docker", "compose"], nginx_text=NGINX, running=True)
            probe.assert_not_called()

    def test_probe_never_learns_an_observed_peer_and_always_cleans_up(self):
        inspect = [{"NetworkSettings": {"Ports": {"8000/tcp": [{"HostPort": "12345"}]}}}]
        run = Mock(side_effect=["probe-id", json.dumps(inspect), ""])
        with patch.object(proxy, "fetch_probe", return_value={"peer": "10.84.0.1", "source": "198.51.100.10"}), self.assertRaises(proxy.ProxyError):
            proxy.probe_transport(run, "release-image", "test-bridge", "10.83.0.1")
        self.assertEqual(run.call_args.args[0][:3], ["docker", "rm", "-f"])
        command = run.call_args_list[0].args[0]
        self.assertIn("PAYMENT_TRUSTED_PROXY_IPS=10.83.0.1", command)
        self.assertFalse(any(x in command for x in ("--env-file", "--volume", "-v")))


class ReleaseGateTests(unittest.TestCase):
    def test_transaction_blocks_before_maintenance_backup_or_switch(self):
        with patch.object(deploy_release, "load_manifest", return_value={}), \
             patch.object(deploy_release, "verify_checksums"), \
             patch.object(deploy_release, "render_environment"), \
             patch.object(deploy_release, "ensure_release_images"), \
             patch.object(deploy_release, "verify_payment_proxy", side_effect=deploy_release.ReleaseError("stale proxy")), \
             patch.object(deploy_release, "backup_host_config") as backup, \
             patch.object(deploy_release, "switch_links") as switch:
            on = Mock()
            with self.assertRaisesRegex(deploy_release.ReleaseError, "stale proxy"):
                deploy_release.transactional_deploy(deploy_release.Paths(), Path("/candidate"), Mock(),
                    health_check=Mock(), maintenance_on=on, maintenance_off=Mock(), migrate=Mock(), has_migration=False)
            on.assert_not_called()
            backup.assert_not_called()
            switch.assert_not_called()

    def test_rollback_blocks_before_changing_current(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(deploy_release, "verify_payment_proxy", side_effect=deploy_release.ReleaseError("stale proxy")), \
             patch.object(deploy_release, "restore_current") as restore:
            with self.assertRaisesRegex(deploy_release.ReleaseError, "stale proxy"):
                deploy_release.rollback(deploy_release.Paths(), Path(tmp), Mock())
            restore.assert_not_called()

    def test_start_checks_configuration_before_up_and_runtime_after(self):
        events = []
        paths = deploy_release.Paths()
        runner = Mock()
        runner.run.side_effect = lambda args: events.append("up")
        with patch.object(deploy_release, "current_compose_args", return_value=["docker", "compose"]), \
             patch.object(deploy_release, "ensure_release_images"), \
             patch.object(deploy_release, "confirm_running_digests"), \
             patch.object(deploy_release, "verify_payment_proxy", side_effect=lambda *a, **kw: events.append("running" if kw.get("running") else "preflight")):
            deploy_release.compose_up(paths, runner)
        self.assertEqual(events, ["preflight", "up", "running"])


if __name__ == "__main__":
    unittest.main()
