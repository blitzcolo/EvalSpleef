# evalspleef-egress

Allowlist forward proxy for sandboxes whose carrier can only restrict egress by CIDR or an
on/off switch (TECH-DESIGN 7.3, PRD FR-SBX-03 / FR-ISO-04). The sandbox is given a single
route to this proxy; the proxy applies the task's network mode, the allowlist and blocklist,
read-only package mirrors, and writes the I16 audit logs outside the sandbox.

PRD appendix D.3 spells the modes `off`, `docs_whitelist`, `package_mirror`, `full`; this
package imports `evalspleef.spi.NetMode` so the spellings cannot drift.

## Components

| Module | Purpose |
| --- | --- |
| `policy.py` | YAML policy model: mode, allowed hosts (wildcards and URL path prefixes), global blocklist, blocked CIDRs, package-mirror routes, read-only methods. |
| `proxy.py` | Real asyncio HTTP forward proxy: absolute-form HTTP and `CONNECT` tunnels, 403 with `blocked_reason`, mirror path rewriting, bounded upstream connects. |
| `logs.py` | Writers for `url.jsonl` and `dns.jsonl` with exactly the PRD D.3 fields. |
| `configgen.py` | `squid_conf(policy)` and `envoy_yaml(policy)` for managed Squid/Envoy deployments. |
| `cli.py` | `spleef-egress` console script. |

## Policy file

```yaml
mode: package_mirror            # off | docs_whitelist | package_mirror | full
allow:                          # exact hosts, "*.suffix" wildcards, or URLs with path prefixes
  - developer.huawei.com
  - "*.example.com"
  - https://docs.example.com/guide
block:                          # added to the platform blocklist
  - leaderboard.example.net
blocked_cidrs:
  - 169.254.169.254/32
mirrors:                        # registry name -> internal read-only mirror base URL
  pip: http://pypi-mirror.internal:8080
  npm: http://npm-mirror.internal:4873
  cargo: http://cargo-mirror.internal:8081
  ohpm: http://ohpm-mirror.internal:8082
```

Loading a policy from YAML always adds `DEFAULT_BLOCKED_HOSTS` (`git.internal`,
`leaderboard.internal`, `eval-artifacts.internal`) and
`DEFAULT_BLOCKED_CIDRS` (`169.254.169.254/32`, `169.254.0.0/16`); an explicit `block` or
`blocked_cidrs` list extends them rather than replacing them. Unknown keys are rejected so a
typo cannot silently open the sandbox.

Behaviour by mode:

- `off` denies every request with `mode_off`.
- `docs_whitelist` allows `GET`/`HEAD` (and `CONNECT` for a TLS tunnel) to allowlisted hosts
  only. Path prefixes are enforced for plain HTTP; a `CONNECT` tunnel exposes no path, so a
  path-scoped entry permits the whole host over HTTPS.
- `package_mirror` adds the read-only rule to all methods: anything except `GET`/`HEAD`
  (and `CONNECT` for allowlisted docs hosts) is `403 method_not_allowed`. Requests to the
  built-in registry hosts (`REGISTRY_HOSTS`) are rewritten to the configured mirror base
  URL, path included. `CONNECT` to a registry host is denied with
  `mirror_connect_not_supported`, because an HTTPS tunnel cannot be path-rewritten without
  TLS interception and must not silently bypass the read-only rule.
- `full` allows everything except the blocklist and blocked CIDRs.

The blocklist and blocked CIDRs always win over an allow entry.

## Running the proxy

```bash
uv run --frozen --no-sync spleef-egress \
  --policy policy.yaml \
  --listen 127.0.0.1:3128 \
  --log-dir /var/log/evalspleef/trial-42 \
  --trial-id trial-42
```

`--listen` accepts `HOST:PORT`; port `0` picks a free port. In code, use the async context
manager:

```python
async with EgressProxy(policy, host="127.0.0.1", port=0, log_dir=dir, trial_id="trial-42") as proxy:
    host, port = proxy.address
```

`trial_id` and `round` are per-listener settings fixed at construction. The CLI sets
`round=0` and `process="unknown"`; deployments that run one listener per round pass both to
`EgressProxy` directly.

Denials return HTTP 403 with a short JSON body, `{"blocked_reason": "..."}`. Reasons:
`mode_off`, `blocked_host`, `blocked_cidr`, `method_not_allowed`, `host_not_allowed`,
`mirror_connect_not_supported`, `invalid_host`.

## Audit logs (PRD D.3)

`url.jsonl` fields, in order: `ts, trial_id, round, process, method, host, path, status,
bytes, blocked_reason`. `dns.jsonl` fields: `ts, trial_id, qname, qtype, answer, blocked`.
`answer` is a JSON array of resolved addresses. `bytes` is the number of response-body bytes
relayed to the client (tunnelled bytes for `CONNECT`). Denied requests have status 403 and a
`blocked_reason`; DNS lookups that were refused by policy are logged with `blocked: true` and
no answer. Upstream failures use status 502 and a null `blocked_reason`.

## Squid and Envoy generators

`squid_conf(policy)` emits `dstdomain` and `ssl::server_name` ACLs, `ssl_bump peek` /
`splice` for SNI filtering, blocklist and metadata CIDR denies, and the read-only method ACL.
`envoy_yaml(policy)` emits a listener with `tls_inspector`, `dynamic_forward_proxy` +
`sni_dynamic_forward_proxy`, an HTTP RBAC filter for the allowlist and blocklist, and a
network RBAC filter for the blocked destination CIDRs.

**Caveat: the generated Squid and Envoy files have not been run against real Squid or Envoy
in this repository, and are not verified on a real host.** Validate them with
`squid -k parse` / `envoy --mode validate` before deployment. Path-prefix allow entries and
package-mirror path rewriting are enforced by the Python proxy only; Squid's forward-proxy
ACLs express the host allowlist and read-only methods, not path rewriting.

## Verification status

- Policy matching, log fields and config generation are unit-tested.
- The proxy is tested against a real local HTTP server on `127.0.0.1:0` (plain HTTP
  forwarding, `CONNECT` tunnels, 403 paths, mirror rewriting).
- Not verified on a real host: Squid/Envoy generated configs, TLS upstreams for `https://`
  mirror base URLs, and any behaviour behind a real sandbox network namespace.
