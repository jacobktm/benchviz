from __future__ import annotations

import re
import shlex
from typing import Iterable


def parse_args_tokens(args_str: str) -> list[str]:
    """
    Tokenize a Phoronix/CLI-like args string.

    We prefer `shlex.split` so quoted values survive. If tokenization fails,
    fall back to a whitespace split.
    """
    if args_str is None:
        return []
    s = str(args_str).strip()
    if not s:
        return []
    try:
        return shlex.split(s)
    except Exception:
        return s.split()


def _normalize_flag(flag: str) -> str:
    f = (flag or "").strip()
    if not f:
        return ""
    # Users might paste "--cycles-device" or "cycles-device".
    if not f.startswith("-"):
        return "--" + f
    return f


def parse_pool_flags(pool_arg_flags: str | None) -> list[str]:
    """
    Parse `pool_arg_flags` from the query string.

    Supports comma-separated and newline-separated inputs.
    """
    if pool_arg_flags is None:
        return []
    raw = str(pool_arg_flags).strip()
    if not raw:
        return []
    # Split on commas/newlines but keep spaces inside tokens untouched.
    parts: list[str] = []
    # Split on commas or actual newline characters.
    for chunk in re.split(r"[,\n]+", raw):
        f = _normalize_flag(chunk)
        if f:
            parts.append(f)
    # De-dupe while preserving order
    out: list[str] = []
    seen = set()
    for f in parts:
        if f in seen:
            continue
        seen.add(f)
        out.append(f)
    return out


def extract_flag_values(args_str: str, pool_flags: Iterable[str]) -> list[str]:
    """
    Return the values associated with each `pool_flag` as discovered in args_str.

    Examples supported:
    - --flag=value
    - --flag value
    - -Fvalue  (short flag concatenated with value)
    - -F value
    """
    tokens = parse_args_tokens(args_str)
    if not tokens:
        return []

    flags = [_normalize_flag(f) for f in pool_flags if _normalize_flag(f)]
    if not flags:
        return []

    values: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        handled = False
        for flag in flags:
            if not flag:
                continue
            # --flag=value
            if tok.lower().startswith(flag.lower() + "="):
                v = tok.split("=", 1)[1]
                if v:
                    values.append(v)
                handled = True
                break
            # --flag value
            if tok.lower() == flag.lower():
                v = ""
                if i + 1 < len(tokens):
                    nxt = tokens[i + 1]
                    # If next token looks like another flag, assume missing value.
                    if not (isinstance(nxt, str) and nxt.startswith("-")):
                        v = nxt
                if v:
                    values.append(v)
                    i += 1 if v else 0
                handled = True
                break
            # -Fvalue (short flag concatenated with its value)
            if flag.startswith("-") and not flag.startswith("--"):
                if tok.lower().startswith(flag.lower()) and len(tok) > len(flag):
                    v = tok[len(flag) :]
                    if v:
                        values.append(v)
                    handled = True
                    break
        if handled:
            i += 1
            continue
        i += 1
    return values


def pool_key_for_args_by_flags(args_str: str | None, pool_flags: list[str]) -> str | None:
    """
    Build a canonical args key by *removing* the value(s) for each flag listed
    in `pool_flags`.

    If `pool_flags` is empty, returns None.
    """
    if not args_str:
        return None
    if not pool_flags:
        return None
    tokens = parse_args_tokens(args_str)
    if not tokens:
        return None

    flags = [_normalize_flag(f) for f in pool_flags if _normalize_flag(f)]
    if not flags:
        return None

    # Remove the pool flag tokens and their associated value tokens.
    out_tokens: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        removed = False
        lower_tok = tok.lower()

        for flag in flags:
            if not flag:
                continue
            fl = flag.lower()
            # --flag=value
            if lower_tok.startswith(fl + "="):
                removed = True
                break
            # --flag value
            if lower_tok == fl:
                # skip flag and (if present) value
                if i + 1 < len(tokens):
                    nxt = tokens[i + 1]
                    if not (isinstance(nxt, str) and nxt.startswith("-")):
                        i += 1
                removed = True
                break
            # -Fvalue (short flag concatenated with value)
            if fl.startswith("-") and not fl.startswith("--"):
                if lower_tok.startswith(fl) and len(tok) > len(flag):
                    removed = True
                    break

        if removed:
            i += 1
            continue

        out_tokens.append(tok)
        i += 1

    pooled = " ".join(out_tokens).strip()
    # If we removed everything, treat as a "match-all" key.
    if not pooled:
        pooled = "<pooled>"
    return pooled

