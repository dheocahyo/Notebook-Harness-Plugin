"""nhctl: Notebook Harness project CLI (stdlib only, Python >= 3.9).

Every command prints human text, or a single JSON object with --json.
Exit codes: 0 ok, 1 failed, 2 needs the user's confirmation (or bad usage).
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.realpath(os.path.join(HERE, "..", ".."))  # plugins/nh
sys.path[:0] = [HERE, os.path.join(ROOT, "server", "src")]

if sys.version_info < (3, 9):  # noqa: UP036 - nhctl runs on whatever python3 the user has
    sys.stderr.write("nhctl needs Python 3.9 or newer.\n")
    sys.exit(1)

import argparse  # noqa: E402
import json  # noqa: E402
import traceback  # noqa: E402

import common  # noqa: E402
import doctor  # noqa: E402
import envsync  # noqa: E402
import freshrun  # noqa: E402
import lab  # noqa: E402
import metrics  # noqa: E402
import scaffold  # noqa: E402
import settings  # noqa: E402


class Parser(argparse.ArgumentParser):
    """Usage errors become a JSON object too when --json was asked for."""

    def error(self, message: str) -> None:  # type: ignore[override]
        if "--json" in sys.argv[1:]:
            error = {"code": "D100", "message": message, "fix": f"See: {self.prog} --help"}
            sys.stdout.write(json.dumps({"ok": False, "error": error}) + "\n")
            sys.exit(2)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS keeps a subcommand's default from clobbering a value given before it.
    common_opts = Parser(add_help=False)
    common_opts.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON object"
    )
    common_opts.add_argument(
        "--project", default=argparse.SUPPRESS, help="project folder (default: the current one)"
    )
    common_opts.add_argument(
        "--plugin-data",
        default=argparse.SUPPRESS,
        help='nh plugin data dir; pass "${CLAUDE_PLUGIN_DATA}"',
    )
    parser = Parser(prog="nhctl", description=__doc__, parents=[common_opts])
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for module in (doctor, scaffold, envsync, lab, settings, metrics, freshrun):
        module.add_parsers(sub, common_opts)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        result = args.func(args)
    except common.NhctlError as exc:
        result = common.error_result(exc)
    except KeyboardInterrupt:
        result = common.error_result(common.NhctlError("D198", "Interrupted."))
    except Exception as exc:
        if os.environ.get("NHCTL_DEBUG"):
            traceback.print_exc()
        message = common.scrub(f"nhctl hit an internal error: {type(exc).__name__}: {exc}")
        fix = "Run again with NHCTL_DEBUG=1 for the traceback."
        result = common.error_result(common.NhctlError("D199", message, fix))
    common.print_result(result, as_json)
    return result.code


if __name__ == "__main__":
    sys.exit(main())
