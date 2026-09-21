"""Sorted set commands."""

from __future__ import annotations

import math
from typing import Any

from kvstore.core.exceptions import InvalidArgumentError
from kvstore.engine.commands.registry import (
    DENYOOM,
    WRITE,
    CommandContext,
    command,
    float_arg,
    int_arg,
    key_arg,
    score_bound,
    str_arg,
    syntax_error,
    upper,
)
from kvstore.engine.datatypes import SortedSet
from kvstore.engine.datatypes.skiplist import Node
from kvstore.protocol.resp import format_float


def _flatten(nodes: list[Node], with_scores: bool) -> list[str]:
    if not with_scores:
        return [node.member for node in nodes]
    out: list[str] = []
    for node in nodes:
        out += [node.member, format_float(node.score)]
    return out


@command("ZADD", min_args=3, max_args=None, flags=[WRITE, DENYOOM])
def zadd(ctx: CommandContext, args: list[Any]) -> Any:
    """ZADD key [NX | XX] [GT | LT] [CH] [INCR] score member [score member ...]"""
    key = key_arg(args[0])
    options: set[str] = set()
    i = 1
    while i < len(args) and upper(args[i]) in ("NX", "XX", "GT", "LT", "CH", "INCR"):
        options.add(upper(args[i]))
        i += 1
    rest = args[i:]
    if not rest or len(rest) % 2:
        raise syntax_error()
    if {"NX", "XX"} <= options:
        raise InvalidArgumentError("XX and NX options at the same time are not compatible")
    if len(options & {"GT", "LT", "NX"}) > 1:
        raise InvalidArgumentError("GT, LT, and/or NX options at the same time are not compatible")
    incr = "INCR" in options
    if incr and len(rest) != 2:
        raise InvalidArgumentError("INCR option supports a single increment-element pair")
    pairs = [(float_arg(rest[j]), str_arg(rest[j + 1])) for j in range(0, len(rest), 2)]

    existing = ctx.store.read(key, SortedSet)
    if existing is None and "XX" in options:
        return None if incr else 0
    target = existing if existing is not None else ctx.store.write_target(key, SortedSet)

    added = changed = 0
    effects: list[Any] = []
    result_score: float | None = None
    for score, member in pairs:
        old = target.score(member)
        if old is None and "XX" in options:
            continue
        if old is not None and "NX" in options:
            continue
        new = old + score if incr and old is not None else score
        if math.isnan(new):
            raise InvalidArgumentError("resulting score is not a number (NaN)")
        if old is not None and (
            ("GT" in options and new <= old) or ("LT" in options and new >= old)
        ):
            continue
        result_score = new
        if old is None:
            added += 1
        elif new != old:
            changed += 1
        else:
            continue
        target.add(member, new)
        effects += [format_float(new), member]
    if effects:
        ctx.propagate("ZADD", key, *effects)
    if incr:
        return None if result_score is None else format_float(result_score)
    return added + changed if "CH" in options else added


@command("ZINCRBY", min_args=3, max_args=3, flags=[WRITE, DENYOOM])
def zincrby(ctx: CommandContext, args: list[Any]) -> str:
    key, delta, member = key_arg(args[0]), float_arg(args[1]), str_arg(args[2])
    ctx.store.read(key, SortedSet)  # type check before creating anything
    score = ctx.store.write_target(key, SortedSet).incr(member, delta)
    ctx.propagate("ZADD", key, format_float(score), member)
    return format_float(score)


@command("ZREM", min_args=2, max_args=None, flags=[WRITE])
def zrem(ctx: CommandContext, args: list[Any]) -> int:
    key = key_arg(args[0])
    members = [str_arg(m) for m in args[1:]]
    target = ctx.store.read(key, SortedSet)
    if target is None:
        return 0
    removed = [m for m in members if target.remove(m)]
    if removed:
        ctx.propagate("ZREM", key, *removed)
    return len(removed)


@command("ZSCORE", min_args=2, max_args=2)
def zscore(ctx: CommandContext, args: list[Any]) -> str | None:
    target = ctx.store.read(key_arg(args[0]), SortedSet)
    score = None if target is None else target.score(str_arg(args[1]))
    return None if score is None else format_float(score)


@command("ZCARD", min_args=1, max_args=1)
def zcard(ctx: CommandContext, args: list[Any]) -> int:
    target = ctx.store.read(key_arg(args[0]), SortedSet)
    return 0 if target is None else len(target)


@command("ZCOUNT", min_args=3, max_args=3)
def zcount(ctx: CommandContext, args: list[Any]) -> int:
    (lo, lo_ex), (hi, hi_ex) = score_bound(args[1]), score_bound(args[2])
    target = ctx.store.read(key_arg(args[0]), SortedSet)
    return 0 if target is None else target.count_in_range(lo, hi, lo_ex, hi_ex)


def _rank(ctx: CommandContext, args: list[Any], *, reverse: bool) -> int | None:
    target = ctx.store.read(key_arg(args[0]), SortedSet)
    return None if target is None else target.rank(str_arg(args[1]), reverse=reverse)


@command("ZRANK", min_args=2, max_args=2)
def zrank(ctx: CommandContext, args: list[Any]) -> int | None:
    return _rank(ctx, args, reverse=False)


@command("ZREVRANK", min_args=2, max_args=2)
def zrevrank(ctx: CommandContext, args: list[Any]) -> int | None:
    return _rank(ctx, args, reverse=True)


def _range(
    ctx: CommandContext,
    key: str,
    start: Any,
    stop: Any,
    *,
    by_score: bool,
    reverse: bool,
    limit: tuple[int, int] | None,
    with_scores: bool,
) -> list[str]:
    if by_score:
        # For reversed score ranges the caller passes (max, min), as Redis does.
        (lo, lo_ex), (hi, hi_ex) = (
            score_bound(stop if reverse else start),
            score_bound(start if reverse else stop),
        )
        offset, count = limit if limit is not None else (0, -1)
        if offset < 0:
            return []
    else:
        if limit is not None:
            raise syntax_error()
        start_i, stop_i = int_arg(start), int_arg(stop)
    target = ctx.store.read(key, SortedSet)
    if target is None:
        return []
    if by_score:
        nodes = target.range_by_score(
            lo, hi, lo_ex=lo_ex, hi_ex=hi_ex, reverse=reverse, offset=offset, count=count
        )
    else:
        nodes = target.range_by_rank(start_i, stop_i, reverse=reverse)
    return _flatten(nodes, with_scores)


def _parse_range_options(args: list[Any]) -> tuple[bool, bool, tuple[int, int] | None, bool]:
    by_score = reverse = with_scores = False
    limit: tuple[int, int] | None = None
    i = 0
    while i < len(args):
        option = upper(args[i])
        if option == "BYSCORE":
            by_score = True
        elif option == "REV":
            reverse = True
        elif option == "WITHSCORES":
            with_scores = True
        elif option == "LIMIT" and i + 2 < len(args):
            limit = (int_arg(args[i + 1]), int_arg(args[i + 2]))
            i += 2
        else:
            raise syntax_error()
        i += 1
    return by_score, reverse, limit, with_scores


@command("ZRANGE", min_args=3, max_args=None)
def zrange(ctx: CommandContext, args: list[Any]) -> list[str]:
    """ZRANGE key start stop [BYSCORE] [REV] [LIMIT offset count] [WITHSCORES]"""
    by_score, reverse, limit, with_scores = _parse_range_options(args[3:])
    return _range(
        ctx,
        key_arg(args[0]),
        args[1],
        args[2],
        by_score=by_score,
        reverse=reverse,
        limit=limit,
        with_scores=with_scores,
    )


@command("ZREVRANGE", min_args=3, max_args=4)
def zrevrange(ctx: CommandContext, args: list[Any]) -> list[str]:
    _, _, _, with_scores = _parse_range_options(args[3:])
    return _range(
        ctx,
        key_arg(args[0]),
        args[1],
        args[2],
        by_score=False,
        reverse=True,
        limit=None,
        with_scores=with_scores,
    )


def _range_by_score(ctx: CommandContext, args: list[Any], *, reverse: bool) -> list[str]:
    by_score, rev, limit, with_scores = _parse_range_options(args[3:])
    if by_score or rev:
        raise syntax_error()
    return _range(
        ctx,
        key_arg(args[0]),
        args[1],
        args[2],
        by_score=True,
        reverse=reverse,
        limit=limit,
        with_scores=with_scores,
    )


@command("ZRANGEBYSCORE", min_args=3, max_args=None)
def zrangebyscore(ctx: CommandContext, args: list[Any]) -> list[str]:
    return _range_by_score(ctx, args, reverse=False)


@command("ZREVRANGEBYSCORE", min_args=3, max_args=None)
def zrevrangebyscore(ctx: CommandContext, args: list[Any]) -> list[str]:
    return _range_by_score(ctx, args, reverse=True)


def _pop(ctx: CommandContext, args: list[Any], *, from_max: bool) -> list[str]:
    key = key_arg(args[0])
    count = int_arg(args[1]) if len(args) > 1 else 1
    if count < 0:
        raise InvalidArgumentError("value is out of range, must be positive")
    target = ctx.store.read(key, SortedSet)
    if target is None:
        return []
    popped = target.pop(count, from_max=from_max)
    if popped:
        ctx.propagate("ZREM", key, *(member for member, _ in popped))
    return [item for member, score in popped for item in (member, format_float(score))]


@command("ZPOPMIN", min_args=1, max_args=2, flags=[WRITE])
def zpopmin(ctx: CommandContext, args: list[Any]) -> list[str]:
    return _pop(ctx, args, from_max=False)


@command("ZPOPMAX", min_args=1, max_args=2, flags=[WRITE])
def zpopmax(ctx: CommandContext, args: list[Any]) -> list[str]:
    return _pop(ctx, args, from_max=True)
