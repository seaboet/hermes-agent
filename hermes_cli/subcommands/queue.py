"""``hermes queue`` submits a follow-up to an existing live session."""

from __future__ import annotations


def build_queue_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "queue", help="Submit a queued prompt to an existing live session")
    parser.add_argument("--session", "-s", required=True,
                        help="Exact durable ID, live/UI ID or unique exact name")
    parser.add_argument("--message", "-m", required=True, help="Prompt text")
    parser.set_defaults(func=cmd_queue)


def cmd_queue(args) -> int:
    from hermes_cli.queue_cmd import queue_message

    return queue_message(args.session, args.message)
