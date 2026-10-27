"""``helmlog-hosting`` — run and manage the multi-boat hosted web server.

Configuration comes from the environment (``/etc/helmlog-web/helmlog-web.env``
on the server, written by ``scripts/deploy-web.sh``):

  HELMLOG_HOSTING_DIR          data root: registry.db + boats/<slug>/ (default data/hosting)
  HELMLOG_HOSTING_BASE_DOMAIN  serve boats at <slug>.<domain> (optional)
  HELMLOG_HOSTING_HOST/PORT    bind address (default 127.0.0.1:3003, behind nginx)
  HELMLOG_HOSTING_IDLE_S       close a boat's DB after this many idle seconds (900)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from loguru import logger

from helmlog.hosting.registry import BoatRegistry, RegistryError


def _registry(args: argparse.Namespace) -> BoatRegistry:
    return BoatRegistry(Path(args.data_dir))


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from helmlog.auth import _is_auth_disabled
    from helmlog.hosting.dispatcher import HostedDispatcher
    from helmlog.hosting.pool import BoatAppPool

    if _is_auth_disabled():
        # The hosted server is reachable from outside the boat; an auth bypass
        # here would publish every boat's data.
        logger.error("AUTH_DISABLED is set — refusing to start the hosted server")
        return 2

    registry = _registry(args)
    pool = BoatAppPool(registry, idle_s=float(os.environ.get("HELMLOG_HOSTING_IDLE_S", "900")))
    base_domain = os.environ.get("HELMLOG_HOSTING_BASE_DOMAIN") or None
    app = HostedDispatcher(registry, pool, base_domain=base_domain)
    logger.info(
        "Hosted web server: {} boat(s), data in {}, base domain {}",
        len(registry.list()),
        registry.data_dir,
        base_domain or "(none)",
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
    return 0


def _cmd_boat_list(args: argparse.Namespace) -> int:
    boats = _registry(args).list()
    if not boats:
        print("No boats yet. Add one with: helmlog-hosting boat add <slug> --name <name>")
        return 0
    for b in boats:
        default = " (default)" if b.is_default else ""
        data = Path(b.db_path).name if b.db_path else "no data imported"
        print(f"{b.slug}{default}  {b.name}")
        print(f"    data:  {data}")
        print(f"    hosts: {', '.join(b.hosts) or '-'}")
    return 0


def _cmd_boat_add(args: argparse.Namespace) -> int:
    boat = _registry(args).add(
        args.slug, args.name, hosts=tuple(args.host or ()), is_default=args.default
    )
    print(f"Added boat {boat.slug}. Next: push its data with scripts/push-to-web.sh")
    return 0


def _cmd_boat_remove(args: argparse.Namespace) -> int:
    reg = _registry(args)
    reg.remove(args.slug)
    print(f"Removed {args.slug} from the registry. Its files remain in {reg.boat_dir(args.slug)}")
    return 0


def _cmd_boat_add_host(args: argparse.Namespace) -> int:
    _registry(args).add_host(args.slug, args.host)
    print(f"{args.host} now serves {args.slug}")
    return 0


def _cmd_boat_set_default(args: argparse.Namespace) -> int:
    _registry(args).set_default(args.slug)
    print(f"{args.slug} is now the default boat")
    return 0


def _cmd_boat_import(args: argparse.Namespace) -> int:
    dest = _registry(args).import_snapshot(args.slug, Path(args.snapshot))
    print(f"Imported into {dest}. The server switches to it on the next request.")
    return 0


def _cmd_boat_videos_dir(args: argparse.Namespace) -> int:
    reg = _registry(args)
    reg.get(args.slug)
    path = reg.videos_dir(args.slug)
    path.mkdir(parents=True, exist_ok=True)
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="helmlog-hosting", description=__doc__.splitlines()[0])
    p.add_argument(
        "--data-dir",
        default=os.environ.get("HELMLOG_HOSTING_DIR", "data/hosting"),
        help="Data root holding registry.db and boats/ (env HELMLOG_HOSTING_DIR)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the hosted web server")
    serve.add_argument("--host", default=os.environ.get("HELMLOG_HOSTING_HOST", "127.0.0.1"))
    serve.add_argument(
        "--port", type=int, default=int(os.environ.get("HELMLOG_HOSTING_PORT", "3003"))
    )
    serve.set_defaults(func=_cmd_serve)

    boat = sub.add_parser("boat", help="Manage hosted boats")
    bsub = boat.add_subparsers(dest="boat_command", required=True)

    bsub.add_parser("list", help="List boats").set_defaults(func=_cmd_boat_list)

    add = bsub.add_parser("add", help="Register a boat")
    add.add_argument("slug", help="Short id, also the subdomain: lower-case letters, digits, '-'")
    add.add_argument("--name", required=True, help="Display name")
    add.add_argument("--host", action="append", help="Hostname that serves this boat (repeatable)")
    add.add_argument("--default", action="store_true", help="Serve this boat on unmatched hosts")
    add.set_defaults(func=_cmd_boat_add)

    rm = bsub.add_parser("remove", help="Unregister a boat (files are kept)")
    rm.add_argument("slug")
    rm.set_defaults(func=_cmd_boat_remove)

    ah = bsub.add_parser("add-host", help="Serve a boat on another hostname")
    ah.add_argument("slug")
    ah.add_argument("host")
    ah.set_defaults(func=_cmd_boat_add_host)

    sd = bsub.add_parser("set-default", help="Serve this boat on unmatched hosts")
    sd.add_argument("slug")
    sd.set_defaults(func=_cmd_boat_set_default)

    imp = bsub.add_parser("import", help="Import a DB snapshot as the boat's current data")
    imp.add_argument("slug")
    imp.add_argument("snapshot", help="Path to a HelmLog .db snapshot")
    imp.set_defaults(func=_cmd_boat_import)

    vd = bsub.add_parser("videos-dir", help="Print (and create) the boat's videos directory")
    vd.add_argument("slug")
    vd.set_defaults(func=_cmd_boat_videos_dir)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
