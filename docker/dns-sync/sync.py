#!/usr/bin/env python3
import base64
import ipaddress
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("dns-sync")

HEALTH_FILE = "/tmp/healthy"
PER_PAGE = 100
MAX_PAGES = 100
COMMENT = "managed by traefik-dns-sync"
_warned = set()

HOST_CALL = re.compile(r"(?<![!\w])Host\(([^)]*)\)")
QUOTED = re.compile(r"`([^`]*)`|\"([^\"]*)\"")
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?"
    r"(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$"
)


def env_str(name, default=""):
    return os.environ.get(name, default).strip()


def env_bool(name, default):
    return env_str(name, str(default)).lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        sys.exit(f"{name} must be an integer")


def secret(name):
    path = env_str(f"{name}_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return env_str(name)


class Config:
    def __init__(self):
        self.traefik_url = env_str("TRAEFIK_API_URL", "http://traefik:8080").rstrip("/")
        self.traefik_user = env_str("TRAEFIK_USER")
        self.traefik_password = secret("TRAEFIK_PASSWORD")
        self.traefik_tls_verify = env_bool("TRAEFIK_TLS_VERIFY", True)

        self.tech_url = env_str("TECHNITIUM_URL", "http://technitium:5380").rstrip("/")
        self.tech_token = secret("TECHNITIUM_TOKEN")

        zones = env_str("DNS_ZONES")
        self.zones = sorted(
            {z.strip().strip(".").lower() for z in zones.split(",") if z.strip()},
            key=len,
            reverse=True,
        )
        self.target_ip = env_str("TARGET_IP")
        self.ttl = env_int("RECORD_TTL", 300)
        self.overwrite_unmanaged = env_bool("OVERWRITE_UNMANAGED", False)

        self.interval = env_int("SYNC_INTERVAL", 30)
        self.delete_stale = env_bool("DELETE_STALE", True)
        self.grace = env_int("STALE_GRACE_SECONDS", 120)
        self.dry_run = env_bool("DRY_RUN", False)
        self.state_file = env_str("STATE_FILE", "/data/state.json")

    def validate(self):
        errors = []
        if not self.tech_token:
            errors.append("TECHNITIUM_TOKEN (or TECHNITIUM_TOKEN_FILE) is not set")
        if not self.zones:
            errors.append("DNS_ZONES is not set")
        try:
            ipaddress.IPv4Address(self.target_ip)
        except ValueError:
            errors.append("TARGET_IP must be a valid IPv4 address")
        if errors:
            sys.exit("Configuration error:\n  - " + "\n  - ".join(errors))


def http(method, url, data=None, headers=None, ctx=None, timeout=10):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.headers, r.read()


class TechError(Exception):
    pass


class Technitium:
    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, path, **params):
        params["token"] = self.cfg.tech_token
        body = urllib.parse.urlencode(params).encode()
        try:
            _, raw = http(
                "POST",
                self.cfg.tech_url + path,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except urllib.error.HTTPError as e:
            raise TechError(f"{path}: HTTP {e.code}") from None
        data = json.loads(raw)
        status = data.get("status")
        if status != "ok":
            msg = data.get("errorMessage") or status
            if status == "invalid-token":
                msg = "invalid-token (the token is wrong or has been deleted)"
            raise TechError(f"{path}: {msg}")
        return data.get("response") or {}

    def a_records(self, zone):
        resp = self.call(
            "/api/zones/records/get", domain=zone, zone=zone, listZone="true"
        )
        out = {}
        for r in resp.get("records", []):
            if r.get("type") == "A":
                name = r["name"].lower().rstrip(".")
                out.setdefault(name, set()).add(r["rData"]["ipAddress"])
        return out

    def set_a(self, zone, host, ip, ttl):
        self.call(
            "/api/zones/records/add",
            zone=zone,
            domain=host,
            type="A",
            ipAddress=ip,
            ttl=ttl,
            overwrite="true",
            comments=COMMENT,
        )

    def delete_a(self, zone, host, ip):
        self.call(
            "/api/zones/records/delete", zone=zone, domain=host, type="A", ipAddress=ip
        )


class Traefik:
    def __init__(self, cfg):
        self.cfg = cfg
        self.headers = {"Accept": "application/json"}
        if cfg.traefik_user:
            cred = f"{cfg.traefik_user}:{cfg.traefik_password}".encode()
            self.headers["Authorization"] = "Basic " + base64.b64encode(cred).decode()
        self.ctx = ssl.create_default_context()
        if not cfg.traefik_tls_verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def routers(self):
        out, page = [], 1
        while page <= MAX_PAGES:
            url = f"{self.cfg.traefik_url}/api/http/routers?page={page}&per_page={PER_PAGE}"
            headers, raw = http("GET", url, headers=self.headers, ctx=self.ctx)
            chunk = json.loads(raw)
            out += chunk
            nxt = headers.get("X-Next-Page")
            if len(chunk) < PER_PAGE or not nxt or int(nxt) <= page:
                break
            page = int(nxt)
        return out


def hosts_from_rule(rule):
    hosts = []
    for call in HOST_CALL.finditer(rule or ""):
        for q in QUOTED.finditer(call.group(1)):
            h = (q.group(1) or q.group(2) or "").strip().lower().rstrip(".")
            if h and HOSTNAME.match(h):
                hosts.append(h)
    return hosts


def zone_for(host, zones):
    for z in zones:
        if host == z or host.endswith("." + z):
            return z
    return None


def desired_hosts(routers, zones):
    want = {}
    for r in routers:
        if r.get("status") == "disabled":
            continue
        if r.get("provider") == "internal" or str(r.get("name", "")).endswith(
            "@internal"
        ):
            continue
        for h in hosts_from_rule(r.get("rule")):
            z = zone_for(h, zones)
            if z:
                want[h] = z
    return want


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("records", {})
        return state
    except FileNotFoundError:
        return {"records": {}}
    except (OSError, ValueError) as e:
        log.warning("failed to read %s (%s), starting with an empty state", path, e)
        return {"records": {}}


def save_state(path, state):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        log.error("failed to save state to %s: %s", path, e)


def sync(cfg, traefik, tech, state):
    routers = traefik.routers()
    if not routers:
        raise RuntimeError("Traefik returned 0 routers, not changing anything")

    want = desired_hosts(routers, cfg.zones)
    records = state["records"]
    existing = {z: tech.a_records(z) for z in cfg.zones}
    now = time.time()
    tag = "[dry-run] " if cfg.dry_run else ""

    for host, zone in sorted(want.items()):
        info = records.get(host)
        if info:
            info["missing_since"] = None
        have = existing[zone].get(host, set())

        if have == {cfg.target_ip}:
            if not info:
                records[host] = {
                    "zone": zone,
                    "ip": cfg.target_ip,
                    "missing_since": None,
                }
                log.info("%sadopt  %s -> %s (already exists)", tag, host, cfg.target_ip)
            continue

        if have and not info and not cfg.overwrite_unmanaged:
            if host not in _warned:
                _warned.add(host)
                log.warning(
                    "skip   %s: an A record %s already exists, created by another service "
                    "(set OVERWRITE_UNMANAGED=true to overwrite)",
                    host,
                    sorted(have),
                )
            continue

        log.info(
            "%sset    %s -> %s (was: %s)",
            tag,
            host,
            cfg.target_ip,
            sorted(have) or "-",
        )
        if cfg.dry_run:
            continue
        try:
            tech.set_a(zone, host, cfg.target_ip, cfg.ttl)
            records[host] = {"zone": zone, "ip": cfg.target_ip, "missing_since": None}
        except TechError as e:
            log.error("failed to create %s: %s", host, e)

    for host, info in list(records.items()):
        if host in want or not cfg.delete_stale:
            continue
        if info.get("missing_since") is None:
            info["missing_since"] = now
            log.info(
                "router for %s disappeared, waiting %ss before deletion",
                host,
                cfg.grace,
            )
        if now - info["missing_since"] < cfg.grace:
            continue

        zone = info["zone"]
        if zone not in existing:
            log.warning(
                "zone %s is no longer in DNS_ZONES, forgetting %s without deletion",
                zone,
                host,
            )
            records.pop(host)
            continue
        if info["ip"] in existing[zone].get(host, set()):
            log.info("%sdelete %s (%s)", tag, host, info["ip"])
            if cfg.dry_run:
                continue
            try:
                tech.delete_a(zone, host, info["ip"])
            except TechError as e:
                log.error("failed to delete %s: %s", host, e)
                continue
        records.pop(host)


def healthcheck(cfg):
    limit = max(90, cfg.interval * 3)
    try:
        return 0 if time.time() - os.path.getmtime(HEALTH_FILE) < limit else 1
    except OSError:
        return 1


def main():
    logging.basicConfig(
        level=env_str("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    cfg = Config()
    if "--healthcheck" in sys.argv:
        sys.exit(healthcheck(cfg))
    cfg.validate()

    log.info(
        "Traefik: %s | Technitium: %s | zones: %s | IP: %s | interval: %ss%s",
        cfg.traefik_url,
        cfg.tech_url,
        ",".join(cfg.zones),
        cfg.target_ip,
        cfg.interval,
        " | DRY-RUN" if cfg.dry_run else "",
    )

    traefik, tech = Traefik(cfg), Technitium(cfg)
    state = load_state(cfg.state_file)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    while not stop.is_set():
        before = json.dumps(state, sort_keys=True)
        try:
            sync(cfg, traefik, tech, state)
            with open(HEALTH_FILE, "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            log.error("sync cycle failed: %s", e)
        finally:
            if json.dumps(state, sort_keys=True) != before:
                save_state(cfg.state_file, state)
        stop.wait(cfg.interval)
    log.info("stopped")


if __name__ == "__main__":
    main()
