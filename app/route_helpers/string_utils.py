from __future__ import annotations


def _longest_common_prefix(strs):
    """Return the longest string that is a prefix of all non-empty strings in strs."""
    strs = [s for s in strs if s]
    if not strs:
        return ""
    s0, s1 = min(strs), max(strs)
    for i, c in enumerate(s0):
        if i >= len(s1) or c != s1[i]:
            return s0[:i]
    return s0


def _longest_common_suffix(strs):
    rev = [s[::-1] for s in strs if s]
    return _longest_common_prefix(rev)[::-1] if rev else ""


def _unique_part_of_description(empty_description, other_descriptions):
    """
    Given the description for the no-arguments config and descriptions for other
    configs, return the part of empty_description that is not common to all
    (e.g. strip common prefix/suffix so "Build System: Unix Makefiles" with
    others "Build System: Ninja" yields "Unix Makefiles").
    """
    if not (empty_description or "").strip():
        return empty_description or ""
    empty_description = (empty_description or "").strip()
    others = [(d or "").strip() for d in (other_descriptions or []) if (d or "").strip()]
    if not others:
        return empty_description
    common_prefix = _longest_common_prefix([empty_description] + others)
    common_suffix = _longest_common_suffix([empty_description] + others)
    out = empty_description
    if common_prefix:
        out = out.removeprefix(common_prefix)
    if common_suffix:
        out = out.removesuffix(common_suffix)
    return out.strip() or empty_description
